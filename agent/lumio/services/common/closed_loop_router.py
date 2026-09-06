"""闭环管理 API: Badcase 采集/归因/裁决 + 金标扩充"""

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


def _build_judge(request: Request) -> BadcaseJudge:
    """裁判构造: 配置了 judge_base_url 则走远程跨家族裁判 (GLM coding plan),
    远程失败由 RemoteJudgeClient 自动回退本地; 否则用本地 qwen。"""
    from lumio.services.common.judge_client import RemoteJudgeClient

    settings = get_settings()
    llm_client = getattr(request.app.state, "llm_client", None)
    if llm_client is None:
        raise LumioError(code=5001, message="LLM 未就绪")
    if settings.llm.judge_base_url and settings.llm.judge_api_key:
        judge_llm = RemoteJudgeClient(fallback_llm=llm_client)
        return BadcaseJudge(judge_llm, model=settings.llm.judge_model, min_confidence=0.7, samples=3)
    # 未配置远程端点: 本地兜底走 primary_model (本地没有 GLM 权重, 不能用 judge_model 名)
    return BadcaseJudge(llm_client, model=settings.llm.primary_model, min_confidence=0.7, samples=3)

router = APIRouter(prefix="/admin/closed-loop", tags=["closed-loop"])

AdminOnlyUser = Annotated[AuthUser, Depends(require_role("admin"))]
AdminAgentUser = Annotated[AuthUser, Depends(require_role("admin", "agent"))]

