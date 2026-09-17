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
