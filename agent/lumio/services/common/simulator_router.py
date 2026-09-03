"""对话模拟器管理 API (客户端模拟 Agent 的启停/状态/场景清单)

POST /admin/simulator/start  — 启动虚拟客户流量 (场景勾选/并发数/间隔)
POST /admin/simulator/stop   — 停止并返回最终统计
GET  /admin/simulator/status — 运行状态 + 统计 + 最近轮次

2026-09-03: 模拟器挪到独立 worker 进程 (simulator_worker.py) — 此前跑在
bot 服务进程内, 服务每次重启/发布模拟器即被杀掉, 长时流量采样断档。
管理端点改为子进程管理 (start_new_session, 父进程退出不影响 worker),
状态经 worker 心跳写入的 state file 读取; 响应契约不变, 前端零改动。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from lumio.shared.auth import AuthUser, require_role
from lumio.shared.exceptions import LumioError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/simulator", tags=["simulator"])

AdminOnlyUser = Annotated[AuthUser, Depends(require_role("admin"))]
AdminAgentUser = Annotated[AuthUser, Depends(require_role("admin", "agent"))]

# worker 心跳文件 (agent/data/ 下, 与模型产物同级的运行时目录)
_STATE_FILE = Path(__file__).resolve().parents[3] / "data" / "simulator_state.json"
_HEARTBEAT_STALE_SECONDS = 30.0


def _pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 进程存在但属主不同


def _read_state() -> dict[str, Any]:
    """读 worker 心跳状态; 心跳过期且进程不在 → 修正为 stopped"""
    try:
        st = json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"running": False, "config": {"scenario_keys": [], "users": 0, "interval": 0.0}, "stats": {}, "recent": []}
    pid = st.get("pid")
    hb = float(st.get("heartbeat_at") or 0.0)
    if st.get("running") and (not _pid_alive(pid) or time.time() - hb > _HEARTBEAT_STALE_SECONDS):
        st["running"] = False
        st["stale"] = True
    return st


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
    """启动模拟器 worker 子进程 (黑盒走自身 HTTP 链路: send → poll → feedback)

    worker 以 start_new_session 拉起 — bot 服务重启/发布不影响模拟器,
    流量采样跨发布连续。
    """
    from lumio.services.common.simulator import SCENARIO_MAP

    body = body or {}
    scenario_keys = [str(k) for k in body.get("scenario_keys") or []]
    users = max(1, min(int(body.get("users") or 2), 10))
    interval = max(1.0, float(body.get("interval") or 8.0))
    base_url = str(body.get("base_url") or "http://127.0.0.1:8000")

    valid = [k for k in scenario_keys if k in SCENARIO_MAP] or list(SCENARIO_MAP)
    current = _read_state()
    if current.get("running") and _pid_alive(current.get("pid")):
        raise LumioError(code=3001, message="模拟器已在运行 (独立 worker 进程)")

    # 旧 state 清掉再拉起, 防止读到上一次的终态
    with contextlib.suppress(Exception):
        _STATE_FILE.unlink(missing_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "lumio.services.common.simulator_worker",
        "--base-url",
        base_url,
        "--users",
        str(users),
        "--interval",
        str(interval),
        "--scenarios",
        ",".join(valid),
        "--state-file",
        str(_STATE_FILE),
    ]
    # cwd=agent 根: `-m lumio...` 的包解析根; start_new_session: 脱离服务进程会话组
    # stdout/stderr 落 worker 自己的日志文件 — 继承服务日志 fd 会在服务重启
    # (日志文件被截断轮转) 后互相干扰, 且排障时无处可查
    agent_root = Path(__file__).resolve().parents[3]
    worker_log = agent_root / "data" / "simulator_worker.log"
    worker_log.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.Popen(
        cmd,
        cwd=str(agent_root),
        stdin=subprocess.DEVNULL,
        stdout=open(worker_log, "ab"),  # noqa: SIM115 - Popen 接管生命周期
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    logger.info("模拟器 worker 已拉起: pid=%d scenarios=%d users=%d", proc.pid, len(valid), users)

    # 等首个心跳 (最多 ~4s), 让前端拿到确定的 running 状态
    for _ in range(20):
        await asyncio.sleep(0.2)
        st = _read_state()
        if st.get("running") or not _pid_alive(proc.pid):
            return st
    st = _read_state()
    st["pid"] = proc.pid
    return st


@router.post("/stop")
async def stop(user: AdminOnlyUser) -> dict[str, Any]:
    """停止模拟器 worker (SIGTERM 优雅停止, 保留最终统计)"""
    st = _read_state()
    pid = st.get("pid")
    if _pid_alive(pid):
        with contextlib.suppress(ProcessLookupError):
            os.kill(int(pid), signal.SIGTERM)
        for _ in range(25):  # 等终态心跳 (最多 ~5s)
            await asyncio.sleep(0.2)
            st = _read_state()
            if not st.get("running"):
                return st
        with contextlib.suppress(ProcessLookupError):
            os.kill(int(pid), signal.SIGKILL)
    st["running"] = False
    return st


@router.get("/status")
async def status(user: AdminAgentUser) -> dict[str, Any]:
    return _read_state()
