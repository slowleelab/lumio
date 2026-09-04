"""目标架构 ④ 两级路由 + 执行链 + 出站闸门测试"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from lumio.services.bot.outbound_guard import OutboundGuard
from lumio.services.bot.parallel_race import race
from lumio.services.bot.query_chain import QueryChain
from lumio.services.bot.routing import (
    TrafficClass,
    classify_traffic,
    decision_two,
    detect_composite,
    is_chitchat_redirect,
)
from lumio.shared.models import IntentLabel
from lumio.shared.safety import SafetyFilter

# ── ① 流量分类表: 149 意图全覆盖不漏类 ──


class TestTrafficClassification:
    def test_all_intents_classified(self) -> None:
        from lumio.services.common.classifier import INTENT_DOMAINS

        for intent in INTENT_DOMAINS:
            _, traffic = classify_traffic(intent)
            assert traffic is None or traffic in list(TrafficClass), intent

    def test_known_mappings(self) -> None:
        # classify_traffic 返回 (五域, 交易性质|None); 五域骨架为域权威
        cases = {
            IntentLabel.ACCOUNT_BILL_QUERY: ("query", TrafficClass.READ_ONLY_QUERY),
            IntentLabel.CARD_LOSS_REPORT: ("transaction", TrafficClass.FINANCIAL_TRANSACTION),
            IntentLabel.COMPLAINT: ("service", TrafficClass.HIGH_RISK),
            IntentLabel.DISPUTE_CHARGEBACK: ("service", TrafficClass.HIGH_RISK),
            IntentLabel.INST_APPLY: ("transaction", TrafficClass.FINANCIAL_TRANSACTION),
        }
        for intent, (want_domain, want_traffic) in cases.items():
            domain, traffic = classify_traffic(intent)
            assert (domain.value, traffic.value if traffic else None) == (
                want_domain,
                want_traffic.value if want_traffic else None,
            )
        # 咨询域无交易性质 → (consulting, None), 进决策二
        assert classify_traffic(IntentLabel.FAQ) == ("consulting", None)

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
        # start_date 无智能默认 (period 默认本期), 真缺参才反问
        schema = {
            "properties": {"start_date": {"type": "string"}, "card_no": {"type": "string"}},
            "required": ["start_date"],
        }
        chain = QueryChain(mcp_client=_mock_mcp(schema), redis_client=None, degradation_mgr=None)
        out = await chain.run(
            intent_label="account_bill_query",
            user_input="查账单",
            tool_names=["query_card_bill"],
            slot_values={},
            customer_id="c1",
        )
        assert out.missing_params == ["start_date"] and out.content == ""

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


class TestChitchatRedirect:
    """闲聊域轻回复判定 (会话 8700a2ea: "锄禾日当午"进 RAG 链答非所问)"""

    def test_pure_chitchat_redirects(self) -> None:
        assert is_chitchat_redirect(IntentLabel.NB_CHITCHAT, []) is True
        assert is_chitchat_redirect(IntentLabel.NB_NOISE, []) is True
        # LLM 慢路径自评通胀的高置信同样拦 (置信封顶兜底之外的域级短路)
        assert is_chitchat_redirect(IntentLabel.NB_CHITCHAT, []) is True

    def test_business_alternative_passes_through(self) -> None:
        # 混合句 "哈哈帮我查下账单": alternatives 携带业务域意图 → 不拦
        assert is_chitchat_redirect(IntentLabel.NB_CHITCHAT, [IntentLabel.BILL_QUERY]) is False
        assert is_chitchat_redirect(IntentLabel.NB_CHITCHAT, [IntentLabel.TRANSFER_AGENT]) is False

    def test_nonbusiness_alternative_still_redirects(self) -> None:
        # FAQ/闲聊类 alternatives 不是业务诉求, 照拦
        assert is_chitchat_redirect(IntentLabel.NB_CHITCHAT, [IntentLabel.FAQ]) is True

    def test_consulting_primary_never_redirects(self) -> None:
        # 咨询/查询主意图与闲聊判定无关
        assert is_chitchat_redirect(IntentLabel.FAQ, []) is False
        assert is_chitchat_redirect(IntentLabel.BILL_QUERY, []) is False

    def test_legacy_alias_intent_redirects(self) -> None:
        """旧 flat 别名 CHITCHAT ("chitchat") 与 NB_CHITCHAT 同判 (E2E 实测走别名)"""
        assert is_chitchat_redirect(IntentLabel.CHITCHAT, []) is True
        assert is_chitchat_redirect(IntentLabel.CHITCHAT, [IntentLabel.FAQ]) is True
        assert is_chitchat_redirect(IntentLabel.CHITCHAT, [IntentLabel.BILL_QUERY]) is False

    def test_weak_business_alt_score_redirects(self) -> None:
        """弱次选 (<0.30, softmax 对冲) 不再挡闲聊短路 — 会话 22ad 根治"""
        assert (
            is_chitchat_redirect(
                IntentLabel.CHITCHAT, [IntentLabel.TRANSFER_AGENT, IntentLabel.TRANSACTION_QUERY], [0.18, 0.12]
            )
            is True
        )

    def test_strong_business_alt_score_passes_through(self) -> None:
        """强次选 (≥0.30, 真混合句) 仍放行, 保护「哈哈帮我查下账单」"""
        assert is_chitchat_redirect(IntentLabel.CHITCHAT, [IntentLabel.BILL_QUERY], [0.45]) is False

    def test_missing_scores_conservative_passthrough(self) -> None:
        """无分数 (旧调用方) 保持保守放行, 不弱化混合句保护"""
        assert is_chitchat_redirect(IntentLabel.CHITCHAT, [IntentLabel.BILL_QUERY]) is False
        # 部分带分数: 有分数的按分数, 缺分数的按强处理
        assert (
            is_chitchat_redirect(IntentLabel.CHITCHAT, [IntentLabel.BILL_QUERY, IntentLabel.COMPLAINT], [0.1]) is False
        )


class TestEmergencyFaqExemption:
    """紧急意图豁免 FAQ 前置短路 (qa_scan 复盘: "钱包被偷"被硬钱包 FAQ 0.2s 劫持)"""

    def test_markers(self) -> None:
        from lumio.services.bot.bot_agent import _has_emergency_marker

        assert _has_emergency_marker("钱包被偷了, 卡也在里面") is True
        assert _has_emergency_marker("我的卡丢了, 要挂失啊") is True
        assert _has_emergency_marker("卡好像被盗了, 赶紧给我停了") is True
        assert _has_emergency_marker("信用卡怎么挂失") is True
        # 无紧急标记的常规咨询不受影响
        assert _has_emergency_marker("账单日是哪天") is False
        assert _has_emergency_marker("数字人民币硬钱包没电怎么办") is False
        assert _has_emergency_marker("") is False
