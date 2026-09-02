"""两级路由决策层（目标架构 ④）

决策一：交易性质判定 —— 金融类交易 / 非金融查询 / 高风险转人工
决策二：只读咨询流量四分流 —— FAQ 直出 / RAG 链路 / 复合意图 / 低置信并行竞速

分类表以既有 INTENT_DOMAINS + SENSITIVE_INTENTS 为单一事实源归并生成，
另加显式覆盖表，避免第三处意图清单漂移。

特性开关 bot.routing_v2_enabled 控制分派层走新决策还是旧链路（回滚保底）。
"""

from __future__ import annotations

from enum import StrEnum

from lumio.services.bot.tool_selection import TOOL_INTENTS
from lumio.shared.intent_taxonomy import IntentDomain, domain_of
from lumio.shared.models import SENSITIVE_INTENTS, IntentLabel


class TrafficClass(StrEnum):
    """决策一 · 交易性质三分流 (CONSULTING 由五域骨架 IntentDomain.CONSULTING 表达)

    五域骨架 (intent_taxonomy.IntentDomain) 是域判定的唯一权威;
    本枚举只表达"交易性质"这一个维度: 金融交易 / 只读查询 / 高风险。
    """

    FINANCIAL_TRANSACTION = "financial_transaction"  # 资金变动/账户变更 → 链 A (工具编排+确认状态机)
    READ_ONLY_QUERY = "read_only_query"  # 查余额/明细/进度 → 链 B (轻路径)
    HIGH_RISK = "high_risk"  # 投诉/争议/转人工诉求 → 人工坐席


class RouteDecision(StrEnum):
    """决策二 · 只读咨询四分流"""

    FAQ_DIRECT = "faq_direct"  # E · 高置信 FAQ
    RAG_CHAIN = "rag_chain"  # F · 高置信咨询
    COMPOSITE = "composite"  # C · 复合意图 (查询取数+解释)
    PARALLEL_RACE = "parallel_race"  # D · 低置信 FAQ/RAG 并行竞速


# ── 决策一分类表 ──

# 高风险: 投诉/争议/明确转人工诉求 —— 统一第一级出口直转人工
# (归并来源: INTENT_DOMAINS 的 complain/transfer 域)
_HIGH_RISK_DOMAINS = {"complain", "transfer"}

# 金融交易: 资金变动/账户变更类。
# v1 口径 = risk 域 (挂失/冻结/欺诈上报, 有确认状态机背书的敏感工具) ∪ SENSITIVE_INTENTS
# 中非投诉争议类。其余"knowledge 域的写类意图" (如电子账单设置) 仍走知识介绍——
# 无对应执行工具, 贸然进交易链会零工具可用 (与现状一致, 工具补齐后在此表追加)。
_FINANCIAL_DOMAINS = {"risk"}

# 有真实执行工具的写类意图显式覆盖 (knowledge 域默认只给介绍, 但这些在 MCP
# 工具面有对应交易工具 + 确认状态机背书, 应进链 A):
# apply_bill_installment / adjust_temp_credit_limit / repay_credit_card 等
_FINANCIAL_TOOL_OVERRIDES: set[IntentLabel] = {
    IntentLabel.INST_APPLY,  # apply_bill_installment
    IntentLabel.LIMIT_APPLY_INCREASE,  # adjust_temp_credit_limit
    IntentLabel.LIMIT_APPLY_DECREASE,
    IntentLabel.REPAY_EARLY,  # repay_credit_card
    IntentLabel.REPAY_SETTLE,
}


def classify_traffic(intent: IntentLabel) -> tuple[IntentDomain, TrafficClass | None]:
    """决策一: 意图 → (五域, 交易性质)

    返回 (domain, traffic): domain 恒为五域之一 (骨架权威);
    traffic 为交易性质 (咨询域无交易性质 → None, 进决策二)。
    """

    domain = domain_of(intent)
    if domain == IntentDomain.SERVICE:
        return domain, TrafficClass.HIGH_RISK
    if domain == IntentDomain.TRANSACTION or intent in _FINANCIAL_TOOL_OVERRIDES:
        return domain, TrafficClass.FINANCIAL_TRANSACTION
    if intent in SENSITIVE_INTENTS:
        # 敏感但域不是 service/transaction (理论上不出现) → 保守按交易
        return domain, TrafficClass.FINANCIAL_TRANSACTION
    if intent in TOOL_INTENTS or normalize_for_query(intent):
        return domain, TrafficClass.READ_ONLY_QUERY
    if domain == IntentDomain.QUERY:
        return domain, TrafficClass.READ_ONLY_QUERY
    return domain, None  # CONSULTING/CHITCHAT → 无交易性质, 进决策二


