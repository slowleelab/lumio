"""P1 反问确认跟进 + P2 噪声门提前 (会话 b561cd04 死胡同复盘) 测试。

场景: bot 上轮自由反问("需要帮助查询吗?") → 用户回"是的"被 faq@0.25 低置信澄清,
对话断裂。修复后确认词走轻量跟进(confirm_followup), 不进澄清/检索;
且低置信/分歧的输入在检索之前就被噪声门拦回 (不再白付 RAG 检索)。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from lumio.services.bot.bot_agent import (
    LumioAgent,
    _is_confirm_after_question,
    _last_bot_turn_asked,
)
from lumio.services.bot.prompts import CONFIRM_FOLLOWUP_RESPONSE
from lumio.shared.models import IntentLabel, IntentResult


@pytest.fixture(autouse=True)
def _pin_v1_routing(monkeypatch):
    """本文件基于 v1 链路 mock 编写: 显式关闭 v2 路由, 不随部署 env 漂移。

    v2 分派的专测见 test_routing_v2.py / test_query_chain.py。
    """

    pass  # v1 链路已删除 (2026-09-04): v2 是唯一路径, 无需再 pin


# ── 纯函数: 上轮反问检测 ──


class TestLastBotTurnAsked:
    def test_question_marks(self) -> None:
        assert _last_bot_turn_asked([{"speaker": "bot", "content": "需要帮助查询吗？"}]) is True
        assert _last_bot_turn_asked([{"speaker": "bot", "content": "需要帮助查询吗?"}]) is True

    def test_ma_suffix_without_question_mark(self) -> None:
        # 结尾 8 字内含 "吗" 的口语问句也算反问
        assert _last_bot_turn_asked([{"speaker": "bot", "content": "需要帮助查询吗"}]) is True

    def test_statement_is_not_question(self) -> None:
        assert _last_bot_turn_asked([{"speaker": "bot", "content": "好的，已为您办理"}]) is False
        # 槽位追问(陈述式)不带问号 → 不是自由反问
        assert _last_bot_turn_asked([{"speaker": "bot", "content": "请提供您信用卡的后四位以便验证身份"}]) is False

    def test_empty_and_customer_last(self) -> None:
        assert _last_bot_turn_asked(None) is False
        assert _last_bot_turn_asked([]) is False
        assert _last_bot_turn_asked([{"speaker": "customer", "content": "好的"}]) is False
        # 最近一条 bot 之后客户又说过话 → 不再算"上轮反问"
        assert (
            _last_bot_turn_asked(
                [
                    {"speaker": "bot", "content": "需要帮助吗？"},
                    {"speaker": "customer", "content": "随便说说"},
                ]
            )
            is False
        )


class TestIsConfirmAfterQuestion:
    def test_confirm_after_question_no_slots(self) -> None:
        history = [{"speaker": "bot", "content": "需要帮助查询吗？"}]
        assert _is_confirm_after_question("是的", history, []) is True
        assert _is_confirm_after_question("好的", history, None) is True

    def test_missing_slots_keeps_original_path(self) -> None:
        # 有缺槽 = 上文是槽位追问, 确认词沿用回话豁免原链路, 不抢本分支
        history = [{"speaker": "bot", "content": "请问您想分期的金额是多少？"}]
        assert _is_confirm_after_question("是的", history, [("amount", "分期金额")]) is False

    def test_not_confirmation_or_not_question(self) -> None:
        history = [{"speaker": "bot", "content": "需要帮助查询吗？"}]
        assert _is_confirm_after_question("帮我查账单", history, []) is False  # 新意图, 不是确认词
        assert _is_confirm_after_question("是的", [{"speaker": "bot", "content": "已为您办理"}], []) is False


# ── 集成: knowledge 路径确认词轻量跟进 + 拦截不检索 ──


def _make_agent(intent: IntentResult, history: list[dict]) -> tuple[LumioAgent, MagicMock]:
    from types import SimpleNamespace

    classifier = MagicMock()
    classifier.classify = AsyncMock(return_value=(intent, [], MagicMock(), ""))
    degradation_mgr = MagicMock()
    degradation_mgr.generate_with_fallback = AsyncMock(return_value=MagicMock(content="RAG 知识回复", source="llm"))
    degradation_mgr._degrader = MagicMock()
    degradation_mgr._degrader.hardcoded_fallback = MagicMock(return_value="兜底")
    transfer_checker = MagicMock()
    transfer_checker.check = MagicMock(return_value=(False, "", ""))
    session_manager = MagicMock()
    # _load_history 按属性访问轮次 (.speaker/.content), 与真实 session.get_history 返回一致
    turns = [SimpleNamespace(**t) for t in history]
    session_manager.get_history = AsyncMock(return_value=turns)
    session_manager.get_session = AsyncMock(return_value=None)
    agent = LumioAgent(
        classifier=classifier,
        degradation_mgr=degradation_mgr,
        transfer_checker=transfer_checker,
        session_manager=session_manager,
    )
    return agent, session_manager


@pytest.mark.asyncio
async def test_confirm_after_question_returns_followup_without_retrieve() -> None:
    """会话 b561cd04 第 4 轮复现: bot 反问后回"是的" → 跟进话术, 不检索不清醒."""
    agent, _ = _make_agent(
        IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.25),
        [{"speaker": "bot", "content": "今天的天气情况我不确定……需要帮助查询吗？"}],
    )
    agent._retrieve = AsyncMock(side_effect=AssertionError("确认跟进不应触发检索"))

    result = await agent.run("s1", "是的")

    assert result["response_source"] == "confirm_followup"
    assert result["response"] == CONFIRM_FOLLOWUP_RESPONSE
    agent._retrieve.assert_not_awaited()


@pytest.mark.asyncio
async def test_low_confidence_blocked_before_retrieve() -> None:
    """P2: 低置信输入在检索之前被噪声门拦回澄清, 零 RAG 浪费 (会话第 1/4 轮同款)."""
    agent, _ = _make_agent(
        IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.2),
        [],  # 无反问历史
    )
    agent._retrieve = AsyncMock(side_effect=AssertionError("低置信拦截不应触发检索"))

    result = await agent.run("s1", "乱码输入 hjfw")

    assert result["response_source"] == "clarify"
    agent._retrieve.assert_not_awaited()


@pytest.mark.asyncio
async def test_normal_question_still_retrieves() -> None:
    """正常知识提问(高置信 faq)不触发确认分支, 检索照常."""
    agent, _ = _make_agent(
        IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.8),
        [],
    )
    agent._retrieve = AsyncMock(return_value="年费减免政策: 刷满 6 次免年费")

    result = await agent.run("s1", "年费怎么免")

    agent._retrieve.assert_awaited_once()
    assert result["response"] == "RAG 知识回复"


class TestLastBotTurnAskedRoleFormat:
    def test_role_format_compat(self) -> None:
        # _load_history 归一化后的 role 格式 (assistant/user) 同样生效
        assert _last_bot_turn_asked([{"role": "assistant", "content": "需要帮助查询吗？"}]) is True
        assert _last_bot_turn_asked([{"role": "assistant", "content": "好的，已为您办理"}]) is False
        assert _last_bot_turn_asked([{"role": "user", "content": "好的"}]) is False


# ── 澄清话术轮换 (会话 bcf51ded: 连续两轮一字不差的生硬话术) ──


class TestClarifyRotation:
    """按 low_confidence_streak 轮换澄清话术; 无会话状态回退旧常量."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("streak", "expected"),
        [
            (0, "抱歉，我没太明白您的意思，您能换个说法吗？"),  # 首轮即软化变体
            (1, "您好，这条我没能理解。您是想了解账单、额度，还是其他业务呢？"),
            (2, "我没太听明白，您可以直接说想办的事，比如“帮我查账单”。"),
            (
                3,
                "这几次似乎还没能帮您解决问题，需要为您转接人工客服吗？（回复“是”或“需要”即可）",
            ),  # 阈值轮: 澄清升级为转人工邀约 (L3 设计)
            (4, "您好，这条我没能理解。您是想了解账单、额度，还是其他业务呢？"),  # 邀约轮不占澄清轮, 4%3=1
            (5, "我没太听明白，您可以直接说想办的事，比如“帮我查账单”。"),  # 5%3=2
        ],
    )
    async def test_rotation_by_streak(self, streak: int, expected: str) -> None:
        from types import SimpleNamespace

        agent, session_manager = _make_agent(
            IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.2),
            [],  # 无反问历史 → 走噪声门低置信 → 澄清
        )
        session_manager.get_session = AsyncMock(
            return_value=SimpleNamespace(
                low_confidence_streak=streak,
                pending_action=None,
                slot_values={},
                last_entities=[],
                entity_pool=[],
                intent_stack=[],
                last_intent=None,
                conversation_summary="",
                summary_turn_count=0,
                last_summarized_turn_id="",
                version=1,
            )
        )
        agent._retrieve = AsyncMock(side_effect=AssertionError("拦截不检索"))

        result = await agent.run("s1", "乱码输入")

        assert result["response_source"] == "clarify"
        assert result["response"] == expected
        agent._retrieve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fallback_when_state_unavailable(self) -> None:
        agent, _ = _make_agent(
            IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.2),
            [],
        )
        agent._retrieve = AsyncMock(return_value="")
        result = await agent.run("s1", "乱码输入")  # get_session=None → 回退旧常量
        assert result["response_source"] == "clarify"
        assert result["response"] == "您的意思我还没太理解。"


