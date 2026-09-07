"""诉求跟踪器测试 (多轮会话管理: 断档/带偏同根源修复)"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from lumio.services.bot.bot_agent import LumioAgent
from lumio.shared.models import (
    IntentLabel,
    IntentResult,
    TopicRequest,
    TopicRequestStatus,
)


def _make_agent() -> LumioAgent:
    classifier = MagicMock()
    classifier.classify = AsyncMock(
        return_value=(IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.5), [], MagicMock(), "")
    )
    return LumioAgent(
        classifier=classifier,
        degradation_mgr=MagicMock(_degrader=MagicMock(hardcoded_fallback=MagicMock(return_value="降级话术"))),
        transfer_checker=MagicMock(),
        session_manager=MagicMock(get_session=AsyncMock(return_value=None)),
    )


def _topic(intent: str = "card_loss", urgency: str = "high", status: TopicRequestStatus = TopicRequestStatus.OPEN) -> TopicRequest:
    return TopicRequest(
        id=intent,
        intent=intent,
        label_zh={"card_loss": "挂失", "bill_query": "账单查询"}.get(intent, intent),
        urgency=urgency,
        status=status,
        raised_turn=1,
        updated_at=datetime.now(UTC),
    )


class TestIntentAsTopic:
    def test_loss_is_high_urgency(self) -> None:
        agent = _make_agent()
        t = agent._intent_as_topic(
            IntentResult(primary_intent=IntentLabel.CARD_LOSS, primary_confidence=0.96), "risk", 3
        )
        assert t is not None and t.urgency == "high" and t.label_zh == "挂失"

    def test_low_conf_faq_not_a_topic(self) -> None:
        agent = _make_agent()
        assert agent._intent_as_topic(IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.5), "knowledge", 3) is None

    def test_unknown_intent_not_a_topic(self) -> None:
        agent = _make_agent()
        assert agent._intent_as_topic(IntentResult(primary_intent=IntentLabel.NB_CHITCHAT, primary_confidence=0.9), "fallback", 3) is None


class TestMergeTopicRequests:
    def test_tool_result_fulfills(self) -> None:
        agent = _make_agent()
        out = agent._merge_topic_requests([], _topic(), "tool")
        assert out[0].status == TopicRequestStatus.FULFILLED

    def test_clarify_waits_info(self) -> None:
        agent = _make_agent()
        out = agent._merge_topic_requests([], _topic(), "clarify")
        assert out[0].status == TopicRequestStatus.WAITING_INFO

    def test_template_keeps_open(self) -> None:
        agent = _make_agent()
        out = agent._merge_topic_requests([], _topic(), "template")
        assert out[0].status == TopicRequestStatus.OPEN

    def test_same_intent_refresh_not_duplicate(self) -> None:
        agent = _make_agent()
        out = agent._merge_topic_requests([_topic()], _topic(urgency="normal"), "clarify")
        assert len(out) == 1 and out[0].urgency == "high"  # high 不降级

    def test_overflow_evicts_oldest_fulfilled(self) -> None:
        agent = _make_agent()
        reqs = [_topic(status=TopicRequestStatus.FULFILLED) for _ in range(6)]
        out = agent._merge_topic_requests(reqs, None, "tool")
        assert len(out) <= LumioAgent._TOPIC_MAX_ACTIVE


class TestPickFollowup:
    def test_high_urgency_open_other_topic_triggers(self) -> None:
        agent = _make_agent()
        f = agent._pick_followup([_topic()], "bill_query", 2)
        assert f is not None and f.label_zh == "挂失"

    def test_same_intent_no_followup(self) -> None:
        agent = _make_agent()
        assert agent._pick_followup([_topic()], "card_loss", 2) is None

    def test_normal_urgency_no_followup(self) -> None:
        agent = _make_agent()
        assert agent._pick_followup([_topic(urgency="normal")], "bill_query", 2) is None

    def test_fulfilled_no_followup(self) -> None:
        agent = _make_agent()
        assert agent._pick_followup([_topic(status=TopicRequestStatus.FULFILLED)], "bill_query", 2) is None

    def test_revisit_cap(self) -> None:
        agent = _make_agent()
        t = _topic()
        t.revisit_count = 2
        assert agent._pick_followup([t], "bill_query", 2) is None


class TestTrackAndFollowup:
    @pytest.mark.asyncio
    async def test_followup_appended_and_state_written(self) -> None:
        """挂失(未办结) → 本轮查账单: 回复尾部追加回访 + active_requests 写回"""
        agent = _make_agent()
        agent._session_manager.patch_state = AsyncMock()
        state = MagicMock(
            version=7,
            turn_count=3,
            active_requests=[_topic()],
        )
        result = {"response": "您的账单金额为 8650 元。", "response_source": "tool"}
        await agent._track_and_followup(
            "s1",
            state,
            IntentResult(primary_intent=IntentLabel.BILL_QUERY, primary_confidence=0.84),
            "query",
            result,
        )
        assert "挂失" in result["response"] and "未办理完成" in result["response"]
        agent._session_manager.patch_state.assert_awaited_once()
        patches = agent._session_manager.patch_state.await_args.kwargs["patches"]
        assert any(t["intent"] == "bill_query" and t["status"] == "fulfilled" for t in patches["active_requests"])

    @pytest.mark.asyncio
    async def test_no_new_topic_no_revisit(self) -> None:
        """本轮无新诉求 (闲聊) 不触发回访 — 避免对闲聊轮刷提醒"""
        agent = _make_agent()
        agent._session_manager.patch_state = AsyncMock()
        state = MagicMock(version=7, turn_count=3, active_requests=[_topic()])
        result = {"response": "哈哈~", "response_source": "template"}
        await agent._track_and_followup(
            "s1", state, IntentResult(primary_intent=IntentLabel.NB_CHITCHAT, primary_confidence=0.29), "fallback", result
        )
        assert "未办理完成" not in result["response"]


class TestClassifyContextAntiBias:
    """防带偏: 无未办结诉求时 BERT 上下文只留最近一对轮次"""

    @pytest.mark.asyncio
    async def test_fulfilled_history_narrowed(self) -> None:
        agent = _make_agent()
        turns = [MagicMock(speaker="customer" if i % 2 == 0 else "bot", content=f"t{i}", confidence=0.8, response_source="knowledge") for i in range(6)]
        agent._session_manager.get_history = AsyncMock(return_value=turns)
        agent._session_manager.get_session = AsyncMock(
            return_value=MagicMock(active_requests=[_topic(status=TopicRequestStatus.FULFILLED)])
        )
        ctx = await agent._classify_context("s1")
        assert len(ctx) == 2  # 6 轮 → 只留最近一对

    @pytest.mark.asyncio
    async def test_live_request_keeps_full_context(self) -> None:
        agent = _make_agent()
        turns = [MagicMock(speaker="customer" if i % 2 == 0 else "bot", content=f"t{i}", confidence=0.8, response_source="knowledge") for i in range(6)]
        agent._session_manager.get_history = AsyncMock(return_value=turns)
        agent._session_manager.get_session = AsyncMock(return_value=MagicMock(active_requests=[_topic()]))  # open
        ctx = await agent._classify_context("s1")
        assert len(ctx) == 6  # 有未办结诉求 → 保留全部 (多轮补槽需要上文)
