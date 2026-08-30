"""L2 向量检索意图识别（目标架构 ③ 分层管道的第二层）

L1 规则匹配未命中 → 本层把用户输入向量化，与意图种子语料（seed_dataset.json
的 191 条标注样例）的向量做余弦检索，取 top-1 意图 + 相似度作置信；
置信不足 → 交 L3 大模型分类。

向量持久化在 Milvus 集合 lumio_intent_vectors（与 RAG 同一 mxbai 嵌入模型，
1024 维 cosine），首次构建后常驻，重启不重建。
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

COLLECTION_NAME = "lumio_intent_domain_vectors"
DIM = 1024
SEED_PATH = Path(__file__).resolve().parents[3] / "data" / "intent_classification" / "seed_dataset.json"


@dataclass
class VectorIntentMatch:
    """L2 检索结果 (五域骨架)

    intent 为五域值 (query/transaction/consulting/service/chitchat);
    score 为余弦相似度 (0~1)；matched=False 表示索引不可用或无候选。
    """

    matched: bool
    intent: str = ""  # 五域值
    score: float = 0.0
    exemplar: str = ""


class IntentVectorIndex:
    """意图种子向量索引（Milvus 持久化, 惰性建库）"""

    def __init__(self, milvus_collection=None, embedding_provider=None) -> None:
        self._coll = milvus_collection
        self._embed = embedding_provider
        self._build_lock = asyncio.Lock()
        self._built = False

    # ── 建库 ──

    def _load_seeds(self) -> list[dict]:
        """种子按五域标注 (骨架第一级, 5 类比 10 叶类检索准得多)"""
        from lumio.shared.intent_taxonomy import domain_of

        with open(SEED_PATH, encoding="utf-8") as f:
            data = json.load(f)
        rows = []
        for ex in data.get("examples", []):
            if not (ex.get("text") and ex.get("intent")):
                continue
            try:
                domain = domain_of(ex["intent"]).value
            except Exception:
                continue
            rows.append({"text": ex["text"], "intent": domain})
        return rows

    def _collection_exists(self) -> bool:
        from pymilvus import utility

        return utility.has_collection(COLLECTION_NAME)

    def _sync_create_and_insert(self, rows: list[dict]) -> None:
        from pymilvus import Collection, CollectionSchema, DataType, FieldSchema

        if self._collection_exists():
            self._coll = Collection(COLLECTION_NAME)
            if self._coll.num_entities > 0:
                self._coll.load()
                return  # 已有数据, 不重建 (但确保已加载)
        else:
            fields = [
                FieldSchema(name="pk", dtype=DataType.INT64, is_primary=True, auto_id=True),
                FieldSchema(name="intent", dtype=DataType.VARCHAR, max_length=64),
                FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=512),
                FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=DIM),
            ]
            schema = CollectionSchema(fields, description="L2 意图种子向量索引")
            self._coll = Collection(COLLECTION_NAME, schema)
            self._coll.create_index(
                "embedding", {"index_type": "HNSW", "metric_type": "COSINE", "params": {"M": 16, "efConstruction": 200}}
            )
        self._coll.insert(
            [
                [r["intent"] for r in rows],
                [r["text"] for r in rows],
                [r["embedding"] for r in rows],
            ]
        )
        self._coll.flush()
        self._coll.load()  # Milvus 检索前必须 load 进内存

    async def ensure_built(self) -> bool:
        """惰性建库：集合不存在或为空时, 嵌入种子语料并写入 Milvus。

        Returns: 索引是否可用（建库失败返回 False, 调用方跳过 L2）。
        """
        if self._built and self._coll is not None:
            return True
        if self._coll is None or self._embed is None:
            return False
        async with self._build_lock:
            if self._built and self._coll is not None:
                return True
            try:
                seeds = self._load_seeds()
                if not seeds:
                    return False
                logger.info("L2 意图向量索引构建开始: %d 条种子", len(seeds))
                embeddings = await self._embed.embed([s["text"] for s in seeds])
                rows = [{**s, "embedding": e} for s, e in zip(seeds, embeddings, strict=True)]
                await asyncio.to_thread(self._sync_create_and_insert, rows)
                self._built = True
                logger.info("L2 意图向量索引就绪: %d 条", len(rows))
                return True
            except Exception as exc:
                logger.warning("L2 意图向量索引构建失败(跳过 L2): %s", exc)
                return False

    # ── 检索 ──

    async def search(self, query: str) -> VectorIntentMatch:
        """查询向量 → 种子余弦检索, 返回 top-1 意图"""
        if self._coll is None or self._embed is None:
            return VectorIntentMatch(matched=False)
        if not await self.ensure_built():
            return VectorIntentMatch(matched=False)
        try:
            self._coll.load()  # 幂等: 已加载时近零开销
            emb = await self._embed.embed_query(query)
            res = await asyncio.to_thread(
                self._coll.search,
                data=[emb],
                anns_field="embedding",
                param={"metric_type": "COSINE", "params": {"ef": 64}},
                limit=1,
                output_fields=["intent", "text"],
            )
            hits = res[0] if res else []
            if not hits:
                return VectorIntentMatch(matched=False)
            top = hits[0]
            return VectorIntentMatch(
                matched=True,
                intent=top.entity.get("intent", ""),
                score=float(top.score),
                exemplar=top.entity.get("text", "")[:80],
            )
        except Exception as exc:
            logger.warning("L2 意图向量检索失败(跳过 L2): %s", exc)
            return VectorIntentMatch(matched=False)