# ── P-a 敏感索取回话豁免 + 桥接 (会话 956a5fd2: "卡号后四位"→"8765" 被噪声门双杀) ──


class TestSensitiveAskExemption:
    def test_last_bot_turn_asked_sensitive(self) -> None:
        from lumio.services.bot.bot_agent import _last_bot_turn_asked_sensitive

        ask = [{"speaker": "bot", "content": "请提供一下您的卡号后四位数字"}]
        assert _last_bot_turn_asked_sensitive(ask) == "卡号后四位"
        # 完整卡号索取 (MCP 工具编排 schema 话术) → 裸"卡号"全卡类
        assert _last_bot_turn_asked_sensitive([{"speaker": "bot", "content": "请告知您的信用卡卡号"}]) == "卡号"
        # role 格式兼容 (_load_history 归一化后)
        assert _last_bot_turn_asked_sensitive([{"role": "assistant", "content": "请输入验证码"}]) == "验证码"
        # 长短语优先: "卡号后四位" 命中短类而非裸"卡号"全卡类
        assert _last_bot_turn_asked_sensitive([{"role": "assistant", "content": "请提供卡号后四位"}]) == "卡号后四位"
        # 无索取短语 → None
        assert _last_bot_turn_asked_sensitive([{"speaker": "bot", "content": "好的，已为您办理"}]) is None
        # 最近一条是客户消息 → None
        assert (
            _last_bot_turn_asked_sensitive(
                [
                    {"speaker": "bot", "content": "请提供卡号后四位"},
                    {"speaker": "customer", "content": "等一下"},
                ]
            )
            is None
        )
        assert _last_bot_turn_asked_sensitive(None) is None

    @pytest.mark.asyncio
    async def test_digits_after_sensitive_ask_bridge_reply(self) -> None:
        """会话 956a5fd2 末轮复现: 上轮索卡号 → 本轮回 4 位数字 → 桥接回执, 不澄清不检索."""
        agent, _ = _make_agent(
            IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.0),
            [{"speaker": "bot", "content": "请问您确认要办理吗？请提供一下您的卡号后四位数字"}],
        )
        agent._retrieve = AsyncMock(side_effect=AssertionError("桥接不应触发检索"))

        result = await agent.run("s1", "8765")

        from lumio.services.bot.prompts import SENSITIVE_REPLY_BRIDGE_RESPONSE

        assert result["response"] == SENSITIVE_REPLY_BRIDGE_RESPONSE
        assert result["response_source"] == "template"
        agent._retrieve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_non_digit_after_sensitive_ask_still_clarified(self) -> None:
        """索取豁免只认 4-6 位纯数字; 乱码照常澄清."""
        agent, _ = _make_agent(
            IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.0),
            [{"speaker": "bot", "content": "请提供卡号后四位"}],
        )
        agent._retrieve = AsyncMock(return_value="")
        result = await agent.run("s1", "hjfw")
        assert result["response_source"] == "clarify"
        agent._retrieve.assert_not_awaited()


