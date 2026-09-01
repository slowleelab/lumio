"""对话模拟器管理 API (客户端模拟 Agent 的启停/状态/场景清单)

POST /admin/simulator/start  — 启动虚拟客户流量 (场景勾选/并发数/间隔)
POST /admin/simulator/stop   — 停止并返回最终统计
GET  /admin/simulator/status — 运行状态 + 统计 + 最近轮次
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from lumio.shared.auth import AuthUser, require_role
from lumio.shared.exceptions import LumioError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/simulator", tags=["simulator"])

AdminOnlyUser = Annotated[AuthUser, Depends(require_role("admin"))]
AdminAgentUser = Annotated[AuthUser, Depends(require_role("admin", "agent"))]


@router.get("/scenarios")
async def list_scenarios(user: AdminAgentUser) -> dict[str, Any]:
    """场景剧本清单 (前端勾选用)"""
    from lumio.services.common.simulator import SCENARIOS

    return {
        "total": len(SCENARIOS),
        "scenarios": [
            {
                "key": s.key,
                "name_zh": s.name_zh,
                "turns": len(s.turns),
                "variants": s.variant_count(),
                "final_feedback": s.final_feedback,
                "tags": s.tags,
            }
            for s in SCENARIOS
        ],
    }


@router.post("/start")
async def start(user: AdminOnlyUser, request: Request, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """启动模拟器 (黑盒走自身 HTTP 链路: send → poll → feedback)"""
    from lumio.services.common.simulator import start_simulator

    body = body or {}
    scenario_keys = [str(k) for k in body.get("scenario_keys") or []]
    users = int(body.get("users") or 2)
    interval = float(body.get("interval") or 8.0)
    base_url = str(body.get("base_url") or "http://127.0.0.1:8000")
    try:
        return start_simulator(base_url, scenario_keys=scenario_keys, users=users, interval=interval)
    except RuntimeError as exc:
        raise LumioError(code=3001, message=str(exc)) from exc


@router.post("/stop")
async def stop(user: AdminOnlyUser) -> dict[str, Any]:
    from lumio.services.common.simulator import stop_simulator

    return stop_simulator()


@router.get("/status")
async def status(user: AdminAgentUser) -> dict[str, Any]:
    from lumio.services.common.simulator import status_dict

    return status_dict()
