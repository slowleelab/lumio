"""对话模拟器测试: 剧本随机化 + 启停状态机 + 轮次提取"""

from __future__ import annotations

import random

import pytest

from lumio.services.common.simulator import (
    SCENARIO_MAP,
    SCENARIOS,
    SimulatorState,
    decorate,
    expect_hit,
    fill_slots,
    gen_noise,
    pick_turn_text,
    start_simulator,
    state,
    stop_simulator,
)


def test_scenarios_well_formed() -> None:
    assert len(SCENARIOS) >= 10
    for s in SCENARIOS:
        assert s.turns, f"场景 {s.key} 无轮次"
        for t in s.turns:
            # 轮次为 noise 标记或 variants/text 池
            if not t.get("noise"):
                variants = t.get("variants") or [t.get("text") or ""]
                assert all(v.strip() for v in variants), f"场景 {s.key} 存在空话术变体"
                assert len(variants) >= 1
        assert s.final_feedback in ("", "down")
        assert SCENARIO_MAP[s.key] is s
    # 闭环关键场景必须在册: 转人工采集 + 差评喂闭环
    assert "transfer_direct" in SCENARIO_MAP
    assert SCENARIO_MAP["knowledge_gap"].final_feedback == "down"


def test_variant_randomization() -> None:
    """同一轮多次抽取应产生不同话术 (随机化生效), 且都不为空"""
    rng = random.Random(42)
    turn = SCENARIO_MAP["transfer_direct"].turns[0]
    samples = {pick_turn_text(turn, rng, raw=True) for _ in range(40)}
    assert len(samples) >= 3, f"话术未随机化: {samples}"
    assert all(samples)
    # 多数场景有多变体
    multi = [s.key for s in SCENARIOS if not any(t.get("noise") for t in s.turns) and s.variant_count() >= 3]
    assert len(multi) >= 8


def test_slot_filling() -> None:
    rng = random.Random(7)
    out = fill_slots("查下我{month}账单，卡号 {card_no}", rng)
    assert "{month}" not in out and "{card_no}" not in out
    assert len(out) > 10
    # 无槽位文本原样返回
    assert fill_slots("你好", rng) == "你好"


def test_decorate_safety() -> None:
    rng = random.Random(1)
    for _ in range(100):
        out = decorate("帮我查一下信用卡账单", rng)
        assert out and len(out) >= 8
    assert decorate("", rng) == ""


def test_noise_generator() -> None:
    rng = random.Random(3)
    samples = {gen_noise(rng) for _ in range(30)}
    assert len(samples) >= 25  # 几乎每次不同
    assert all(4 <= len(s) <= 24 for s in samples)
    turn = {"noise": True}
    outs = {pick_turn_text(turn, rng) for _ in range(20)}
    assert len(outs) >= 15


def test_expect_hit_semantics() -> None:
    assert expect_hit("卡号", "请提供卡号") is True
    assert expect_hit(["转", "专员"], "已为您转接") is True
    assert expect_hit(["转", "专员"], "好的呢") is False
    assert expect_hit("", "任意") is None
    assert expect_hit(None, "") is None


@pytest.mark.asyncio
async def test_start_stop_state_machine() -> None:
    # 空场景拒绝
    with pytest.raises(RuntimeError, match="有效场景"):
        start_simulator("http://127.0.0.1:8000", scenario_keys=["nonexistent"], users=1, interval=1)
    # 正常启动 (后台 loop 空转, interval 长, 不真发请求前就停)
    r = start_simulator("http://127.0.0.1:1", scenario_keys=["chitchat"], users=1, interval=60)
    assert r["running"] is True
    assert r["config"]["scenario_keys"] == ["chitchat"]
    # 重复启动拒绝
    with pytest.raises(RuntimeError, match="已在运行"):
        start_simulator("http://127.0.0.1:1", scenario_keys=["chitchat"], users=1, interval=60)
    # 停止 → 复位
    r2 = stop_simulator()
    assert r2["running"] is False
    assert state.tasks == set()
    # 停止后可再次启动
    r3 = start_simulator("http://127.0.0.1:1", scenario_keys=["chitchat", "noise"], users=2, interval=60)
    assert r3["config"]["users"] == 2
    stop_simulator()


