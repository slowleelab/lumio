"""FAQ 知识库服务

提供 FAQ 的 CRUD、审批、索引和检索能力。

检索策略（串行短路）:
1. 精确匹配（Redis）— variant_questions 归一化后精确命中 → 直接返回
2. 语义匹配（Milvus）— question + variants 的 embedding COSINE 相似 → top_k
3. 降级到通用 RAG — 文档检索（调用方处理）
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import unicodedata
from datetime import date
from typing import Any

import redis.asyncio as aioredis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumio.shared.orm_models import KbFaq, KbFaqSearchLog

logger = logging.getLogger(__name__)

_FAQ_CACHE_PREFIX = "lumio:faq:exact"
_FAQ_CACHE_TTL = 3600  # 1 小时


def _normalize_query(text: str) -> str:
    """查询归一化: NFKC + 小写 + 去全部标点 + 压缩空格

    客户问 "账单日和还款日是什么" 与 "账单日和还款日是什么？" 应命中同一条
    精确缓存 —— 标点差异不该击穿精确匹配 (语义路兜底延迟高得多)。
    """
    text = unicodedata.normalize("NFKC", text).lower()
    # Python re 不支持 \p{P}: 显式枚举空白 + ASCII 标点 + CJK 标点区
    text = re.sub(r"[\s\u3000-\u303f\uff00-\uffef!-/:-@\[-`{-~]+", "", text)
    return text


def _cache_key(query: str) -> str:
    """生成 Redis 精确匹配缓存 key"""
    normalized = _normalize_query(query)
    h = hashlib.md5(normalized.encode()).hexdigest()
    return f"{_FAQ_CACHE_PREFIX}:{h}"


# ── CRUD ──


def _coerce_faq_id(faq_id: str):
    """字符串 ID → Uuid(native_uuid=False) 列可绑定的 uuid_utils.UUID。

    字符串直接与 Uuid 列比较会在 bind 阶段抛 StatementError (审批/CRUD 全链路
    此前只被 mock 单测覆盖, 未打过真实 PG, 会话 2026-08-28 界面联调发现)。
    合法 UUID 归一为 UUID 对象; 非法字符串原样透传——交由上层"不存在"语义
    (get→None / update、delete→False) 或真实 PG 的既有报错处理, 不在此处改写契约。
    """
    import uuid_utils

    try:
        return uuid_utils.UUID(str(faq_id))
    except ValueError:
        return str(faq_id)


async def create_faq(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    question: str,
    answer: str,
    variant_questions: list[str] | None = None,
    category: str,
    card_types: list[str] | None = None,
    customer_tiers: list[str] | None = None,
    keywords: list[str] | None = None,
    effective_date: date | None = None,
    expiry_date: date | None = None,
    allowed_roles: list[str] | None = None,
    regulatory_tags: list[str] | None = None,
    created_by: str = "system",
) -> KbFaq:
    """创建 FAQ"""
    import uuid_utils

    doc_group = f"faq_{uuid_utils.uuid7().hex[:16]}"

    async with session_factory() as session:
        faq = KbFaq(
            question=question,
            answer=answer,
            variant_questions=variant_questions or [],
            category=category,
            card_types=card_types or [],
            customer_tiers=customer_tiers or [],
            keywords=keywords or [],
            doc_group=doc_group,
            approval_status="DRAFT",
            effective_date=effective_date,
            expiry_date=expiry_date,
            allowed_roles=allowed_roles or [],
            regulatory_tags=regulatory_tags or [],
            created_by=created_by,
        )
        session.add(faq)
        await session.commit()
        await session.refresh(faq)
        return faq


async def list_faqs(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    category: str | None = None,
    approval_status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """列出 FAQ"""
    async with session_factory() as session:
        query = select(KbFaq).where(KbFaq.is_deleted == False)  # noqa: E712
        if category:
            query = query.where(KbFaq.category == category)
        if approval_status:
            query = query.where(KbFaq.approval_status == approval_status)

        count_q = select(func.count()).select_from(query.subquery())
        total = (await session.execute(count_q)).scalar() or 0

        query = query.order_by(KbFaq.sort_order, KbFaq.created_at.desc()).offset(offset).limit(limit)
        result = await session.execute(query)
        faqs = result.scalars().all()

    return [
        {
            "id": str(f.id),
            "question": f.question,
            "category": f.category,
            "approval_status": f.approval_status,
            "version": f.version,
            "is_current_version": f.is_current_version,
            "card_types": f.card_types,
            "effective_date": f.effective_date.isoformat() if f.effective_date else None,
            "expiry_date": f.expiry_date.isoformat() if f.expiry_date else None,
            "created_at": f.created_at.isoformat() if f.created_at else None,
        }
        for f in faqs
    ], total


async def get_faq(
    session_factory: async_sessionmaker[AsyncSession],
    faq_id: str,
) -> dict | None:
    """获取 FAQ 详情"""
    async with session_factory() as session:
        result = await session.execute(select(KbFaq).where(KbFaq.id == _coerce_faq_id(faq_id)))
        f = result.scalar_one_or_none()
        if not f:
            return None

    return {
        "id": str(f.id),
        "question": f.question,
        "answer": f.answer,
        "variant_questions": f.variant_questions,
        "category": f.category,
        "card_types": f.card_types,
        "customer_tiers": f.customer_tiers,
        "keywords": f.keywords,
        "sort_order": f.sort_order,
        "doc_group": f.doc_group,
        "version": f.version,
        "approval_status": f.approval_status,
        "is_current_version": f.is_current_version,
        "effective_date": f.effective_date.isoformat() if f.effective_date else None,
        "expiry_date": f.expiry_date.isoformat() if f.expiry_date else None,
        "allowed_roles": f.allowed_roles,
        "regulatory_tags": f.regulatory_tags,
        "created_by": f.created_by,
        "updated_by": f.updated_by,
        "created_at": f.created_at.isoformat() if f.created_at else None,
        "updated_at": f.updated_at.isoformat() if f.updated_at else None,
    }


async def update_faq(
    session_factory: async_sessionmaker[AsyncSession],
    faq_id: str,
    *,
    question: str | None = None,
    answer: str | None = None,
    variant_questions: list[str] | None = None,
    category: str | None = None,
    card_types: list[str] | None = None,
    keywords: list[str] | None = None,
    sort_order: int | None = None,
    updated_by: str = "system",
) -> bool:
    """更新 FAQ"""
    async with session_factory() as session:
        result = await session.execute(select(KbFaq).where(KbFaq.id == _coerce_faq_id(faq_id)))
        faq = result.scalar_one_or_none()
        if not faq:
            return False

        if question is not None:
            faq.question = question
        if answer is not None:
            faq.answer = answer
        if variant_questions is not None:
            faq.variant_questions = variant_questions
        if category is not None:
            faq.category = category
        if card_types is not None:
            faq.card_types = card_types
        if keywords is not None:
            faq.keywords = keywords
        if sort_order is not None:
            faq.sort_order = sort_order
        faq.updated_by = updated_by

        await session.commit()
    return True


async def delete_faq(
    session_factory: async_sessionmaker[AsyncSession],
    faq_id: str,
    redis_client: aioredis.Redis | None = None,
    milvus_collection: Any = None,
) -> bool:
    """软删除 FAQ (同时清精确缓存 + 删语义向量, 防删除后仍被命中)"""
    async with session_factory() as session:
        result = await session.execute(select(KbFaq).where(KbFaq.id == _coerce_faq_id(faq_id)))
        faq = result.scalar_one_or_none()
        if not faq:
            return False
        faq.is_deleted = True
        faq.is_current_version = False
        faq.approval_status = "ARCHIVED"
        await session.commit()
    await _remove_faq_from_cache(faq, redis_client)
    await _remove_faq_vectors(milvus_collection, faq.doc_group or str(faq.id))
    return True


# ── 审批工作流 ──


_FAQ_TRANSITIONS = {
    "DRAFT": {"IN_REVIEW"},
    "IN_REVIEW": {"APPROVED", "REJECTED"},
    "APPROVED": {"PUBLISHED"},
    "REJECTED": {"DRAFT"},
    "PUBLISHED": {"SUPERSEDED", "ARCHIVED"},
    "SUPERSEDED": {"ARCHIVED"},
    # 归档不是死胡同: 允许回到草稿重新走审批链 (改版重发布), 否则归档内容
    # 只能删除重建, 已填写的问答/变体/关键词全部作废。
    "ARCHIVED": {"DRAFT"},
}


async def transition_faq_approval(
    session_factory: async_sessionmaker[AsyncSession],
    faq_id: str,
    target_status: str,
    *,
    actor_id: str,
    actor_role: str,
    comment: str = "",
    redis_client: aioredis.Redis | None = None,
    embedding_provider: Any = None,
    milvus_collection: Any = None,
) -> dict:
    """执行 FAQ 审批状态转换"""
    async with session_factory() as session:
        result = await session.execute(select(KbFaq).where(KbFaq.id == _coerce_faq_id(faq_id)))
        faq = result.scalar_one_or_none()
        if not faq:
            from lumio.shared.exceptions import LumioError

            raise LumioError(code=2001, message=f"FAQ 不存在: {faq_id}")

        current = faq.approval_status
        allowed = _FAQ_TRANSITIONS.get(current, set())
        if target_status not in allowed:
            from lumio.shared.exceptions import LumioError

            raise LumioError(code=3005, message=f"非法审批转换: {current} → {target_status}")

        old_status = current
        faq.approval_status = target_status
        faq.updated_by = actor_id

        # 发布时: 同 doc_group 旧版本标记为 SUPERSEDED
        if target_status == "PUBLISHED" and faq.doc_group:
            old_result = await session.execute(
                select(KbFaq).where(
                    KbFaq.doc_group == faq.doc_group,
                    KbFaq.id != faq.id,
                    KbFaq.approval_status == "PUBLISHED",
                    KbFaq.is_deleted == False,  # noqa: E712
                )
            )
            for old_faq in old_result.scalars().all():
                old_faq.approval_status = "SUPERSEDED"
                old_faq.is_current_version = False

            faq.is_current_version = True

            # 写入 Milvus 语义索引 (chunk_type=faq_qa)
            await _index_faq_to_search(faq, redis_client, embedding_provider, milvus_collection)

            # 预热精确匹配缓存
            await _warm_exact_match_cache(faq, redis_client)

        # 下线时: 清除缓存 + 删除语义向量
        if target_status in ("SUPERSEDED", "ARCHIVED"):
            await _remove_faq_from_cache(faq, redis_client)
            await _remove_faq_vectors(milvus_collection, faq.doc_group or str(faq.id))

        await session.commit()

    logger.info("FAQ 审批: %s %s→%s by=%s", faq_id, old_status, target_status, actor_id)
    return {"status": "ok", "faq_id": faq_id, "approval_status": target_status}


# ── 语义去重检测 ──


async def check_faq_duplicate(
    question: str,
    embedding_provider: Any,
    milvus_collection: Any,
    threshold: float = 0.92,
) -> list[dict]:
    """检测新建 FAQ 是否与已有 FAQ 语义重复

    利用 Milvus 向量检索，如果相似度超过 threshold 则认为是重复。
    """
    if not embedding_provider or not milvus_collection:
        return []

    try:
        query_embedding = await embedding_provider.embed_query(question)
        results = milvus_collection.search(
            data=[query_embedding],
            anns_field="embedding",
            param={"metric_type": "COSINE", "params": {"nprobe": 16}},
            limit=5,
            expr='chunk_type == "faq_qa" and approval_status == "PUBLISHED"',
            output_fields=["chunk_id", "content", "category"],
        )

        duplicates = []
        for hit in results[0]:
            if hit.score >= threshold:
                duplicates.append(
                    {
                        "faq_id": hit.entity.get("chunk_id", ""),
                        "question": hit.entity.get("content", "")[:200],
                        "similarity": hit.score,
                    }
                )
        return duplicates
    except Exception as e:
        logger.warning("FAQ 去重检测失败: %s", e)
        return []


# ── 检索 ──


async def search_faq(
    query: str,
    redis_client: aioredis.Redis | None,
    embedding_provider: Any = None,
    milvus_collection: Any = None,
    *,
    user_role: str | None = None,
    card_type: str | None = None,
    top_k: int = 5,
    # 语义命中双门槛 (第三轮模拟 P0: mxbai 本地对中文短句区分度不足 — 同义对
    # 0.556 / 无关对 0.648 信号倒挂, 单一 0.75 阈值让任何查询都命中 top1 FAQ,
    # 全部流量被 FAQ 直出劫持)。高分 + 与次名显著拉开间隔才判语义命中;
    # 同义问法主要由 question/variants 的归一化精确匹配承接。
    min_score: float = 0.85,
    min_margin: float = 0.04,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    session_id: str | None = None,
) -> dict:
    """FAQ 检索（串行短路）

    1. 精确匹配（Redis 缓存）→ 命中直接返回
    2. 语义匹配（Milvus）→ top_k FAQ
    3. 未命中 → 返回空，调用方降级到通用 RAG

    Returns:
        {"match_type": "exact"|"semantic"|"miss", "results": [...]}
    """
    # 1. 精确匹配
    if redis_client:
        cache_k = _cache_key(query)
        cached = await redis_client.get(cache_k)
        if cached:
            import json

            faq_data = json.loads(cached)
            # 权限过滤
            if user_role and faq_data.get("allowed_roles") and user_role not in faq_data["allowed_roles"]:
                pass  # 无权限，降级到语义
            else:
                # 卡种过滤
                if card_type and faq_data.get("card_types") and card_type not in faq_data["card_types"]:
                    pass  # 不适用，降级到语义
                else:
                    await _log_search(session_factory, query, "exact", faq_data.get("id"), 1.0, user_role, session_id)
                    return {"match_type": "exact", "results": [faq_data]}

    # 2. 语义匹配
    if embedding_provider and milvus_collection:
        try:
            query_embedding = await embedding_provider.embed_query(query)
            results = milvus_collection.search(
                data=[query_embedding],
                anns_field="embedding",
                param={"metric_type": "COSINE", "params": {"nprobe": 16}},
                limit=top_k * 2,
                expr='chunk_type == "faq_qa" and approval_status == "PUBLISHED" and is_current_version == "true"',
                output_fields=["chunk_id", "content", "category", "card_type", "keywords"],
            )

            faq_results = []
            for hit in results[0]:
                entity = hit.entity
                # 卡种过滤
                card_types = entity.get("card_type", "")
                if card_type and card_types and card_type not in card_types:
                    continue

                if hit.score < min_score:
                    continue  # 低分 nearest 不是"命中", 闲聊/离题必须放行走常规流程
                # 间隔判别: top1 与 top2 分数接近 = 嵌入无法区分语义, 放行走常规流程
                if len(results[0]) > 1:
                    second = results[0][1].score
                    if hit.score - second < min_margin:
                        logger.info(
                            "FAQ 语义间隔不足放行: q=%r top1=%.3f top2=%.3f",
                            query[:24],
                            hit.score,
                            second,
                        )
                        return {"match_type": "miss", "results": []}
                faq_results.append(
                    {
                        "faq_id": entity.get("chunk_id", ""),
                        "question": entity.get("content", "")[:200],
                        "category": entity.get("category", ""),
                        "score": hit.score,
                    }
                )

                if len(faq_results) >= top_k:
                    break

            if faq_results:
                # chunk_id 可能带变体序号后缀 (faqid#0), 落库前剥离取真实 FAQ UUID
                faq_uuid = faq_results[0]["faq_id"].split("#", 1)[0]
                await _log_search(
                    session_factory,
                    query,
                    "semantic",
                    faq_uuid,
                    faq_results[0]["score"],
                    user_role,
                    session_id,
                )
                return {"match_type": "semantic", "results": faq_results}
        except Exception as e:
            logger.warning("FAQ 语义检索失败: %s", e)

    # 3. 未命中
    await _log_search(session_factory, query, "miss", None, None, user_role, session_id)
    return {"match_type": "miss", "results": []}


# ── 索引 ──


async def _index_faq_to_search(
    faq: KbFaq,
    redis_client: aioredis.Redis | None,
    embedding_provider: Any = None,
    milvus_collection: Any = None,
) -> int:
    """将已发布 FAQ 写入 Milvus (chunk_type=faq_qa)，供 search_faq 语义匹配。

    FAQ 不走分块管道：question + 每条 variant 各成一条向量，answer 经 PG 回查。
    此前是只打日志的桩函数 —— 发布后语义检索无数据、FAQ 命中率指标恒空
    (2026-08-29 修复)。失败不阻断发布主流程，返回实际写入条数。
    """
    all_questions = [faq.question, *(faq.variant_questions or [])]
    if not embedding_provider or not milvus_collection:
        logger.warning(
            "FAQ 索引缺依赖 (embedding=%s milvus=%s), 仅预热精确缓存: id=%s",
            embedding_provider is not None,
            milvus_collection is not None,
            faq.id,
        )
        return 0

    doc_group = faq.doc_group or str(faq.id)

    def _sync_write(rows: list[dict]) -> None:
        # 同组旧向量先清 (改版重发布防重复命中)
        milvus_collection.delete(expr=f'doc_id == "{doc_group}"')
        milvus_collection.insert(rows)

    try:
        rows = []
        for i, q in enumerate(all_questions):
            emb = await embedding_provider.embed_query(q)
            rows.append(
                {
                    "chunk_id": f"{faq.id}#{i}",
                    "doc_id": doc_group,
                    "content": q,
                    "embedding": emb,
                    "category": faq.category,
                    "doc_type": "faq",
                    "keywords": list(faq.keywords or []),
                    "card_type": ",".join(faq.card_types or []),
                    "customer_tier": "",
                    "security_level": "internal",
                    "effective_date": 0,
                    "expiry_date": 0,
                    "chunk_type": "faq_qa",
                    "parent_chunk_id": "",
                    "approval_status": "PUBLISHED",
                    "is_current_version": "true",
                }
            )
        await asyncio.to_thread(_sync_write, rows)
        logger.info("FAQ 索引写入 Milvus: id=%s 条数=%d", faq.id, len(rows))
        return len(rows)
    except Exception:
        logger.exception("FAQ 索引写入失败 (不阻断发布): id=%s", faq.id)
        return 0


async def _remove_faq_vectors(milvus_collection: Any, doc_group: str) -> int:
    """下线 (归档/被替代) 时清理该 FAQ 组的语义向量"""
    if not milvus_collection:
        return 0
    try:
        await asyncio.to_thread(milvus_collection.delete, expr=f'doc_id == "{doc_group}"')
        return 1
    except Exception:
        logger.warning("FAQ 向量清理失败: doc_group=%s", doc_group)
        return 0


async def _warm_exact_match_cache(faq: KbFaq, redis_client: aioredis.Redis | None) -> None:
    """预热精确匹配缓存: 对 question + 所有 variant_questions 建立缓存"""
    import json

    if not redis_client:
        return

    faq_data = {
        "id": str(faq.id),
        "question": faq.question,
        "answer": faq.answer,
        "category": faq.category,
        "card_types": faq.card_types,
        "allowed_roles": faq.allowed_roles,
    }
    faq_json = json.dumps(faq_data, ensure_ascii=False)

    # 主问题 + 所有变体都建立缓存
    all_queries = [faq.question, *list(faq.variant_questions)]
    for q in all_queries:
        cache_k = _cache_key(q)
        await redis_client.setex(cache_k, _FAQ_CACHE_TTL, faq_json)


async def _remove_faq_from_cache(faq: KbFaq, redis_client: aioredis.Redis | None) -> None:
    """FAQ 下线时清除缓存"""
    if not redis_client:
        return

    all_queries = [faq.question, *list(faq.variant_questions)]
    for q in all_queries:
        cache_k = _cache_key(q)
        await redis_client.delete(cache_k)


async def _log_search(
    session_factory: async_sessionmaker[AsyncSession] | None,
    query: str,
    match_type: str,
    faq_id: str | None,
    score: float | None,
    user_role: str | None,
    session_id: str | None,
) -> None:
    """记录检索日志（用于分析）"""
    from lumio.shared.metrics import FAQ_MATCH

    FAQ_MATCH.labels(match_type=match_type).inc()
    if not session_factory:
        return
    try:
        import uuid_utils

        async with session_factory() as session:
            log = KbFaqSearchLog(
                query=query[:512],
                match_type=match_type,
                faq_id=uuid_utils.UUID(faq_id) if faq_id else None,
                score=score,
                user_role=user_role,
                session_id=session_id,
            )
            session.add(log)
            await session.commit()
    except Exception:
        logger.debug("FAQ 检索日志写入失败")


# ── 自动过期 ──


async def expire_overdue_faqs(
    session_factory: async_sessionmaker[AsyncSession],
    redis_client: aioredis.Redis | None = None,
) -> int:
    """将过期的 PUBLISHED FAQ 自动下线

    定时任务调用（如每天凌晨）。
    Returns: 过期下线的 FAQ 数量
    """
    today = date.today()
    count = 0

    async with session_factory() as session:
        result = await session.execute(
            select(KbFaq).where(
                KbFaq.approval_status == "PUBLISHED",
                KbFaq.expiry_date < today,
                KbFaq.is_deleted == False,  # noqa: E712
            )
        )
        for faq in result.scalars().all():
            faq.approval_status = "SUPERSEDED"
            faq.is_current_version = False
            await _remove_faq_from_cache(faq, redis_client)
            count += 1

        if count:
            await session.commit()
            logger.info("自动过期 %d 条 FAQ", count)

    return count
