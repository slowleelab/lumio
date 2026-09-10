"""bot_agent 纯函数单元测试"""

from __future__ import annotations

import pytest

# ── 对话理解升级: 答案已在手复述守卫 ──


def test_answer_grounded_digits_guard() -> None:
    """复述守卫: 答案中的数字串必须逐个出现在来源里, 防转述走样。"""
    from lumio.services.bot.bot_agent import _answer_grounded

    src = "账单金额 8650.00元, 还款日 2026年7月25日"
    assert _answer_grounded("您的还款日为2026年7月25日", src) is True
    assert _answer_grounded("您的还款日为2026年7月26日", src) is False  # 日期被改
    assert _answer_grounded("账单金额是8650.00元", src) is True
    assert _answer_grounded("金额是9999元", src) is False  # 编造金额
    assert _answer_grounded("是的", src) is True  # 无数字短复述放行


@pytest.mark.asyncio
async def test_followup_context_gated_by_pending_reply() -> None:
    """回话轮闸: 机器人正等客户回答 (awaiting_slots/pending_action) 时, 不带改写上下文。

    该轮按定义是"回话"而非"新问题", 改写上下文直接不给 — 卡号补答被
    改写成问句的构造性消除 (长对话模拟 sim-long_bill_marathon 实测)。
    """
    from lumio.services.bot.bot_agent import LumioAgent
    from lumio.shared.orm_models import DialogueLog  # noqa: F401

    agent = LumioAgent.__new__(LumioAgent)

    class _SM:
        async def resolve_session_id(self, sid):
            return sid

        def __init__(self, state):
            self._state = state

        async def read_state(self, sid):
            return self._state

    history = [{"speaker": "customer", "content": "帮我查账单"}, {"speaker": "bot", "content": "请提供卡号"}]

    # 正常追问轮: 有上下文
    agent._session_manager = _SM({"last_tool_result": {"tool": "t", "summary": "还款日 2026-07-25"}, "awaiting_slots": {}, "pending_action": None})
    ctx = await agent._build_followup_context("s1", history)
    assert ctx is not None and "上一轮系统动作" in ctx

    # 待补槽轮: 无上下文 (回话交槽位状态机)
    agent._session_manager = _SM({"awaiting_slots": {"card_no": "..."}, "pending_action": None})
    assert await agent._build_followup_context("s1", history) is None

    # 待确认轮: 无上下文 (交确认状态机)
    agent._session_manager = _SM({"awaiting_slots": {}, "pending_action": {"action": "installment"}})
    assert await agent._build_followup_context("s1", history) is None