# ── P-b 明确业务轮跳过 L3 邀约 ──


class TestConfidentBusinessIntent:
    def test_business_intent_detection(self) -> None:
        from lumio.services.bot.bot_agent import LumioAgent

        assert (
            LumioAgent._is_confident_business_intent(
                IntentResult(primary_intent=IntentLabel.INSTALLMENT_INQUIRY, primary_confidence=0.8)
            )
            is True
        )
        # 泛化兜底类不算业务意图
        assert (
            LumioAgent._is_confident_business_intent(
                IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.9)
            )
            is False
        )
        assert (
            LumioAgent._is_confident_business_intent(
                IntentResult(primary_intent=IntentLabel.CHITCHAT, primary_confidence=0.9)
            )
            is False
        )
        # 低置信业务不算
        assert (
            LumioAgent._is_confident_business_intent(
                IntentResult(primary_intent=IntentLabel.INSTALLMENT_INQUIRY, primary_confidence=0.3)
            )
            is False
        )
        assert LumioAgent._is_confident_business_intent(None) is False


# ── P-a 升级: 工具可用时敏感凭证回复交回 MCP 工具编排 (方案 B, 状态机背书) ──


class TestSensitiveRerouteToTools:
    @pytest.mark.asyncio
    async def test_reroute_to_tool_when_tools_available(self) -> None:
        """工具可用: "8765" 交回工具编排续办 (apply_bill_installment 状态机背书),
        不落桥接话术、不检索."""
        from lumio.services.bot.tool_executor import ToolExecutionResult

        agent, _ = _make_agent(
            IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.0),
            [{"speaker": "bot", "content": "请提供您的卡号后四位"}],
        )
        te = MagicMock()
        te.has_tools = MagicMock(return_value=True)
        te.run_conversation = AsyncMock(
            return_value=ToolExecutionResult(content="已为您登记分期申请，请确认是否办理", source="tool")
        )
        agent._tool_executor = te
        agent._retrieve = AsyncMock(side_effect=AssertionError("重路由不应检索"))

        result = await agent.run("s1", "8765")

        te.run_conversation.assert_awaited_once()
        assert result["response"] == "已为您登记分期申请，请确认是否办理"
        agent._retrieve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_no_tools_falls_back_to_bridge(self) -> None:
        """无工具环境: 回确定性桥接话术 (既有行为不变)."""
        from lumio.services.bot.prompts import SENSITIVE_REPLY_BRIDGE_RESPONSE

        agent, _ = _make_agent(
            IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.0),
            [{"speaker": "bot", "content": "请提供卡号后四位"}],
        )
        agent._retrieve = AsyncMock(return_value="")

        result = await agent.run("s1", "8765")

        assert result["response"] == SENSITIVE_REPLY_BRIDGE_RESPONSE
        agent._retrieve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_tool_failure_no_infinite_loop(self) -> None:
        """工具编排持续失败 → 防循环标志生效, 落桥接话术而非死循环."""
        from lumio.services.bot.prompts import SENSITIVE_REPLY_BRIDGE_RESPONSE

        agent, _ = _make_agent(
            IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.0),
            [{"speaker": "bot", "content": "请提供卡号后四位"}],
        )
        te = MagicMock()
        te.has_tools = MagicMock(return_value=True)
        te.run_conversation = AsyncMock(side_effect=RuntimeError("mcp down"))
        agent._tool_executor = te
        agent._retrieve = AsyncMock(return_value="")

        result = await agent.run("s1", "8765")

        assert result["response"] == SENSITIVE_REPLY_BRIDGE_RESPONSE
        # run_conversation 只被调用一次 (第二次进入 knowledge 带防循环标志, 不再重路由)
        assert te.run_conversation.await_count == 1