def normalize_for_query(intent: IntentLabel) -> bool:
    """查询类意图判定 (与 TOOL_INTENTS 归一化后对齐)"""
    from lumio.services.common.classifier import normalize_intent

    return normalize_intent(intent.value) in {normalize_intent(t.value) for t in TOOL_INTENTS}


# ── 决策二阈值 ──

# 低置信带宽: 0.4 ≤ conf < 0.6 → 并行竞速 (对冲路由偏差)
LOW_CONF_FLOOR = 0.4
LOW_CONF_CEILING = 0.6


def decision_two(confidence: float, has_composite: bool) -> RouteDecision:
    """决策二: 只读咨询流量四分流

    优先级: 复合意图 > 低置信竞速 > FAQ 直出 > RAG 链路。
    (复合意图携带查询取数诉求, 竞速只会稀释它; FAQ 直出已由分类前短路承担
    大部分高置信 FAQ, 此处 FAQ_DIRECT 供显式 faq 主意图高置信时走标准答案)
    """
    if has_composite:
        return RouteDecision.COMPOSITE
    if LOW_CONF_FLOOR <= confidence < LOW_CONF_CEILING:
        return RouteDecision.PARALLEL_RACE
    return RouteDecision.RAG_CHAIN


# ── 复合意图检测 (链 C, v1 规则) ──

_EXPLAIN_PATTERNS = ("为什么", "怎么算", "如何计算", "怎么收费", "什么意思", "解释")


def detect_composite(intent: IntentLabel, alternatives: list[IntentLabel], text: str) -> bool:
    """查询诉求 + 解释诉求的复合检测

    v1 规则: 主意图为查询类 且 (alternatives 携带知识类意图 或 文本含解释诉求词)。
    """
    if not normalize_for_query(intent):
        return False
    if any(a not in (IntentLabel.FAQ,) and domain_of(a) == IntentDomain.CONSULTING for a in alternatives or []):
        return True
    return any(p in text for p in _EXPLAIN_PATTERNS)


# 闲聊域轻回复引导 (会话 8700a2ea 复盘): "锄禾日当午"被分类成 chitchat@0.70 进
# 决策二, 高置信直落 RAG 链, 检索靠单字"日"BM25 非零命中"账单日"文档, 15 秒生成
# 了整段账单说明 — 答非所问。闲聊/无义输入没有业务诉求, 检索与生成只有成本和
# 幻觉风险, 应模板轻回复引导回业务。
# alternatives 携带业务域意图的混合句 ("哈哈帮我查下账单") 不拦 — 照常走决策二,
# 让检索/竞速链路服务其中的业务诉求。
_NONBUSINESS_PASSTHROUGH = frozenset(
    {IntentLabel.FAQ, IntentLabel.NB_CHITCHAT, IntentLabel.NB_NOISE, IntentLabel.CHITCHAT}
)
_BUSINESS_DOMAINS = frozenset({IntentDomain.QUERY, IntentDomain.TRANSACTION, IntentDomain.SERVICE})
# 业务次选"强到足以代表业务诉求"的分数线 (softmax 概率)。会话 22ad: "丈二和尚"
# 的 BERT 次选 transfer_agent/transaction_query 是对冲性弱次选 (<0.3), 却挡掉了
# 闲聊短路。带分数时低于此线的弱次选不拦; 无分数 (旧调用方) 保持保守放行。
_ALT_BUSINESS_PASS_SCORE = 0.30


def is_chitchat_redirect(
    intent: IntentLabel,
    alternatives: list[IntentLabel] | None,
    alternative_scores: list[float] | None = None,
) -> bool:
    """闲聊域轻回复判定: 主意图属闲聊域 且 alternatives 不携带**强**业务域意图。

    强弱由 alternative_scores (与 alternatives 按下标对齐) 判定:
    分数缺失的次选按"强"处理 (保守放行, 不弱化混合句保护)。
    """
    if domain_of(intent) != IntentDomain.CHITCHAT:
        return False
    scores = alternative_scores or []
    for i, alt in enumerate(alternatives or []):
        if alt in _NONBUSINESS_PASSTHROUGH:
            continue
        if domain_of(alt) in _BUSINESS_DOMAINS:
            score = scores[i] if i < len(scores) else None
            if score is None or score >= _ALT_BUSINESS_PASS_SCORE:
                return False
    return True
