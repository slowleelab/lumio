"""管理控制台 API（一期：对话审计 + RAG 指标监控）

独立于 bot/router.py 的控制台专用路由，统一挂在 /api/admin 前缀下。
数据源全部为既有 PG 持久层（dialogue_log / chat_message / decision_log /
audit_log / kb_faq_search_log），不新建表。

审计合规注意：
- 会话回放默认不返回 retrieval_context（可能含大段知识原文），需显式 include_context。
- 所有端点强制角色鉴权：审计类 admin/agent，其余敏感操作 admin-only。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, case, func, or_, select

from lumio.services.common.deps import DbSession
from lumio.shared.auth import AuthUser, require_role
from lumio.shared.exceptions import LumioError
from lumio.shared.orm_models import (
    AuditLog,
    ChatMessage,
    ChatMessageStatus,
    DecisionLog,
    DialogueLog,
    KbFaqSearchLog,
)

router = APIRouter(prefix="/admin", tags=["console"])

AdminAgentUser = Annotated[AuthUser, Depends(require_role("admin", "agent"))]
AdminOnlyUser = Annotated[AuthUser, Depends(require_role("admin"))]

_MAX_PAGE_LIMIT = 200


def _parse_dt(value: str | None, field: str) -> datetime | None:
    """解析 ISO8601 时间过滤参数（支持带/不带时区，不带按 UTC）"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise LumioError(f"{field} 不是合法的 ISO8601 时间: {value}", code=2001) from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _round(value: float | None, digits: int = 4) -> float | None:
    return round(value, digits) if value is not None else None


# ── 1. 会话列表（对话审计入口） ──


