"""渐进式工具暴露选择器单元测试"""

from __future__ import annotations

import pytest

from lumio.services.bot.tool_selection import TOOL_INTENTS, select_tools_for_intent
from lumio.shared.config import MCPSettings
from lumio.shared.models import IntentLabel


class TestConfigDefaults:
    """MCPSettings 渐进式暴露相关默认值（零回归）"""

    def test_disabled_by_default(self, monkeypatch) -> None:
        # config 导入时 load_dotenv 已把 .env 灌进 os.environ (本地联调常开 MCP),
        # 断言代码默认值前需摘掉对应环境变量
        monkeypatch.delenv("MCP_PROGRESSIVE_DISCLOSURE_ENABLED", raising=False)
        m = MCPSettings()
        assert m.progressive_disclosure_enabled is False
        assert m.pd_confidence_threshold == 0.7

    def test_default_intent_tool_map_covers_query_intents_and_card_loss(self) -> None:
        """五类查询意图 + card_loss (高置信直连, 挂失链跳过 LLM 编排)"""
        m = MCPSettings()
        assert set(m.intent_tool_map) == {
            "bill_query",
            "transaction_query",
            "limit_query",
            "installment_inquiry",
            "reward_query",
            "card_loss",
        }
        # 每个意图都有非空工具子集
        assert all(tools for tools in m.intent_tool_map.values())

    def test_env_prefix_maps_flag(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MCP_PROGRESSIVE_DISCLOSURE_ENABLED", "true")
        assert MCPSettings().progressive_disclosure_enabled is True


class TestSelectToolsForIntent:
    def _settings(self, **overrides: object) -> MCPSettings:
        base = {
            "progressive_disclosure_enabled": True,
            "pd_confidence_threshold": 0.7,
        }
        base.update(overrides)
        return MCPSettings(**base)  # type: ignore[arg-type]

    def test_disabled_returns_none(self) -> None:
        """开关关闭 → None（暴露全量，零回归）"""
        s = MCPSettings(progressive_disclosure_enabled=False)
        assert select_tools_for_intent(IntentLabel.BILL_QUERY, 0.99, s) is None

    def test_hit_high_confidence_returns_subset(self) -> None:
        s = self._settings()
        tools = select_tools_for_intent(IntentLabel.BILL_QUERY, 0.9, s)
        assert tools == s.intent_tool_map["bill_query"]
        # 返回副本，避免调用方修改配置
        assert tools is not s.intent_tool_map["bill_query"]

    def test_low_confidence_returns_none(self) -> None:
        s = self._settings()
        assert select_tools_for_intent(IntentLabel.BILL_QUERY, 0.5, s) is None

    def test_confidence_equal_threshold_included(self) -> None:
        s = self._settings(pd_confidence_threshold=0.7)
        assert select_tools_for_intent(IntentLabel.LIMIT_QUERY, 0.7, s) is not None

    def test_unmapped_intent_returns_none(self) -> None:
        s = self._settings()
        # FAQ 不在 intent_tool_map → None
        assert select_tools_for_intent(IntentLabel.FAQ, 0.99, s) is None

    def test_empty_subset_returns_none(self) -> None:
        s = self._settings(intent_tool_map={"bill_query": []})
        assert select_tools_for_intent(IntentLabel.BILL_QUERY, 0.99, s) is None


class TestToolIntents:
    def test_tool_intents_are_four_query_intents(self) -> None:
        """工具编排白名单 = 4 个查询类 (旧 flat 别名 + 主名双世界, 归一化后集合为 4 类).

        会话 48882b05 决策: 分期(INST_PARAM_QUERY/INSTALLMENT_INQUIRY)移出 —— 裸"分期"
        是歧义输入, 走知识问答给分期介绍, 不再被工具编排反问参数。
        """
        from lumio.shared.models import normalize_intent

        expected_canonical = frozenset(
            {
                IntentLabel.ACCOUNT_BILL_QUERY,
                IntentLabel.TXN_QUERY,
                IntentLabel.LIMIT_QUERY,
                IntentLabel.POINTS_BALANCE_QUERY,
            }
        )
        normalized = frozenset(normalize_intent(i.value) for i in TOOL_INTENTS)
        assert expected_canonical == normalized
        # 旧别名与主名都在白名单里 (分类器批 2 重训前输出旧值, 之后输出主名)
        assert IntentLabel.BILL_QUERY in TOOL_INTENTS
        assert IntentLabel.ACCOUNT_BILL_QUERY in TOOL_INTENTS

    def test_installment_intents_excluded(self) -> None:
        """分期意图不再触发工具编排拦截 (会话 48882b05)"""
        assert IntentLabel.INSTALLMENT_INQUIRY not in TOOL_INTENTS
        assert IntentLabel.INST_PARAM_QUERY not in TOOL_INTENTS

    def test_transfer_intents_excluded(self) -> None:
        assert IntentLabel.CARD_LOSS not in TOOL_INTENTS
        assert IntentLabel.COMPLAINT not in TOOL_INTENTS
        assert IntentLabel.TRANSFER_AGENT not in TOOL_INTENTS
