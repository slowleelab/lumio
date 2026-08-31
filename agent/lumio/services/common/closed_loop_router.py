"""闭环管理 API: Badcase 采集/归因/裁决 + 金标扩充 (方案 v2.0)"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select

from lumio.services.common.badcase_loop import (
    BadcaseJudge,
    filter_variants,
    rule_augment,
)
from lumio.services.common.badcase_store import (
    attribute_and_save,
    get_badcase,
    list_badcases,
    update_fix_status,
)
from lumio.services.common.deps import DbSession
from lumio.shared.auth import AuthUser, require_role
from lumio.shared.config import get_settings
from lumio.shared.exceptions import LumioError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/closed-loop", tags=["closed-loop"])

AdminOnlyUser = Annotated[AuthUser, Depends(require_role("admin"))]
AdminAgentUser = Annotated[AuthUser, Depends(require_role("admin", "agent"))]

_VALID_SIGNALS = (
    "negative_feedback",
    "transfer",
    "agent_revoke",
    "behavior_anomaly",
    "compliance_alert",
)
_VALID_FIX_STATUS = ("pending", "fixing", "canary", "deployed", "rejected")


@router.post("/badcases/collect")
async def collect_badcase(
    user: AdminAgentUser,
    request: Request,
    body: dict[str, Any],
    db: DbSession,
) -> dict[str, Any]:
    """五路信号采集入口: 转人工/撤回/行为异常/合规告警由各链路调用;
    负面反馈由 feedback 流程调用。幂等性由调用方保证 (同 trace 重复采集由
    fix_status=pending 去重查询兜底)。"""
    from lumio.services.common.badcase_store import capture_badcase as _capture

    trace_id = str(body.get("trace_id") or body.get("session_id") or "")
    session_id = str(body.get("session_id") or "")
    signal = str(body.get("signal_source") or "")
    user_input = str(body.get("user_input") or "")
    if not session_id or not user_input:
        raise LumioError(code=2001, message="session_id / user_input 必填")
    if signal not in _VALID_SIGNALS:
        raise LumioError(code=2001, message=f"signal_source 非法: {signal}")

    sf = getattr(request.app.state, "db_session_factory", None)
    if not sf:
        raise LumioError(code=5001, message="数据库未就绪")

    bc = await _capture(
        sf,
        trace_id=trace_id,
        session_id=session_id,
        signal_source=signal,
        user_input=user_input,
        customer_id=str(body.get("customer_id") or "") or None,
        channel=str(body.get("channel") or "") or None,
        bot_output=body.get("bot_output"),
        signal_detail=body.get("signal_detail"),
        snapshot=body.get("snapshot"),
    )
    return {"id": str(bc.id), "signal_source": bc.signal_source, "fix_status": bc.fix_status}


@router.get("/badcases")
async def list_badcases_endpoint(
    user: AdminAgentUser,
    db: DbSession,
    signal_source: str | None = None,
    root_cause_layer: str | None = None,
    fix_status: str | None = None,
    fix_table: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Badcase 列表 (支持按信号/根因层/修复状态过滤)"""
    items, total = await list_badcases(
        db,
        signal_source=signal_source,
        root_cause_layer=root_cause_layer,
        fix_status=fix_status,
        fix_table=fix_table,
        limit=limit,
        offset=offset,
    )
    return {"total": total, "badcases": items}


@router.post("/badcases/{badcase_id}/attribute")
async def attribute_badcase(
    badcase_id: str,
    user: AdminAgentUser,
    request: Request,
) -> dict[str, Any]:
    """模块 A: LLM-as-Judge 自动归因 (n=3 自一致性, 闸门不足转人工)"""

    settings = get_settings()
    bc = await get_badcase(request.app.state.db_session_factory, badcase_id)
    if bc is None:
        raise LumioError(code=2001, message=f"Badcase 不存在: {badcase_id}")

    # judge 模型: 与生成模型同端点 (本地单 LLM), 跨家族原则在生产多模型时生效
    llm_client = getattr(request.app.state, "llm_client", None)
    if llm_client is None:
        raise LumioError(code=5001, message="LLM 未就绪")
    judge = BadcaseJudge(
        llm_client,
        model=settings.llm.judge_model,
        min_confidence=0.7,
        samples=3,
    )
    sf = getattr(request.app.state, "db_session_factory", None)
    if not sf:
        raise LumioError(code=5001, message="数据库未就绪")
    result = await attribute_and_save(sf, judge, badcase_id=badcase_id)
    if result is None:
        raise LumioError(code=5000, message="归因失败 (LLM 不可用或 Badcase 不存在)")
    return {
        "root_cause_layer": result.root_cause_layer,
        "root_cause_category": result.root_cause_category,
        "evidence": result.evidence,
        "confidence": result.confidence,
        "fix_table": result.fix_table,
        "needs_human_review": result.needs_human_review,
        "majority_ratio": result.majority_ratio,
    }


