"""问题治理 API (共性聚合层 · 质检不合格案例的归类/修复方案/执行)

三旅程定位: 智能质检发现问题 → 案例工作台单案例流转 → 本页把共性问题聚成
治理专项 (业务 × 根因), 一次修复覆盖整组 — 从 N 次重复劳动到一次治理.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from lumio.services.common import pattern_store
from lumio.services.common.badcase_store import update_fix_status
from lumio.shared.auth import AuthUser, require_role
from lumio.shared.exceptions import LumioError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/patterns", tags=["patterns"])

AdminOnlyUser = Annotated[AuthUser, Depends(require_role("admin"))]
AdminAgentUser = Annotated[AuthUser, Depends(require_role("admin", "agent"))]


def _sf(request: Any) -> Any:
    sf = getattr(request.app.state, "db_session_factory", None)
    if not sf:
        raise LumioError(code=5001, message="数据库未就绪")
    return sf


@router.get("")
async def list_patterns_endpoint(user: AdminAgentUser, request: Request) -> dict[str, Any]:
    """问题组清单: open 案例按 业务×根因 实时聚合 + 方案状态 + 待归因计数"""
    return await pattern_store.list_patterns(_sf(request))


# ── P0 飞轮: 组级重放验证 (verified 从"单例闭环"升级为"一类问题根治") ──
# 修复上线后对组内案例批量重放 (当前代码重新回答), 按新质检判定自动流转:
# 通过(pass/warn)→verified, 未通过(fail)→reopened。进度轮询同 attribute-batch 模式。
_recheck_state: dict[str, Any] = {
    "running": False,
    "group_key": "",
    "total": 0,
    "done": 0,
    "passed": 0,
    "failed": 0,
    "error": "",
    "started_at": 0.0,
}


@router.post("/{group_key}/recheck")
async def group_recheck_endpoint(group_key: str, user: AdminOnlyUser, request: Request) -> dict[str, Any]:
    """组级重放验证: 组内已上线案例逐个重放, 按判定自动 verified/reopened。

    串行执行 (每会话重放走完整消息管线+自动质检, 避免并发压垮 LLM);
    通过率 ≥ 80% 的判定语义: 案例维度独立流转 (通过的 verified,
    未通过的 reopened 重进治理), 组是否根治由漏斗与哨兵观测。
    """
    import asyncio
    import time

    if _recheck_state["running"]:
        raise LumioError(code=3001, message=f"组级重放进行中 (group={_recheck_state['group_key']}), 完成后再发起")
    sf = _sf(request)
    detail = await pattern_store.get_pattern(sf, group_key)
    if detail is None:
        raise LumioError(code=2004, message="问题组不存在或已无未结案例", status_code=404)
    targets = [c for c in detail["cases"] if c["fix_status"] in ("deployed",)]
    if not targets:
        raise LumioError(code=2001, message="组内无已上线 (deployed) 案例, 无可验证对象")

    _recheck_state.update(
        running=True,
        group_key=group_key,
        total=len(targets),
        done=0,
        passed=0,
        failed=0,
        error="",
        started_at=time.time(),
    )
    task = asyncio.create_task(_run_group_recheck(request.app, sf, group_key, targets))
    task.add_done_callback(lambda t: t.exception() and logger.warning("组级重放任务异常: %s", t.exception()))
    return {"started": True, "total": len(targets)}


@router.get("/recheck/status")
async def group_recheck_status(user: AdminAgentUser) -> dict[str, Any]:
    """组级重放进度 (前端轮询)"""
    return dict(_recheck_state)


async def _run_group_recheck(app: Any, sf: Any, group_key: str, targets: list[dict[str, Any]]) -> None:
    """串行: 逐案例 重放→等完成→查新会话判定→自动流转。失败不阻断后续。"""

    from lumio.services.common import closed_loop_router as _clr

    for c in targets:
        try:
            verdict = await _replay_and_judge(_clr, app, sf, c["session_id"])
            if verdict is None:
                _recheck_state["failed"] += 1
                await update_fix_status(
                    sf, c["id"], fix_status="reopened", note="组级重放验证: 会话无客户消息/重放未完成 — 重开待复验"
                )
            elif verdict in ("pass", "warn"):
                _recheck_state["passed"] += 1
                await update_fix_status(sf, c["id"], fix_status="verified", note=f"组级重放验证通过 (判定: {verdict})")
            else:
                _recheck_state["failed"] += 1
                await update_fix_status(
                    sf, c["id"], fix_status="reopened", note="组级重放验证未通过 (判定: fail) — 重开重新治理"
                )
        except Exception as exc:
            _recheck_state["failed"] += 1
            logger.warning("组级重放单例失败: case=%s err=%s", c.get("id"), exc)
        finally:
            _recheck_state["done"] += 1
    _recheck_state["running"] = False
    logger.info("组级重放完成: group=%s 通过 %d/%d", group_key, _recheck_state["passed"], _recheck_state["total"])


async def _replay_and_judge(_clr: Any, app: Any, sf: Any, session_id: str) -> str | None:
    """触发一次重放 (复用 /quality/replay 内部任务), 等待完成后查新会话质检判定。

    Returns: pass/warn/fail; None = 无法重放或未产出判定。
    """
    import asyncio
    import time

    from sqlalchemy import select

    from lumio.shared.orm_models import DialogueLog, QualityRecord

    redis = getattr(app.state, "redis_client", None)
    if redis is None:
        return None
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
        return None

    new_sid = f"replay-{session_id[:20]}-{int(time.time() * 1000) % 100000}"
    key = f"qa:replay:progress:{new_sid}"
    await redis.hset(
        key,
        mapping={
            "status": "running",
            "total": str(len(msgs)),
            "done": "0",
            "timeouts": "0",
            "error": "",
            "origin": session_id[:64],
        },
    )
    task = asyncio.create_task(_clr._run_serial_replay(app, sf, redis, new_sid, msgs))
    _clr._replay_tasks.add(task)
    task.add_done_callback(_clr._replay_tasks.discard)

    # 等待完成 (轮询进度 hash; 上限 10 分钟防挂死)
    for _ in range(600):
        await asyncio.sleep(1)
        st = await redis.hgetall(key)
        if not st:
            continue
        if st.get("status") in ("done", "error"):
            break
    else:
        return None
    if st.get("status") != "done":
        return None

    # 查新会话的质检判定
    async with sf() as db:
        row = (
            await db.execute(
                select(QualityRecord.verdict)
                .where(QualityRecord.session_id == new_sid)
                .order_by(QualityRecord.scanned_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return row


@router.get("/funnel-stats")
async def funnel_stats_endpoint(user: AdminAgentUser, request: Request) -> dict[str, Any]:
    """飞轮漏斗: 五阶段案例计数 + 根治周期 — 修复投入与质量回报的一屏判读。

    阶段口径: 发现(总) → 待处置(pending) → 修复中(fixing/canary) →
    已上线(deployed) → 已根治(verified); 另报 reopened (飞轮回流) 与
    verified 平均周期 (created→resolved, 天)。
    """
    from sqlalchemy import func, select

    from lumio.shared.orm_models import Badcase

    sf = _sf(request)
    async with sf() as db:
        rows = (await db.execute(select(Badcase.fix_status, func.count()).group_by(Badcase.fix_status))).all()
        avg_days_row = (
            await db.execute(
                select(func.avg(func.extract("epoch", Badcase.resolved_at - Badcase.created_at) / 86400.0)).where(
                    Badcase.fix_status == "verified"
                )
            )
        ).scalar()
        total = (await db.execute(select(func.count()).select_from(Badcase))).scalar()
    by = dict(rows)
    avg_days = round(float(avg_days_row), 1) if avg_days_row is not None else None
    return {
        "stages": [
            {"key": "total", "label": "发现", "count": int(total)},
            {"key": "pending", "label": "待处置", "count": int(by.get("pending", 0))},
            {"key": "fixing", "label": "修复中", "count": int(by.get("fixing", 0) + by.get("canary", 0))},
            {"key": "deployed", "label": "已上线", "count": int(by.get("deployed", 0))},
            {"key": "verified", "label": "已根治", "count": int(by.get("verified", 0))},
        ],
        "reopened": int(by.get("reopened", 0)),
        "avg_cycle_days": avg_days,
    }


@router.get("/{group_key}")
async def get_pattern_endpoint(group_key: str, user: AdminAgentUser, request: Request) -> dict[str, Any]:
    """组详情: 方案 + 组内 open 案例清单"""
    detail = await pattern_store.get_pattern(_sf(request), group_key)
    if detail is None:
        raise LumioError(code=2004, message="问题组不存在或已无未结案例", status_code=404)
    return detail


class PlanPayload(BaseModel):
    intent_label: str | None = None
    root_cause_layer: str | None = None
    fix_table: str | None = None
    plan_text: str
    plan_owner: str = ""


@router.put("/{group_key}/plan")
async def save_plan_endpoint(
    group_key: str, body: PlanPayload, user: AdminOnlyUser, request: Request
) -> dict[str, Any]:
    """保存治理方案 (upsert)。方案落定 = 治理人确认组内共性根因, 是批量动作的把关前置"""
    if not body.plan_text.strip():
        raise LumioError(code=2001, message="修复方案不能为空")
    return await pattern_store.save_plan(
        _sf(request),
        group_key,
        intent_label=body.intent_label,
        root_cause_layer=body.root_cause_layer,
        plan_text=body.plan_text.strip(),
        plan_owner=body.plan_owner.strip(),
        fix_table=body.fix_table,
        created_by=user.user_id,
    )


class BatchBody(BaseModel):
    case_ids: list[str] | None = None  # 缺省 = 组内全部 pending 且已归因案例
    fix_status: str = "fixing"  # 目标状态 (申报类: fixing/canary/deployed)


@router.post("/{group_key}/cases/batch")
async def batch_transition_endpoint(
    group_key: str, body: BatchBody, user: AdminOnlyUser, request: Request
) -> dict[str, Any]:
    """批量执行: 组内案例批量推进状态机 (复用 update_fix_status, 转移守门/根因确认语义不变)

    fix_status=fixing 时同时把组根因落为人工确认 (批量确认根因→修复中);
    其余状态为纯申报类流转。逐例执行, 单例失败不阻断 (返回逐例结果)。
    """
    from lumio.services.common import pattern_store as ps

    detail = await ps.get_pattern(_sf(request), group_key)
    if detail is None:
        raise LumioError(code=2004, message="问题组不存在或已无未结案例", status_code=404)
    layer = detail["root_cause_layer"]
    targets = [c for c in detail["cases"]]
    if body.case_ids:
        wanted = set(body.case_ids)
        targets = [c for c in targets if c["id"] in wanted]
    if body.fix_status == "fixing":
        # 确认根因→修复中: 只推进 pending/reopened 且已有根因的案例
        targets = [c for c in targets if c["fix_status"] in ("pending", "reopened") and c["root_cause_layer"]]
    else:
        targets = [c for c in targets if c["fix_status"] != body.fix_status]

    results: list[dict[str, Any]] = []
    done = 0
    sf = _sf(request)
    for c in targets:
        human_layer = layer if body.fix_status == "fixing" else None
        note = f"问题治理批量: {group_key}" if body.fix_status == "fixing" else f"问题治理批量流转 → {body.fix_status}"
        try:
            ok = await update_fix_status(
                sf, c["id"], fix_status=body.fix_status, note=note, human_confirmed_layer=human_layer
            )
        except LumioError as exc:
            results.append({"id": c["id"], "ok": False, "message": exc.message})
            continue
        if ok:
            done += 1
            results.append({"id": c["id"], "ok": True})
        else:
            results.append({"id": c["id"], "ok": False, "message": "案例不存在"})
    logger.info(
        "问题治理批量: group=%s → %s, 成功 %d/%d by=%s", group_key, body.fix_status, done, len(targets), user.user_id
    )
    return {"done": done, "total": len(targets), "results": results}
