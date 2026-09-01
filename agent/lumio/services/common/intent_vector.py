"""L2 向量检索意图识别（目标架构 ③ 分层管道的第二层）

L1 规则匹配未命中 → 本层把用户输入向量化，与意图种子语料（seed_dataset.json
的 191 条出厂标注 + 意图注册表已生效意图的种子）的向量做余弦检索，取 top-1
域 + 相似度作置信；置信不足 → 交 L3 大模型分类。

蓝绿索引 (流派二 · 版本化重建):
- 物理集合按版本命名 lumio_intent_domain_vectors_v{N}, 别名 lumio_intent_domain_vectors
  指向当前生效版本, 检索始终走别名。
- 重建 = 建 v{N+1} (嵌入新语料) → 校验非空 → alias 原子切换 → 保留上一版兜底回滚,
  更旧版本清理。切换瞬间的检索失败由既有 try/except 降级为跳过 L2, 不影响可用性。
- 影子/下线意图的种子不进索引 (影子只观察; 下线即从语料移除后重建生效)。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

COLLECTION_NAME = "lumio_intent_domain_vectors"
DIM = 1024
SEED_PATH = Path(__file__).resolve().parents[3] / "data" / "intent_classification" / "seed_dataset.json"

_VERSION_RE = re.compile(re.escape(COLLECTION_NAME) + r"_v(\d+)$")
_RETAIN_VERSIONS = 2  # 保留当前 + 上一版 (回滚兜底)


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


@dataclass
class RebuildStatus:
    """蓝绿重建状态 (模块级单例, 管理端轮询读)"""

    running: bool = False
    action: str = ""  # rebuild/rollback
    version: int = 0  # 当前生效版本 (0 = 未知/未版本化)
    entities: int = 0
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


_status = RebuildStatus()
_status_lock = threading.Lock()


def get_rebuild_status() -> dict:
    with _status_lock:
        return _status.to_dict()


class IntentVectorIndex:
    """意图种子向量索引（Milvus 持久化, 惰性建库, 蓝绿重建）"""

    def __init__(self, milvus_collection: Any = None, embedding_provider: Any = None) -> None:
        self._coll = milvus_collection
        self._embed = embedding_provider
        self._build_lock = asyncio.Lock()
        self._rebuild_lock = asyncio.Lock()
        self._built = False

    # ── 建库 ──

    def _load_seeds(self) -> list[dict]:
        """种子按五域标注 (骨架第一级, 5 类比 10 叶类检索准得多)。

        语料 = 出厂 seed_dataset.json + 意图注册表已生效 (active) 意图的种子。
        """
        from lumio.shared.intent_registry import RegistryState, get_registry
        from lumio.shared.intent_taxonomy import domain_of

        rows = []
        with open(SEED_PATH, encoding="utf-8") as f:
            data = json.load(f)
        for ex in data.get("examples", []):
            if not (ex.get("text") and ex.get("intent")):
                continue
            try:
                domain = domain_of(ex["intent"]).value
            except Exception:
                continue
            rows.append({"text": ex["text"], "intent": domain})
        for entry in get_registry().list_entries():
            if entry.state != RegistryState.ACTIVE:
                continue
            rows.extend({"text": s, "intent": entry.domain} for s in entry.seeds)
        return rows

    def _collection_exists(self) -> bool:
        from pymilvus import utility

        return bool(utility.has_collection(COLLECTION_NAME))

    def _sync_create_and_insert(self, rows: list[dict], *, name: str = COLLECTION_NAME) -> None:
        from pymilvus import Collection, CollectionSchema, DataType, FieldSchema

        if name == COLLECTION_NAME and self._collection_exists():
            self._coll = Collection(COLLECTION_NAME)
            if self._coll.num_entities > 0:
                self._coll.load()
                return  # 已有数据, 不重建 (但确保已加载)
        fields = [
            FieldSchema(name="pk", dtype=DataType.INT64, is_primary=True, auto_id=True),
            FieldSchema(name="intent", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=512),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=DIM),
        ]
        schema = CollectionSchema(fields, description="L2 意图种子向量索引")
        self._coll = Collection(name, schema)
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

    # ── 蓝绿重建 ──

    def _existing_versions(self) -> list[int]:
        from pymilvus import utility

        names = utility.list_collections()
        return sorted(int(m.group(1)) for n in names if (m := _VERSION_RE.match(n)))

    def _sync_build_version(self, name: str, rows: list[dict]) -> None:
        """建物理版本集合 (不带别名判断, 恒新建)"""
        from pymilvus import utility

        if utility.has_collection(name):
            utility.drop_collection(name)
        self._sync_create_and_insert(rows, name=name)

    def _sync_switch_alias(self, target: str) -> None:
        """别名原子切到 target 物理集合; 名字被旧物理集合占用时先清掉"""
        from pymilvus import utility

        try:
            utility.alter_alias(target, COLLECTION_NAME)
            return
        except Exception:
            pass
        try:
            utility.create_alias(target, COLLECTION_NAME)
            return
        except Exception:
            # 名字被未版本化的存量物理集合占用 (旧部署) → 清掉再挂别名
            if utility.has_collection(COLLECTION_NAME):
                utility.drop_collection(COLLECTION_NAME)
            utility.create_alias(target, COLLECTION_NAME)

    def _sync_gc_old_versions(self, current_v: int) -> None:
        from pymilvus import utility

        for v in self._existing_versions():
            if v <= current_v - _RETAIN_VERSIONS:
                utility.drop_collection(f"{COLLECTION_NAME}_v{v}")

    async def rebuild_versioned(self) -> dict:
        """蓝绿重建: 建新版本 → 校验 → alias 原子切换 → 旧版本回收。

        Returns: {"version": N, "entities": M}
        """
        async with self._rebuild_lock:
            if self._embed is None:
                raise RuntimeError("嵌入服务不可用, 无法重建")
            with _status_lock:
                _status.running = True
                _status.action = "rebuild"
                _status.error = ""
                _status.started_at = time.time()
            try:
                seeds = self._load_seeds()
                if not seeds:
                    raise RuntimeError("种子语料为空, 拒绝重建")
                versions = self._existing_versions()
                next_v = (versions[-1] + 1) if versions else 1
                target = f"{COLLECTION_NAME}_v{next_v}"
                logger.info("L2 意图索引蓝绿重建: v%d, %d 条种子", next_v, len(seeds))
                embeddings = await self._embed.embed([s["text"] for s in seeds])
                rows = [{**s, "embedding": e} for s, e in zip(seeds, embeddings, strict=True)]
                await asyncio.to_thread(self._sync_build_version, target, rows)
                built = await asyncio.to_thread(self._fetch_collection, target)
                entities = built.num_entities if built is not None else 0
                if entities <= 0:
                    raise RuntimeError("新版本集合写入为空, 中止切换")
                await asyncio.to_thread(self._sync_switch_alias, target)
                await asyncio.to_thread(self._sync_gc_old_versions, next_v)
                # 重绑到别名 (后续 search 走新版本)
                self._coll = await asyncio.to_thread(self._fetch_collection, COLLECTION_NAME)
                if self._coll is not None:
                    self._coll.load()
                self._built = True
                from lumio.shared.metrics import INTENT_INDEX_REBUILDS

                INTENT_INDEX_REBUILDS.labels(result="success").inc()
                with _status_lock:
                    _status.running = False
                    _status.version = next_v
                    _status.entities = entities
                    _status.finished_at = time.time()
                logger.info("L2 意图索引蓝绿重建完成: v%d (%d 条)", next_v, entities)
                return {"version": next_v, "entities": entities}
            except Exception as exc:
                from lumio.shared.metrics import INTENT_INDEX_REBUILDS

                INTENT_INDEX_REBUILDS.labels(result="failed").inc()
                with _status_lock:
                    _status.running = False
                    _status.error = str(exc)
                    _status.finished_at = time.time()
                logger.warning("L2 意图索引蓝绿重建失败: %s", exc)
                raise

    async def rollback_version(self) -> dict:
        """回滚到上一版本 (保留的兜底集合)"""
        async with self._rebuild_lock:
            versions = await asyncio.to_thread(self._existing_versions)
            if len(versions) < 2:
                raise RuntimeError("无可回滚的上一版本")
            target_v = versions[-2]
            target = f"{COLLECTION_NAME}_v{target_v}"
            await asyncio.to_thread(self._sync_switch_alias, target)
            self._coll = await asyncio.to_thread(self._fetch_collection, COLLECTION_NAME)
            if self._coll is not None:
                self._coll.load()
            self._built = True
            from lumio.shared.metrics import INTENT_INDEX_REBUILDS

            INTENT_INDEX_REBUILDS.labels(result="rolled_back").inc()
            with _status_lock:
                _status.action = "rollback"
                _status.version = target_v
                _status.finished_at = time.time()
            logger.info("L2 意图索引回滚: v%d", target_v)
            return {"version": target_v}

    @staticmethod
    def _fetch_collection(name: str) -> Any:
        from pymilvus import Collection

        try:
            return Collection(name)
        except Exception:
            return None

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
            logger.warning("L2 向量意图检索失败(跳过 L2): %s", exc)
            return VectorIntentMatch(matched=False)