_VALID_SIGNALS = (
    "negative_feedback",
    "transfer",
    "agent_revoke",
    "behavior_anomaly",
    "compliance_alert",
    "qa_scan",  # 全量质检巡检 (从原始对话内容审查, 非信号触发)
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
    needs_review: bool | None = None,
    keyword: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """Badcase 列表 (信号/根因层/修复状态/复核态过滤 + 输入关键字搜索)"""
    items, total = await list_badcases(
        db,
        signal_source=signal_source,
        root_cause_layer=root_cause_layer,
        fix_status=fix_status,
        fix_table=fix_table,
        needs_review=needs_review,
        keyword=keyword,
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

    bc = await get_badcase(request.app.state.db_session_factory, badcase_id)
    if bc is None:
        raise LumioError(code=2001, message=f"Badcase 不存在: {badcase_id}")

    judge = _build_judge(request)
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


# ── 批量归因 (后台任务, 每条 ~21s 本地 n=3; 大库存一条条点不现实) ──

_batch_state: dict[str, Any] = {"running": False, "total": 0, "done": 0, "failed": 0, "started_at": 0.0, "error": "", "scope": None}
_batch_tasks: set = set()


async def _batch_attribute_task(request: Request, limit: int, flt: dict[str, Any]) -> None:
    sf = getattr(request.app.state, "db_session_factory", None)
    llm_client = getattr(request.app.state, "llm_client", None)
    try:
        if sf is None or llm_client is None:
            raise RuntimeError("LLM 或数据库未就绪")
        judge = _build_judge(request)
        from sqlalchemy import or_

        from lumio.shared.orm_models import Badcase

        async with sf() as db:
            conds: list[Any] = [Badcase.root_cause_layer.is_(None)]
            if flt.get("signal_source"):
                conds.append(Badcase.signal_source == flt["signal_source"])
            if flt.get("keyword"):
                conds.append(Badcase.user_input.ilike(f"%{flt['keyword']}%"))
            if flt.get("layer") == "uncertain":
                conds.append(or_(Badcase.root_cause_layer.is_(None), Badcase.root_cause_layer == "uncertain"))
            rows = (
                (
                    await db.execute(
                        select(Badcase.id)
                        .where(*conds)
                        .order_by(Badcase.created_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
        ids = [str(r) for r in rows]
        _batch_state.update(total=len(ids), done=0, failed=0)
        logger.info("批量归因启动: %d 条待归因", len(ids))
        for bid in ids:
            result = await attribute_and_save(sf, judge, badcase_id=bid)
            if result is None:
                _batch_state["failed"] += 1
            else:
                _batch_state["done"] += 1
        logger.info(
            "批量归因完成: 成功 %d / 失败 %d / 共 %d",
            _batch_state["done"],
            _batch_state["failed"],
            len(ids),
        )
    except Exception as exc:
        _batch_state["error"] = str(exc)
        logger.warning("批量归因异常: %s", exc)
    finally:
        _batch_state["running"] = False


@router.post("/badcases/attribute-batch")
async def attribute_batch(user: AdminOnlyUser, request: Request, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """批量归因待归因坏例 (后台任务; 可带 signal_source/keyword 过滤, 按当前筛选范围跑)"""
    import asyncio
    import time as _time

    if _batch_state["running"]:
        raise LumioError(code=3001, message="批量归因进行中, 请等待完成")
    body = body or {}
    limit = int(body.get("limit") or 50)
    limit = max(1, min(limit, 500))
    flt = {"signal_source": body.get("signal_source"), "keyword": body.get("keyword"), "layer": body.get("layer")}
    _batch_state.update(running=True, total=0, done=0, failed=0, started_at=_time.time(), error="", scope=flt)
    task = asyncio.create_task(_batch_attribute_task(request, limit, flt))
    _batch_tasks.add(task)
    task.add_done_callback(_batch_tasks.discard)
    return {"scheduled": True, "limit": limit, "scope": flt}


@router.get("/badcases/attribute-batch/status")
async def attribute_batch_status(user: AdminAgentUser) -> dict[str, Any]:
    """批量归因进度 (前端轮询)"""
    return dict(_batch_state)


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
    """模块 B: 金标评测集扩充 (引擎二·规则模板层 + 四层过滤)

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
    human_confirmed = (
        await db.execute(
            select(func.count())
            .select_from(Badcase)
            .where(Badcase.needs_human_review.is_(False) & Badcase.human_confirmed_layer.is_not(None))
        )
    ).scalar() or 0
    llm_auto = confirmed - human_confirmed
    pass_rate = (llm_auto / confirmed) if confirmed else None
    # 分布聚合 (质检工作台概览条)
    layer_rows = (
        await db.execute(
            select(Badcase.root_cause_layer, func.count())
            .where(Badcase.root_cause_layer.is_not(None))
            .group_by(Badcase.root_cause_layer)
        )
    ).all()
    signal_rows = (await db.execute(select(Badcase.signal_source, func.count()).group_by(Badcase.signal_source))).all()
    return {
        "total": total,
        "today_new": today_new,
        "pending_review": pending_review,
        "confirmed": confirmed,
        "deployed": deployed,
        "llm_pass_rate": round(pass_rate, 3) if pass_rate is not None else None,
        "layer_dist": {str(k or "uncertain"): v for k, v in layer_rows},
        "signal_dist": {str(k): v for k, v in signal_rows},
    }


@router.get("/badcases/{badcase_id}")
async def get_badcase_endpoint(badcase_id: str, user: AdminAgentUser, request: Request) -> dict[str, Any]:
    """单条 Badcase 详情 (质检记录 fail 项 → 整改闭环跳转用; 注意注册在
    /badcases/stats 之后, 防止 stats 被当成 path 参数吞掉)"""
    from lumio.services.common.badcase_store import _to_dict

    bc = await get_badcase(request.app.state.db_session_factory, badcase_id)
    if bc is None:
        raise LumioError(code=2001, message=f"Badcase 不存在: {badcase_id}")
    return _to_dict(bc)


@router.get("/health-metrics")
async def closed_loop_health(user: AdminAgentUser, db: DbSession) -> dict[str, Any]:
    """闭环健康度七指标 (方案 §8.1): 转人工率/修复时长/复现率等聚合

    口径说明:
    - 转人工率: transfer 信号 Badcase 数 / 总会话数 (近期 7 天)
    - 平均修复时长: deployed 样本 created→resolved 均值
    - 复现率需 L2 回归集积累, 暂返回 None
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


# ── 全量质检巡检 (qa_scan): 所有会话从原始对话内容过质检, 不依赖置信度/信号 ──


@router.post("/quality/scan")
async def start_quality_scan(
    user: AdminOnlyUser, request: Request, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    """启动全量会话质检巡检 (后台任务; GLM 裁判读对话原文逐项审查)

    - limit: 单轮巡检会话上限 (默认 200, 上限 1000)
    - sample_rate: 抽样率 0~1 (生产全量成本高时可 0.1 抽检)
    - lookback_hours: 回看窗口 (默认 720h = 30 天)
    - reinspect: 强制重检 (忽略 30 天已检去重)
    """
    from lumio.services.common import quality_scan

    body = body or {}
    limit = max(1, min(int(body.get("limit") or 200), 5000))
    sample_rate = min(max(float(body.get("sample_rate") or 1.0), 0.01), 1.0)
    lookback_hours = max(1, min(int(body.get("lookback_hours") or 720), 24 * 90))
    reinspect = bool(body.get("reinspect") or False)

    sf = getattr(request.app.state, "db_session_factory", None)
    redis_client = getattr(request.app.state, "redis_client", None)
    if sf is None:
        raise LumioError(code=5001, message="DB 未就绪")

    judge_llm = _build_judge_llm_only(request)
    started = quality_scan.start_scan(
        sf,
        judge_llm,
        redis_client,
        quality_scan.judge_model_name(get_settings()),
        limit=limit,
        sample_rate=sample_rate,
        lookback_hours=lookback_hours,
        reinspect=reinspect,
    )
    if not started:
        raise LumioError(code=3001, message="质检巡检进行中, 请等待完成")
    return {"scheduled": True, "limit": limit, "sample_rate": sample_rate, "lookback_hours": lookback_hours}


@router.get("/quality/scan/status")
async def quality_scan_status(user: AdminAgentUser, request: Request) -> dict[str, Any]:
    """巡检进度 + 上轮结果 (前端轮询; pass_rate = 合格率)"""
    from lumio.services.common import quality_scan

    redis_client = getattr(request.app.state, "redis_client", None)
    status = quality_scan.scan_status()
    status["last_run"] = await quality_scan.last_run(redis_client)
    return status


@router.get("/quality/records")
async def quality_records_endpoint(
    user: AdminAgentUser,
    db: DbSession,
    verdict: str | None = None,
    keyword: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """质检记录列表: 每一个被巡检会话一条判定 (pass/warn/fail 全量),
    按会话时间倒序 (对话发生顺序) — 工作台"质检记录"页签数据源"""
    from lumio.services.common.badcase_store import list_quality_records

    if verdict and verdict not in ("pass", "warn", "fail"):
        raise LumioError(code=2001, message=f"verdict 非法: {verdict}")
    items, total = await list_quality_records(db, verdict=verdict, keyword=keyword, limit=limit, offset=offset)
    return {"total": total, "records": items}


@router.get("/quality/sessions")
async def quality_sessions_endpoint(
    user: AdminAgentUser,
    db: DbSession,
    category: str = Query("all"),
    keyword: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    """统一会话质检列表: 最新判定 ⟕ 最新问题案例 (全外联), 会话维度一行。

    category: all | pass | warn | fail | pending_review(待人工判定) | unscanned
    """
    from lumio.services.common.badcase_store import list_qc_sessions

    if category not in ("all", "pass", "warn", "fail", "pending_review", "unscanned"):
        raise LumioError(code=2001, message=f"category 非法: {category}")
    items, total = await list_qc_sessions(db, category=category, keyword=keyword, limit=limit, offset=offset)
    return {"total": total, "sessions": items}


@router.post("/quality/rescan")
async def quality_rescan_endpoint(user: AdminAgentUser, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """单会话强制复检: 绕过 30 天 Redis 去重重跑裁判 (整改效果验证)。

    新判定追加落库, 会话维度列表自动展示最新一条。
    """
    import lumio.services.common.quality_scan as quality_scan
    from lumio.shared.config import get_settings

    session_id = str((body or {}).get("session_id") or "").strip()
    if not session_id:
        raise LumioError(code=2001, message="session_id 必填")
    judge_llm = _build_judge_llm_only(request)
    sf = getattr(request.app.state, "db_session_factory", None)
    redis = getattr(request.app.state, "redis_client", None)
    if sf is None or redis is None:
        raise LumioError(code=5001, message="DB/Redis 未就绪")
    result = await quality_scan.scan_session_by_id(
        sf, judge_llm, redis, session_id, quality_scan.judge_model_name(get_settings()), force=True
    )
    if result is None:
        return {"status": "skipped", "message": "对话不足 2 轮或无判定产出"}
    return {
        "status": "ok",
        "verdict": result.get("verdict"),
        "problems": result.get("problems"),
        "summary": result.get("summary"),
    }


@router.post("/quality/replay")
async def quality_replay_endpoint(user: AdminAgentUser, request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """重放执行: 取原会话全部客户消息, 按原顺序重新发给机器人 (新会话)。

    修复验证口径 — 同样的输入过当前链路, 逐轮对比新旧回复 (前端轮询新会话
    回放直至客服轮数齐, 再结束会话触发自动质检)。走真实消息管线 (审计/闸门
    /队列全量生效), per-session worker 串行消费保证轮序。
    """
    import asyncio as _asyncio
    import contextlib as _cl
    import time as _time
    import uuid as _uuid

    from lumio.services.bot.router import CHAT_STREAM_KEY
    from lumio.services.common.audit import write_chat_message
    from lumio.shared.orm_models import DialogueLog

    session_id = str((body or {}).get("session_id") or "").strip()
    if not session_id:
        raise LumioError(code=2001, message="session_id 必填")
    sf = getattr(request.app.state, "db_session_factory", None)
    redis = getattr(request.app.state, "redis_client", None)
    if sf is None or redis is None:
        raise LumioError(code=5001, message="DB/Redis 未就绪")

    async with sf() as db:
        msgs = (
            (
                await db.execute(
                    select(DialogueLog.content)
                    .where(DialogueLog.session_id == session_id, DialogueLog.speaker == "customer")
                    .order_by(DialogueLog.timestamp)
                )
            )
            .scalars()
            .all()
        )
    msgs = [m for m in msgs if (m or "").strip()][:30]
    if not msgs:
        raise LumioError(code=2001, message="原会话无客户消息, 无法重放")

    new_sid = f"replay-{session_id[:20]}-{int(_time.time() * 1000) % 100000}"
    for msg in msgs:
        message_id = _uuid.uuid4().hex
        with _cl.suppress(Exception):  # 审计失败不阻断重放
            await write_chat_message(sf, session_id=new_sid, message_id=message_id, content=msg, customer_id="replay-bot")
        await redis.xadd(
            CHAT_STREAM_KEY,
            {
                "session_id": new_sid,
                "message_id": message_id,
                "message": msg,
                "verification_result": "",
                "_trace_context": "",
                "_enqueue_time": _asyncio.get_event_loop().time(),
                "customer_id": "replay-bot",
                "customer_name": "重放执行",
                "channel": "web",
            },
        )
    return {"status": "ok", "new_session_id": new_sid, "total_rounds": len(msgs)}


@router.get("/quality/coverage")
async def quality_coverage_endpoint(
    user: AdminAgentUser,
    db: DbSession,
    lookback_hours: int = Query(720, ge=1, le=24 * 90),
) -> dict[str, Any]:
    """质检覆盖统计: 窗口内应检会话 vs 已检会话 + 判定分布/合格率"""
    from lumio.services.common.badcase_store import quality_coverage_stats

    return await quality_coverage_stats(db, lookback_hours=lookback_hours)


def _build_judge_llm_only(request: Request) -> Any:
    """质检巡检裁判: 远程 GLM 优先 (与归因裁判同源), 未配置走本地"""
    from lumio.services.common import quality_scan

    settings = get_settings()
    llm_client = getattr(request.app.state, "llm_client", None)
    if llm_client is None:
        raise LumioError(code=5001, message="LLM 未就绪")
    return quality_scan.build_judge_llm(llm_client, settings)
