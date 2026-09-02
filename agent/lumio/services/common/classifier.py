"""双通道意图分类器

Fast Path: RuleClassifier（正则 + 关键词 + 模板匹配），覆盖高频意图
Slow Path: LLMClassifier（Qwen2.5-7B via Ollama，json_mode + few-shot），覆盖模糊/长尾意图

Fast Path 置信度 < 阈值时自动 fallthrough 到 Slow Path。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import OrderedDict
from typing import TYPE_CHECKING, Any

from lumio.services.bot.entity_extractor import extract_entities, normalize_entity_type
from lumio.services.common.bert_classifier import BertIntentClassifier
from lumio.services.common.trap_collector import TrapCollector, TrapRecord
from lumio.shared.config import get_settings
from lumio.shared.models import Entity, IntentLabel, IntentResult, SentimentLabel, normalize_intent

if TYPE_CHECKING:
    from lumio.services.common.llm import LLMClient

if TYPE_CHECKING:
    from lumio.services.common.llm import LLMClient

logger = logging.getLogger(__name__)

# 规则分类器阈值：Fast Path 置信度 >= 此值直接使用
_FAST_PATH_THRESHOLD = 0.7

# 办理词规则覆盖 (会话 48882b05 同型消歧): BERT 标签空间是旧扁平 10 类, 发不出
# 写类主名意图; 规则层对这些意图高置信命中时覆盖 BERT 快路径结果。仅收办理动作词
# (提额/降额), 覆盖阈值取两规则置信 0.96 之下、其余规则最高置信 0.95 之上。
_APPLY_INTENT_RULE_OVERRIDE: frozenset[IntentLabel] = frozenset(
    {IntentLabel.LIMIT_APPLY_INCREASE, IntentLabel.LIMIT_APPLY_DECREASE}
)
_APPLY_OVERRIDE_CONF = 0.95

# 查询类意图的规则覆盖集 (与 tool_selection.TOOL_INTENTS 查询子集对齐, 不新增
# 第三处清单 —— 在此显式列出并注明对齐关系, 因 services.common 不得顶层依赖
# services.bot)。旧 flat 别名与主名成对收录。
_QUERY_INTENT_OVERRIDES: frozenset[IntentLabel] = frozenset(
    {
        IntentLabel.BILL_QUERY,
        IntentLabel.TRANSACTION_QUERY,
        IntentLabel.LIMIT_QUERY,
        IntentLabel.REWARD_QUERY,
        IntentLabel.ACCOUNT_BILL_QUERY,
        IntentLabel.TXN_QUERY,
        IntentLabel.POINTS_BALANCE_QUERY,
    }
)
# 咨询标记词: 含这些词的句子是"关于账单/额度的问题"而非"查账单/额度"本身,
# 规则关键词命中不作数 (如"账单分期手续费怎么算" ≠ 账单查询)。
_CONSULTIVE_MARKERS = (
    "怎么",
    "如何",
    "为什么",
    "什么意思",
    "是什么",
    "什么是",
    "什么叫",
    "介绍",
    "规则",
    "政策",
    "手续费",
    "利率",
    "条件",
    "区别",
    "划算",
)

# 低置信噪声闸下限: BERT 快路径置信度低于此值时视为"没认出来", 直接不花一次 LLM 慢路径分类.
# 实测 LLM 慢路径会把噪声置信抬到恰 ≥ 此下限 (如 0.219→0.30), 既浪费一次 6s+ 调用,
# 又掩盖噪声信号让下游 low_conf 噪声闸漏放. 仅 BERT 为快路径时激进兜底; 与
# bot_agent 的 CLARIFY_CONFIDENCE_FLOOR (0.3) 对齐, 低于它本来就该回确定性澄清.
_LOW_CONF_FLOOR = 0.3


def _collect_alternatives(
    hits: list[tuple[IntentLabel, float]], primary: IntentLabel, limit: int = 3
) -> list[IntentLabel]:
    """从规则命中列表收集次意图 (多意图).

    - 去重、剔除主意图
    - 按置信度降序
    - 低置信 (< 0.5) 的不算作有意义的次意图
    """
    seen: set[IntentLabel] = set()
    out: list[IntentLabel] = []
    for intent, conf in sorted(hits, key=lambda x: x[1], reverse=True):
        if intent == primary or intent in seen:
            continue
        if conf < 0.5:
            continue
        seen.add(intent)
        out.append(intent)
        if len(out) >= limit - 1:  # include primary, so return at most limit-1
            break
    return out


# 写类（办理/申请/设置/变更/取消/绑定/兑换/激活）意图集合。
# 行业共识: 银行客服机器人只做「查询 + 引导」, 办理动作交官方渠道, 机器人返回办理
# 介绍/入口。故这些意图走 knowledge 域(知识问答, RAG 检索办理介绍 + KNOWLEDGE prompt
# 第 6 条引导官方渠道), 不进工具编排真正执行。与 TOOL_INTENTS(查询类, 保留工具编排)
# 互补不重叠。
WRITE_INTENTS: frozenset[IntentLabel] = frozenset(
    {
        # 1.1 account 设置/补寄
        IntentLabel.ACCOUNT_E_BILL_SET,
        IntentLabel.ACCOUNT_PAPER_BILL_REISSUE,
        IntentLabel.ACCOUNT_BILL_REPAY_SPLIT_SET,
        IntentLabel.ACCOUNT_BILL_ALERT_SET,
        # 1.2 transaction 设置/锁定
        IntentLabel.TXN_AUTO_DEBIT_SET,
        IntentLabel.TXN_CURRENCY_SET,
        IntentLabel.TXN_OVERSEAS_LOCK,
        # 1.3 repay 还款动作/设置
        IntentLabel.REPAY_AUTO_SET,
        IntentLabel.REPAY_EARLY,
        IntentLabel.REPAY_SETTLE,
        IntentLabel.REPAY_DEDUCTION_ORDER,
        # 1.4 limit 提额/降额/提醒设置
        IntentLabel.LIMIT_APPLY_INCREASE,
        IntentLabel.LIMIT_APPLY_DECREASE,
        IntentLabel.LIMIT_USAGE_ALERT_SET,
        # 1.5 installment 办理/结清/变更/取消
        IntentLabel.INST_APPLY,
        IntentLabel.INST_EARLY_SETTLE,
        IntentLabel.INST_CHANGE_SET,
        IntentLabel.INST_CANCEL,
        # 1.6 points/benefit 兑换/转赠/领取/报名
        IntentLabel.POINTS_REDEEM,
        IntentLabel.POINTS_TRANSFER,
        IntentLabel.POINTS_EXPIRY_ALARM_SET,
        IntentLabel.BENEFIT_CLAIM,
        IntentLabel.BENEFIT_REASSIGN,
        IntentLabel.CAMPAIGN_SIGNUP,
        # 1.7 card 补卡/申请/激活/续卡/销卡/设置/升级
        IntentLabel.CARD_REISSUE,
        IntentLabel.CARD_APPLY_NEW,
        IntentLabel.CARD_ACTIVATE,
        IntentLabel.CARD_EXPIRE_RENEW,
        IntentLabel.CARD_CANCEL,
        IntentLabel.CARD_PIN_SET,
        IntentLabel.CARD_SUPPLEMENTARY,
        IntentLabel.CARD_UPGRADE,
        IntentLabel.CARD_LOSS_CANCEL,
        # 1.8 payment 绑定/解绑/闪付/在线支付设置
        IntentLabel.PAY_WALLET_BIND,
        IntentLabel.PAY_WALLET_UNBIND,
        IntentLabel.PAY_CONTACTLESS,
        IntentLabel.PAY_ONLINE_SET,
        IntentLabel.PAY_PASSWORD_ONLINE,
        IntentLabel.PAY_PAUSE,
        IntentLabel.PAY_MAGNETIC_ISSUE,
    }
)


# 意图域映射：用于 supervisor 路由 (draft-0.3 §1 主表 decision_path 全量)。
# 语义口径: 查询/办理类 = business (工具编排, 无工具时 business 路径带 RAG 兜底,
# 见 bot_agent._handle_business); knowledge = 政策/规则咨询; risk/complain/transfer
# 在 bot_agent.run() 分派时与 business 同走 _handle_business (敏感/转人工逻辑在内),
# fallback = 闲聊/结束/噪声。get_domain 入口先 normalize_intent, 旧 flat 值自动落到主名。
INTENT_DOMAINS: dict[IntentLabel, str] = {
    # 1.1 account
    IntentLabel.ACCOUNT_BILL_QUERY: "business",
    IntentLabel.ACCOUNT_E_BILL_SET: "knowledge",
    IntentLabel.ACCOUNT_PAPER_BILL_REISSUE: "knowledge",
    IntentLabel.ACCOUNT_STMT_QUERY: "business",
    IntentLabel.ACCOUNT_STMT_DISPUTE: "complain",
    IntentLabel.ACCOUNT_BILL_EXPORT: "business",
    IntentLabel.ACCOUNT_BILL_REPAY_SPLIT_SET: "knowledge",
    IntentLabel.ACCOUNT_BILL_ALERT_SET: "knowledge",
    IntentLabel.ACCOUNT_BALANCE_QUERY: "business",
    IntentLabel.ACCOUNT_FOREX_RATE_QUERY: "knowledge",
    # 1.2 transaction
    IntentLabel.TXN_QUERY: "business",
    IntentLabel.TXN_CASH_ADVANCE_QUERY: "business",
    IntentLabel.TXN_AUTO_DEBIT_SET: "knowledge",
    IntentLabel.TXN_AUTO_DEBIT_QUERY: "business",
    IntentLabel.TXN_REFUND_QUERY: "business",
    IntentLabel.TXN_RECEIPT_GET: "business",
    IntentLabel.TXN_CURRENCY_SET: "knowledge",
    IntentLabel.TXN_OVERSEAS_LOCK: "knowledge",
    IntentLabel.TXN_CATEGORY_STAT: "business",
    IntentLabel.TXN_EXPORT: "business",
    # 1.3 repay
    IntentLabel.REPAY_PLAN_QUERY: "business",
    IntentLabel.REPAY_RECORD_QUERY: "business",
    IntentLabel.REPAY_CALC: "business",
    IntentLabel.REPAY_METHOD_QUERY: "knowledge",
    IntentLabel.REPAY_AUTO_SET: "knowledge",
    IntentLabel.REPAY_EARLY: "knowledge",
    IntentLabel.REPAY_GRACE_PERIOD: "knowledge",
    IntentLabel.REPAY_OVERDUE_QUERY: "business",
    IntentLabel.REPAY_OVERDUE_RELIEF: "complain",
    IntentLabel.REPAY_OVERDUE_PLAN: "risk",
    IntentLabel.REPAY_APPOINTMENT: "risk",
    IntentLabel.REPAY_VOUCHER: "business",
    IntentLabel.REPAY_SETTLE: "knowledge",
    IntentLabel.REPAY_DEDUCTION_ORDER: "knowledge",
    # 1.4 limit
    IntentLabel.LIMIT_QUERY: "business",
    IntentLabel.LIMIT_APPLY_INCREASE: "knowledge",
    IntentLabel.LIMIT_APPLY_DECREASE: "knowledge",
    IntentLabel.LIMIT_POLICY_QUERY: "knowledge",
    IntentLabel.LIMIT_HISTORY_QUERY: "business",
    IntentLabel.LIMIT_APPLY_STATUS: "business",
    IntentLabel.LIMIT_TYING_QUERY: "knowledge",
    IntentLabel.LIMIT_POOL_QUERY: "knowledge",
    IntentLabel.LIMIT_USAGE_ALERT_SET: "knowledge",
    # 1.5 installment
    IntentLabel.INST_APPLY: "knowledge",
    IntentLabel.INST_PARAM_QUERY: "knowledge",
    IntentLabel.INST_CALC: "business",
    IntentLabel.INST_STATUS_QUERY: "business",
    IntentLabel.INST_EARLY_SETTLE: "knowledge",
    IntentLabel.INST_CHANGE_SET: "knowledge",
    IntentLabel.INST_CANCEL: "knowledge",
    IntentLabel.INST_REFUND_RULE: "knowledge",
    IntentLabel.INST_FOREX: "knowledge",
    IntentLabel.INST_PROMOTION: "knowledge",
    IntentLabel.INST_CONTRACT: "knowledge",
    # 1.6 points
    IntentLabel.POINTS_BALANCE_QUERY: "business",
    IntentLabel.POINTS_REDEEM: "knowledge",
    IntentLabel.POINTS_EXPIRY_QUERY: "knowledge",
    IntentLabel.POINTS_EXPIRY_ALARM_SET: "knowledge",
    IntentLabel.POINTS_TRANSFER: "knowledge",
    IntentLabel.POINTS_RULE_QUERY: "knowledge",
    IntentLabel.POINTS_ORDER_QUERY: "business",
    IntentLabel.BENEFIT_QUERY: "business",
    IntentLabel.BENEFIT_CLAIM: "knowledge",
    IntentLabel.BENEFIT_REASSIGN: "knowledge",
    IntentLabel.BENEFIT_UPGRADE: "knowledge",
    IntentLabel.CAMPAIGN_QUERY: "knowledge",
    IntentLabel.CAMPAIGN_SIGNUP: "knowledge",
    # 1.7 card
    IntentLabel.CARD_LOSS_REPORT: "risk",
    IntentLabel.CARD_LOSS_CANCEL: "knowledge",
    IntentLabel.CARD_REISSUE: "knowledge",
    IntentLabel.CARD_APPLY_NEW: "knowledge",
    IntentLabel.CARD_ACTIVATE: "knowledge",
    IntentLabel.CARD_EXPIRE_RENEW: "knowledge",
    IntentLabel.CARD_CANCEL: "knowledge",
    IntentLabel.CARD_STATUS_QUERY: "business",
    IntentLabel.CARD_FREEZE: "risk",
    IntentLabel.CARD_PIN_SET: "knowledge",
    IntentLabel.CARD_PIN_FORGOT: "risk",
    IntentLabel.CARD_INFO_QUERY: "knowledge",
    IntentLabel.CARD_SUPPLEMENTARY: "knowledge",
    IntentLabel.CARD_UPGRADE: "knowledge",
    IntentLabel.CARD_GIFT_QUERY: "knowledge",
    # 1.8 payment
    IntentLabel.PAY_METHOD_QUERY: "knowledge",
    IntentLabel.PAY_WALLET_BIND: "knowledge",
    IntentLabel.PAY_WALLET_UNBIND: "knowledge",
    IntentLabel.PAY_CONTACTLESS: "knowledge",
    IntentLabel.PAY_LARGE_VERIFY: "business",
    IntentLabel.PAY_ONLINE_SET: "knowledge",
    IntentLabel.PAY_PASSWORD_ONLINE: "knowledge",
    IntentLabel.PAY_PAUSE: "knowledge",
    IntentLabel.PAY_MAGNETIC_ISSUE: "knowledge",
    # 1.9 fee
    IntentLabel.FEE_ANNUAL: "knowledge",
    IntentLabel.FEE_INTEREST: "knowledge",
    IntentLabel.FEE_PENALTY: "knowledge",
    IntentLabel.FEE_OVERLIMIT: "knowledge",
    IntentLabel.FEE_SERVICE: "knowledge",
    IntentLabel.FEE_OVERSEAS: "knowledge",
    IntentLabel.FEE_CASH: "knowledge",
    IntentLabel.FEE_TRANSFER: "knowledge",
    IntentLabel.FEE_CARD_MATERIAL: "knowledge",
    IntentLabel.FEE_RATE_QUERY: "knowledge",
    IntentLabel.FEE_SETTLE_INQUIRY: "knowledge",
    IntentLabel.FEE_CHARGED_QUERY: "business",
    IntentLabel.FEE_APPEAL: "complain",
    # 1.10 risk
    IntentLabel.RISK_FRAUD_REPORT: "risk",
    IntentLabel.RISK_CASH_ADVANCE_WARN: "risk",
    IntentLabel.RISK_MONEY_LAUNDRY: "risk",
    IntentLabel.RISK_ACCOUNT_FREEZE: "risk",
    IntentLabel.RISK_CONTACT_WARN: "risk",
    IntentLabel.RISK_FRAUD_HOTLINE: "knowledge",
    IntentLabel.RISK_ATM_ANOMALY: "risk",
    IntentLabel.RISK_POS_ANOMALY: "business",
    IntentLabel.RISK_KYC: "knowledge",
    IntentLabel.RISK_SMS_VERIFY: "risk",
    IntentLabel.RISK_PIN_LEAK: "risk",
    IntentLabel.RISK_OVERSEAS_TRAVEL: "knowledge",
    IntentLabel.RISK_WALLET_SAFETY: "knowledge",
    # 1.11 dispute
    IntentLabel.DISPUTE_SUBMIT: "complain",
    IntentLabel.DISPUTE_STATUS: "business",
    IntentLabel.DISPUTE_APPEAL: "complain",
    IntentLabel.DISPUTE_CHARGEBACK: "transfer",
    IntentLabel.DISPUTE_REGULATE: "knowledge",
    IntentLabel.DISPUTE_HOTLINE: "knowledge",
    IntentLabel.DISPUTE_URGE: "complain",
    IntentLabel.DISPUTE_WITHDRAW: "business",
    IntentLabel.DISPUTE_MATERIAL: "business",
    IntentLabel.DISPUTE_COMPENSATION: "complain",
    IntentLabel.DISPUTE_CLOSE: "business",
    IntentLabel.DISPUTE_POLICY: "knowledge",
    # 1.12 handoff
    IntentLabel.TRANSFER_AGENT: "transfer",
    IntentLabel.HANDOFF_QUEUE_QUERY: "business",
    IntentLabel.HANDOFF_HOURS_QUERY: "knowledge",
    IntentLabel.HANDOFF_END: "fallback",
    IntentLabel.HANDOFF_RESTART: "transfer",
    IntentLabel.HANDOFF_SCHEDULE: "transfer",
    IntentLabel.HANDOFF_HOTLINE: "knowledge",
    IntentLabel.HANDOFF_VERIFY: "knowledge",
    # 1.13 faq
    IntentLabel.FAQ_PRODUCT: "knowledge",
    IntentLabel.FAQ_CREDIT_REPORT: "knowledge",
    IntentLabel.FAQ_CONTRACT: "knowledge",
    IntentLabel.FAQ_NOTICE: "knowledge",
    IntentLabel.FAQ_COMPLIANCE: "knowledge",
    IntentLabel.FAQ_DATA: "knowledge",
    IntentLabel.FAQ_CHANNEL: "knowledge",
    IntentLabel.FAQ_ACCOUNT_POLICY: "knowledge",
    IntentLabel.FAQ_ANY: "knowledge",
    # 1.14 nonbusiness
    IntentLabel.NB_CHITCHAT: "fallback",
    IntentLabel.NB_NOISE: "fallback",
    IntentLabel.NB_HELP: "knowledge",
}

# Fast Path 规则定义
# 每条规则包含: intent, patterns (正则), keywords (关键词), confidence
# confidence 已于 2026-08-25 从拍脑袋常数标定为「seed 实测命中率」 (clamp [0.35, 0.95],
# 来源: scripts/intent_classifier_spike.py 的规则逐类命中率报告, seed_dataset v0.3.2,
# 191 例; 每类 16~34 条, 样本量有限, 种子集扩容/回流并入后应重跑报告复核):
#   bill .84 / txn .56 / limit 1.00→.95 / inst .71 / reward .94 / faq .47 /
#   card_loss .56 / complaint .62 / transfer .90 / chitchat .35
# 标定含义: 低于快路径阈值(0.7)的类命中后不再直接放行, 改走 BERT/LLM — 代价是这些类
# 的慢路径调用增加, 换掉此前"规则高置信误放"(如挂失规则 0.9 实测仅 0.56)的漏点。
# 同置信平局时敏感意图优先为主意图: "挂失+查询流水"混合句里两者实测置信接近,
# 若查询类抢走主意图, 挂失可能被推迟转人工 — 平局宁可判敏感。
_SENSITIVE_RULE_PRIORITY: frozenset[IntentLabel] = frozenset(
    {IntentLabel.CARD_LOSS, IntentLabel.COMPLAINT, IntentLabel.TRANSFER_AGENT}
)
_RULES: list[dict[str, Any]] = [
    # 账单类
    {
        "intent": IntentLabel.BILL_QUERY,
        "patterns": [r"账单", r"消费记录", r"还款金额", r"本期账单", r"上个?月.?花了多少"],
        "keywords": ["账单", "消费", "还款", "欠款", "应还", "最低还款"],
        "confidence": 0.84,
    },
    # 交易查询
    {
        "intent": IntentLabel.TRANSACTION_QUERY,
        "patterns": [r"交易记录", r"明细", r"流水", r"扣款"],
        "keywords": ["交易", "明细", "流水", "扣款", "刷卡"],
        "confidence": 0.56,
    },
    # 额度类
    # 额度调整类（办理词优先于查询 — 会话 48882b05 同型消歧: "提额/降额"是办理动作,
    # 走 knowledge 办理介绍 (行业共识: 办理交官方渠道); "额度/可用额度"才是查询,
    # 留在 limit_query 工具编排。办理规则置信度必须高于 limit_query 的 0.95。）
    {
        "intent": IntentLabel.LIMIT_APPLY_INCREASE,
        "patterns": [r"提额", r"提升额度", r"临时提额", r"调高额度"],
        "keywords": ["提额", "提升额度", "调高额度"],
        "confidence": 0.96,
    },
    {
        "intent": IntentLabel.LIMIT_APPLY_DECREASE,
        "patterns": [r"降额", r"降低额度", r"调低额度"],
        "keywords": ["降额", "降低额度"],
        "confidence": 0.96,
    },
    # 额度查询类
    {
        "intent": IntentLabel.LIMIT_QUERY,
        # 注意: 关键词不含 "信用" — 它是 "信用卡" 的子串, 会把任何含"信用卡"的句子
        # (如"信用卡丢了要挂失") 误拉进额度类; 标定(2026-08-25)时实测暴露了该歧义。
        # 注意: 不含 "提额/降额" — 办理词已上移至上方调整类 (limit_apply_increase/decrease)。
        "patterns": [r"额度", r"可用额度", r"信用额度"],
        "keywords": ["额度", "可用", "临时额度", "授信"],
        "confidence": 0.95,
    },
    # 分期类
    {
        "intent": IntentLabel.INSTALLMENT_INQUIRY,
        "patterns": [r"分期", r"期数", r"手续费率", r"账单分期", r"消费分期"],
        "keywords": ["分期", "期数", "手续费", "分期费率"],
        "confidence": 0.71,
    },
    # 积分类
    {
        "intent": IntentLabel.REWARD_QUERY,
        "patterns": [r"积分", r"积分兑换", r"积分过期", r"积分余额"],
        "keywords": ["积分", "兑换", "过期", "积分商城"],
        "confidence": 0.94,
    },
    # FAQ
    {
        "intent": IntentLabel.FAQ,
        "patterns": [r"什么是", r"怎么办理", r"如何操作", r"流程是什么"],
        "keywords": [],
        "confidence": 0.47,
    },
    # 挂失
    {
        "intent": IntentLabel.CARD_LOSS,
        "patterns": [r"挂失", r"补卡", r"换卡", r"卡片丢失"],
        "keywords": ["挂失", "丢失", "补卡", "换卡"],
        "confidence": 0.56,
    },
    # 投诉
    {
        "intent": IntentLabel.COMPLAINT,
        "patterns": [r"投诉", r"不满意", r"举报", r"投诉你们"],
        "keywords": ["投诉", "不满", "举报"],
        "confidence": 0.62,
    },
    # 转人工
    {
        "intent": IntentLabel.TRANSFER_AGENT,
        "patterns": [r"转人工", r"人工客服", r"找人工", r"我要找.*人"],
        "keywords": ["人工", "转人工", "真人"],
        "confidence": 0.9,
    },
    # 闲聊
    {
        "intent": IntentLabel.CHITCHAT,
        "patterns": [r"你好", r"嗨", r"在吗", r"你是谁", r"谢谢", r"再见"],
        "keywords": [],
        "confidence": 0.35,
    },
]

# LLM 分类 Prompt
_CLASSIFY_SYSTEM_PROMPT = """你是一个银行信用卡客服意图分类器。根据用户输入，输出 JSON 格式的分类结果。

