"""批 1 意图体系不变量测试 (draft-0.3 落地): 枚举完整性 / 域映射全覆盖 / 归一化往返 /
敏感集合 / 槽位主名归一化。全部纯逻辑, 无 DB/torch。"""

from __future__ import annotations

from lumio.services.bot.slot_tracker import SlotTracker
from lumio.services.common.classifier import INTENT_DOMAINS, get_domain
from lumio.shared.models import (
    SENSITIVE_INTENTS,
    IntentLabel,
    normalize_intent,
)

# 14 域 × 主名数 (draft-0.3 §1 主表)
_DOMAIN_COUNTS = {
    "account": 10,
    "txn": 10,
    "repay": 14,
    "limit": 9,
    "inst": 11,
    "points": 13,
    "card": 15,
    "pay": 9,
    "fee": 13,
    "risk": 13,
    "dispute": 12,
    "handoff": 8,
    "faq": 9,
    "nb": 3,
}
_OLD_FLAT = {
    "faq",
    "bill_query",
    "transaction_query",
    "limit_query",
    "installment_inquiry",
    "reward_query",
    "card_loss",
    "complaint",
    "transfer_agent",
    "chitchat",
}


def _canonical() -> set[IntentLabel]:
    """主名集合 = 枚举全集减去旧 flat 别名 (transfer_agent/limit_query 是 identity, 不在别名减集)."""
    return {i for i in IntentLabel if i.value not in _OLD_FLAT - {"limit_query", "transfer_agent"}}


def test_canonical_label_counts_match_draft03() -> None:
    """149 个主名, 各域数量与 draft-0.3 主表一致."""
    canon = _canonical()
    assert len(canon) == 149
    by_domain: dict[str, int] = {}
    for label in canon:
        prefix = label.value.split("_")[0]
        by_domain[prefix] = by_domain.get(prefix, 0) + 1
    # 值前缀 → 域归并: benefit/campaign 属 points 域, transfer_agent 属 handoff 域
    by_domain["points"] = by_domain.get("points", 0) + by_domain.pop("benefit", 0) + by_domain.pop("campaign", 0)
    by_domain["handoff"] = by_domain.get("handoff", 0) + by_domain.pop("transfer", 0)
    assert by_domain == _DOMAIN_COUNTS


def test_intent_domains_covers_all_canonical() -> None:
    """每个主名都有域映射, 且域值只取自六种路径."""
    canon = _canonical()
    assert set(INTENT_DOMAINS) == canon
    assert set(INTENT_DOMAINS.values()) <= {"knowledge", "business", "transfer", "fallback", "risk", "complain"}


def test_normalize_roundtrip_and_old_alias() -> None:
    """主名归一化幂等; 旧 flat 全部有明确主名归宿."""
    for label in _canonical():
        assert normalize_intent(label.value) == label
    # 旧 flat → 主名 (非 FAQ 兜底, 除 identity 外旧值 ≠ 主名)
    assert normalize_intent("bill_query") == IntentLabel.ACCOUNT_BILL_QUERY
    assert normalize_intent("faq") == IntentLabel.FAQ_PRODUCT
    assert normalize_intent("chitchat") == IntentLabel.NB_CHITCHAT


def test_sensitive_set_union_of_worlds() -> None:
    """敏感集合 = draft-0.3 ⚠️ 主名 ∪ 旧 flat 别名 (重训前双世界兼容)."""
    canonical_sensitive = {
        IntentLabel.CARD_LOSS_REPORT,
        IntentLabel.CARD_PIN_FORGOT,
        IntentLabel.RISK_FRAUD_REPORT,
        IntentLabel.RISK_ACCOUNT_FREEZE,
        IntentLabel.RISK_CONTACT_WARN,
        IntentLabel.RISK_SMS_VERIFY,
        IntentLabel.RISK_PIN_LEAK,
        IntentLabel.DISPUTE_SUBMIT,
        IntentLabel.DISPUTE_APPEAL,
        IntentLabel.DISPUTE_CHARGEBACK,
        IntentLabel.FEE_APPEAL,
    }
    assert canonical_sensitive <= SENSITIVE_INTENTS
    assert IntentLabel.CARD_LOSS in SENSITIVE_INTENTS
    assert IntentLabel.COMPLAINT in SENSITIVE_INTENTS


def test_slot_tracker_normalizes_old_to_canonical() -> None:
    """槽位追踪器入口归一化: 旧 flat 与主名落到同一 schema."""
    old_tracker = SlotTracker.for_intent(IntentLabel.CARD_LOSS)
    new_tracker = SlotTracker.for_intent(IntentLabel.CARD_LOSS_REPORT)
    assert old_tracker.intent == new_tracker.intent == "card_loss_report"
    assert any(s.name == "card_tail" and s.required for s in new_tracker.slots)


def test_risk_complain_transfer_domains_present() -> None:
    """risk/complain/transfer 域已映射 (派发层与 business 同走 _handle_business)."""
    assert get_domain(IntentLabel.CARD_LOSS_REPORT) == "risk"
    assert get_domain(IntentLabel.DISPUTE_SUBMIT) == "complain"
    assert get_domain(IntentLabel.TRANSFER_AGENT) == "transfer"
    assert get_domain(IntentLabel.REPAY_OVERDUE_PLAN) == "risk"
    assert get_domain(IntentLabel.FEE_APPEAL) == "complain"


def test_write_intents_route_to_knowledge() -> None:
    """写类(办理)意图全部走 knowledge 域 — 办理动作交官方渠道, 机器人返回介绍/引导。

    行业共识 (会话 1efbd1ad 复盘): 银行客服机器人只做「查询+引导」, 不真正办理资金操作。
    """
    from lumio.services.bot.tool_selection import TOOL_INTENTS
    from lumio.services.common.classifier import WRITE_INTENTS

    assert len(WRITE_INTENTS) == 40
    for intent in WRITE_INTENTS:
        assert get_domain(intent) == "knowledge", f"{intent.value} 应走 knowledge"
        # 写类与查询类工具意图不重叠
        assert intent not in TOOL_INTENTS, f"{intent.value} 不应在查询类工具意图中"
    # 抽查核心办理意图
    assert get_domain(IntentLabel.INST_APPLY) == "knowledge"  # 办分期
    assert get_domain(IntentLabel.LIMIT_APPLY_INCREASE) == "knowledge"  # 提额
    assert get_domain(IntentLabel.REPAY_SETTLE) == "knowledge"  # 还款
    assert get_domain(IntentLabel.CARD_REISSUE) == "knowledge"  # 补卡


def test_query_intents_still_route_to_business() -> None:
    """查询类意图未被误标成写类, 抽查核心查询类仍走 business 域(工具编排)."""
    from lumio.services.bot.tool_selection import TOOL_INTENTS
    from lumio.services.common.classifier import WRITE_INTENTS

    # 查询类(TOOL_INTENTS)与写类(WRITE_INTENTS)不重叠 — 查询类没被误改
    assert set(TOOL_INTENTS).isdisjoint(WRITE_INTENTS)
    assert get_domain(IntentLabel.ACCOUNT_BILL_QUERY) == "business"
    assert get_domain(IntentLabel.TXN_QUERY) == "business"
    assert get_domain(IntentLabel.POINTS_BALANCE_QUERY) == "business"
    # 会话 48882b05 决策: 分期查询不再触发工具编排拦截, 走 knowledge 知识问答
    assert IntentLabel.INST_PARAM_QUERY not in TOOL_INTENTS
    assert get_domain(IntentLabel.INST_PARAM_QUERY) == "knowledge"