@router.get("/conversations")
async def list_conversations(
    user: AdminAgentUser,
    db: DbSession,
    session_id: str | None = None,
    customer_id: str | None = None,
    intent: str | None = None,
    response_source: str | None = None,
    start: str | None = Query(None, description="ISO8601 起始时间"),
    end: str | None = Query(None, description="ISO8601 截止时间"),
    limit: int = Query(20, ge=1, le=_MAX_PAGE_LIMIT),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """会话维度审计列表（dialogue_log 聚合 + chat_message 处理统计）

    过滤语义：intent / response_source 命中会话内任意一轮即返回该会话
    （HAVING bool_or），其余条件作用于轮次行（WHERE）。
    """
    start_dt = _parse_dt(start, "start")
    end_dt = _parse_dt(end, "end")

    turns_agg = select(
        DialogueLog.session_id.label("session_id"),
        func.count().label("turns"),
        func.min(DialogueLog.timestamp).label("started_at"),
        func.max(DialogueLog.timestamp).label("last_at"),
        func.avg(case((DialogueLog.speaker == "bot", DialogueLog.confidence))).label("avg_bot_confidence"),
        func.max(DialogueLog.customer_id).label("customer_id"),
        func.max(DialogueLog.channel_type).label("channel_type"),
    )
    conds = []
    if session_id:
        conds.append(DialogueLog.session_id == session_id)
    if customer_id:
        conds.append(DialogueLog.customer_id == customer_id)
    if start_dt:
        conds.append(DialogueLog.timestamp >= start_dt)
    if end_dt:
        conds.append(DialogueLog.timestamp <= end_dt)
    if conds:
        turns_agg = turns_agg.where(*conds)
    having = []
    if intent:
        having.append(func.bool_or(DialogueLog.intent == intent))
    if response_source:
        having.append(func.bool_or(DialogueLog.response_source == response_source))
    turns_agg = turns_agg.group_by(DialogueLog.session_id)
    if having:
        turns_agg = turns_agg.having(or_(*having))
    sessions_sq = turns_agg.subquery()

    # 主意图：会话内出现次数最多的 intent（窗口函数排名）
    intent_counts = (
        select(
            DialogueLog.session_id.label("session_id"),
            DialogueLog.intent.label("intent"),
            func.count().label("cnt"),
        )
        .where(DialogueLog.intent.isnot(None))
        .group_by(DialogueLog.session_id, DialogueLog.intent)
        .subquery()
    )
    intent_ranked = select(
        intent_counts.c.session_id.label("session_id"),
        intent_counts.c.intent.label("intent"),
        func.row_number()
        .over(partition_by=intent_counts.c.session_id, order_by=intent_counts.c.cnt.desc())
        .label("rn"),
    ).subquery()

    msg_agg = (
        select(
            ChatMessage.session_id.label("session_id"),
            func.count().label("messages"),
            func.sum(case((ChatMessage.processing_status == ChatMessageStatus.ERROR, 1), else_=0)).label("errors"),
            func.avg(ChatMessage.processing_duration_ms).label("avg_duration_ms"),
        )
        .group_by(ChatMessage.session_id)
        .subquery()
    )

    total = (await db.execute(select(func.count()).select_from(sessions_sq))).scalar_one()

    rows = (
        await db.execute(
            select(
                sessions_sq.c.session_id,
                sessions_sq.c.turns,
                sessions_sq.c.started_at,
                sessions_sq.c.last_at,
                sessions_sq.c.avg_bot_confidence,
                sessions_sq.c.customer_id,
                sessions_sq.c.channel_type,
                intent_ranked.c.intent.label("top_intent"),
                msg_agg.c.messages.label("messages"),
                msg_agg.c.errors.label("errors"),
                msg_agg.c.avg_duration_ms.label("avg_duration_ms"),
            )
            .join(
                intent_ranked,
                (intent_ranked.c.session_id == sessions_sq.c.session_id) & (intent_ranked.c.rn == 1),
                isouter=True,
            )
            .join(msg_agg, msg_agg.c.session_id == sessions_sq.c.session_id, isouter=True)
            .order_by(sessions_sq.c.last_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()

    return {
        "total": total,
        "conversations": [
            {
                "session_id": r.session_id,
                "customer_id": r.customer_id,
                "channel_type": r.channel_type,
                "turns": r.turns,
                "messages": r.messages or 0,
                "top_intent": r.top_intent,
                "avg_bot_confidence": _round(r.avg_bot_confidence),
                "errors": int(r.errors or 0),
                "avg_duration_ms": _round(r.avg_duration_ms, 1),
                "started_at": _iso(r.started_at),
                "last_at": _iso(r.last_at),
            }
            for r in rows
        ],
    }


# ── 2. 会话回放 ──


@router.get("/conversations/{session_id}/replay")
async def replay_conversation(
    session_id: str,
    user: AdminAgentUser,
    db: DbSession,
    include_context: bool = Query(False, description="是否包含 retrieval_context（可能很大）"),
    turn_limit: int = Query(500, ge=1, le=2000),
) -> dict[str, Any]:
    """单会话完整回放：对话轮次 + 决策链 + 消息处理记录（三段结构）

    只选业务列, 不物化 ORM 实体: Uuid(native_uuid=False) 下 asyncpg 反序列化
    UUID 主键列会抛 'pgproto.UUID' object has no attribute 'replace' (会话 48882b05 同源坑)。
    """
    turn_rows = (
        await db.execute(
            select(
                DialogueLog.turn_id,
                DialogueLog.speaker,
                DialogueLog.content,
                DialogueLog.intent,
                DialogueLog.confidence,
                DialogueLog.entities,
                DialogueLog.response_source,
                DialogueLog.emotion_label,
                DialogueLog.emotion_score,
                DialogueLog.retrieval_context,
                DialogueLog.timestamp,
            )
            .where(DialogueLog.session_id == session_id)
            .order_by(DialogueLog.timestamp)
            .limit(turn_limit)
        )
    ).all()

    decision_rows = (
        await db.execute(
            select(
                DecisionLog.decision_id,
                DecisionLog.turn_id,
                DecisionLog.agent_name,
                DecisionLog.action,
                DecisionLog.reasoning,
                DecisionLog.evidence_json,
                DecisionLog.latency_ms,
                DecisionLog.created_at,
            )
            .where(DecisionLog.session_id == session_id)
            .order_by(DecisionLog.created_at)
            .limit(500)
        )
    ).all()

    message_rows = (
        await db.execute(
            select(
                ChatMessage.message_id,
                ChatMessage.content,
                ChatMessage.intent,
                ChatMessage.processing_status,
                ChatMessage.processing_duration_ms,
                ChatMessage.source,
                ChatMessage.error_message,
                ChatMessage.created_at,
            )
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.created_at)
            .limit(turn_limit)
        )
    ).all()

    return {
        "session_id": session_id,
        "turns": [
            {
                "turn_id": t.turn_id,
                "speaker": t.speaker,
                "content": t.content,
                "intent": t.intent,
                "confidence": _round(t.confidence),
                "entities": t.entities,
                "response_source": t.response_source,
                "emotion_label": t.emotion_label,
                "emotion_score": _round(t.emotion_score),
                "retrieval_context": t.retrieval_context if include_context else None,
                "timestamp": _iso(t.timestamp),
            }
            for t in turn_rows
        ],
        "decisions": [
            {
                "decision_id": d.decision_id,
                "turn_id": d.turn_id,
                "agent_name": d.agent_name,
                "action": d.action,
                "reasoning": d.reasoning,
                "evidence": d.evidence_json,
                "latency_ms": _round(d.latency_ms, 1),
                "created_at": _iso(d.created_at),
            }
            for d in decision_rows
        ],
        "messages": [
            {
                "message_id": m.message_id,
                "content": m.content,
                "intent": m.intent,
                "processing_status": m.processing_status.value if m.processing_status else None,
                "processing_duration_ms": m.processing_duration_ms,
                "source": m.source,
                "error_message": m.error_message,
                "created_at": _iso(m.created_at),
            }
            for m in message_rows
        ],
    }


# ── 3. 操作审计 ──


@router.get("/operation-logs")
async def list_operation_logs(
    user: AdminOnlyUser,
    db: DbSession,
    actor_id: str | None = None,
    action: str | None = None,
    target_type: str | None = None,
    path_contains: str | None = None,
    status_code: int | None = None,
    start: str | None = None,
    end: str | None = None,
    limit: int = Query(50, ge=1, le=_MAX_PAGE_LIMIT),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """操作审计（audit_log，append-only）：谁在什么时候对什么做了什么"""
    start_dt = _parse_dt(start, "start")
    end_dt = _parse_dt(end, "end")

    conds = []
    if actor_id:
        conds.append(AuditLog.actor_id == actor_id)
    if action:
        conds.append(AuditLog.action == action)
    if target_type:
        conds.append(AuditLog.target_type == target_type)
    if path_contains:
        conds.append(AuditLog.path.ilike(f"%{path_contains}%"))
    if status_code is not None:
        conds.append(AuditLog.status_code == status_code)
    if start_dt:
        conds.append(AuditLog.timestamp >= start_dt)
    if end_dt:
        conds.append(AuditLog.timestamp <= end_dt)

    base = select(AuditLog)
    count_base = select(func.count()).select_from(AuditLog)
    if conds:
        base = base.where(*conds)
        count_base = count_base.where(*conds)

    total = (await db.execute(count_base)).scalar_one()
    rows = (await db.execute(base.order_by(AuditLog.timestamp.desc()).limit(limit).offset(offset))).scalars().all()

    return {
        "total": total,
        "logs": [
            {
                "id": str(r.id),
                "timestamp": _iso(r.timestamp),
                "actor_id": r.actor_id,
                "actor_role": r.actor_role,
                "action": r.action,
                "target_type": r.target_type,
                "target_id": r.target_id,
                "method": r.method,
                "path": r.path,
                "status_code": r.status_code,
                "ip_address": r.ip_address,
                "detail": r.detail,
            }
            for r in rows
        ],
    }


# ── 4. RAG 质量聚合 ──


@router.get("/rag/quality-summary")
async def rag_quality_summary(
    user: AdminAgentUser,
    db: DbSession,
    days: int = Query(7, ge=1, le=90, description="统计窗口（天）"),
    top_n: int = Query(10, ge=1, le=50, description="意图 TOP-N"),
) -> dict[str, Any]:
    """RAG 服务质量聚合（按天）：响应来源 / FAQ 命中 / 意图分布 / 置信度健康 / 决策延迟"""
    since = datetime.now(UTC) - timedelta(days=days)

    dlg_day = func.date_trunc("day", DialogueLog.timestamp)
    daily_rows = (
        await db.execute(
            select(dlg_day, func.count(), func.count(func.distinct(DialogueLog.session_id)))
            .where(DialogueLog.timestamp >= since)
            .group_by(dlg_day)
            .order_by(dlg_day)
        )
    ).all()

    source_daily_rows = (
        await db.execute(
            select(dlg_day, DialogueLog.response_source, func.count())
            .where(and_(DialogueLog.timestamp >= since, DialogueLog.response_source.isnot(None)))
            .group_by(dlg_day, DialogueLog.response_source)
            .order_by(dlg_day)
        )
    ).all()

    source_total_rows = (
        await db.execute(
            select(DialogueLog.response_source, func.count())
            .where(and_(DialogueLog.timestamp >= since, DialogueLog.response_source.isnot(None)))
            .group_by(DialogueLog.response_source)
            .order_by(func.count().desc())
        )
    ).all()

    faq_day = func.date_trunc("day", KbFaqSearchLog.created_at)
    faq_daily_rows = (
        await db.execute(
            select(faq_day, KbFaqSearchLog.match_type, func.count())
            .where(KbFaqSearchLog.created_at >= since)
            .group_by(faq_day, KbFaqSearchLog.match_type)
            .order_by(faq_day)
        )
    ).all()

    faq_total_rows = (
        await db.execute(
            select(KbFaqSearchLog.match_type, func.count())
            .where(KbFaqSearchLog.created_at >= since)
            .group_by(KbFaqSearchLog.match_type)
        )
    ).all()

    intent_top_rows = (
        await db.execute(
            select(
                DialogueLog.intent,
                func.count().label("cnt"),
                func.avg(DialogueLog.confidence).label("avg_conf"),
            )
            .where(and_(DialogueLog.timestamp >= since, DialogueLog.intent.isnot(None)))
            .group_by(DialogueLog.intent)
            .order_by(func.count().desc())
            .limit(top_n)
        )
    ).all()

    from lumio.shared.config import get_settings

    conf_threshold = get_settings().rag.confidence_threshold
    confidence_row = (
        await db.execute(
            select(
                func.sum(case((DialogueLog.speaker == "bot", 1), else_=0)).label("bot_turns"),
                func.avg(case((DialogueLog.speaker == "bot", DialogueLog.confidence))).label("avg_conf"),
                func.sum(
                    case(
                        (and_(DialogueLog.speaker == "bot", DialogueLog.confidence < conf_threshold), 1),
                        else_=0,
                    )
                ).label("low_conf_bot"),
            ).where(DialogueLog.timestamp >= since)
        )
    ).one()

    latency_rows = (
        await db.execute(
            select(
                DecisionLog.agent_name,
                DecisionLog.action,
                func.count().label("cnt"),
                func.avg(DecisionLog.latency_ms).label("avg_ms"),
                func.percentile_cont(0.95).within_group(DecisionLog.latency_ms).label("p95_ms"),
            )
            .where(DecisionLog.created_at >= since)
            .group_by(DecisionLog.agent_name, DecisionLog.action)
            .order_by(func.count().desc())
        )
    ).all()

    faq_total = {r[0]: r[1] for r in faq_total_rows}
    faq_hit = faq_total.get("exact", 0) + faq_total.get("semantic", 0)
    faq_all = sum(faq_total.values())
    bot_turns = int(confidence_row.bot_turns or 0)
    low_conf_bot = int(confidence_row.low_conf_bot or 0)

    return {
        "days": days,
        "daily_volume": [{"date": r[0].date().isoformat(), "turns": r[1], "sessions": r[2]} for r in daily_rows],
        "response_source": {
            "daily": [{"date": r[0].date().isoformat(), "source": r[1], "count": r[2]} for r in source_daily_rows],
            "total": [{"source": r[0], "count": r[1]} for r in source_total_rows],
        },
        "faq": {
            "daily": [{"date": r[0].date().isoformat(), "match_type": r[1], "count": r[2]} for r in faq_daily_rows],
            "total": [{"match_type": k, "count": v} for k, v in sorted(faq_total.items())],
            "hit_rate": _round(faq_hit / faq_all) if faq_all else None,
        },
        "intent_top": [
            {"intent": r.intent, "count": r.cnt, "avg_confidence": _round(r.avg_conf)} for r in intent_top_rows
        ],
        "confidence": {
            "threshold": conf_threshold,
            "bot_turns": bot_turns,
            "avg_bot_confidence": _round(confidence_row.avg_conf),
            "low_confidence_share": _round(low_conf_bot / bot_turns) if bot_turns else None,
        },
        "decision_latency": [
            {
                "agent": r.agent_name,
                "action": r.action,
                "count": r.cnt,
                "avg_ms": _round(r.avg_ms, 1),
                "p95_ms": _round(r.p95_ms, 1),
            }
            for r in latency_rows
        ],
    }


# ── 5. RAG 实时指标（读进程内 Prometheus REGISTRY，免抓取延迟） ──

# 注意：prometheus_client 的 Counter family 名不含 _total 后缀，匹配时统一归一
_WATCHED_METRICS = {
    "lumio_retrieval_duration_seconds",
    "lumio_degradation_level",
    "lumio_agent_responses_total",
    "lumio_agent_timeouts_total",
    "lumio_bot_answer_duration_seconds",
    "lumio_fast_reply_total",
    "lumio_eval_regression_pass_rate",
    "lumio_bad_case_marked_total",
    "lumio_rag_cache_ops_total",
    "lumio_rerank_degradation_total",
    "lumio_faq_match_total",
    "lumio_circuit_breaker_state",
    "lumio_injection_attempts_total",
    "lumio_injection_blocked_total",
}

_WATCHED_BASES = {name.removesuffix("_total") for name in _WATCHED_METRICS}


def _collect_metric_samples() -> dict[str, list[dict[str, Any]]]:
    """从进程内 REGISTRY 抓取关注指标的原始样本（保留 sample 名以区分 _count/_sum）"""
    from prometheus_client import REGISTRY

    raw: dict[str, list[dict[str, Any]]] = {}
    for family in REGISTRY.collect():
        if family.name.removesuffix("_total") not in _WATCHED_BASES:
            continue
        for sample in family.samples:
            raw.setdefault(family.name, []).append(
                {"name": sample.name, "labels": dict(sample.labels), "value": sample.value}
            )
    return raw


@router.get("/rag/live-metrics")
async def rag_live_metrics(user: AdminAgentUser) -> dict[str, Any]:
    """实时指标快照（进程内 Prometheus 指标直读，供控制台指标卡轮询）"""
    raw = _collect_metric_samples()

    def samples_of(name: str) -> list[dict[str, Any]]:
        # 传入名可能带 _total（声出名），family 存储键不带 → 双向兼容查找
        return raw.get(name) or raw.get(name.removesuffix("_total"), [])

    def counter_by(name: str, label: str | None = None) -> dict[str, float] | float:
        # Counter 会额外暴露 *_created 时间戳样本, 必须排除, 只取 *_total 本体
        total_name = name.removesuffix("_total") + "_total"
        values = [s for s in samples_of(name) if s["name"] == total_name]
        if label:
            return {str(s["labels"].get(label, "?")): float(s["value"]) for s in values}
        return float(sum(s["value"] for s in values))

    def hist_stats(base: str, label_key: str) -> dict[str, dict[str, float | None]]:
        """把 Histogram 样本折叠为 per-label 的 count/sum/avg（label_key 为空串表示无标签）"""
        count_by: dict[str, float] = {}
        sum_by: dict[str, float] = {}
        for s in samples_of(base):
            key = s["labels"].get(label_key, "")
            if s["name"] == base + "_count":
                count_by[key] = s["value"]
            elif s["name"] == base + "_sum":
                sum_by[key] = s["value"]
        out: dict[str, dict[str, float | None]] = {}
        for key, count in count_by.items():
            total = sum_by.get(key, 0.0)
            out[key] = {"count": count, "sum": total, "avg": (total / count) if count else None}
        return out

    degradation = 0.0
    for s in raw.get("lumio_degradation_level", []):
        if not s["labels"]:
            degradation = s["value"]

    answer_latency_by = hist_stats("lumio_bot_answer_duration_seconds", "")

    return {
        "retrieval": hist_stats("lumio_retrieval_duration_seconds", "search_type"),
        "answer_latency": answer_latency_by.get("", {"count": 0.0, "sum": 0.0, "avg": None}),
        "degradation_level": degradation,
        "agent_responses": counter_by("lumio_agent_responses_total", "source"),
        "agent_timeouts": counter_by("lumio_agent_timeouts_total", "source"),
        "fast_reply_total": counter_by("lumio_fast_reply_total"),
        "eval_regression_pass_rate": counter_by("lumio_eval_regression_pass_rate", "golden_set_version"),
        "bad_cases_total": counter_by("lumio_bad_case_marked_total"),
        "rag_cache_ops": counter_by("lumio_rag_cache_ops_total", "result"),
        "rerank_degradation": counter_by("lumio_rerank_degradation_total", "reason"),
        "faq_match": counter_by("lumio_faq_match_total", "match_type"),
        "circuit_breakers": {
            s["labels"].get("name", "?"): s["value"] for s in raw.get("lumio_circuit_breaker_state", [])
        },
        "injection": {
            "attempts_total": counter_by("lumio_injection_attempts_total"),
            "blocked_total": counter_by("lumio_injection_blocked_total"),
        },
    }
