"""Badcase 闭环持久化 (PG) + LLM 归因落库"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

import uuid_utils
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumio.services.common.badcase_loop import (
    AttributionResult,
    BadcaseJudge,
    dedup_key,
)
from lumio.shared.orm_models import Badcase, DialogueLog, QualityRecord

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
    session_time: datetime | None = None,
) -> Badcase:
    """信号采集: 五路信号 → badcase 落库。

    session_time: 会话时间锚点 (该会话最后一轮对话时间) — 工作台按对话
    发生顺序排列, 不受巡检/采集时刻影响。

    去重聚合: 同 (dedup_group_id + signal_source) 且未处置 (fix_status=pending)
    的既有条目只累加出现次数 (signal_detail.occurrences + 最近一次现场快照),
    不再插新行 —— 否则同一句"我的卡丢了"一小时就能刷出 41 条重复记录,
    列表被刷屏、归因重复劳动。处置过 (fixing/deployed) 的组重新计数开新行,
    保留修复前后对照。
    """
    async with session_factory() as session:
        group = dedup_key(user_input)
        existing = (
            await session.execute(
                select(Badcase)
                .where(
                    Badcase.dedup_group_id == group,
                    Badcase.signal_source == signal_source,
                    Badcase.fix_status == "pending",
                )
                .order_by(Badcase.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            detail = dict(existing.signal_detail or {})
            detail["occurrences"] = int(detail.get("occurrences", 1)) + 1
            detail["last_seen_session"] = session_id
            existing.signal_detail = detail
            if snapshot:
                merged = dict(existing.snapshot or {})
                merged.update(snapshot)
                existing.snapshot = merged
            if bot_output:
                existing.bot_output = bot_output
            # 去重组跨会话复现: 会话时间锚点取最近一次 (问题最近何时仍发生)
            if session_time is not None and (existing.session_time is None or session_time > existing.session_time):
                existing.session_time = session_time
            await session.commit()
            await session.refresh(existing)
            logger.info(
                "Badcase 聚合去重: group=%s source=%s occurrences=%d", group, signal_source, detail["occurrences"]
            )
            return existing
        bc = Badcase(
            trace_id=trace_id or session_id,
            session_id=session_id,
            customer_id=customer_id,
            channel=channel,
            signal_source=signal_source,
            signal_detail={**(signal_detail or {}), "occurrences": 1},
            user_input=user_input,
            bot_output=bot_output,
            snapshot=snapshot,
            dedup_group_id=group,
            needs_human_review=True,
            fix_status=fix_status,
            session_time=session_time,
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
    needs_review: bool | None = None,
    keyword: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """查询 Badcase 列表 (统计卡联动过滤 + 关键字搜索)"""
    conds = []
    if signal_source:
        conds.append(Badcase.signal_source == signal_source)
    if root_cause_layer:
        conds.append(Badcase.root_cause_layer == root_cause_layer)
    if fix_status:
        conds.append(Badcase.fix_status == fix_status)
    if fix_table:
        conds.append(Badcase.fix_table == fix_table)
    if needs_review is not None:
        conds.append(Badcase.needs_human_review.is_(needs_review))
    if keyword:
        # 关键词同时匹配 用户输入 与 会话 ID — 支持从审计/质检记录复制会话 ID
        # 直接查该会话的全部问题案例 (与质检记录页搜索口径一致)
        conds.append(or_(Badcase.user_input.ilike(f"%{keyword}%"), Badcase.session_id.ilike(f"%{keyword}%")))

    query = select(Badcase)
    count_q = select(func.count()).select_from(Badcase)
    if conds:
        query = query.where(*conds)
        count_q = count_q.where(*conds)

    total = (await session.execute(count_q)).scalar() or 0
    # 按会话时间倒序 (对话发生顺序): created_at 只是采集/巡检时刻, qa_scan
    # 批量回扫会打乱现场时序; 无 session_time 的旧数据退回 created_at。
    # 次序键 created_at/id 保证同锚点并列行 (同会话多条) 分页稳定
    rows = (
        (
            await session.execute(
                query.order_by(
                    func.coalesce(Badcase.session_time, Badcase.created_at).desc(),
                    Badcase.created_at.desc(),
                    Badcase.id.desc(),
                )
                .limit(limit)
                .offset(offset)
            )
        )
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
        "session_time": b.session_time.isoformat() if b.session_time else None,
        "occurrences": int((b.signal_detail or {}).get("occurrences", 1)),
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


# ── 质检记录 (qa_scan 全量判定持久化: pass/warn/fail 每会话一条) ──


async def record_quality(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    session_id: str,
    verdict: str,
    problems: list[dict[str, Any]] | None = None,
    summary: str | None = None,
    preview: str | None = None,
    judge_model: str | None = None,
    turns: int | None = None,
    session_time: datetime | None = None,
    badcase_id: str | None = None,
    scanned_at: datetime | None = None,
) -> QualityRecord:
    """落一条质检判定记录 (每个被巡检会话一条, append-only)"""
    async with session_factory() as session:
        rec = QualityRecord(
            session_id=session_id,
            verdict=verdict,
            problems=problems or None,
            summary=summary,
            preview=(preview or "")[:160] or None,
            judge_model=judge_model,
            turns=turns,
            session_time=session_time,
            badcase_id=badcase_id,
            scanned_at=scanned_at or datetime.now(UTC),
        )
        session.add(rec)
        await session.commit()
        await session.refresh(rec)
        return rec


def quality_record_to_dict(r: QualityRecord) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "session_id": r.session_id,
        "verdict": r.verdict,
        "problems": r.problems or [],
        "summary": r.summary,
        "preview": r.preview,
        "judge_model": r.judge_model,
        "turns": r.turns,
        "session_time": r.session_time.isoformat() if r.session_time else None,
        "scanned_at": r.scanned_at.isoformat() if r.scanned_at else None,
        "badcase_id": r.badcase_id,
    }


async def list_quality_records(
    session: AsyncSession,
    *,
    verdict: str | None = None,
    keyword: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict[str, Any]], int]:
    """质检记录列表 · 会话维度: 每会话取最新一次判定 (复检多行只展示最新),
    按会话时间倒序 (对话发生顺序; 无锚点退回质检时刻)。

    筛选 (判定/关键词) 作用于记录后再按会话去重 — 判"不合格"筛选时展示的是
    该会话最新一条不合格记录 (曾不合格复检转合格的会话仍可被检索到)。
    """
    conds = []
    if verdict:
        conds.append(QualityRecord.verdict == verdict)
    if keyword:
        conds.append(QualityRecord.preview.ilike(f"%{keyword}%") | QualityRecord.session_id.ilike(f"%{keyword}%"))

    from sqlalchemy.orm import aliased

    # 会话去重取最新: row_number 窗口 (方言中立, 等价 PG DISTINCT ON)
    sub = (
        select(
            QualityRecord,
            func.row_number()
            .over(partition_by=QualityRecord.session_id, order_by=QualityRecord.scanned_at.desc())
            .label("rn"),
        )
        .where(*conds)
    ).subquery()
    qr = aliased(QualityRecord, sub)

    total = (await session.execute(select(func.count()).select_from(qr).where(sub.c.rn == 1))).scalar() or 0
    rows = (
        (
            await session.execute(
                select(qr)
                .where(sub.c.rn == 1)
                .order_by(func.coalesce(qr.session_time, qr.scanned_at).desc(), qr.scanned_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return [quality_record_to_dict(r) for r in rows], total


async def quality_coverage_stats(
    session: AsyncSession,
    *,
    lookback_hours: int = 720,
    min_turns: int = 2,
) -> dict[str, Any]:
    """质检覆盖统计: 窗口内应检会话 (dialogue_log ≥min_turns 轮) vs 已检会话"""
    from datetime import timedelta

    since = datetime.now(UTC) - timedelta(hours=lookback_hours)
    eligible = (
        select(DialogueLog.session_id)
        .where(DialogueLog.timestamp >= since)
        .group_by(DialogueLog.session_id)
        .having(func.count() >= min_turns)
        .subquery()
    )
    total_sessions = (await session.execute(select(func.count()).select_from(eligible))).scalar() or 0
    scanned = (
        await session.execute(
            select(func.count(func.distinct(QualityRecord.session_id))).where(QualityRecord.scanned_at >= since)
        )
    ).scalar() or 0
    verdict_rows = (
        await session.execute(
            select(QualityRecord.verdict, func.count()).where(QualityRecord.scanned_at >= since).group_by(QualityRecord.verdict)
        )
    ).all()
    by_verdict = {str(k): v for k, v in verdict_rows}
    judged = by_verdict.get("pass", 0) + by_verdict.get("warn", 0) + by_verdict.get("fail", 0)
    return {
        "lookback_hours": lookback_hours,
        "total_sessions": total_sessions,
        "scanned_sessions": scanned,
        "coverage": round(scanned / total_sessions, 4) if total_sessions else None,
        "by_verdict": by_verdict,
        "pass_rate": round(by_verdict.get("pass", 0) / judged, 4) if judged else None,
    }


def coerce_uuid(value: str) -> uuid_utils.UUID | None:
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
        llm_used = getattr(judge, "_llm", None)
        row.attribution_model = getattr(llm_used, "effective_model", None) or judge._model
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