## 输出格式
```json
{
  "intent": "意图标签",
  "confidence": 0.0-1.0的置信度,
  "entities": [{"entity_type": "类型", "value": "值"}],
  "sentiment": "positive/neutral/negative/angry"
}
```

## 可选意图标签
- bill_query: 账单查询
- transaction_query: 交易记录查询
- limit_query: 额度查询
- installment_inquiry: 分期咨询
- reward_query: 积分查询
- faq: 常见问题
- card_loss: 挂失/补卡
- complaint: 投诉
- transfer_agent: 转人工
- chitchat: 闲聊

## 示例
用户: 我上个月花了多少钱
输出: {"intent": "bill_query", "confidence": 0.9, "entities": [{"entity_type": "time_range", "value": "上个月"}], "sentiment": "neutral"}

用户: 额度太低了能不能提一下
输出: {"intent": "limit_query", "confidence": 0.85, "entities": [{"entity_type": "action", "value": "提额"}], "sentiment": "neutral"}

用户: 你们的年费怎么这么贵，我要投诉
输出: {"intent": "complaint", "confidence": 0.95, "entities": [{"entity_type": "topic", "value": "年费"}], "sentiment": "angry"}

用户: 你好呀
输出: {"intent": "chitchat", "confidence": 0.9, "entities": [], "sentiment": "positive"}

