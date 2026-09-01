"""对话模拟器测试: 场景剧本完整性 + 启停状态机 + 轮次提取"""

from __future__ import annotations

import pytest

from lumio.services.common.simulator import (
    SCENARIO_MAP,
    SCENARIOS,
    SimulatorState,
    start_simulator,
    state,
    stop_simulator,
)


def test_scenarios_well_formed() -> None:
    assert len(SCENARIOS) >= 10
    for s in SCENARIOS:
        assert s.turns, f"场景 {s.key} 无轮次"
        for t in s.turns:
            assert t.get("text", "").strip(), f"场景 {s.key} 存在空轮次文本"
        assert s.final_feedback in ("", "down")
        assert SCENARIO_MAP[s.key] is s
    # 闭环关键场景必须在册: 转人工采集 + 差评喂闭环
    assert "transfer_direct" in SCENARIO_MAP
    assert SCENARIO_MAP["knowledge_gap"].final_feedback == "down"


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
    """黑盒链路 mock: send 200 → poll 返回 reply → 差评成功计数"""
    import httpx

    from lumio.services.common.simulator import SCENARIO_MAP, SimCustomer, state

    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/chat/send":
            return httpx.Response(200, json={"accepted": True})
        if request.url.path == "/api/chat/poll":
            return httpx.Response(200, json={"status": "done", "has_message": True, "reply": "模拟回复内容"})
        if request.url.path == "/api/chat/feedback":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    sc = SCENARIO_MAP["knowledge_gap"]
    orig_init = httpx.AsyncClient.__init__

    def patched_init(self, *a, **k):  # type: ignore[no-untyped-def]
        k["transport"] = transport
        orig_init(self, *a, **k)

    httpx.AsyncClient.__init__ = patched_init  # type: ignore[method-assign]
    try:
        # 隔离: 清掉前序用例 stop_simulator 置位的停止信号, 复位统计
        state._stop_event.clear()
        state.stats.feedbacks = 0
        customer = SimCustomer("http://fake", "sim-test-1")
        records = await customer.run_scenario(sc)
    finally:
        httpx.AsyncClient.__init__ = orig_init  # type: ignore[method-assign]

    assert len(records) == 1
    assert records[0].reply == "模拟回复内容"
    assert records[0].latency >= 0
    assert state.stats.feedbacks == 1  # 差评成功才计数
    assert "/api/chat/send" in calls and "/api/chat/feedback" in calls


def test_state_singleton_shape() -> None:
    s = SimulatorState()
    assert s.running is False
    assert len(s.recent) == 0
    d = s.stats.to_dict()
    assert d["sessions"] == 0 and d["latency_avg_ms"] == 0