# ── P-b 集成: 明确业务轮 L3 不追加邀约 (会话 956a5fd2 第 9 轮复现) ──


@pytest.mark.asyncio
async def test_l3_offer_skipped_for_confident_business_intent() -> None:
    """分期@0.8 且 L3 触发 → 正常业务回复, 尾部不追加"是否转人工"邀约."""
    from lumio.services.common.transfer import TransferTriggerLevel
    from lumio.shared.models import SentimentLabel

    agent, _ = _make_agent(IntentResult(primary_intent=IntentLabel.INSTALLMENT_INQUIRY, primary_confidence=0.8), [])
    agent._retrieve = AsyncMock(return_value="")
    agent._transfer_checker.check = MagicMock(return_value=(False, TransferTriggerLevel.L3, "streak"))
    agent._degradation_mgr.generate_with_fallback = AsyncMock(
        return_value=MagicMock(content="分期介绍内容", source="llm")
    )

    result = await agent._handle_knowledge(
        "s1",
        "分期",
        IntentResult(primary_intent=IntentLabel.INSTALLMENT_INQUIRY, primary_confidence=0.8),
        [],
        [],
        SentimentLabel.NEUTRAL,
    )

    assert result["response"] == "分期介绍内容"
    assert "转人工" not in result["response"]
    assert result["response_source"] == "llm"


