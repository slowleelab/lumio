"""Badcase 闭环持久化 (PG) + LLM 归因落库"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumio.services.common.badcase_loop import (
    AttributionResult,
    BadcaseJudge,
    dedup_key,
)
from lumio.shared.orm_models import Badcase

logger = logging.getLogger(__name__)


async def capture_badcase(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    trace_id: str,
    session_id: str,
    signal_source: str,
    user_input: str,
    customer_id: str | None = None,
    channel: str | None = None,
    bot_output: str | None = None,
    signal_detail: dict[str, Any] | None = None,
    snapshot: dict[str, Any] | None = None,
    fix_status: str = "pending",
) -> Badcase:
    """信号采集: 五路信号 → badcase 落库 (粗去重 key 同步写入)"""
    async with session_factory() as session:
        bc = Badcase(
            trace_id=trace_id or session_id,
            session_id=session_id,
            customer_id=customer_id,
            channel=channel,
            signal_source=signal_source,
            signal_detail=signal_detail,
            user_input=user_input,
            bot_output=bot_output,
            snapshot=snapshot,
            dedup_group_id=dedup_key(user_input),
            needs_human_review=True,
            fix_status=fix_status,
        )
        session.add(bc)
        await session.commit()
        await session.refresh(bc)
        logger.info("Badcase 采集: source=%s session=%s", signal_source, session_id)
        return bc


async def list_badcases(
    session: AsyncSession,
    *,
    signal_source: str | None = None,
    root_cause_layer: str | None = None,
    fix_status: str | None = None,
    fix_table: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """查询 Badcase 列表"""
    conds = []
    if signal_source:
        conds.append(Badcase.signal_source == signal_source)
    if root_cause_layer:
        conds.append(Badcase.root_cause_layer == root_cause_layer)
    if fix_status:
        conds.append(Badcase.fix_status == fix_status)
    if fix_table:
        conds.append(Badcase.fix_table == fix_table)

    query = select(Badcase)
    count_q = select(func.count()).select_from(Badcase)
    if conds:
        query = query.where(*conds)
        count_q = count_q.where(*conds)

    total = (await session.execute(count_q)).scalar() or 0
    rows = (
        (await session.execute(query.order_by(Badcase.created_at.desc()).limit(limit).offset(offset)))
        .scalars()
        .all()
    )
    return [_to_dict(b) for b in rows], total



def _to_dict(b: Badcase) -> dict[str, Any]:
    return {
        "id": str(b.id),
        "trace_id": b.trace_id,
        "session_id": b.session_id,
        "customer_id": b.customer_id,
        "channel": b.channel,
        "signal_source": b.signal_source,
        "signal_detail": b.signal_detail,
        "user_input": b.user_input,
        "bot_output": (b.bot_output or "")[:200],
        "snapshot": b.snapshot,
        "root_cause_layer": b.root_cause_layer,
        "root_cause_category": b.root_cause_category,
        "attribution_evidence": b.attribution_evidence,
        "attribution_confidence": b.attribution_confidence,
        "attribution_model": b.attribution_model,
        "needs_human_review": b.needs_human_review,
        "human_confirmed_layer": b.human_confirmed_layer,
        "fix_table": b.fix_table,
        "fix_status": b.fix_status,
        "fix_note": b.fix_note,
        "resolved_at": b.resolved_at.isoformat() if b.resolved_at else None,
        "created_at": b.created_at.isoformat() if b.created_at else None,
    }


async def get_badcase(session_factory: async_sessionmaker[AsyncSession], badcase_id: str) -> Badcase | None:
    import uuid_utils

    try:
        pk = uuid_utils.UUID(str(badcase_id))
    except ValueError:
        return None
    async with session_factory() as session:
        result = await session.execute(select(Badcase).where(Badcase.id == pk))
        return result.scalar_one_or_none()


def coerce_uuid(value: str):
    import uuid_utils

    try:
        return uuid_utils.UUID(str(value))
    except ValueError:
        return None


async def attribute_and_save(
    session_factory: async_sessionmaker[AsyncSession],
    judge: BadcaseJudge,
    *,
    badcase_id: str,
) -> AttributionResult | None:
    """对已采集的 Badcase 跑 LLM 归因并回写 (模块 A)"""
    import uuid_utils

    bc = await get_badcase(session_factory, badcase_id)
    if bc is None:
        return None
    snapshot = bc.snapshot or {}
    context = {
        "trace_id": bc.trace_id,
        "user_input": bc.user_input,
        "user_feedback": str((bc.signal_detail or {}).get("feedback", "无")),
        "intent": snapshot.get("intent", ""),
        "confidence": snapshot.get("confidence", ""),
        "traffic_class": snapshot.get("traffic_class", ""),
        "rag_hit": snapshot.get("rag_hit", ""),
        "context_len": snapshot.get("context_len", 0),
        "bot_output": bc.bot_output,
        "response_source": snapshot.get("response_source", ""),
    }
    result = await judge.attribute(context)
    if result is None:
        return None

    async with session_factory() as session:
        row = await session.get(Badcase, uuid_utils.UUID(badcase_id))
        if row is None:
            return None
        row.root_cause_layer = result.root_cause_layer
        row.root_cause_category = result.root_cause_category
        row.attribution_evidence = result.evidence
        row.attribution_confidence = result.confidence
        row.attribution_model = judge._model
        row.needs_human_review = result.needs_human_review
        row.fix_table = result.fix_table
        await session.commit()
    logger.info(
        "Badcase 归因完成: id=%s layer=%s conf=%.2f review=%s",
        badcase_id,
        result.root_cause_layer,
        result.confidence,
        result.needs_human_review,
    )
    return result


async def update_fix_status(
    session_factory: async_sessionmaker[AsyncSession],
    badcase_id: str,
    *,
    fix_status: str,
    fix_table: str | None = None,
    note: str | None = None,
    human_confirmed_layer: str | None = None,
) -> bool:
    """人工裁决/状态流转 (方案 §7.4 错误案例库结构化字段)"""
    import uuid_utils

    async with session_factory() as session:
        row = await session.get(Badcase, uuid_utils.UUID(badcase_id))
        if row is None:
            return False
        row.fix_status = fix_status
        if fix_table:
            row.fix_table = fix_table
        if human_confirmed_layer:
            row.human_confirmed_layer = human_confirmed_layer
        if note:
            row.fix_note = note
        if fix_status in ("deployed", "rejected"):
            row.resolved_at = datetime.now(UTC)
        await session.commit()
        return True