@pytest.mark.asyncio
async def test_scenario_runner_with_fake_transport() -> None:
    """黑盒链路 mock: send 200 → poll 返回 reply → 差评成功计数 (固定 rng 关闭挂断)"""
    import httpx

    from lumio.services.common.simulator import SimCustomer

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/chat/send":
            return httpx.Response(200, json={"accepted": True})
        if request.url.path == "/api/chat/poll":
            # 差评条件化后: 回复需含拒答话术才触发差评
            return httpx.Response(200, json={"status": "done", "has_message": True, "reply": "抱歉，无法直接查询，请通过官方渠道"})
        if request.url.path == "/api/chat/feedback":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    orig_init = httpx.AsyncClient.__init__

    def patched_init(self, *a, **k):  # type: ignore[no-untyped-def]
        k["transport"] = transport
        orig_init(self, *a, **k)

    httpx.AsyncClient.__init__ = patched_init  # type: ignore[method-assign]
    try:
        # 隔离: 清掉前序用例 stop_simulator 置位的停止信号, 复位统计
        state._stop_event.clear()
        state.stats.feedbacks = 0
        state.stats.sessions = 0
        state.stats.abandoned = 0
        sc = SCENARIO_MAP["knowledge_gap"]
        # rng.random 恒 0.99 → 不挂断/不寒暄/不连问 (choice 仍真实随机)
        rng = random.Random(0)
        rng.random = lambda: 0.99  # type: ignore[method-assign]
        customer = SimCustomer("http://fake", "sim-test-1", rng=rng)
        records = await customer.run_scenario(sc)
    finally:
        httpx.AsyncClient.__init__ = orig_init  # type: ignore[method-assign]

    assert len(records) == 1
    assert "无法" in records[0].reply  # 拒答话术完整透传
    assert records[0].text  # 随机化后话术仍非空
    assert state.stats.feedbacks == 1  # 拒答回复 → 差评计数
    assert "/api/chat/send" in calls and "/api/chat/feedback" in calls


def test_state_singleton_shape() -> None:
    s = SimulatorState()
    assert s.running is False
    assert len(s.recent) == 0
    d = s.stats.to_dict()
    assert d["sessions"] == 0 and d["latency_avg_ms"] == 0 and d["abandoned"] == 0


def test_feedback_only_on_bad_reply() -> None:
    """差评条件化: 正常作答(无拒答话术)不差评 — 消除假阳性"""
    from lumio.services.common.simulator import SimCustomer

    assert SimCustomer._reply_is_bad(None, "抱歉，无法直接查询，请通过官方渠道") is True
    assert SimCustomer._reply_is_bad(None, "您的意思我还没太理解") is True
    assert SimCustomer._reply_is_bad(None, "您可以通过手机银行APP数字人民币专区为硬钱包充值") is False
    assert SimCustomer._reply_is_bad(None, "") is False


# ── 独立 worker 进程管理 (2026-09-03: 模拟器挪出 bot 服务进程) ──


