"""决策日志可解释性单元测试 (decision_log.py)"""

from __future__ import annotations

import asyncio
import json

from lumio.services.common.decision_log import (
    DecisionAction,
    DecisionLogger,
    DecisionRecord,
    get_decision_logger,
    log_decision,
)


def test_decision_action_values():
    """11 种决策动作"""
    assert DecisionAction.INTENT_CLASSIFY.value == "intent_classify"
    assert DecisionAction.TOOL_CALL.value == "tool_call"
    assert DecisionAction.TRANSFER_AGENT.value == "transfer_agent"
    assert DecisionAction.INJECTION_BLOCKED.value == "injection_blocked"
    assert DecisionAction.USER_CONFIRM.value == "user_confirm"


def test_decision_record_to_dict():
    """记录序列化"""
    rec = DecisionRecord(
        decision_id="d1",
        session_id="s1",
        turn_id="t1",
        agent_name="bot",
        action=DecisionAction.RAG_RETRIEVE,
        reasoning="检索知识库",
        evidence={"k": "v"},
        latency_ms=12.5,
        customer_id="c1",
        created_at=1.0,
    )
    data = rec.to_dict()
    assert data["action"] == "rag_retrieve"
    assert data["evidence"] == {"k": "v"}
    assert data["latency_ms"] == 12.5
    assert data["created_at"] == 1.0


class _FakeRedis:
    """异步 Redis mock (记录调用)"""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.stored: list[tuple[str, str]] = []

    async def lpush(self, key: str, value: str) -> None:
        self.calls.append(("lpush", key))
        self.stored.insert(0, (key, value))

    async def ltrim(self, key: str, start: int, stop: int) -> None:
        self.calls.append(("ltrim", key, start, stop))

    async def expire(self, key: str, ttl: int) -> None:
        self.calls.append(("expire", key, ttl))

    async def lrange(self, key: str, start: int, stop: int) -> list[str]:
        return [v for k, v in self.stored if k == key][start : stop + 1]


async def _drain(logger: DecisionLogger) -> None:
    """等待后台 task 完成"""
    if logger._pending_tasks:
        await asyncio.gather(*list(logger._pending_tasks), return_exceptions=True)


async def test_record_with_redis_writes_ring_buffer():
    """有 Redis 时记录写入 ring buffer (最近 100 条 + 7 天 TTL)"""
    logger = DecisionLogger()
    fake = _FakeRedis()
    logger._redis = fake

    decision_id = logger.record(
        session_id="s1",
        turn_id="t1",
        agent_name="bot",
        action=DecisionAction.INTENT_CLASSIFY,
        reasoning="意图: 账单查询",
        evidence={"intent": "bill_query"},
        latency_ms=3.2,
    )
    await _drain(logger)

    assert decision_id
    keys = [c[0] for c in fake.calls]
    assert "lpush" in keys and "ltrim" in keys and "expire" in keys
    assert fake.calls[0][1] == "lumio:decision:session:s1"


async def test_record_truncates_reasoning():
    """reasoning 超 500 字符截断"""
    logger = DecisionLogger()
    fake = _FakeRedis()
    logger._redis = fake

    logger.record(
        session_id="s1",
        turn_id="t1",
        agent_name="bot",
        action=DecisionAction.LLM_GENERATE,
        reasoning="x" * 2000,
    )
    await _drain(logger)
    saved = json.loads(fake.stored[0][1])
    assert len(saved["reasoning"]) == 500


def test_record_without_loop_uses_fallback():
    """无 event loop 时落入同步 fallback buffer"""
    logger = DecisionLogger()
    decision_id = logger.record(
        session_id="s1",
        turn_id="t1",
        agent_name="bot",
        action=DecisionAction.TOOL_CALL,
        reasoning="调工具",
    )
    assert decision_id
    assert len(logger._sync_fallback) == 1
    assert logger._sync_fallback[0].action == DecisionAction.TOOL_CALL