## 要求
- 只输出 JSON，不要其他文字
- 置信度 0-1 之间，不确定时给低分
- 模糊输入给 intent="faq"，confidence < 0.5
"""


def build_classify_system_prompt() -> str:
    """L3 分类 prompt = 静态基线 + 运营注册表增量 (影子/生效意图)。

    注册表为空时与静态串逐字节一致 (零回归); 有增量时追加候选段。
    影子意图标注 [影子观察中]: 模型可选它, 线上只记日志不改变路由。
    """
    from lumio.shared.intent_registry import get_registry

    extra = get_registry().prompt_intents()
    if not extra:
        return _CLASSIFY_SYSTEM_PROMPT
    lines = ["", "## 运营新增意图 (与上面的意图标签同等可选)"]
    for it in extra:
        mark = " [影子观察中]" if it["state"] == "shadow" else ""
        definition = f" — {it['definition']}" if it["definition"] else ""
        lines.append(f"- {it['slug']}: {it['name_zh']}{definition}{mark}")
    return _CLASSIFY_SYSTEM_PROMPT + "\n".join(lines) + "\n"


def _apply_registry_intent(raw_intent: str) -> IntentLabel | None:
    """LLM 输出命中运营注册表意图 → 按域落代表叶子。

    v2 路由按 (五域, 交易性质) 分流, 注册表意图尚无专属工具/槽位, 域代表
    叶子即其真实路由语义; 影子状态只记命中日志与指标, 不进路由索引。
    """
    from lumio.shared.intent_registry import RegistryState, get_registry
    from lumio.shared.intent_taxonomy import IntentDomain, domain_representative

    entry = get_registry().get(raw_intent)
    if entry is None or entry.state not in (RegistryState.SHADOW, RegistryState.ACTIVE):
        return None
    try:
        rep = domain_representative(IntentDomain(entry.domain))
    except (ValueError, KeyError):
        return None
    is_shadow = entry.state == RegistryState.SHADOW
    get_registry().record_hit(entry.slug, shadow=is_shadow)
    from lumio.shared.metrics import INTENT_REGISTRY_HITS

    INTENT_REGISTRY_HITS.labels(slug=entry.slug, state=entry.state).inc()
    if is_shadow:
        logger.info(
            "[影子命中] %s (%s) domain=%s conf 待上层记录 — 仅观察不影响路由",
            entry.slug,
            entry.name_zh,
            entry.domain,
        )
    else:
        logger.info(
            "[注册表意图] %s (%s) domain=%s → 叶子 %s",
            entry.slug,
            entry.name_zh,
            entry.domain,
            rep.value,
        )
    return rep


class RuleClassifier:
    """规则分类器（Fast Path）

    正则匹配 + 关键词匹配，返回最高置信度的意图。
    """

    def __init__(self, rules: list[dict[str, Any]] | None = None) -> None:
        self._rules = rules or _RULES
        # 预编译正则
        self._compiled: list[dict[str, Any]] = []
        for rule in self._rules:
            compiled_patterns = [re.compile(p) for p in rule.get("patterns", [])]
            self._compiled.append(
                {
                    "intent": rule["intent"],
                    "patterns": compiled_patterns,
                    "keywords": rule.get("keywords", []),
                    "confidence": rule.get("confidence", 0.7),
                }
            )

    def classify(self, text: str) -> IntentResult:
        """对用户输入进行规则分类

        匹配逻辑：最长匹配优先，多意图时取置信度最高的。
        关键词匹配置信度略低于正则匹配。

        Args:
            text: 用户输入文本

        Returns:
            IntentResult（primary_confidence 低于阈值时触发 Slow Path）
        """
        # 多意图: 收集所有命中的规则, 置信度最高的为主, 其余去重后进 alternatives.
        hits: list[tuple[IntentLabel, float]] = []
        best_intent = IntentLabel.FAQ
        best_confidence = 0.0

        for rule in self._compiled:
            matched = False
            rule_confidence = rule["confidence"]

            # 正则匹配
            for pattern in rule["patterns"]:
                match = pattern.search(text)
                if match:
                    matched = True
                    # 正则匹配使用规则设定的置信度
                    break

            # 关键词匹配（置信度降 0.1）
            if not matched:
                for keyword in rule["keywords"]:
                    if keyword in text:
                        matched = True
                        rule_confidence -= 0.1
                        break

            if matched:
                hits.append((rule["intent"], rule_confidence))
                # 平局时敏感意图优先 (防"挂失+查询"混合句被查询类抢走主意图, 延迟转人工)
                sensitive_tiebreak = (
                    rule_confidence == best_confidence
                    and rule["intent"] in _SENSITIVE_RULE_PRIORITY
                    and best_intent not in _SENSITIVE_RULE_PRIORITY
                )
                if rule_confidence > best_confidence or sensitive_tiebreak:
                    best_intent = rule["intent"]
                    best_confidence = rule_confidence

        return IntentResult(
            primary_intent=best_intent,
            primary_confidence=best_confidence,
            alternatives=_collect_alternatives(hits, best_intent),
        )


class LLMClassifier:
    """LLM 分类器（Slow Path）

    通过 Qwen2.5-7B 的 json_mode 输出结构化分类结果。
    同时提取实体和情感分析。

    #7 降本: 相同输入在 TTL 内复用分类结果 (LRU 有界)。分类慢路径与生成是两次 LLM
    调用, 重复问法/重复会话词面完全一致时免掉第二次分类。缓存只存成功结果, 失败/
    超时兜底不缓存; 返回深拷贝, 调用方对结果的突变(energy/fast_conf 透传)不污染缓存。
    """

    def __init__(self, llm_client: LLMClient) -> None:
        self._llm = llm_client
        self._cache: OrderedDict[str, tuple[float, tuple[IntentResult, list[Entity], SentimentLabel]]] = OrderedDict()

    @staticmethod
    def _copy_cached(
        result: tuple[IntentResult, list[Entity], SentimentLabel],
    ) -> tuple[IntentResult, list[Entity], SentimentLabel]:
        intent, entities, sentiment = result
        return intent.model_copy(deep=True), [e.model_copy(deep=True) for e in entities], sentiment

    async def classify(self, text: str) -> tuple[IntentResult, list[Entity], SentimentLabel]:
        """LLM 意图分类

        Args:
            text: 用户输入文本

        Returns:
            (IntentResult, 实体列表, 情感标签)
        """
        llm_settings = get_settings().llm
        cache_key = text.strip()
        if llm_settings.classify_cache_enabled and cache_key:
            now = time.monotonic()
            hit = self._cache.get(cache_key)
            if hit is not None:
                ts, cached = hit
                if now - ts < llm_settings.classify_cache_ttl_seconds:
                    self._cache.move_to_end(cache_key)
                    logger.debug("LLM 分类缓存命中 (免一次分类调用): %r", text[:40])
                    return self._copy_cached(cached)
                del self._cache[cache_key]
        try:
            # 强制总时长上限: 此前 timeout 只透传给 OpenAI SDK 作 per-read 超时, 对
            # 流式输出(小 token 持续到达)永不触发 → LLM 分类可跑 8s+ (拖垮整轮应答).
            # asyncio.wait_for 给硬总 deadline; 超时走下方兜底 FAQ@0.0 → 下游 low_conf
            # 噪声闸拦回确定性澄清, 不再叠加第二次 LLM 生成.
            timeout = llm_settings.classify_timeout
            result = await asyncio.wait_for(
                self._llm.classify(
                    system_prompt=build_classify_system_prompt(),
                    user_input=text,
                    timeout=timeout,
                ),
                timeout=timeout,
            )
        except Exception:
            # 2026-08-30 修复: 此前内部吞异常返回 faq@0.0, IntentClassifier 的
            # "慢路径失败用 Fast Path 兜底"永远不触发 —— BERT 已认出的 faq@0.446
            # 被覆写成 0.0, 噪声门 low_conf 误杀真问题 (会话 f1fec705/9d64b59)。
            # 改为上抛, 由 IntentClassifier except 走 fast_result 兜底。
            logger.warning("LLM 分类调用失败/超时(≤%.1fs)，上抛交 Fast Path 兜底", llm_settings.classify_timeout)
            raise

        # 运营注册表意图优先: 命中影子/生效条目时按域落代表叶子 (未命中走枚举解析)
        intent_label = _apply_registry_intent(str(result.get("intent", "")))
        if intent_label is None:
            intent_label = _parse_intent(result.get("intent", ""))
        confidence = result.get("confidence", 0.0)
        entities = _parse_entities(result.get("entities", []))
        sentiment = _parse_sentiment(result.get("sentiment", ""))

        parsed = (
            IntentResult(primary_intent=intent_label, primary_confidence=confidence),
            entities,
            sentiment,
        )
        if llm_settings.classify_cache_enabled and cache_key:
            # 写入即深拷贝: 首次返回给调用方的对象仍可被随意突变, 缓存内版本不受污染
            self._cache[cache_key] = (time.monotonic(), self._copy_cached(parsed))
            while len(self._cache) > llm_settings.classify_cache_max_entries:
                self._cache.popitem(last=False)
            self._cache.move_to_end(cache_key)
        return parsed

    async def arbitrate(self, text: str, history: list[dict[str, str]] | None = None) -> dict[str, Any]:
        """P1 模糊带宽仲裁: 让 LLM 判 {business|chitchat|noise} 作为弱信号。

        只作为"决策仲裁"(非分类慢路径): 语义落在模糊带宽(energy/BERT 中段、无铁证)时,
        把原文(可拼 history)交 LLM 判定它到底是业务、闲聊还是噪声, 与证据链投票。
        LLM 结果不单独作通过依据 — 调用方需结合置信/energy/缺槽; 自评置信视为弱信号。

        Returns:
            {"domain": "business"|"chitchat"|"noise"|"unknown",
             "confidence": float, "structured": bool}
            调用失败/LLM 不可用时返回 {"domain": "unknown", "confidence": 0.0, "structured": False}.
        """
        try:
            history_txt = ""
            if history:
                turns = [f"{t.get('speaker')}:{t.get('content')}" for t in history if t.get("content")]
                history_txt = ("\n".join(turns[-6:]) + "\n") if turns else ""
            timeout = get_settings().llm.classify_timeout
            result = await asyncio.wait_for(
                self._llm.classify(
                    system_prompt=_ARBITRATE_SYSTEM_PROMPT,
                    user_input=f"{history_txt}用户: {text}",
                    timeout=timeout,
                ),
                timeout=timeout,
            )
        except Exception:
            logger.warning("LLM 仲裁调用失败，返回 unknown 弱信号")
            return {"domain": "unknown", "confidence": 0.0, "structured": False}

        raw = (result.get("domain") or "").strip().lower()
        if raw not in ("business", "chitchat", "noise"):
            return {"domain": "unknown", "confidence": 0.0, "structured": False}
        conf = float(result.get("confidence", 0.0))
        return {"domain": raw, "confidence": conf, "structured": True}


# P1 LLM 仲裁 prompt: 只判"这到底属于哪一类", 用于模糊带的保守兜底裁决.
_ARBITRATE_SYSTEM_PROMPT = """你是银行信用卡客服的"输入仲裁"。判断一句话到底属于哪类，输出 JSON。
只允许三种类别（严格小写）：
- business: 与银行业务相关的真实诉求（查账/分期/挂失/投诉/额度/积分/转人工等）
- chitchat: 闲聊/寒暄/玩笑，不涉及具体业务
- noise: 乱码/按键误触/无意义输入/与对话无关的噪音