@router.post("/badcases/{badcase_id}/resolve")
async def resolve_badcase(
    badcase_id: str,
    user: AdminOnlyUser,
    request: Request,
    db: DbSession,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """人工裁决/修复状态流转 (方案 §7.4): fix_status + 人工确认根因覆盖"""

    body = body or {}
    status = str(body.get("fix_status") or "fixing")
    if status not in _VALID_FIX_STATUS:
        raise LumioError(code=2001, message=f"fix_status 非法: {status}")
    sf = getattr(request.app.state, "db_session_factory", None)
    if not sf:
        raise LumioError(code=5001, message="数据库未就绪")
    ok = await update_fix_status(
        sf,
        badcase_id,
        fix_status=status,
        fix_table=body.get("fix_table"),
        note=body.get("note"),
        human_confirmed_layer=body.get("human_confirmed_layer"),
    )
    if not ok:
        raise LumioError(code=2001, message=f"Badcase 不存在: {badcase_id}")
    return {"status": "ok", "fix_status": status}


@router.post("/golden/expand")
async def expand_golden_set(
    user: AdminOnlyUser,
    request: Request,
    body: dict[str, Any],
) -> dict[str, Any]:
    """模块 B: 金标评测集扩充 (引擎二·规则模板层 v1 + 四层过滤)

    输入种子问法列表 → 同义/口语化变体生成 → 去重/合规过滤 → 返回变体
    (人工抽检后入库金标集 L1/L3; LLM 改写层后续接入)
    """
    seeds = body.get("seeds") or []
    if not seeds or not isinstance(seeds, list):
        raise LumioError(code=2001, message="seeds 必须为非空列表")
    existing: set[str] = {str(x) for x in body.get("existing") or []}
    passed_total, rejected_total = [], []
    for seed in seeds:
        text = str(seed).strip()
        if not text:
            continue
        passed, rejected = filter_variants(rule_augment(text), existing=existing)
        passed_total.extend(passed)
        rejected_total.extend(rejected)
        existing.update(passed)
    return {"seed_count": len(seeds), "variants": passed_total, "rejected_count": len(rejected_total)}


@router.get("/badcases/stats")
async def badcase_stats(user: AdminAgentUser, db: DbSession) -> dict[str, Any]:
    """Badcase 工作台统计: 今日/待复核/已确认/LLM 直通率 (方案 §3.1.6 今日统计)"""
    from datetime import UTC as _UTC
    from datetime import datetime, timedelta

    now = datetime.now(_UTC)
    day_ago = now - timedelta(days=1)
    from sqlalchemy import func, select

    from lumio.shared.orm_models import Badcase

    total = (await db.execute(select(func.count()).select_from(Badcase))).scalar() or 0
    pending_review = (
        await db.execute(select(func.count()).select_from(Badcase).where(Badcase.needs_human_review.is_(True)))
    ).scalar() or 0
    confirmed = (
        await db.execute(select(func.count()).select_from(Badcase).where(Badcase.needs_human_review.is_(False)))
    ).scalar() or 0
    today_new = (
        await db.execute(select(func.count()).select_from(Badcase).where(Badcase.created_at >= day_ago))
    ).scalar() or 0
    deployed = (
        await db.execute(select(func.count()).select_from(Badcase).where(Badcase.fix_status == "deployed"))
    ).scalar() or 0
    llm_auto = (
        confirmed
        - (
            await db.execute(
                select(func.count())
                .select_from(Badcase)
                .where(Badcase.needs_human_review.is_(False) & Badcase.human_confirmed_layer.is_not(None))
            )
        ).scalar()
        or 0
    )
    pass_rate = (llm_auto / confirmed) if confirmed else None
    return {
        "total": total,
        "today_new": today_new,
        "pending_review": pending_review,
        "confirmed": confirmed,
        "deployed": deployed,
        "llm_pass_rate": round(pass_rate, 3) if pass_rate is not None else None,
    }


@router.get("/health-metrics")
async def closed_loop_health(user: AdminAgentUser, db: DbSession) -> dict[str, Any]:
    """闭环健康度七指标 (方案 §8.1): 转人工率/修复时长/复现率等聚合

    v1 口径说明:
    - 转人工率: transfer 信号 Badcase 数 / 总会话数 (近期 7 天)
    - 平均修复时长: deployed 样本 created→resolved 均值
    - 复现率 v1 需 L2 回归集积累, 暂返回 None
    """
    from datetime import UTC, datetime, timedelta

    from lumio.shared.orm_models import Badcase, DialogueLog

    now = datetime.now(UTC)
    since = now - timedelta(days=7)

    transfer_count = (
        await db.execute(
            select(func.count())
            .select_from(Badcase)
            .where(Badcase.signal_source == "transfer", Badcase.created_at >= since)
        )
    ).scalar() or 0
    sessions = (
        await db.execute(
            select(func.count(func.distinct(DialogueLog.session_id))).where(DialogueLog.timestamp >= since)
        )
    ).scalar() or 0
    transfer_rate = round(transfer_count / sessions, 4) if sessions else None

    deployed_rows = (
        await db.execute(
            select(Badcase.created_at, Badcase.resolved_at).where(
                Badcase.fix_status == "deployed", Badcase.resolved_at.is_not(None)
            )
        )
    ).all()
    fix_days = [
        (r.resolved_at - r.created_at).total_seconds() / 86400 for r in deployed_rows if r.resolved_at and r.created_at
    ]
    avg_fix_days = round(sum(fix_days) / len(fix_days), 1) if fix_days else None

    return {
        "window_days": 7,
        "transfer_count_7d": transfer_count,
        "sessions_7d": sessions,
        "transfer_rate_7d": transfer_rate,
        "avg_fix_days": avg_fix_days,
        "badcases_deployed": len(fix_days),
        "recurrence_rate": None,  # 需 L2 回归集积累后计算
        "golden_pass_rate": None,  # 由评测流水线回填
    }