async def test_record_redis_failure_soft():
    """Redis 写入失败不影响 record 返回值"""
    logger = DecisionLogger()

    class _BoomRedis:
        async def lpush(self, *a):
            raise RuntimeError("redis down")

        async def ltrim(self, *a):
            raise RuntimeError("redis down")

        async def expire(self, *a):
            raise RuntimeError("redis down")

    logger._redis = _BoomRedis()
    decision_id = logger.record(
        session_id="s1",
        turn_id="t1",
        agent_name="bot",
        action=DecisionAction.GUARD_DENIED,
        reasoning="护栏拒绝",
    )
    await _drain(logger)
    assert decision_id


async def test_query_session_with_redis():
    """查询会话决策记录"""
    logger = DecisionLogger()
    fake = _FakeRedis()
    logger._redis = fake
    payload = json.dumps(
        {"decision_id": "d1", "session_id": "s1", "action": "tool_call"},
        ensure_ascii=False,
    )
    fake.stored.append(("lumio:decision:session:s1", payload))

    items = await logger.query_session("s1", limit=10)
    assert len(items) == 1
    assert items[0]["action"] == "tool_call"


async def test_query_session_no_redis():
    """无 Redis 时返回空列表"""
    logger = DecisionLogger()
    assert await logger.query_session("s1") == []


async def test_get_redis_failure_cached(monkeypatch):
    """Redis 初始化失败后缓存 False, 后续不再重试"""
    import lumio.services.common.redis_client as rc

    def boom():
        raise RuntimeError("no redis")

    monkeypatch.setattr(rc, "get_redis_client", boom)
    logger = DecisionLogger()
    assert await logger._get_redis() is None
    assert logger._redis is False
    # 二次调用不再触发 import (已被缓存)
    assert await logger._get_redis() is None


async def test_get_db_factory_failure_cached(monkeypatch):
    """DB factory 初始化失败后缓存 False"""
    import lumio.services.common.database as db

    def boom():
        raise RuntimeError("no db")

    monkeypatch.setattr(db, "get_async_session_factory", boom)
    logger = DecisionLogger()
    assert await logger._get_db_session_factory() is None
    assert logger._db_session_factory is False


def test_singleton_and_helper():
    """单例 + 便捷函数"""
    g1 = get_decision_logger()
    g2 = get_decision_logger()
    assert g1 is g2
    decision_id = log_decision(
        session_id="s1",
        agent_name="bot",
        action=DecisionAction.CACHE_HIT,
        reasoning="KV 命中",
        turn_id="t1",
    )
    assert decision_id


def test_turn_context_inheritance(monkeypatch):
    """turn_id 按轮贯穿: 绑定后留空的 log_decision 自动继承本轮 ID

    会话回放决策链按轮分组依赖此行为 (此前每条决策独立 uuid4, 无法归组)。
    """
    from lumio.services.common.decision_log import bind_turn_context, current_turn_id

    bind_turn_context("turn-abc")
    assert current_turn_id() == "turn-abc"
    captured: list[DecisionRecord] = []
    def _fake_record(self, **kw):
        captured.append(DecisionRecord(decision_id=f"d{len(captured)}", **kw))
        return "id"

    monkeypatch.setattr(DecisionLogger, "record", _fake_record)
    log_decision(session_id="s", agent_name="bot", action=DecisionAction.TURN_START, reasoning="出队")
    log_decision(session_id="s", agent_name="bot", action=DecisionAction.INTENT_CLASSIFY, reasoning="分类", turn_id="")
    # 显式指定的 turn_id 优先于上下文
    log_decision(session_id="s", agent_name="bot", action=DecisionAction.TOOL_CALL, reasoning="工具", turn_id="manual")
    assert [r.turn_id for r in captured] == ["turn-abc", "turn-abc", "manual"]

    # 新一轮重新绑定后旧值不泄漏 (串行 worker 语义)
    bind_turn_context("turn-xyz")
    log_decision(session_id="s", agent_name="bot", action=DecisionAction.CHAIN_COMPLETE, reasoning="完成")
    assert captured[-1].turn_id == "turn-xyz"


def test_turn_start_action_value():
    """出队留痕动作存在 (排队耗时归因)"""
    assert DecisionAction.TURN_START.value == "turn_start"
