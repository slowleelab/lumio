"""对话模拟器独立 worker 进程 (2026-09-03: 挪出 bot 服务进程)

此前模拟器跑在 bot 服务进程内 — 服务每次重启/发布模拟器即被静默杀掉,
长时流量采样断档。本模块是可独立运行的 worker:

    python -m lumio.services.common.simulator_worker \
        --users 2 --interval 8 --state-file <path> [--scenarios k1,k2] \
        [--base-url http://127.0.0.1:8000]

- 复用 simulator.py 的全部场景/SimCustomer 逻辑 (进程内启动)
- 心跳: 每 2s 把 status_dict()+pid 写入 state file, 管理端点据此读状态
- SIGTERM/SIGINT: 优雅停止 (写最终统计 running=False) 后退出
- start_new_session 拉起, 父进程 (bot 服务) 重启/退出不影响本进程
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import signal
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s")
logger = logging.getLogger(__name__)

_HEARTBEAT_SECONDS = 2.0


def _write_state(state_file: Path) -> None:
    from lumio.services.common.simulator import status_dict

    payload = {**status_dict(), "pid": os.getpid(), "heartbeat_at": time.time()}
    tmp = state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    tmp.replace(state_file)


async def _run(args: argparse.Namespace, state_file: Path) -> None:
    from lumio.services.common import simulator

    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _graceful(signum: int, _frame: object) -> None:
        logger.info("收到信号 %s, 优雅停止模拟器", signum)
        loop.call_soon_threadsafe(stop.set)

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)

    simulator.start_simulator(
        args.base_url,
        scenario_keys=args.scenarios,
        users=args.users,
        interval=args.interval,
    )
    _write_state(state_file)

    async def _heartbeat() -> None:
        while not stop.is_set():
            with contextlib.suppress(Exception):
                _write_state(state_file)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=_HEARTBEAT_SECONDS)

    hb = asyncio.create_task(_heartbeat())
    # 退出信号 ≠ simulator._stop_event: 先等外部信号, 再调 stop_simulator 落终态
    await stop.wait()
    hb.cancel()
    with contextlib.suppress(Exception):
        simulator.stop_simulator()
    _write_state(state_file)
    logger.info("模拟器 worker 退出")


def main() -> None:
    parser = argparse.ArgumentParser(description="Lumio 对话模拟器独立 worker")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--users", type=int, default=2)
    parser.add_argument("--interval", type=float, default=8.0)
    parser.add_argument("--scenarios", default="", help="逗号分隔场景 key, 空=全部")
    parser.add_argument("--state-file", required=True)
    args = parser.parse_args()
    args.scenarios = [k for k in args.scenarios.split(",") if k]

    state_file = Path(args.state_file).resolve()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        asyncio.run(_run(args, state_file))
    except Exception as exc:  # 启动失败也要留下可读状态, 管理端点不悬等
        logger.exception("模拟器 worker 异常退出: %s", exc)
        with contextlib.suppress(Exception):
            state_file.write_text(
                json.dumps(
                    {"running": False, "pid": os.getpid(), "error": str(exc), "heartbeat_at": time.time()},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
