"""意图体系五域骨架（目标架构 ③ 重建）

按银行客服意图体系骨架定义三级结构:

    五域 (IntentDomain) → 子域组 (GroupKey) → 叶子意图 (既有 IntentLabel)

五域与 v2 路由 TrafficClass 直接对齐:
    query 查询域(非金融) → READ_ONLY_QUERY → 链 B
    transaction 交易域(金融类) → FINANCIAL_TRANSACTION → 链 A
    consulting 咨询域 → CONSULTING → 决策二
    service 服务域(人工转接/投诉) → HIGH_RISK → 人工
    chitchat 闲聊域 → CONSULTING/兜底

既有 149 个 IntentLabel 通过 INTENT_DOMAINS 归并映射进五域, 不新增第三处
意图清单 —— 本模块只是骨架视图 + 域判定函数。
"""

from __future__ import annotations

from enum import StrEnum

from lumio.services.common.classifier import INTENT_DOMAINS, get_domain
from lumio.shared.models import IntentLabel, normalize_intent


class IntentDomain(StrEnum):
    """五域骨架 · 第一级"""

    QUERY = "query"  # A 查询域（非金融）
    TRANSACTION = "transaction"  # B 交易域（金融类）
    CONSULTING = "consulting"  # C 咨询域
    SERVICE = "service"  # D 服务域
    CHITCHAT = "chitchat"  # E 闲聊域


# ── 旧域 → 五域映射 (归并自 INTENT_DOMAINS 的 6 个旧域值) ──

_LEGACY_DOMAIN_TO_SKELETON: dict[str, IntentDomain] = {
    "business": IntentDomain.QUERY,  # 账单/交易/额度/积分查询 → A 查询域
    "knowledge": IntentDomain.CONSULTING,  # 办理/设置/规则介绍 → C 咨询域
    "risk": IntentDomain.TRANSACTION,  # 挂失/冻结/欺诈 → B2 账户变更类
    "complain": IntentDomain.SERVICE,  # 投诉/争议 → D2
    "transfer": IntentDomain.SERVICE,  # 人工转接 → D1
    "fallback": IntentDomain.CHITCHAT,  # FAQ 兜底/闲聊 → E
}

# ── 子域组 (第二级, 按骨架树) ──

GROUP_A1_ACCOUNT = "A1_account_query"  # 账户查询: 查余额/查明细
GROUP_A2_BILL = "A2_bill_query"  # 账单查询: 查金额/查明细
GROUP_A3_PROGRESS = "A3_progress_query"  # 进度查询
GROUP_B1_FUNDS = "B1_funds"  # 资金类: 转账/缴费/还款
GROUP_B2_ACCOUNT_CHANGE = "B2_account_change"  # 账户变更类: 挂失/冻结/改手机号
GROUP_C1_PRODUCT = "C1_product"  # 产品咨询: 利率/费用/规则
GROUP_C2_BUSINESS = "C2_business"  # 业务咨询: 怎么开通/怎么取消
GROUP_C3_DISPUTE = "C3_dispute"  # 争议咨询: 费用异议/扣款质疑
GROUP_D1_TRANSFER = "D1_transfer"  # 人工转接
GROUP_D2_COMPLAINT = "D2_complaint"  # 投诉建议

# 叶子意图 → 子域组 (按旧域名 + 意图名段归组; 未列出的意图沿用域默认组)
_DOMAIN_DEFAULT_GROUP: dict[IntentDomain, str] = {
    IntentDomain.QUERY: GROUP_A2_BILL,
    IntentDomain.TRANSACTION: GROUP_B2_ACCOUNT_CHANGE,
    IntentDomain.CONSULTING: GROUP_C2_BUSINESS,
    IntentDomain.SERVICE: GROUP_D1_TRANSFER,
    IntentDomain.CHITCHAT: "",
}

