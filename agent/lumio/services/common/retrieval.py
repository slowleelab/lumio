"""混合检索引擎

实现 BM25 + 向量 + RRF 融合检索，支持 Reranker 精排和 Parent-Child 分块展开。
降级策略：Milvus 不可用 → BM25 only；ES 不可用 → 向量 only；均不可用 → 空结果。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from typing import TYPE_CHECKING, Any

from lumio.shared.config import get_settings
from lumio.shared.metrics import RAG_CACHE_OPS, RERANK_DEGRADATION, RETRIEVE_DURATION
from lumio.shared.models import RetrievedChunk, RetrieveRequest, RetrieveResponse
from lumio.shared.tracing import traced

if TYPE_CHECKING:
    from elasticsearch import AsyncElasticsearch
    from pymilvus import Collection

    from lumio.services.common.embedding import EmbeddingProvider
    from lumio.services.common.reranker import RerankerProvider

logger = logging.getLogger(__name__)

# ES keyword 过滤字段
_ES_KEYWORD_FIELDS = {
    "category",
    "doc_type",
    "card_type",
    "customer_tier",
    "security_level",
    "version",
    "chunk_type",
    "approval_status",
    "is_current_version",
}
# Milvus 标量索引字段（在 build_milvus_expr 中 == 比较）
_MILVUS_SCALAR_FIELDS = {
    "category",
    "doc_type",
    "card_type",
    "customer_tier",
    "security_level",
    "chunk_type",
    "approval_status",  # S4: 合规过滤
    "is_current_version",  # S4: 版本过滤
}
# ES date 过滤字段
_ES_DATE_FIELDS = {"effective_date", "expiry_date"}


def build_es_filters(filters: dict) -> list[dict]:
    """将 RetrieveRequest.filters 转换为 ES bool.filter 子句列表

    - keyword 字段 → term 查询
    - date 字段 → range 查询 (gte/lte, yyyy-MM-dd)
    - keywords → terms 查询（匹配任一关键词）
    """
    clauses: list[dict] = []
    for key, value in filters.items():
        if value is None:
            continue
        if key in _ES_KEYWORD_FIELDS:
            clauses.append({"term": {key: value}})
        elif key in _ES_DATE_FIELDS:
            if isinstance(value, dict):
                range_clause: dict[str, Any] = {}
                if "gte" in value:
                    epoch_ms = _date_to_epoch_ms(value["gte"])
                    if epoch_ms:
                        range_clause["gte"] = epoch_ms
                if "lte" in value:
                    epoch_ms = _date_to_epoch_ms(value["lte"])
                    if epoch_ms:
                        range_clause["lte"] = epoch_ms
                if range_clause:
                    clauses.append({"range": {key: range_clause}})
            elif isinstance(value, str):
                # 简写: 单个日期值视为 gte，统一转 epoch 毫秒（ES 数值范围边界按毫秒解释）
                epoch_ms = _date_to_epoch_ms(value)
                if epoch_ms:
                    clauses.append({"range": {key: {"gte": epoch_ms}}})
        elif key == "keywords":
            if isinstance(value, list):
                clauses.append({"terms": {key: value}})
            else:
                clauses.append({"term": {key: value}})
    return clauses


def build_milvus_expr(filters: dict) -> str:
    """将 RetrieveRequest.filters 转换为 Milvus 过滤表达式字符串

    - keyword 字段 -> field == "value"
    - date 字段 -> field >= epoch_sec (整型比较)
    - keywords -> keywords like "%value%" (VARCHAR 字段)
    - 多条件用 " and " 连接
    - 空 filters 返回 ""

    布尔值必须转小写字符串: ingestion 写入 Milvus 的是 VARCHAR "true"/"false",
    Python f-string 直转 True 会得到 "True" -> 恒不匹配 -> 向量检索静默 0 命中
    (修复前合规过滤让向量通道在真实环境整体失效, RAG 实际只有 BM25 单通道).
    """
    conditions: list[str] = []
    for key, value in filters.items():
        if value is None:
            continue
        if key in _MILVUS_SCALAR_FIELDS:
            value_str = str(value).lower() if isinstance(value, bool) else str(value)
            conditions.append(f'{key} == "{value_str}"')
        elif key in _ES_DATE_FIELDS:
            # 将日期字符串转为 epoch 秒（与 ES mapping epoch_second 格式对齐）
            if isinstance(value, dict):
                if "gte" in value:
                    epoch_sec = _date_to_epoch(value["gte"])
                    if epoch_sec:
                        conditions.append(f"{key} >= {epoch_sec}")
                if "lte" in value:
                    epoch_sec = _date_to_epoch(value["lte"])
                    if epoch_sec:
                        conditions.append(f"{key} <= {epoch_sec}")
            elif isinstance(value, str):
                epoch_sec = _date_to_epoch(value)
                if epoch_sec:
                    conditions.append(f"{key} >= {epoch_sec}")
        elif key == "keywords":
            # v2.1: ARRAY_CONTAINS 精确过滤，替代 like 模糊匹配
            if isinstance(value, list):
                kw_conds = [f'ARRAY_CONTAINS(keywords, "{kw}")' for kw in value]
                conditions.append("(" + " or ".join(kw_conds) + ")")
            else:
                conditions.append(f'ARRAY_CONTAINS(keywords, "{value}")')
    return " and ".join(conditions)


def _build_cache_key(
    query: str,
    filters: dict,
    search_type: str,
    include_expired: bool = False,  # S5: 第五轮修复 — 过期文档结果不得与默认请求共享缓存
    rerank: bool = False,  # S5: 精排结果与未精排结果不得互用缓存
) -> str:
    """生成检索缓存 key: lumio:rag:cache:{search_type}:{query_hash}:{filters_hash}[:exp|rerank]"""
    query_hash = hashlib.md5(query.encode()).hexdigest()[:12]
    filters_str = json.dumps(filters, sort_keys=True, ensure_ascii=False) if filters else "{}"
    filters_hash = hashlib.md5(filters_str.encode()).hexdigest()[:8]
    key = f"lumio:rag:cache:{search_type}:{query_hash}:{filters_hash}"
    if include_expired:
        key += ":exp"
    if rerank:
        key += ":rerank"
    return key


# ── 查询词法重叠门 (会话 8700a2ea 复盘) ──
# "锄禾日当午"靠单字"日"BM25 非零命中"账单日"文档: reranker 可用时 confidence_threshold
# (rerank 分数对 query+doc 联合打分) 能拦, 但 reranker 退化 (Ollama /api/rerank 404) 时
# 回退 RRF 阈值 0.0 = 零过滤, 词法证据门又只拦"BM25 零命中" — 单字命中即绕过。
# 本门在调用侧兜底: 查询的全部信息性词块 (CJK 2-gram / ≥2 字符拉丁数字词) 与所有
# 检索片段零重叠 → 无任何词法证据, 视为 miss。与 FAQ 双门槛同一哲学: 没有证据不生成。
_CJK_RUN = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z0-9]+")
_ALNUM_RUN = re.compile(r"[A-Za-z0-9]+")


def _query_grams(query: str) -> set[str]:
    """提取查询的信息性词块: 连续 CJK 段的 2-gram + ≥2 字符拉丁/数字词"""
    grams: set[str] = set()
    for run in _CJK_RUN.findall(query or ""):
        if _ALNUM_RUN.fullmatch(run):
            if len(run) >= 2:
                grams.add(run.lower())
        else:
            grams.update(run[i : i + 2] for i in range(len(run) - 1))
    return grams


# 通用动词/疑问/助词类 bigram: 几乎存在于所有银行业务文档 ("查看账单/登录APP/
# 办理业务"), 命中不构成相关性证据 (会话 9ed55603: 无义输入"查看开发"靠"查看"
# 一词击穿重叠门, 14.2s 生成了整段账单知识)。与 FAQ 侧 _FAQ_GENERIC_GRAMS 同型。
_GENERIC_GRAMS = frozenset(
    {
        "查看", "登陆", "登录", "办理", "操作", "咨询", "帮忙", "一下", "请问", "麻烦",
        "告诉", "了解", "相关", "问题", "业务", "银行", "网上", "客服", "怎么", "如何",
        "什么", "可以", "能为", "需要", "我想", "还是", "没有", "不是",
    }
)

# 业务名词词块 (强证据): 银行信用卡域稳定名词, 一个命中即构成相关性证据
_BUSINESS_NOUN_GRAMS = frozenset(
    {
        "账单", "额度", "积分", "挂失", "还款", "分期", "年费", "逾期", "密码", "激活",
        "销户", "销卡", "发票", "利息", "手续费", "账单日", "还款日", "信用", "授信",
        "取现", "现金", "透支", "滞纳", "违约", "征信", "卡片", "补卡", "换卡", "盗刷",
        "钱包", "数币", "人民币", "转账", "消费", "交易", "明细", "流水", "最低还款",
        "临时额度", "还款额", "免息", "宽限", "积分兑换", "里程", "话费", "权益",
        "冻结", "解冻", "限额", "申请", "白条",
    }
)


def query_chunk_overlap_zero(query: str, chunks: list[str]) -> bool:
    """查询与检索片段零词法重叠判定 (调用侧相关性兜底门)

    True = 查询没有任何信息性词块出现在任何片段里 → 判 miss;
    查询本身无可提取词块 (如单字"卡") 时无法判定 → False (放行, 不误杀)。
    通用动词/疑问词块不计入证据 (去停用后无词块 = 无法判定, 放行)。
    """
    grams = _query_grams(query) - _GENERIC_GRAMS
    if not grams:
        return False
    blob = "\n".join(chunks)
    # 业务名词强证据: 银行域稳定名词词块命中即放行 ("查看账单"的证据是"账单",
    # 跨词 bigram "看账"会被拆碎 — 强名词一个就够)。
    if any(g in blob for g in grams & _BUSINESS_NOUN_GRAMS):
        return False
    hits = sum(1 for g in grams if g in blob)
    # 弱证据强度判据 (会话 9ed55603: "查看开发"靠碎片"开发"撞上无关文档放行):
    # 表外词块需 ≥2 命中 — 单碎片撞词 (无义输入的 bigram 碎片碰上某文档) 判 miss;
    # 单词块查询 (弱词但整块命中) 保留放行, 不误杀长尾真实问法。
    if len(grams) == 1:
        return hits == 0
    return hits < 2


def _date_to_epoch(date_str: str) -> int | None:
    """将 yyyy-MM-dd 日期字符串转为 epoch 秒（与 ES mapping epoch_second 格式对齐）"""
    try:
        from datetime import datetime

        dt = datetime.strptime(date_str, "%Y-%m-%d")
        return int(dt.timestamp())
    except (ValueError, TypeError):
        return None


def _date_to_epoch_ms(date_str: str) -> int | None:
    """将 yyyy-MM-dd 日期字符串转为 epoch 毫秒

    ES 数值型 range 边界始终按内部毫秒解释（即使 mapping 声明 epoch_second，
    也仅作用于索引解析时），因此 ES 侧 range 需用毫秒，Milvus 侧仍用秒。
    """
    epoch_sec = _date_to_epoch(date_str)
    return epoch_sec * 1000 if epoch_sec is not None else None


async def search_bm25(
    es_client: AsyncElasticsearch | None,
    query: str,
    top_k: int = 5,
    filters: dict | None = None,
) -> tuple[list[RetrievedChunk], float | None]:
    """BM25 全文检索（Elasticsearch + IK 分词）

    Args:
        es_client: ES 异步客户端（None 时返回空列表，触发降级）
        query: 查询文本
        top_k: 返回结果数
        filters: 过滤条件

    Returns:
        (chunks, best_score) 元组:
        - chunks: RetrievedChunk 列表；异常时返回空列表
        - best_score: ES 本次应答的最高原始 BM25 分（0 命中时为 0.0）;
          ES 未查询/查询失败（None 客户端或异常）为 None —— 两者必须可区分,
          供 retrieve() 的相关性下限门判断"ES 说没有相关内容"与"ES 挂了".
    """
    if es_client is None:
        return [], None

    settings = get_settings()
    index_name = f"{settings.elasticsearch.index_prefix}_kb_chunks"

    # 构建 ES 查询体
    match_query = {"match": {"content": {"query": query, "analyzer": "ik_smart"}}}
    filter_clauses = build_es_filters(filters or {})

    if filter_clauses:
        body: dict[str, Any] = {"query": {"bool": {"must": [match_query], "filter": filter_clauses}}}
    else:
        body = {"query": match_query}

    try:
        resp = await es_client.search(index=index_name, body=body, size=top_k)
        hits = resp["hits"]["hits"]
        best_score = max((h["_score"] for h in hits if h.get("_score") is not None), default=0.0)
        results: list[RetrievedChunk] = []

        # 收集所有 parent_chunk_id，批量获取 parent 内容
        parent_ids = set()
        for hit in hits:
            pid = hit["_source"].get("parent_chunk_id")
            if pid:
                parent_ids.add(pid)
        parent_contents = await _batch_fetch_parents_es(es_client, index_name, list(parent_ids))

        for hit in hits:
            source = hit["_source"]
            chunk_id = source.get("chunk_id", hit["_id"])
            parent_chunk_id = source.get("parent_chunk_id")

            metadata = {k: v for k, v in source.items() if k not in ("chunk_id", "content", "doc_id")}
            if parent_chunk_id and parent_chunk_id in parent_contents:
                metadata["parent_content"] = parent_contents[parent_chunk_id]

            results.append(
                RetrievedChunk(
                    chunk_id=chunk_id,
                    content=source.get("content", ""),
                    score=hit["_score"],
                    source_doc=source.get("doc_id", ""),
                    metadata=metadata,
                )
            )
        return results, best_score
    except Exception:
        logger.exception("BM25 检索异常: query=%s", query)
        return [], None


async def _batch_fetch_parents_es(
    es_client: AsyncElasticsearch,
    index_name: str,
    parent_ids: list[str],
) -> dict[str, str]:
    """批量从 ES 获取 parent chunk 内容"""
    if not parent_ids:
        return {}
    try:
        resp = await es_client.mget(index=index_name, body={"ids": parent_ids})
        contents: dict[str, str] = {}
        for doc in resp.get("docs", []):
            if doc.get("found") and doc.get("_source"):
                contents[doc["_id"]] = doc["_source"].get("content", "")
        return contents
    except Exception:
        logger.debug("批量获取 ES parent 内容失败: count=%d", len(parent_ids))
        return {}


async def search_vector(
    milvus_collection: Collection | None,
    query_embedding: list[float],
    top_k: int = 5,
    filters: dict | None = None,
) -> list[RetrievedChunk]:
    """向量检索（Milvus IVF_FLAT COSINE）

    只搜索 child chunks（有 embedding 的块），不搜索 parent chunks。

    Args:
        milvus_collection: Milvus Collection 对象（None 时返回空列表，触发降级）
        query_embedding: 查询向量
        top_k: 返回结果数
        filters: 过滤条件

    Returns:
        RetrievedChunk 列表；异常时返回空列表
    """
    if milvus_collection is None:
        return []

    # 构建 Milvus 过滤表达式
    base_expr = build_milvus_expr(filters or {})
    # Milvus 中只存了有 embedding 的 chunk，所以不需要额外过滤

    search_params = {"metric_type": "COSINE", "params": {"nprobe": 16}}
    output_fields = [
        "chunk_id",
        "doc_id",
        "content",
        "category",
        "doc_type",
        "keywords",
        "card_type",
        "customer_tier",
        "security_level",
        "chunk_type",
        "parent_chunk_id",
        "approval_status",  # S4: 合规过滤
        "is_current_version",  # S4: 版本过滤
    ]

    milvus_timeout = get_settings().milvus.search_timeout
    try:
        results_raw = await asyncio.wait_for(
            asyncio.to_thread(
                milvus_collection.search,
                data=[query_embedding],
                anns_field="embedding",
                param=search_params,
                limit=top_k,
                expr=base_expr if base_expr else None,
                output_fields=output_fields,
            ),
            timeout=milvus_timeout,
        )

        results: list[RetrievedChunk] = []
        if results_raw and len(results_raw) > 0:
            # 收集所有 parent_chunk_id，批量获取 parent 内容
            parent_ids = set()
            for hit in results_raw[0]:
                pid = hit.entity.get("parent_chunk_id")
                if pid:
                    parent_ids.add(pid)
            parent_contents = await asyncio.wait_for(
                _batch_fetch_parents_milvus(milvus_collection, list(parent_ids)),
                timeout=milvus_timeout,
            )

            for hit in results_raw[0]:
                entity = hit.entity
                chunk_id = entity.get("chunk_id") or str(hit.id)
                parent_chunk_id = entity.get("parent_chunk_id")

                metadata: dict[str, Any] = {}
                for field_name in output_fields:
                    if field_name not in ("chunk_id", "content", "doc_id") and entity.get(field_name) is not None:
                        value = entity.get(field_name)
                        # Milvus ARRAY 字段(keywords)返回 protobuf RepeatedScalarContainer,
                        # 原样落 metadata 会让响应模型 JSON 序列化失败 (PydanticSerializationError)
                        metadata[field_name] = (
                            list(value) if not isinstance(value, str | int | float | bool | bytes) else value
                        )
                if parent_chunk_id and parent_chunk_id in parent_contents:
                    metadata["parent_content"] = parent_contents[parent_chunk_id]

                results.append(
                    RetrievedChunk(
                        chunk_id=chunk_id,
                        content=entity.get("content", ""),
                        score=hit.distance,
                        source_doc=entity.get("doc_id", ""),
                        metadata=metadata,
                    )
                )
        return results
    except TimeoutError:
        logger.warning(
            "Milvus 向量检索超时(%.1fs), 降级 BM25-only: top_k=%d",
            milvus_timeout,
            top_k,
        )
        return []
    except Exception:
        logger.exception("向量检索异常: top_k=%d", top_k)
        return []


async def _batch_fetch_parents_milvus(
    milvus_collection: Collection,
    parent_ids: list[str],
) -> dict[str, str]:
    """批量从 Milvus 获取 parent chunk 内容"""
    if not parent_ids:
        return {}
    try:
        ids_str = ", ".join(f'"{pid}"' for pid in parent_ids)
        expr = f"chunk_id in [{ids_str}]"
        results = await asyncio.to_thread(
            milvus_collection.query,
            expr=expr,
            output_fields=["chunk_id", "content"],
        )
        contents: dict[str, str] = {}
        for r in results:
            cid = r.get("chunk_id", "")
            content = r.get("content", "")
            if cid and content:
                contents[cid] = content
        return contents
    except Exception:
        logger.debug("批量获取 Milvus parent 内容失败: count=%d", len(parent_ids))
        return {}


def rrf_fusion(
    bm25_results: list[RetrievedChunk],
    vector_results: list[RetrievedChunk],
    k: int = 60,
) -> list[RetrievedChunk]:
    """Reciprocal Rank Fusion 融合 BM25 和向量检索结果

    RRF 公式: score(d) = Σ 1/(k + rank_i)
    - 以 chunk_id 去重
    - 同时出现在两个列表的 chunk 分数求和
    - 按 RRF 分数降序排列

    Args:
        bm25_results: BM25 检索结果
        vector_results: 向量检索结果
        k: RRF 常数（默认 60）

    Returns:
        融合后的 RetrievedChunk 列表
    """
    # chunk_id → (cumulative_score, RetrievedChunk)
    score_map: dict[str, tuple[float, RetrievedChunk]] = {}

    for rank, chunk in enumerate(bm25_results, start=1):
        rrf_score = 1.0 / (k + rank)
        if chunk.chunk_id in score_map:
            existing_score, existing_chunk = score_map[chunk.chunk_id]
            score_map[chunk.chunk_id] = (existing_score + rrf_score, existing_chunk)
        else:
            score_map[chunk.chunk_id] = (rrf_score, chunk)

    for rank, chunk in enumerate(vector_results, start=1):
        rrf_score = 1.0 / (k + rank)
        if chunk.chunk_id in score_map:
            existing_score, existing_chunk = score_map[chunk.chunk_id]
            score_map[chunk.chunk_id] = (existing_score + rrf_score, existing_chunk)
        else:
            score_map[chunk.chunk_id] = (rrf_score, chunk)

    # 按 RRF 分数降序排列
    sorted_results = sorted(score_map.values(), key=lambda x: x[0], reverse=True)

    # 更新 score 为 RRF 分数
    return [
        RetrievedChunk(
            chunk_id=chunk.chunk_id,
            content=chunk.content,
            score=score,
            source_doc=chunk.source_doc,
            metadata=chunk.metadata,
        )
        for score, chunk in sorted_results
    ]


@traced("Agent: retrieval")
async def retrieve(
    request: RetrieveRequest,
    es_client: AsyncElasticsearch | None = None,
    milvus_collection: Collection | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    reranker: RerankerProvider | None = None,
    redis_client: Any = None,
) -> RetrieveResponse:
    """混合检索编排

    流程:
    0. Redis 缓存命中 → 直接返回
    1. 按 search_type 分发: hybrid / bm25_only / vector_only
    2. Hybrid: 并发执行 BM25 + 向量检索，RRF 融合
    3. 可选 Reranker 精排
    4. 置信度阈值过滤
    5. 截断到 top_k → 写入缓存

    降级矩阵:
    | ES | Milvus | 行为 |
    |----|--------|------|
    | ✓  | ✓      | Hybrid + RRF |
    | ✓  | ✗      | BM25 only |
    | ✗  | ✓      | Vector only |
    | ✗  | ✗      | 空结果 |
    """
    start_time = time.monotonic()
    settings = get_settings()
    rrf_k = request.rrf_k if request.rrf_k is not None else settings.rag.rrf_k
    confidence_threshold = settings.rag.confidence_threshold

    # 0. Redis 缓存检查 (S5: key 含 include_expired/rerank 维度, 防过期文档/未精排结果泄露)
    cache_key = _build_cache_key(
        request.query,
        request.filters or {},
        request.search_type,
        include_expired=request.include_expired,
        rerank=request.rerank,
    )
    if redis_client and request.search_type != "vector_only":
        try:
            cached_raw = await redis_client.get(cache_key)
            if cached_raw:
                RAG_CACHE_OPS.labels(result="hit", search_type=request.search_type).inc()
                cached_data = json.loads(cached_raw)
                cached_results = [
                    RetrievedChunk(
                        chunk_id=c["chunk_id"],
                        content=c["content"],
                        score=c["score"],
                        source_doc=c.get("source_doc", ""),
                        metadata=c.get("metadata", {}),
                    )
                    for c in cached_data["results"]
                ]
                return RetrieveResponse(
                    results=cached_results[: request.top_k],
                    total_candidates=cached_data["total_candidates"],
                    latency_ms=int((time.monotonic() - start_time) * 1000),
                )
            RAG_CACHE_OPS.labels(result="miss", search_type=request.search_type).inc()
        except Exception:
            logger.debug("Redis 缓存读取失败，走检索路径")

    # 扩展候选集
    expanded_k = request.top_k * 3

    # ── 银行合规过滤: 注入审批状态 + 当前版本 + 时间过滤 ──
    compliance_filters = dict(request.filters or {})
    compliance_filters["approval_status"] = "PUBLISHED"
    compliance_filters["is_current_version"] = True
    if not request.include_expired:
        from datetime import date as _date

        today_str = _date.today().isoformat()
        compliance_filters["effective_date"] = {"lte": today_str}
        # expiry_date 为空或 >= 今天（ES 层用 should 处理 OR 逻辑，这里简化为不传，Python 侧后过滤）

    bm25_results: list[RetrievedChunk] = []
    vector_results: list[RetrievedChunk] = []
    # ES 本次应答的最高原始 BM25 分; None = ES 未查询/查询失败 (与"ES 说没有相关内容"区分)
    bm25_best_score: float | None = None

    if request.search_type == "hybrid":
        # 并行: ES BM25 ∥ (embed -> Milvus vector)
        bm25_task = asyncio.create_task(search_bm25(es_client, request.query, expanded_k, compliance_filters))

        if embedding_provider and milvus_collection:
            # 初始化为 None: embed_query 抛异常时 except 分支会引用, 避免 NameError
            vector_task: asyncio.Task[list[RetrievedChunk]] | None = None
            try:
                query_embedding = await embedding_provider.embed_query(request.query)
                vector_task = asyncio.create_task(
                    search_vector(milvus_collection, query_embedding, expanded_k, compliance_filters)
                )
                (bm25_results, bm25_best_score), vector_results = await asyncio.gather(bm25_task, vector_task)
            except Exception:
                logger.warning("向量检索嵌入失败，降级到 BM25 only")
                # 只取消 vector_task (embed 阶段已失败, 向量检索必然拿不到 embedding)
                # bm25_task 不取消, 让其自然完成, 结果可降级使用
                if vector_task is not None and not vector_task.done():
                    vector_task.cancel()
                bm25_results, bm25_best_score = await bm25_task
        else:
            bm25_results, bm25_best_score = await bm25_task

        # 融合
        if bm25_results and vector_results:
            fused = rrf_fusion(bm25_results, vector_results, k=rrf_k)
        elif bm25_results:
            fused = bm25_results
            logger.info("向量检索无结果，降级到 BM25 only")
        elif vector_results:
            fused = vector_results
            logger.info("BM25 检索无结果，降级到向量 only")
        else:
            fused = []

    elif request.search_type == "bm25_only":
        bm25_results, bm25_best_score = await search_bm25(es_client, request.query, expanded_k, compliance_filters)
        fused = bm25_results

    elif request.search_type == "vector_only":
        if embedding_provider and milvus_collection:
            try:
                query_embedding = await embedding_provider.embed_query(request.query)
                vector_results = await search_vector(milvus_collection, query_embedding, expanded_k, compliance_filters)
            except Exception:
                logger.warning("向量检索失败: query=%s", request.query)
        fused = vector_results

    else:
        logger.warning("未知 search_type: %s", request.search_type)
        fused = []

    # Reranker 精排
    use_reranker_threshold = False
    if request.rerank and reranker and fused:
        candidate_count = request.top_k * 2
        candidates = fused[:candidate_count]
        content_list = [c.content for c in candidates]

        try:
            rerank_results = await asyncio.to_thread(reranker.rerank, request.query, content_list, request.top_k)
            # 映射回 RetrievedChunk
            reranked: list[RetrievedChunk] = []
            for rr in rerank_results:
                if 0 <= rr.index < len(candidates):
                    original = candidates[rr.index]
                    reranked.append(
                        RetrievedChunk(
                            chunk_id=original.chunk_id,
                            content=original.content,
                            score=rr.relevance_score,
                            source_doc=original.source_doc,
                            metadata=original.metadata,
                        )
                    )
            if reranked:
                # 退化检测: 评分全为 <= 0 时视为 reranker 不可用/失效
                # (Ollama/无模型时 _score_document 捕获异常返回 0.0), 回退到 RRF 结果,
                # 避免 0 分全部命中置信度阈值而被过滤为空。
                if all(rr.score <= 0.0 for rr in reranked):
                    RERANK_DEGRADATION.labels(reason="zero_scores").inc()
                    logger.warning("Reranker 评分为全 0，判定为退化，回退到 RRF 结果")
                else:
                    fused = reranked
                    use_reranker_threshold = True
        except Exception:
            RERANK_DEGRADATION.labels(reason="error").inc()
            logger.warning("Reranker 调用失败，使用 RRF 结果", exc_info=True)

    # 置信度阈值过滤（RRF 和 Reranker 使用不同阈值）
    threshold = confidence_threshold if use_reranker_threshold else settings.rag.rrf_confidence_threshold
    if threshold > 0 and fused:
        fused = [c for c in fused if c.score >= threshold]
        if not fused:
            logger.warning("置信度过滤后无结果: threshold=%.3f", threshold)

    # ── 词法证据门 (P1): reranker 退化时的兜底门 ──
    # 背景: Ollama /api/rerank 404 (reranker 静默失效) -> 回退 RRF 阈值 0.0 = 零过滤,
    # 乱码输入也能拿到知识文档喂 LLM (会话 e33d1fa8 "额佛呢份" 拿 556 字符相关内容流畅胡答).
    # 门语义: ES 已应答(best 非 None)但 BM25 零命中(best==0, 没有任何 token 匹配) ->
    # 知识库没有能对上这句话的内容, 返回空 (调用方走无知识降级话术).
    # 为什么不是绝对分数下限: 实测真实短查询与乱码的 BM25 原始分区间重叠('年费'@0.92
    # vs '额佛呢份'@2.77), 分数混合了查询词特异度与相关性, 无一刀切阈值; 零命中是
    # 唯一无歧义的"没有词法证据"信号, 向量余弦又无区分度(乱码 0.55-0.63/真实 0.52-0.79).
    # 不拦: ES 挂了(best=None, 保留 vector-only 降级)和 reranker 分数生效时
    # (confidence_threshold 主导 -- reranker 对 query+doc 联合打分, 是比词法更强的证据).
    if (
        settings.rag.require_lexical_evidence
        and not use_reranker_threshold
        and bm25_best_score is not None
        and bm25_best_score <= 0.0
    ):
        logger.warning(
            "BM25 零命中, 判定无相关知识(词法证据门): query=%r candidates=%d",
            request.query,
            len(fused),
        )
        fused = []

    # ── Milvus 合规后过滤 (S4 第五轮修复) ──
    # 旧实现: Milvus schema 无 approval_status/is_current_version, metadata 取默认值
    # "PUBLISHED"/True → 过滤形同虚设, 未审批/非当前版本文档经向量检索泄露.
    # 现: ingestion 已写两字段, 此处严格判定 (缺失字段视为不合规, 不再默认放行).
    if fused:
        pre_count = len(fused)
        fused = [
            c
            for c in fused
            if c.metadata.get("approval_status") == "PUBLISHED"
            and str(c.metadata.get("is_current_version", "")).lower() == "true"
        ]
        if len(fused) < pre_count:
            logger.debug("合规后过滤: %d → %d", pre_count, len(fused))

    # 截断到 top_k
    fused = fused[: request.top_k]

    latency_ms = int((time.monotonic() - start_time) * 1000)
    total_candidates = len(bm25_results) + len(vector_results)

    # 写入 Redis 缓存（TTL 300s，仅非空结果）
    if redis_client and fused and request.search_type != "vector_only":
        try:
            cache_data = {
                "results": [
                    {
                        "chunk_id": c.chunk_id,
                        "content": c.content,
                        "score": c.score,
                        "source_doc": c.source_doc,
                        "metadata": c.metadata,
                    }
                    for c in fused
                ],
                "total_candidates": total_candidates,
            }
            await redis_client.setex(cache_key, 300, json.dumps(cache_data, ensure_ascii=False))
        except Exception:
            logger.debug("Redis 缓存写入失败")

    # 6b: dashboard 缺口补齐 — observe 直方图 (search_type 取 request.search_type.value)
    try:
        st = str(getattr(request.search_type, "value", request.search_type))
        RETRIEVE_DURATION.labels(search_type=st).observe(time.monotonic() - start_time)
    except Exception:
        # 指标失败不影响主流程
        logger.debug("RETRIEVE_DURATION observe 失败, 不影响主流程")

    return RetrieveResponse(
        results=fused,
        total_candidates=total_candidates,
        latency_ms=latency_ms,
    )