规则：
- 拿不准、或像是在接上文的数字/金额/卡号回话但语义不明 → 归 {business}（保守，宁放过不误拦）
- 明确的乱码（如 hjfw、纯键盘乱敲）→ noise
- 输出格式：{"domain": "business|chitchat|noise", "confidence": 0-1}
只输出 JSON，不要其他文字。
"""


class IntentClassifier:
    """双通道意图分类编排器

    Fast Path（规则｜小 BERT） → 置信度 >= 阈值 → 直接使用
                              → 置信度 < 阈值 → Slow Path（LLM）
    """

    def __init__(
        self,
        rule_classifier: RuleClassifier | None = None,
        llm_classifier: LLMClassifier | None = None,
        fast_threshold: float = _FAST_PATH_THRESHOLD,
        bert_classifier: BertIntentClassifier | None = None,
        trap: TrapCollector | None = None,
        intent_vector=None,
    ) -> None:
        self._rule = rule_classifier or RuleClassifier()
        self._llm = llm_classifier
        self._threshold = fast_threshold
        self._bert = bert_classifier
        self._trap = trap  # P1 感知缝: 被动采样失败/不确定/分歧样本
        # 目标架构 L2: 向量检索意图 (规则未命中时, 种子语料余弦检索)
        self._intent_vector = intent_vector
        # P1: 最近一次 BERT 快路径的 energy-OOD 分 (无 BERT/未启用时为 None).
        # 供上层噪声闸读取, 与 classify 主结果解耦, 不改变返回签名。
        self._last_energy: float | None = None

    # P2-17: 规则路径情绪关键词 (愤怒/负面), 支撑情绪转人工
    _ANGRY_KEYWORDS: frozenset[str] = frozenset(
        {"太生气了", "气死", "愤怒", "忍无可忍", "差劲", "什么态度", "投诉你们", "垃圾", "恼火", "火大"}
    )
    _NEGATIVE_KEYWORDS: frozenset[str] = frozenset(
        {"不满", "失望", "很烦", "烦死了", "郁闷", "难受", "心累", "无语", "焦虑", "担心", "害怕"}
    )

    @staticmethod
    def _rule_sentiment(text: str) -> SentimentLabel:
        """规则情绪检测: 关键词命中 → angry/negative, 否则 neutral."""
        if not text:
            return SentimentLabel.NEUTRAL
        for kw in IntentClassifier._ANGRY_KEYWORDS:
            if kw in text:
                return SentimentLabel.ANGRY
        for kw in IntentClassifier._NEGATIVE_KEYWORDS:
            if kw in text:
                return SentimentLabel.NEGATIVE
        return SentimentLabel.NEUTRAL

    async def classify(
        self,
        text: str,
        history: list[dict[str, str]] | None = None,
    ) -> tuple[IntentResult, list[Entity], SentimentLabel, str]:
        """执行双通道分类

        Args:
            text: 用户输入文本
            history: 可选多轮上下文 (speaker/content), 透传给 BERT 快路径做对话级意图判定

        Returns:
            (IntentResult, 实体列表, 情感标签, 分类来源 "bert"|"rule"|"llm"|"fallback"|"bert:lowconf")
        """
        # Fast Path: 优先小 BERT, 否则规则; BERT 异常时回退规则 (打不挂线上)
        fast_source = "rule"
        if self._bert is not None:
            try:
                fast_result = await self._bert.classify(text, history=history)
                fast_source = "bert"
                # P1: 同次前向顺带算 energy-OOD 分, 供上层噪声闸作"认不认"信号.
                # 开启时丢给线程池再跑一次前向; 失败仅清空, 不阻断 (energy 是辅助信号).
                if get_settings().classification.ood_enabled:
                    try:
                        self._last_energy = await self._bert.ood_score(text, history=history)
                        # 随本次分类结果对象透传 (而非共享属性), 避免并发会话读时串线.
                        fast_result.energy = self._last_energy
                    except Exception:
                        self._last_energy = None
            except Exception:
                logger.warning("BERT 分类失败, 回退规则快路径")
                fast_result = self._rule.classify(text)
                self._last_energy = None
        else:
            fast_result = self._rule.classify(text)
            self._last_energy = None

        # ── L2 向量检索意图 (目标架构 ③: L1 规则 → L2 向量 → L3 LLM) ──
        # L1 未强命中时, 与种子语料余弦检索取意图; 置信不足才落 L3 LLM。
        # BERT 保留为证据信号 (fast_conf/fast_intent 透传审计), 不再单独定路由。
        if (
            fast_result.primary_confidence < self._threshold
            and self._intent_vector is not None
            and get_settings().classification.vector_intent_enabled
        ):
            try:
                vm = await self._intent_vector.search(text)
                if vm.matched and vm.score >= get_settings().classification.vector_intent_threshold:
                    from lumio.shared.intent_taxonomy import (
                        IntentDomain,
                        domain_of,
                        domain_of_with_text,
                        domain_representative,
                    )

                    # L2 判定五域 (骨架第一级); 叶子优先取快路径同域意图 (更精确),
                    # 否则用域代表叶子, 保证 v2 路由 TrafficClass 与域一致
                    try:
                        v_domain = IntentDomain(vm.intent)
                    except ValueError:
                        v_domain = None
                    if v_domain is None:
                        raise ValueError(f"L2 返回未知域: {vm.intent}")
                    v_domain = domain_of_with_text(v_domain, text)  # 定义句式强制咨询域
                    if domain_of(fast_result.primary_intent) == v_domain:
                        v_leaf = fast_result.primary_intent
                    else:
                        v_leaf = domain_representative(v_domain)
                    logger.info("L2 向量域命中: %s@%.3f → 叶子 %s", v_domain.value, vm.score, v_leaf.value)
                    fast_result = IntentResult(
                        primary_intent=v_leaf,
                        primary_confidence=round(min(vm.score, 0.99), 4),
                        alternatives=fast_result.alternatives,
                        energy=fast_result.energy,
                        fast_conf=fast_result.primary_confidence,
                        fast_intent=fast_result.primary_intent,
                    )
                    fast_source = "vector"
            except Exception as exc:
                logger.warning("L2 向量意图检索失败(跳过 L2): %s", exc)

        # 办理词确定性修正 (会话 48882b05 同型): BERT 标签空间仍是旧扁平 10 类,
        # 发不出 limit_apply_increase/decrease — "我要提额"被 BERT 判 limit_query
        # 后会经工具编排真执行提额确认链, 违背"办理交官方渠道, 机器人返回办理介绍"。
        # 规则层高置信 (≥0.95) 命中办理词时以规则意图覆盖快路径; 仅覆盖这两个意图,
        # 其余分类零回归。BERT 关闭时 rule==rule 为空操作。
        rule_fast = self._rule.classify(text)
        if (
            fast_result.primary_intent != rule_fast.primary_intent
            and rule_fast.primary_intent in _APPLY_INTENT_RULE_OVERRIDE
            and rule_fast.primary_confidence >= _APPLY_OVERRIDE_CONF
        ):
            logger.info(
                "办理词规则覆盖快路径: %s@%.2f -> %s@%.2f (text=%r)",
                fast_result.primary_intent.value,
                fast_result.primary_confidence,
                rule_fast.primary_intent.value,
                rule_fast.primary_confidence,
                text[:30],
            )
            fast_result = IntentResult(
                primary_intent=rule_fast.primary_intent,
                primary_confidence=rule_fast.primary_confidence,
                alternatives=fast_result.alternatives,
                energy=fast_result.energy,
                # 审计留痕: 保留 BERT 原始判定, 事后可查"哪一路在幻觉"
                fast_conf=fast_result.primary_confidence,
                fast_intent=fast_result.primary_intent,
            )
            fast_source = "rule"

        # 查询类意图规则覆盖 (一小时模拟 badcase 根治: "帮我查一下信用卡账单"被 BERT
        # 判 faq → 知识链被能力边界红线拦截, 实际应走链 B 工具直查; L3 LLM 分类在
        # 负载下 6s 超时后兜底 BERT, 误判被放大)。规则是人工维护的确定性关键词,
        # 查询意图高置信命中且句式无咨询标记 (怎么/为什么/规则/手续费…) 时比 BERT
        # 可靠 —— 与 _APPLY_INTENT_RULE_OVERRIDE 同型, 仅覆盖查询意图, 咨询句零回归。
        if (
            fast_result.primary_intent != rule_fast.primary_intent
            and rule_fast.primary_confidence >= 0.8
            and normalize_intent(rule_fast.primary_intent.value) in _QUERY_INTENT_OVERRIDES
            and not any(m in text for m in _CONSULTIVE_MARKERS)
        ):
            logger.info(
                "查询词规则覆盖快路径: %s@%.2f -> %s@%.2f (text=%r)",
                fast_result.primary_intent.value,
                fast_result.primary_confidence,
                rule_fast.primary_intent.value,
                rule_fast.primary_confidence,
                text[:30],
            )
            fast_result = IntentResult(
                primary_intent=rule_fast.primary_intent,
                primary_confidence=rule_fast.primary_confidence,
                alternatives=fast_result.alternatives,
                energy=fast_result.energy,
                fast_conf=fast_result.primary_confidence,
                fast_intent=fast_result.primary_intent,
            )
            fast_source = "rule:query"

        if fast_result.primary_confidence >= self._threshold:
            logger.debug(
                "Fast Path 命中: intent=%s, confidence=%.2f, source=%s",
                fast_result.primary_intent.value,
                fast_result.primary_confidence,
                fast_source,
            )
            # P2-17: Fast Path 情绪检测 — 此前规则通道恒 NEUTRAL, 情绪只在 LLM 慢路径
            # 生效 (覆盖面窄). 规则命中时用关键词快速判定愤怒/负面, 支撑情绪转人工.
            await self._emit_sample(text, fast_source, fast_result, fast_result, fast_source)
            return fast_result, extract_entities(text), self._rule_sentiment(text), fast_source

        # P-低置信短路: BERT 快路径强"不认"(置信 < 噪声下限)时, 不跑 LLM 慢路径分类.
        # 慢路径不仅贵 (一次 LLM 分类 ~6s), 还把置信抬到恰 ≥ 下限掩盖噪声, 让下游
        # low_conf 噪声闸失效 → 噪声被漏放到第二次 LLM 生成. 直接返回 fast_result,
        # 交给 _evaluate_noise_gate 的 low_conf 拦回确定性澄清, 零 LLM 开销.
        # 仅在 BERT 快路径命中 (<0.7 才会走到这) 且 BERT 置信 < 下限时触发; 真实业务
        # 样本实测均 ≥0.35, 不受影响; is_replying 豁免由下游闸先行断言。
        if fast_source == "bert" and fast_result.primary_confidence < _LOW_CONF_FLOOR:
            logger.debug(
                "BERT 低置信短路慢路径 (conf=%.3f < %s): skip LLM classify, intent=%s",
                fast_result.primary_confidence,
                _LOW_CONF_FLOOR,
                fast_result.primary_intent.value,
            )
            await self._emit_sample(text, fast_source, fast_result, fast_result, "bert:lowconf")
            return fast_result, extract_entities(text), self._rule_sentiment(text), "bert:lowconf"

        # Slow Path
        if self._llm is None:
            logger.debug("Slow Path 不可用，使用 Fast Path 低置信度结果")
            await self._emit_sample(text, fast_source, fast_result, fast_result, "fallback")
            return fast_result, extract_entities(text), SentimentLabel.NEUTRAL, "fallback"

        # 熔断器打开 → 跳过 LLM
        if not self._llm._llm._breaker.is_available:
            logger.debug("LLM 熔断器打开，跳过 Slow Path")
            await self._emit_sample(text, fast_source, fast_result, fast_result, "fallback")
            return fast_result, extract_entities(text), SentimentLabel.NEUTRAL, "fallback"

        logger.debug(
            "Fast Path 置信度不足 (%.2f < %.2f)，进入 Slow Path",
            fast_result.primary_confidence,
            self._threshold,
        )

        try:
            llm_result, entities, sentiment = await self._llm.classify(text)
        except Exception:
            logger.warning("LLM 分类调用失败，使用 Fast Path 结果兜底")
            await self._emit_sample(text, fast_source, fast_result, fast_result, "fallback")
            return fast_result, extract_entities(text), SentimentLabel.NEUTRAL, "fallback"

        # P0 修复 (会话 0681c635): 慢路径不优于快路径时回退快路径。
        # 慢路径失败/超时兜底 FAQ@0.0、或 LLM 输出非法意图被解析成 FAQ 时,
        # 快路径的正确识别(installment_inquiry@0.533)会被慢路径覆盖成 faq@0.00,
        # 噪声门按 low_confidence 误杀明确业务诉求。规则: 快路径认出了业务意图
        # (非 FAQ), 而慢路径"没认出"(FAQ) 或置信更低 → 信任快路径。
        fast_is_business = fast_result.primary_intent != IntentLabel.FAQ
        slow_conf = llm_result.primary_confidence
        fast_conf = fast_result.primary_confidence
        if fast_is_business and (llm_result.primary_intent == IntentLabel.FAQ or slow_conf < fast_conf):
            logger.debug(
                "慢路径不优于快路径, 回退快路径: slow=%s@%.2f fast=%s@%.2f",
                llm_result.primary_intent.value,
                slow_conf,
                fast_result.primary_intent.value,
                fast_conf,
            )
            await self._emit_sample(text, fast_source, fast_result, fast_result, "fallback")
            fast_result.energy = self._last_energy
            return fast_result, extract_entities(text), SentimentLabel.NEUTRAL, "fallback"

        # LLM 结果置信度也很低时，标记来源为 fallback
        source = "llm" if llm_result.primary_confidence >= 0.3 else "fallback"
        await self._emit_sample(text, fast_source, fast_result, llm_result, source)
        llm_result.energy = self._last_energy  # 快路径 energy 随慢路径结果一起透传
        # P0 快慢分歧信号: 慢路径覆盖快路径时保留快路径意图/置信, 供下游噪声闸与
        # 转人工派发门判"两路分歧" -- 慢路径对乱码的自评置信会稳定通胀(会话 e33d1fa8:
        # BERT limit_query@0.39 -> LLM bill_query@0.7), 最终置信单项不可信.
        llm_result.fast_conf = fast_result.primary_confidence
        llm_result.fast_intent = fast_result.primary_intent
        # 快照实体亦并入规则层抽取结果 (规则层在 LLM 抽漏时兜底), 只去重不覆盖
        return llm_result, _merge_entities(entities, extract_entities(text)), sentiment, source

    async def _emit_sample(
        self,
        text: str,
        fast_source: str,
        fast_result: IntentResult,
        final_result: IntentResult,
        final_source: str,
    ) -> None:
        """P1 感知缝: 把一次分类结果快照交给 TrapCollector 判定是否采样.

        规则通道是廉价正则 (<1ms), 仅在 BERT 为快路径时顺带跑一次用于分歧检测,
        避免重复跑昂贵的 BERT. 采样判定在同步阶段完成, 落库由后台 task 承担.
        """
        trap = self._trap
        if trap is None:
            return
        rule_intent: str | None = None
        divergence = False
        if fast_source == "bert":
            rule_intent = self._rule.classify(text).primary_intent.value
            divergence = rule_intent != fast_result.primary_intent.value
        rec = TrapRecord(
            text=text,
            fast_source=fast_source,
            fast_intent=fast_result.primary_intent.value,
            fast_confidence=fast_result.primary_confidence,
            rule_intent=rule_intent,
            final_source=final_source,
            final_intent=final_result.primary_intent.value,
            final_confidence=final_result.primary_confidence,
            margin=abs(final_result.primary_confidence - self._threshold),
            divergence=divergence,
        )
        await trap.capture(rec)


def get_domain(intent: IntentLabel) -> str:
    """获取意图所属域（用于 supervisor 路由）。入口先归一化, 旧 flat 值自动落到主名。"""
    return INTENT_DOMAINS.get(normalize_intent(intent.value), "fallback")


# ── 解析辅助函数 ──


# LLM 慢路径可能输出中文意图而非枚举值 (实测 qwen2.5:7b 对"我要办分期"输出
# "办理分期" 而非 installment_inquiry)。保守中文关键词 → 枚举映射, 仅在枚举解析
# 失败时兜底, 不覆盖正常枚举输出; 顺序即优先级, 长词在前避免子串误配。
_INTENT_CHINESE_ALIASES: list[tuple[str, IntentLabel]] = [
    ("转人工", IntentLabel.TRANSFER_AGENT),
    ("分期", IntentLabel.INSTALLMENT_INQUIRY),
    ("账单", IntentLabel.BILL_QUERY),
    ("交易", IntentLabel.TRANSACTION_QUERY),
    ("额度", IntentLabel.LIMIT_QUERY),
    ("提额", IntentLabel.LIMIT_APPLY_INCREASE),
    ("降额", IntentLabel.LIMIT_APPLY_DECREASE),
    ("积分", IntentLabel.REWARD_QUERY),
    ("挂失", IntentLabel.CARD_LOSS),
    ("补卡", IntentLabel.CARD_LOSS),
    ("投诉", IntentLabel.COMPLAINT),
    ("人工", IntentLabel.TRANSFER_AGENT),
    ("闲聊", IntentLabel.CHITCHAT),
]


def _parse_intent(raw: str) -> IntentLabel:
    """将 LLM 输出的意图字符串转为 IntentLabel 枚举; 中文变体按别名映射兜底"""
    try:
        return IntentLabel(raw)
    except ValueError:
        for keyword, intent in _INTENT_CHINESE_ALIASES:
            if keyword in raw:
                return intent
        logger.debug("LLM 输出未知意图: %s", raw)
        return IntentLabel.FAQ


def _parse_entities(raw_entities: list[dict[str, Any]]) -> list[Entity]:
    """将 LLM 输出的实体列表转为 Entity 模型。

    实体类型经 normalize_entity_type 映射到规范 key (与 slot_tracker 词汇表对齐),
    未知/乱造的类型名被丢弃, 避免污染 slot 填充与指代消解候选。
    """
    entities: list[Entity] = []
    for e in raw_entities:
        try:
            etype = normalize_entity_type(e.get("entity_type", ""))
            if etype is None:
                continue
            entities.append(
                Entity(
                    entity_type=etype,
                    value=e.get("value", ""),
                    confidence=e.get("confidence", 0.7),
                )
            )
        except Exception:
            logger.debug("实体解析失败: %s", e)
    return entities


def _merge_entities(primary: list[Entity], secondary: list[Entity]) -> list[Entity]:
    """合并实体列表, 保序去重 (同类同值). primary 优先, secondary 兜底补漏。"""
    if not secondary:
        return primary
    if not primary:
        return secondary
    seen = {(e.entity_type, e.value) for e in primary}
    out = list(primary)
    for e in secondary:
        key = (e.entity_type, e.value)
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _parse_sentiment(raw: str) -> SentimentLabel:
    """将 LLM 输出的情感字符串转为 SentimentLabel 枚举"""
    try:
        return SentimentLabel(raw)
    except ValueError:
        return SentimentLabel.NEUTRAL