@pytest.mark.asyncio
async def test_l3_offer_still_appends_for_non_business() -> None:
    """对照: faq@0.5 且 L3 触发 → 邀约照常追加 (原 L3 语义不受影响)."""
    from lumio.services.common.transfer import TransferTriggerLevel
    from lumio.shared.models import SentimentLabel

    agent, _ = _make_agent(IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.5), [])
    agent._retrieve = AsyncMock(return_value="")
    agent._transfer_checker.check = MagicMock(return_value=(False, TransferTriggerLevel.L3, "streak"))
    agent._degradation_mgr.generate_with_fallback = AsyncMock(return_value=MagicMock(content="通用回答", source="llm"))

    result = await agent._handle_knowledge(
        "s1",
        "随便问",
        IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.8),
        [{"role": "user", "content": "之前聊过几句"}],  # 有对话依据, 过无检索依据门
        [],
        SentimentLabel.NEUTRAL,
    )

    assert "需要为您转接人工客服" in result["response"]


@pytest.mark.asyncio
async def test_full_card_digits_after_tool_ask() -> None:
    """MCP 工具编排索要完整卡号 → 16 位数字 → 豁免生效 (E2E 联调发现的第二类断点)."""
    from lumio.services.bot.prompts import SENSITIVE_REPLY_BRIDGE_RESPONSE

    agent, _ = _make_agent(
        IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.0),
        [{"speaker": "bot", "content": "请告知您的信用卡卡号"}],
    )
    agent._retrieve = AsyncMock(return_value="")

    result = await agent.run("s1", "6225880012346780")

    assert result["response"] == SENSITIVE_REPLY_BRIDGE_RESPONSE
    agent._retrieve.assert_not_awaited()