class TestWorkerProcessManagement:
    def test_read_state_missing_file_defaults_stopped(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        from lumio.services.common import simulator_router as sr

        monkeypatch.setattr(sr, "_STATE_FILE", tmp_path / "nonexistent.json")
        st = sr._read_state()
        assert st["running"] is False
        assert st["config"]["scenario_keys"] == []

    def test_read_state_fresh_alive_pid_keeps_running(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        import json
        import os
        import time

        from lumio.services.common import simulator_router as sr

        f = tmp_path / "state.json"
        f.write_text(json.dumps({"running": True, "pid": os.getpid(), "heartbeat_at": time.time()}))
        monkeypatch.setattr(sr, "_STATE_FILE", f)
        assert sr._read_state()["running"] is True  # 自己的 pid 活着 + 心跳新鲜

    def test_read_state_stale_heartbeat_corrected_to_stopped(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import json

        from lumio.services.common import simulator_router as sr

        f = tmp_path / "state.json"
        # 死 pid + 过期心跳 → running 修正为 False
        f.write_text(json.dumps({"running": True, "pid": 999999, "heartbeat_at": 0}))
        monkeypatch.setattr(sr, "_STATE_FILE", f)
        st = sr._read_state()
        assert st["running"] is False
        assert st.get("stale") is True

    @pytest.mark.asyncio
    async def test_start_rejects_when_worker_alive(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """worker 存活时重复启动 → 3001 (幂等保护)"""
        import os

        from lumio.services.common import simulator_router as sr
        from lumio.shared.exceptions import LumioError

        monkeypatch.setattr(sr, "_read_state", lambda: {"running": True, "pid": os.getpid()})

        async def _fake_start(*a, **k):  # 不会被调用到 Popen 前
            raise AssertionError

        with pytest.raises(LumioError, match="已在运行"):
            await sr.start(user=None, request=None, body={"scenario_keys": ["chitchat"]})

    def test_worker_write_state_shape(self, tmp_path) -> None:
        """worker 心跳写入: status_dict 展开 + pid + heartbeat_at"""
        from lumio.services.common.simulator_worker import _write_state

        f = tmp_path / "hb.json"
        _write_state(f)
        data = json.loads(f.read_text())
        assert data["pid"] > 0
        assert "heartbeat_at" in data and "running" in data and "config" in data


import json  # noqa: E402  (测试块内多处使用, 统一顶部化由 ruff-isort 兜底)


async def test_scenario_completion_ends_session() -> None:
    """对话完成 → 主动结束会话 (服务端回收 + 触发会话结束质检)"""
    import httpx

    from lumio.services.common.simulator import SimCustomer

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/chat/send":
            return httpx.Response(200, json={"accepted": True})
        if request.url.path == "/api/chat/poll":
            return httpx.Response(200, json={"has_message": True, "reply": "标准回复内容"})
        if request.url.path == "/api/chat/end":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    orig_init = httpx.AsyncClient.__init__

    def patched_init(self, *a, **k):  # type: ignore[no-untyped-def]
        k["transport"] = transport
        orig_init(self, *a, **k)

    httpx.AsyncClient.__init__ = patched_init  # type: ignore[method-assign]
    try:
        state._stop_event.clear()
        sc = SCENARIO_MAP["knowledge_gap"]
        rng = random.Random(0)
        rng.random = lambda: 0.99  # type: ignore[method-assign]
        customer = SimCustomer("http://fake", "sim-test-end", rng=rng)
        await customer.run_scenario(sc)
    finally:
        httpx.AsyncClient.__init__ = orig_init  # type: ignore[method-assign]

    assert "/api/chat/end" in calls, "对话完成后应主动结束会话"


async def test_reply_timeout_ends_session_and_aborts(monkeypatch: pytest.MonkeyPatch) -> None:
    """120s 无回复 → 主动结束会话 + timeouts 计数 + 场景终止 (ReplyTimeout)"""
    import httpx

    import lumio.services.common.simulator as sim
    from lumio.services.common.simulator import ReplyTimeoutError, SimCustomer

    monkeypatch.setattr(sim, "POLL_TIMEOUT", 0.2)  # 测试缩短等待窗

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/chat/send":
            return httpx.Response(200, json={"accepted": True})
        if request.url.path == "/api/chat/poll":
            return httpx.Response(200, json={"has_message": False})  # 永远无回复
        if request.url.path == "/api/chat/end":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    orig_init = httpx.AsyncClient.__init__

    def patched_init(self, *a, **k):  # type: ignore[no-untyped-def]
        k["transport"] = transport
        orig_init(self, *a, **k)

    httpx.AsyncClient.__init__ = patched_init  # type: ignore[method-assign]
    state._stop_event.clear()
    state.stats.timeouts = 0
    try:
        rng = random.Random(0)
        rng.random = lambda: 0.99  # type: ignore[method-assign]
        customer = SimCustomer("http://fake", "sim-test-timeout", rng=rng)
        customer._session_id = "sim-timeout-test"
        async with httpx.AsyncClient(transport=transport) as client:
            with pytest.raises(ReplyTimeoutError):
                await customer._send_and_poll(client, "查账单")
    finally:
        httpx.AsyncClient.__init__ = orig_init  # type: ignore[method-assign]

    assert state.stats.timeouts == 1
    assert "/api/chat/end" in calls, "超时应主动结束会话"
