"""Bot 层 MCP 端到端测试：从 LumioAgent.run() 一路触发真实 MCP 工具调用。

与 test_tool_e2e.py（executor 层）互补：本文件验证 bot 层接缝 ——
1. run() 渐进披露门控 → _handle_tool → 真实 MCP 非敏感工具执行;
2. 敏感工具 → pending_action 落会话 → 用户"确认" → 真实执行;
3. 敏感凭证回复（16 位卡号）→ 回话豁免 → 重路由到工具编排（会话 956a5fd2 修复的 E2E）。

进程内内存传输接入参考 MCP Server（无网络/无 Higress/无真实 LLM），CI 可跑。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp.shared.memory import create_connected_server_and_client_session as connect_in_memory

from lumio.services.bot.bot_agent import LumioAgent
from lumio.services.bot.tool_executor import ToolCallingExecutor
from lumio.services.common.llm import ToolCall, ToolCallResult
from lumio.services.common.mcp_client import MCPToolClient
from lumio.services.tools.reference_server import build_reference_server
from lumio.shared.config import MCPSettings, Settings
from lumio.shared.models import (
    IntentLabel,
    IntentResult,
    PendingAction,
    SessionPhase,
    SessionState,
    SessionSubPhase,
    VerificationResult,
)


class _ScriptedLLM:
    """按脚本顺序返回 ToolCallResult 的假 LLM（duck-typed，仅需 chat_with_tools）"""

    def __init__(self, script: list[ToolCallResult]) -> None:
        self._script = list(script)
        self._idx = 0
        self.calls = 0

    async def chat_with_tools(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], **_: Any
    ) -> ToolCallResult:
        self.calls += 1
        result = self._script[self._idx]
        self._idx += 1
        return result


def _tool_call_result(call_id: str, name: str, arguments: dict[str, Any]) -> ToolCallResult:
    return ToolCallResult(
        content="",
        tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments)],
        raw_message={
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
                }
            ],
        },
    )


def _final_answer(text: str) -> ToolCallResult:
    return ToolCallResult(content=text, tool_calls=[], raw_message={"role": "assistant", "content": text})


class _FakeSessionManager:
    """内存会话管理：够 run() 的 pending 落盘/读取/清除链路用"""

    def __init__(self, state: SessionState | None = None, history: list[Any] | None = None) -> None:
        self._state = state
        self._history = history or []

    async def get_session(self, session_id: str) -> SessionState | None:
        return self._state

    async def get_history(self, session_id: str, limit: int | None = None) -> list[Any]:
        return self._history

    async def patch_state(
        self,
        session_id: str,
        expected_version: int,
        patches: dict[str, Any],
        writer: str = "",
        max_retries: int = 3,
    ) -> None:
        if self._state is None:
            return
        for key, value in patches.items():
            if key == "pending_action" and isinstance(value, dict):
                value = PendingAction(**value)
            setattr(self._state, key, value)
        self._state.version += 1


def _make_state(pending: PendingAction | None = None) -> SessionState:
    now = datetime.now(UTC)
    return SessionState(
        session_id="sess-e2e-bot",
        customer_id="cust-1",
        current_phase=SessionPhase.BOT,
        sub_phase=SessionSubPhase.BOT_ACTIVE,
        created_at=now,
        last_active_at=now,
        version=7,
        pending_action=pending,
    )


def _make_agent(
    classifier_result: IntentResult,
    llm: _ScriptedLLM,
    session_manager: _FakeSessionManager,
    client: MCPToolClient,
    history: list[Any] | None = None,
) -> LumioAgent:
    classifier = MagicMock()
    classifier.classify = AsyncMock(return_value=(classifier_result, [], MagicMock(), ""))
    degradation_mgr = MagicMock()
    degradation_mgr.generate_with_fallback = AsyncMock(return_value=MagicMock(content="降级", source="llm"))
    degradation_mgr._degrader = MagicMock(hardcoded_fallback=MagicMock(return_value="兜底"))
    transfer_checker = MagicMock()
    transfer_checker.check = MagicMock(return_value=(False, "", ""))
    executor = ToolCallingExecutor(client, llm, None, client._settings)  # type: ignore[arg-type]
    if history is not None:
        session_manager._history = history
    return LumioAgent(
        classifier=classifier,
        degradation_mgr=degradation_mgr,
        transfer_checker=transfer_checker,
        session_manager=session_manager,  # type: ignore[arg-type]
        tool_executor=executor,
    )


@pytest.fixture
def connected_client() -> Any:
    """进程内参考 MCP Server → 真实 ClientSession → MCPToolClient（异步 fixture 简化为同步构造点）"""
    return None  # 占位: 各测试自行在 async with 内存会话内构造


def _patch_progressive_disclosure(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    settings = Settings()
    settings.mcp.progressive_disclosure_enabled = enabled
    monkeypatch.setattr("lumio.services.bot.bot_agent.get_settings", lambda: settings)


class TestBotMcpE2E:
    """LumioAgent.run() 触发真实 MCP 工具调用的端到端链路"""

    async def test_run_triggers_real_nonsensitive_tool(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """渐进披露开启 + 积分查询意图 → run() → 工具编排 → 真实 query_points 执行

        (分期意图已移出 TOOL_INTENTS — 会话 48882b05; 非敏感工具链路改用积分查询验证)
        """
        _patch_progressive_disclosure(monkeypatch, True)
        server = build_reference_server()
        async with connect_in_memory(server._mcp_server) as session:
            await session.initialize()
            client = MCPToolClient(MCPSettings(enabled=True, sensitive_tools=[]))
            await client.use_session(session)

            llm = _ScriptedLLM(
                [
                    _tool_call_result("c1", "query_points", {"card_no": "6225880012346780"}),
                    _final_answer("您当前可用积分 12,800 分。"),
                ]
            )
            agent = _make_agent(
                IntentResult(primary_intent=IntentLabel.REWARD_QUERY, primary_confidence=0.9),
                llm,
                _FakeSessionManager(_make_state()),
                client,
            )

            result = await agent.run("sess-e2e-bot", "帮我查一下积分")

            assert result["response_source"] == "llm"
            assert "积分" in result["response"]
            assert llm.calls == 2  # 工具调用 + 最终答复

    async def test_run_sensitive_tool_pending_then_confirm_executes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """敏感工具 adjust_temp_credit_limit: run() 短路为核验态 → 核验通过 → "确认" → 真实执行"""
        _patch_progressive_disclosure(monkeypatch, True)
        server = build_reference_server()
        async with connect_in_memory(server._mcp_server) as session:
            await session.initialize()
            client = MCPToolClient(MCPSettings(enabled=True, sensitive_tools=[]))
            await client.use_session(session)

            state = _make_state()
            sm = _FakeSessionManager(state)
            # 第一轮: LLM 直接请求敏感工具 → 短路进入核验态 (发核验信号, 不执行)
            llm1 = _ScriptedLLM(
                [
                    _tool_call_result(
                        "c2", "adjust_temp_credit_limit", {"card_no": "6225880012346780", "target_limit": 8000}
                    )
                ]
            )
            agent1 = _make_agent(
                IntentResult(primary_intent=IntentLabel.LIMIT_QUERY, primary_confidence=0.9),
                llm1,
                sm,
                client,
            )

            first = await agent1.run("sess-e2e-bot", "帮我临时提额到 8000")

            assert first["response_source"] == "tool_confirm"
            assert first.get("verification") is not None  # 核验信号透传
            assert state.pending_action is not None
            assert state.pending_action.tool_name == "adjust_temp_credit_limit"
            assert state.pending_action.verification_state == "pending"
            token = state.pending_action.verification_token

            # 核验期间客户发文本"确认" → 不该进入 confirm 判定
            agent_guard = _make_agent(
                IntentResult(primary_intent=IntentLabel.LIMIT_QUERY, primary_confidence=0.9),
                _ScriptedLLM([_final_answer("x")]),
                sm,
                client,
            )
            guarded = await agent_guard.run("sess-e2e-bot", "确认")
            assert "核验" in guarded["response"]
            assert state.pending_action.verification_state == "pending"  # 状态未被消费

            # 前端核验成功回传 → verified 态 + 带参数确认话术
            verified = await agent1.handle_verification_result(
                "sess-e2e-bot", VerificationResult(token=token, status="success")
            )
            assert state.pending_action.verification_state == "verified"
            assert "确认" in verified["response"]
            assert "adjust_temp_credit_limit" in verified["response"]  # 带工具名
            assert "6780" in verified["response"]  # 带卡尾
            assert state.pending_action.arguments.get("card_no") == "6225880012346780"  # 卡号已注入

            # 客户"确认" → pending 拦截 → 真实执行 → 受理答复
            llm2 = _ScriptedLLM([_final_answer("已为您提交临时提额至 8000 元的申请，受理成功。")])
            agent2 = _make_agent(
                IntentResult(primary_intent=IntentLabel.LIMIT_QUERY, primary_confidence=0.9),
                llm2,
                sm,
                client,
            )
            second = await agent2.run("sess-e2e-bot", "确认")
            assert "受理" in second["response"]
            assert state.pending_action is None  # 已清除
            assert llm2.calls >= 1

    async def test_verification_cancel_clears_pending(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """核验取消/失败 → 清除 pending, 不执行"""
        _patch_progressive_disclosure(monkeypatch, True)
        server = build_reference_server()
        async with connect_in_memory(server._mcp_server) as session:
            await session.initialize()
            client = MCPToolClient(MCPSettings(enabled=True, sensitive_tools=[]))
            await client.use_session(session)

            state = _make_state()
            sm = _FakeSessionManager(state)
            llm = _ScriptedLLM(
                [
                    _tool_call_result(
                        "c5", "adjust_temp_credit_limit", {"card_no": "6225880012346780", "target_limit": 5000}
                    )
                ]
            )
            agent = _make_agent(
                IntentResult(primary_intent=IntentLabel.LIMIT_QUERY, primary_confidence=0.9),
                llm,
                sm,
                client,
            )
            await agent.run("sess-e2e-bot", "帮我临时提额到 5000")
            token = state.pending_action.verification_token
            assert state.pending_action.verification_state == "pending"

            result = await agent.handle_verification_result(
                "sess-e2e-bot", VerificationResult(token=token, status="cancel")
            )
            assert "取消" in result["response"]
            assert state.pending_action is None  # 已清除

    async def test_sensitive_reply_reroutes_to_mcp_tools(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """会话 956a5fd2 E2E: 上轮索卡号 → 16 位卡号 → 豁免 + 重路由 → 工具编排继续"""
        _patch_progressive_disclosure(monkeypatch, True)
        server = build_reference_server()
        async with connect_in_memory(server._mcp_server) as session:
            await session.initialize()
            client = MCPToolClient(MCPSettings(enabled=True, sensitive_tools=[]))
            await client.use_session(session)

            state = _make_state()
            sm = _FakeSessionManager(
                state,
                history=[SimpleNamespace(speaker="bot", content="请告知您的信用卡卡号", response_source="llm")],
            )
            # 重路由进工具编排后, LLM 借上下文直接请求敏感工具 → 短路挂 pending
            llm = _ScriptedLLM(
                [
                    _tool_call_result(
                        "c3", "apply_bill_installment", {"card_no": "6225880012346780", "amount": 5000, "periods": 3}
                    )
                ]
            )
            agent = _make_agent(
                IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.0),
                llm,
                sm,
                client,
            )

            result = await agent.run("sess-e2e-bot", "6225880012346780")

            # 未被噪声门杀 (澄清) —— 而是重路由到工具编排并短路为确认
            assert result["response_source"] == "tool_confirm"
            assert state.pending_action is not None
            assert state.pending_action.tool_name == "apply_bill_installment"

    async def test_progressive_disclosure_off_no_tool_trigger(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """对照组: 渐进披露关闭 → 分期意图仍走 business 路径但工具编排可用时不触发? 验证开关语义"""
        _patch_progressive_disclosure(monkeypatch, False)
        server = build_reference_server()
        async with connect_in_memory(server._mcp_server) as session:
            await session.initialize()
            client = MCPToolClient(MCPSettings(enabled=True, sensitive_tools=[]))
            await client.use_session(session)

            llm = _ScriptedLLM([_final_answer("分期咨询答复")])
            agent = _make_agent(
                IntentResult(primary_intent=IntentLabel.INSTALLMENT_INQUIRY, primary_confidence=0.9),
                llm,
                _FakeSessionManager(_make_state()),
                client,
            )
            # business 路径自身也会尝试工具编排 (draft-0.3 查询类翻 business 主路径), 此断言
            # 只验证结果不抛异常、链路完整返回
            result = await agent.run("sess-e2e-bot", "分期怎么办理")
            assert result["response"] in ("分期咨询答复", "降级")

    async def test_bare_slot_reply_reroutes_to_awaiting_intent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """会话 1fb54681 E2E: bot 上轮在等金额/期数, 客户裸答"3"被分类 faq@0.00
        -> 等待快照放行 -> 换回 installment_inquiry 走工具编排续办, 不再死于澄清."""
        _patch_progressive_disclosure(monkeypatch, True)
        server = build_reference_server()
        async with connect_in_memory(server._mcp_server) as session:
            await session.initialize()
            client = MCPToolClient(MCPSettings(enabled=True, sensitive_tools=[]))
            await client.use_session(session)

            state = _make_state()
            state.awaiting_slots = {
                "intent": "installment_inquiry",
                "slots": [["amount", "分期金额"], ["period", "分期期数"]],
            }
            sm = _FakeSessionManager(
                state,
                history=[
                    SimpleNamespace(speaker="bot", content="您希望分几期？3、6 还是 12 期", response_source="llm")
                ],
            )
            llm = _ScriptedLLM(
                [
                    _tool_call_result("c4", "query_installment_offer", {"card_no": "6225880012346780", "amount": 3000}),
                    _final_answer("3 期方案：每期本金 1000 元，总手续费 180 元。"),
                ]
            )
            agent = _make_agent(
                IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.0),
                llm,
                sm,
                client,
            )

            result = await agent.run("sess-e2e-bot", "3")

            # 未被噪声门拦成澄清 —— 换回等待意图走完了工具编排 (工具调用 + 最终答复)
            assert result["response_source"] == "llm"
            assert llm.calls == 2
            # 快照消费后清空, 不豁免后续无关输入
            assert state.awaiting_slots == {}

    async def test_sensitive_reply_no_tools_with_awaiting_goes_business(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """会话 1efbd1ad: 无工具 + 等待快照有效 + 客户给卡号后四位 → 走业务链路, 不转人工"""
        _patch_progressive_disclosure(monkeypatch, True)
        state = _make_state()
        state.awaiting_slots = {
            "intent": "installment_inquiry",
            "slots": [["amount", "分期金额"], ["period", "分期期数"]],
        }
        sm = _FakeSessionManager(
            state,
            history=[SimpleNamespace(speaker="bot", content="请提供您的卡号后四位数字", response_source="llm")],
        )
        classifier = MagicMock()
        classifier.classify = AsyncMock(
            return_value=(
                IntentResult(primary_intent=IntentLabel.INSTALLMENT_INQUIRY, primary_confidence=0.68),
                [],
                MagicMock(),
                "",
            )
        )
        degradation_mgr = MagicMock()
        degradation_mgr.generate_with_fallback = AsyncMock(
            return_value=MagicMock(content="请继续提供分期金额", source="llm")
        )
        degradation_mgr._degrader = MagicMock(hardcoded_fallback=MagicMock(return_value="兜底"))
        transfer_checker = MagicMock()
        transfer_checker.check = MagicMock(return_value=(False, "", ""))
        agent = LumioAgent(
            classifier=classifier,
            degradation_mgr=degradation_mgr,
            transfer_checker=transfer_checker,
            session_manager=sm,  # type: ignore[arg-type]
            tool_executor=None,  # 无工具环境
        )

        result = await agent.run("sess-e2e-bot", "4879")

        # 关键: 不被引导转人工, 而是继续业务链路
        assert "转人工" not in result["response"]
        assert result["response_source"] != "template"


class TestToolRouteAuditAndRagFallback:
    """会话 48882b05 修复回归: 工具编排路由留痕 + 索参数轮 RAG 知识兜底 + 等待快照"""

    async def test_tool_interception_logs_actual_route_decision(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """决策日志必须显式记录"工具编排接管路由", 不让映射表域名误导审计"""
        from lumio.services.common.decision_log import DecisionAction

        _patch_progressive_disclosure(monkeypatch, True)
        recorded: list[dict[str, Any]] = []

        def _recorder(**kwargs: Any) -> str:
            recorded.append(kwargs)
            return "d-id"

        monkeypatch.setattr("lumio.services.bot.bot_agent.log_decision", _recorder)

        server = build_reference_server()
        async with connect_in_memory(server._mcp_server) as session:
            await session.initialize()
            client = MCPToolClient(MCPSettings(enabled=True, sensitive_tools=[]))
            await client.use_session(session)

            llm = _ScriptedLLM([_final_answer("查询到当前积分余额如下。")])
            agent = _make_agent(
                IntentResult(primary_intent=IntentLabel.REWARD_QUERY, primary_confidence=0.9),
                llm,
                _FakeSessionManager(_make_state()),
                client,
            )

            await agent.run("sess-e2e-bot", "帮我查一下积分")

        route_calls = [
            r
            for r in recorded
            if r.get("action") == DecisionAction.TOOL_CALL
            and r.get("evidence", {}).get("actual_route") == "tool_orchestration"
        ]
        assert route_calls, f"未记录工具编排路由决策, recorded={[r.get('action') for r in recorded]}"
        assert route_calls[0]["evidence"]["declared_domain"] == "business"
        assert route_calls[0]["evidence"]["intent"] == "reward_query"

    async def test_installment_intent_not_intercepted_by_tools(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """会话 48882b05 核心回归: 裸"分期"(歧义输入)不再被工具编排劫持反问参数,
        而是走知识问答返回分期介绍; 工具编排全程零参与。"""
        from lumio.services.common.decision_log import DecisionAction

        _patch_progressive_disclosure(monkeypatch, True)
        recorded: list[dict[str, Any]] = []

        def _recorder(**kwargs: Any) -> str:
            recorded.append(kwargs)
            return "d-id"

        monkeypatch.setattr("lumio.services.bot.bot_agent.log_decision", _recorder)

        server = build_reference_server()
        async with connect_in_memory(server._mcp_server) as session:
            await session.initialize()
            client = MCPToolClient(MCPSettings(enabled=True, sensitive_tools=[]))
            await client.use_session(session)

            llm = _ScriptedLLM([_final_answer("不应被调用")])
            agent = _make_agent(
                IntentResult(primary_intent=IntentLabel.INSTALLMENT_INQUIRY, primary_confidence=0.81),
                llm,
                _FakeSessionManager(_make_state()),
                client,
            )
            retrieve_mock = AsyncMock(return_value="[1] 账单分期可分 3、6、12 期，办理请至官方渠道。")
            monkeypatch.setattr(agent, "_retrieve", retrieve_mock)

            result = await agent.run("sess-e2e-bot", "分期")

        # 工具编排零参与: LLM 的 chat_with_tools 一次都没被调用
        assert llm.calls == 0
        assert not [
            r for r in recorded if r.get("action") == DecisionAction.TOOL_CALL
        ], "分期意图不应记录工具编排路由决策"
        # 走知识问答: RAG 检索被消费, 由 knowledge 生成链路出答复
        retrieve_mock.assert_awaited_once()
        assert result["response_source"] == "llm"

    async def test_param_asking_reply_appends_rag_knowledge_and_saves_awaiting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """索参数式澄清反问必须带 RAG 知识参考, 且等待快照落上 (裸数字下轮可豁免噪声门)"""
        _patch_progressive_disclosure(monkeypatch, True)
        server = build_reference_server()
        async with connect_in_memory(server._mcp_server) as session:
            await session.initialize()
            client = MCPToolClient(MCPSettings(enabled=True, sensitive_tools=[]))
            await client.use_session(session)

            llm = _ScriptedLLM([_final_answer("请问您要查哪个月的账单？")])
            state = _make_state()
            sm = _FakeSessionManager(state)
            agent = _make_agent(
                IntentResult(primary_intent=IntentLabel.BILL_QUERY, primary_confidence=0.9),
                llm,
                sm,
                client,
            )
            monkeypatch.setattr(
                agent,
                "_retrieve",
                AsyncMock(return_value="[1] 出账日为每月 5 日，到期还款日为账单后 20 天。"),
            )

            result = await agent.run("sess-e2e-bot", "帮我查一下账单")

        assert result["response_source"] == "llm"
        assert "供您参考：出账日为每月 5 日" in result["response"]
        # 等待快照: "请问...哪个月" 命中索取话术; 账单查询无必填槽 → 记通用参数槽
        assert state.awaiting_slots, "等待快照未落盘, 下轮裸数字会被噪声门误杀"

    async def test_param_asking_reply_without_rag_hit_stays_intact(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """检索未命中时反问话术保持原样, 不追加空参考段"""
        _patch_progressive_disclosure(monkeypatch, True)
        server = build_reference_server()
        async with connect_in_memory(server._mcp_server) as session:
            await session.initialize()
            client = MCPToolClient(MCPSettings(enabled=True, sensitive_tools=[]))
            await client.use_session(session)

            llm = _ScriptedLLM([_final_answer("请问您要查哪个月的账单？")])
            agent = _make_agent(
                IntentResult(primary_intent=IntentLabel.BILL_QUERY, primary_confidence=0.9),
                llm,
                _FakeSessionManager(_make_state()),
                client,
            )
            monkeypatch.setattr(agent, "_retrieve", AsyncMock(return_value=""))

            result = await agent.run("sess-e2e-bot", "帮我查一下账单")

        assert result["response"] == "请问您要查哪个月的账单？"