# 显式组覆盖 (按骨架树的代表叶子; 其余意图落域默认组, 不逐一枚举 149 个)
_GROUP_OVERRIDES: dict[IntentLabel, str] = {
    # A1 账户查询
    IntentLabel.ACCOUNT_BALANCE_QUERY: GROUP_A1_ACCOUNT,
    IntentLabel.TXN_QUERY: GROUP_A1_ACCOUNT,
    IntentLabel.TXN_CATEGORY_STAT: GROUP_A1_ACCOUNT,
    # A2 账单查询
    IntentLabel.ACCOUNT_BILL_QUERY: GROUP_A2_BILL,
    IntentLabel.ACCOUNT_STMT_QUERY: GROUP_A2_BILL,
    # A3 进度查询
    IntentLabel.CARD_APPLY_NEW: GROUP_A3_PROGRESS,  # 办卡进度
    IntentLabel.LIMIT_APPLY_STATUS: GROUP_A3_PROGRESS,  # 提额申请进度
    IntentLabel.INST_STATUS_QUERY: GROUP_A3_PROGRESS,
    # B1 资金类
    IntentLabel.REPAY_EARLY: GROUP_B1_FUNDS,
    IntentLabel.REPAY_SETTLE: GROUP_B1_FUNDS,
    IntentLabel.TXN_AUTO_DEBIT_SET: GROUP_B1_FUNDS,
    # B2 账户变更类
    IntentLabel.CARD_LOSS_REPORT: GROUP_B2_ACCOUNT_CHANGE,
    IntentLabel.CARD_LOSS: GROUP_B2_ACCOUNT_CHANGE,
    IntentLabel.CARD_FREEZE: GROUP_B2_ACCOUNT_CHANGE,
    IntentLabel.CARD_PIN_SET: GROUP_B2_ACCOUNT_CHANGE,
    # C1 产品咨询
    IntentLabel.FAQ: GROUP_C1_PRODUCT,
    IntentLabel.POINTS_RULE_QUERY: GROUP_C1_PRODUCT,
    IntentLabel.LIMIT_POLICY_QUERY: GROUP_C1_PRODUCT,
    IntentLabel.INST_REFUND_RULE: GROUP_C1_PRODUCT,
    # C3 争议咨询
    IntentLabel.DISPUTE_SUBMIT: GROUP_C3_DISPUTE,
    IntentLabel.DISPUTE_CHARGEBACK: GROUP_C3_DISPUTE,
    IntentLabel.DISPUTE_APPEAL: GROUP_C3_DISPUTE,
    IntentLabel.FEE_APPEAL: GROUP_C3_DISPUTE,
    # D2 投诉建议
    IntentLabel.COMPLAINT: GROUP_D2_COMPLAINT,
}


# 域级显式覆盖 (组覆盖但旧域名不一致的意图, 两级保持一致)
_DOMAIN_OVERRIDES: dict[IntentLabel, IntentDomain] = {
    IntentLabel.REPAY_EARLY: IntentDomain.TRANSACTION,  # B1 资金类
    IntentLabel.REPAY_SETTLE: IntentDomain.TRANSACTION,
    IntentLabel.TXN_AUTO_DEBIT_SET: IntentDomain.TRANSACTION,
    IntentLabel.FAQ: IntentDomain.CONSULTING,  # C1 产品咨询
    IntentLabel.DISPUTE_CHARGEBACK: IntentDomain.SERVICE,  # C3 争议
}


def domain_of(intent: IntentLabel | str) -> IntentDomain:
    """叶子意图 → 五域 (骨架第一级)"""
    if isinstance(intent, str) and not isinstance(intent, IntentLabel):
        intent = normalize_intent(intent)
    override = _DOMAIN_OVERRIDES.get(intent)
    if override:
        return override
    legacy = get_domain(intent)
    return _LEGACY_DOMAIN_TO_SKELETON.get(legacy, IntentDomain.CONSULTING)


def group_of(intent: IntentLabel | str) -> str:
    """叶子意图 → 子域组 (骨架第二级)"""
    if isinstance(intent, str) and not isinstance(intent, IntentLabel):
        intent = normalize_intent(intent)
    override = _GROUP_OVERRIDES.get(intent)
    if override:
        return override
    return _DOMAIN_DEFAULT_GROUP.get(domain_of(intent), "")


def leaves_in_domain(domain: IntentDomain) -> list[IntentLabel]:
    """域内全部叶子意图 (供 L3 两段分类的第二段裁剪候选)"""
    return [i for i, dom in _all_domains().items() if dom == domain]


def _all_domains() -> dict[IntentLabel, IntentDomain]:
    return {intent: domain_of(intent) for intent in INTENT_DOMAINS}
