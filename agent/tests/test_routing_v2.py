"""目标架构 v2 两级路由 + 执行链 + 出站闸门测试"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI

from lumio.services.bot.outbound_guard import OutboundGuard
from lumio.services.bot.parallel_race import race
from lumio.services.bot.query_chain import QueryChain
from lumio.services.bot.routing import (
    TrafficClass,
    classify_traffic,
    decision_two,
    detect_composite,
)
from lumio.shared.auth import AuthUser, get_current_user
from lumio.shared.models import IntentLabel
from lumio.shared.safety import SafetyFilter

# ── ① 流量分类表: 149 意图全覆盖不漏类 ──


class TestTrafficClassification:
    def test_all_intents_classified(self) -> None:
        from lumio.services.common.classifier import INTENT_DOMAINS

        for intent in INTENT_DOMAINS:
            tc = classify_traffic(intent)
            assert tc in list(TrafficClass), intent

    def test_known_mappings(self) -> None:
        assert classify_traffic(IntentLabel.ACCOUNT_BILL_QUERY) == TrafficClass.READ_ONLY_QUERY
        assert classify_traffic(IntentLabel.CARD_LOSS_REPORT) == TrafficClass.FINANCIAL_TRANSACTION
        assert classify_traffic(IntentLabel.COMPLAINT) == TrafficClass.HIGH_RISK
        assert classify_traffic(IntentLabel.DISPUTE_CHARGEBACK) == TrafficClass.HIGH_RISK
        assert classify_traffic(IntentLabel.INST_APPLY) == TrafficClass.FINANCIAL_TRANSACTION
        assert classify_traffic(IntentLabel.FAQ) == TrafficClass.CONSULTING

    def test_decision_two_bands(self) -> None:
        assert decision_two(0.5, False) == "parallel_race"
        assert decision_two(0.39, False) == "rag_chain"
        assert decision_two(0.6, False) == "rag_chain"
        assert decision_two(0.9, True) == "composite"

    def test_composite_detection(self) -> None:
        assert detect_composite(IntentLabel.LIMIT_QUERY, [], "信用额度是什么, 为什么会调整")
        assert not detect_composite(IntentLabel.LIMIT_QUERY, [], "我的额度是多少")
        assert not detect_composite(IntentLabel.FAQ, [], "为什么会调整")  # 非查询主意图不算复合


# ── ③ 链 D 并行竞速 ──


class TestParallelRace:
    @pytest.mark.asyncio
    async def test_faq_wins(self) -> None:
        async def faq():
            return {"match_type": "exact", "results": [{"answer": "标准答案"}]}

        async def rag():
            return "RAG上下文"

        out = await race(faq, rag)
        assert out.winner == "faq" and out.faq_answer == "标准答案"

    @pytest.mark.asyncio
    async def test_rag_wins_when_faq_miss(self) -> None:
        async def faq():
            return {"match_type": "miss", "results": []}

        async def rag():
            return "RAG上下文"

        out = await race(faq, rag)
        assert out.winner == "rag" and out.rag_context == "RAG上下文"

    @pytest.mark.asyncio
    async def test_none_when_both_empty(self) -> None:
        async def faq():
            return {"match_type": "miss", "results": []}

        async def rag():
            return ""

        out = await race(faq, rag)
        assert out.winner == "none"

    @pytest.mark.asyncio
    async def test_errors_tolerated(self) -> None:
        async def faq():
            raise RuntimeError("faq down")

        async def rag():
            return "上下文"

        out = await race(faq, rag)
        assert out.winner == "rag" and out.faq_error


# ── ④ 链 B 查询轻链路 ──


def _mock_mcp(schema: dict, sensitive: bool = False) -> MagicMock:
    mcp = MagicMock()
    from lumio.services.common.mcp_client import ToolSpec

    spec = ToolSpec(name="query_card_bill", description="", input_schema=schema, sensitive=sensitive)
    mcp.get_tool.return_value = spec
    return mcp


class TestQueryChain:
    @pytest.mark.asyncio
    async def test_missing_params_returns_clarify_signal(self) -> None:
        schema = {"properties": {"period": {"type": "string"}, "card_no": {"type": "string"}}, "required": ["period"]}
        chain = QueryChain(mcp_client=_mock_mcp(schema), redis_client=None, degradation_mgr=None)
        out = await chain.run(
            intent_label="account_bill_query",
            user_input="查账单",
            tool_names=["query_card_bill"],
            slot_values={},
            customer_id="c1",
        )
        assert out.missing_params == ["period"] and out.content == ""

    @pytest.mark.asyncio
    async def test_complete_params_calls_tool_and_summarizes(self) -> None:
        schema = {"properties": {"period": {"type": "string"}, "card_no": {"type": "string"}}, "required": ["period"]}
        mcp = _mock_mcp(schema)
        mcp.call_tool = AsyncMock(return_value={"is_error": False, "content": "账单金额 8650 元"})
        deg = MagicMock()
        deg.generate_with_fallback = AsyncMock(return_value=MagicMock(content="您本期账单 8650 元", source="llm"))
        chain = QueryChain(mcp_client=mcp, redis_client=None, degradation_mgr=deg)
        out = await chain.run(
            intent_label="account_bill_query",
            user_input="查本期账单",
            tool_names=["query_card_bill"],
            slot_values={"period": "2026-08"},
            customer_id="c1",
        )
        assert out.content == "您本期账单 8650 元" and not out.cache_hit
        # card_no 由绑定关系自动注入
        assert out.tool_args.get("card_no")

    @pytest.mark.asyncio
    async def test_cache_hit_skips_tool(self) -> None:
        schema = {"properties": {"period": {"type": "string"}}, "required": ["period"]}
        mcp = _mock_mcp(schema)
        mcp.call_tool = AsyncMock(return_value={"is_error": False, "content": "x"})
        redis = MagicMock()
        redis.get = AsyncMock(return_value="缓存命中回复")
        chain = QueryChain(mcp_client=mcp, redis_client=redis, degradation_mgr=None)
        out = await chain.run(
            intent_label="account_bill_query",
            user_input="查账单",
            tool_names=["query_card_bill"],
            slot_values={"period": "2026-08"},
            customer_id="c1",
        )
        assert out.cache_hit and out.content == "缓存命中回复"
        mcp.call_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sensitive_tool_skipped(self) -> None:
        mcp = _mock_mcp({"properties": {}}, sensitive=True)
        chain = QueryChain(mcp_client=mcp, redis_client=None, degradation_mgr=None)
        out = await chain.run(
            intent_label="x", user_input="q", tool_names=["query_card_bill"], slot_values={}, customer_id=None
        )
        assert out.error == "no_query_tool"


# ── ⑦ 出站闸门 ──


class TestOutboundGuard:
    def _guard(self) -> OutboundGuard:
        return OutboundGuard(SafetyFilter(mask_char="*"), "澄清话术")

    def test_pass_normal(self) -> None:
        v = self._guard().check("您本期账单金额为 8650 元", grounding_source="账单金额 8650 元，最低还款 865")
        assert v.passed

    def test_block_ungrounded_numbers(self) -> None:
        v = self._guard().check("您需要还款 99999 元", grounding_source="账单金额 8650 元")
        assert not v.passed and v.reason == "ungrounded_numbers"

    def test_block_fabricated_execution(self) -> None:
        v = self._guard().check("已为您办理分期业务", grounding_source="")
        assert not v.passed and v.reason == "fabricated_execution"

    def test_fabricated_allowed_with_tool(self) -> None:
        v = self._guard().check("已为您办理分期业务", grounding_source="", tool_executed=True)
        assert v.passed

    def test_no_grounding_no_number_check(self) -> None:
        v = self._guard().check("一般费率为 0.75%", grounding_source="")
        assert v.passed


# ── ② 分派冒烟: 开关开启时走 v2 ──


class TestDispatchV2:
    def _patch_v2(self, monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
        from lumio.shared.config import Settings

        settings = Settings()
        settings.bot.routing_v2_enabled = enabled
        monkeypatch.setattr("lumio.services.bot.bot_agent.get_settings", lambda: settings)

    def _make_app(self, monkeypatch: pytest.MonkeyPatch, enabled: bool) -> FastAPI:
        from lumio.services.bot.bot_agent import LumioAgent  # noqa: F401

        self._patch_v2(monkeypatch, enabled)
        app = FastAPI()
        app.dependency_overrides[get_current_user] = lambda: AuthUser(user_id="a", role="admin", session_id=None)
        return app

    def test_v2_flag_default_off(self) -> None:
        from lumio.shared.config import Settings

        s = Settings(_env_file=())
        assert s.bot.routing_v2_enabled is False
