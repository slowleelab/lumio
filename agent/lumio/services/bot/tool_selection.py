"""渐进式工具暴露（Progressive Disclosure）选择器

纯函数：根据意图 + 置信度 + 配置，决定向 LLM 暴露的工具子集。

设计要点（零回归）：
- 与网关的路由/聚合模式正交——本模块位于编排（host）层，只负责「暴露哪些工具」。
- 关闭开关（``progressive_disclosure_enabled=False``）时返回 ``None``，等价于暴露全量工具，
  行为与打通前完全一致。
- 命中意图且置信度达标 → 返回该意图的工具子集名单；否则（未命中/低置信）返回 ``None``，
  由 LLM 在全量工具上自行判断。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lumio.shared.models import IntentLabel, normalize_intent

if TYPE_CHECKING:
    from lumio.shared.config import MCPSettings

# 允许进入工具编排路径的「查询类工具意图」集合 (draft-0.3 主表 business 查询类)。
# 挂失/投诉/转人工仍直接转人工，闲聊/FAQ 仍走知识问答。
# 含旧 flat 别名与主名双世界: 分类器重训(批 2)前输出旧值, 之后输出主名;
# 调用方统一先 normalize_intent 再判成员 (见 bot_agent.run 与 select_tools_for_intent)。
# 会话 48882b05 决策: INST_PARAM_QUERY/INSTALLMENT_INQUIRY (分期) 移出本集合 ——
# 裸"分期"是歧义输入, 分类器无法区分办理/查费率, 与其让工具编排反问"金额/期数/卡号后四位",
# 不如按行业共识直接给分期知识介绍 (INTENT_DOMAINS 本就映射 knowledge, 此前被本拦截短路架空)。
TOOL_INTENTS: frozenset[IntentLabel] = frozenset(
    {
        # 旧 flat 别名
        IntentLabel.BILL_QUERY,
        IntentLabel.TRANSACTION_QUERY,
        IntentLabel.LIMIT_QUERY,
        IntentLabel.REWARD_QUERY,
        # 主名
        IntentLabel.ACCOUNT_BILL_QUERY,
        IntentLabel.TXN_QUERY,
        IntentLabel.POINTS_BALANCE_QUERY,
    }
)


def select_tools_for_intent(
    intent: IntentLabel,
    confidence: float,
    settings: MCPSettings,
) -> list[str] | None:
    """根据意图与置信度选择要暴露的工具子集。

    :param intent: 主意图
    :param confidence: 主意图置信度
    :param settings: MCP 配置（含开关、阈值、意图→工具映射）
    :returns: 工具名子集；``None`` 表示暴露全量工具（不裁剪）
    """
    # 开关关闭 → 不裁剪，暴露全量（零回归）
    if not settings.progressive_disclosure_enabled:
        return None

    # 键兼容双世界: 配置 intent_tool_map 目前以旧 flat 值为键 ("bill_query"),
    # 归一化后的主名 ("account_bill_query") 优先, 缺失时回退旧键 (批 2 切主名后反向兼容)。
    raw_key = intent.value if isinstance(intent, IntentLabel) else str(intent)
    key = normalize_intent(raw_key).value
    names = settings.intent_tool_map.get(key) or settings.intent_tool_map.get(raw_key)
    # 未配置该意图或子集为空 → 暴露全量交 LLM 判断
    if not names:
        return None

    # 低置信 → 不裁剪，避免因误分类而漏掉必要工具
    if confidence < settings.pd_confidence_threshold:
        return None

    return list(names)
