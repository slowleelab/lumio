"""共享数据模型

对应概要设计 §2 核心数据模型，定义跨模块复用的 Pydantic 模型。
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field

# ── 枚举类型 ──


class ChannelType(StrEnum):
    WEB = "web"
    APP = "app"
    WECHAT = "wechat"
    PHONE = "phone"


class SessionPhase(StrEnum):
    BOT = "bot"
    AGENT = "agent"
    ENDED = "ended"


class SessionSubPhase(StrEnum):
    """会话子阶段，phase:sub 形式

    BOT 阶段:  bot:active
    AGENT 阶段: agent:queued → agent:assigned → agent:active ⇄ agent:on_hold → agent:reviewing
    ENDED 阶段: 无子阶段，end_reason 记录终止原因
    """

    BOT_ACTIVE = "bot:active"
    AG_QUEUED = "agent:queued"
    AG_ASSIGNED = "agent:assigned"
    AG_ACTIVE = "agent:active"
    AG_ON_HOLD = "agent:on_hold"
    AG_REVIEWING = "agent:reviewing"


class IntentLabel(StrEnum):
    """意图标签全集 (draft-0.3 主表: 14 域 × 149 意图 + 10 个旧 flat 别名)。

    旧 10 个 flat 值保留为别名成员, normalize_intent 把旧字符串归一化到主名
    (如 "bill_query" → account_bill_query)。分类器(规则/BERT/LLM)在批 2 重训前
    仍输出旧值, 路由/合规/槽位等边界统一在入口 normalize 后比较; 持久化层
    (session 反序列化)已走 normalize, 存量旧字符串读回即主名。
    """

    # ── 旧 flat 别名 (存量兼容; 归一化目标见 _INTENT_NORMALIZATION) ──
    FAQ = "faq"  # → faq_product
    BILL_QUERY = "bill_query"  # → account_bill_query
    TRANSACTION_QUERY = "transaction_query"  # → txn_query
    LIMIT_QUERY = "limit_query"  # 旧值=主名 identity
    INSTALLMENT_INQUIRY = "installment_inquiry"  # → inst_param_query
    REWARD_QUERY = "reward_query"  # → points_balance_query
    CARD_LOSS = "card_loss"  # → card_loss_report
    COMPLAINT = "complaint"  # → dispute_submit
    TRANSFER_AGENT = "transfer_agent"  # 旧值=主名 identity
    CHITCHAT = "chitchat"  # → nb_chitchat

    # ── 1.1 账户与账单域 (account, 10) ──
    ACCOUNT_BILL_QUERY = "account_bill_query"
    ACCOUNT_E_BILL_SET = "account_e_bill_set"
    ACCOUNT_PAPER_BILL_REISSUE = "account_paper_bill_reissue"
    ACCOUNT_STMT_QUERY = "account_stmt_query"
    ACCOUNT_STMT_DISPUTE = "account_stmt_dispute"
    ACCOUNT_BILL_EXPORT = "account_bill_export"
    ACCOUNT_BILL_REPAY_SPLIT_SET = "account_bill_repay_split_set"
    ACCOUNT_BILL_ALERT_SET = "account_bill_alert_set"
    ACCOUNT_BALANCE_QUERY = "account_balance_query"
    ACCOUNT_FOREX_RATE_QUERY = "account_forex_rate_query"

    # ── 1.2 交易与消费域 (transaction, 10) ──
    TXN_QUERY = "txn_query"
    TXN_CASH_ADVANCE_QUERY = "txn_cash_advance_query"
    TXN_AUTO_DEBIT_SET = "txn_auto_debit_set"
    TXN_AUTO_DEBIT_QUERY = "txn_auto_debit_query"
    TXN_REFUND_QUERY = "txn_refund_query"
    TXN_RECEIPT_GET = "txn_receipt_get"
    TXN_CURRENCY_SET = "txn_currency_set"
    TXN_OVERSEAS_LOCK = "txn_overseas_lock"
    TXN_CATEGORY_STAT = "txn_category_stat"
    TXN_EXPORT = "txn_export"

    # ── 1.3 还款与还款日域 (repay, 14) ──
    REPAY_PLAN_QUERY = "repay_plan_query"
    REPAY_RECORD_QUERY = "repay_record_query"
    REPAY_CALC = "repay_calc"
    REPAY_METHOD_QUERY = "repay_method_query"
    REPAY_AUTO_SET = "repay_auto_set"
    REPAY_EARLY = "repay_early"
    REPAY_GRACE_PERIOD = "repay_grace_period"
    REPAY_OVERDUE_QUERY = "repay_overdue_query"
    REPAY_OVERDUE_RELIEF = "repay_overdue_relief"
    REPAY_OVERDUE_PLAN = "repay_overdue_plan"
    REPAY_APPOINTMENT = "repay_appointment"
    REPAY_VOUCHER = "repay_voucher"
    REPAY_SETTLE = "repay_settle"
    REPAY_DEDUCTION_ORDER = "repay_deduction_order"

    # ── 1.4 额度与授信域 (limit, 9) ──
    LIMIT_APPLY_INCREASE = "limit_apply_increase"
    LIMIT_APPLY_DECREASE = "limit_apply_decrease"
    LIMIT_POLICY_QUERY = "limit_policy_query"
    LIMIT_HISTORY_QUERY = "limit_history_query"
    LIMIT_APPLY_STATUS = "limit_apply_status"
    LIMIT_TYING_QUERY = "limit_tying_query"
    LIMIT_POOL_QUERY = "limit_pool_query"
    LIMIT_USAGE_ALERT_SET = "limit_usage_alert_set"

    # ── 1.5 分期业务域 (installment, 11) ──
    INST_APPLY = "inst_apply"
    INST_PARAM_QUERY = "inst_param_query"
    INST_CALC = "inst_calc"
    INST_STATUS_QUERY = "inst_status_query"
    INST_EARLY_SETTLE = "inst_early_settle"
    INST_CHANGE_SET = "inst_change_set"
    INST_CANCEL = "inst_cancel"
    INST_REFUND_RULE = "inst_refund_rule"
    INST_FOREX = "inst_forex"
    INST_PROMOTION = "inst_promotion"
    INST_CONTRACT = "inst_contract"

    # ── 1.6 积分与权益域 (points, 13) ──
    POINTS_BALANCE_QUERY = "points_balance_query"
    POINTS_REDEEM = "points_redeem"
    POINTS_EXPIRY_QUERY = "points_expiry_query"
    POINTS_EXPIRY_ALARM_SET = "points_expiry_alarm_set"
    POINTS_TRANSFER = "points_transfer"
    POINTS_RULE_QUERY = "points_rule_query"
    POINTS_ORDER_QUERY = "points_order_query"
    BENEFIT_QUERY = "benefit_query"
    BENEFIT_CLAIM = "benefit_claim"
    BENEFIT_REASSIGN = "benefit_reassign"
    BENEFIT_UPGRADE = "benefit_upgrade"
    CAMPAIGN_QUERY = "campaign_query"
    CAMPAIGN_SIGNUP = "campaign_signup"

    # ── 1.7 卡片与生命周期域 (card, 15) ──
    CARD_LOSS_REPORT = "card_loss_report"
    CARD_LOSS_CANCEL = "card_loss_cancel"
    CARD_REISSUE = "card_reissue"
    CARD_APPLY_NEW = "card_apply_new"
    CARD_ACTIVATE = "card_activate"
    CARD_EXPIRE_RENEW = "card_expire_renew"
    CARD_CANCEL = "card_cancel"
    CARD_STATUS_QUERY = "card_status_query"
    CARD_FREEZE = "card_freeze"
    CARD_PIN_SET = "card_pin_set"
    CARD_PIN_FORGOT = "card_pin_forgot"
    CARD_INFO_QUERY = "card_info_query"
    CARD_SUPPLEMENTARY = "card_supplementary"
    CARD_UPGRADE = "card_upgrade"
    CARD_GIFT_QUERY = "card_gift_query"

    # ── 1.8 支付与渠道域 (payment, 9) ──
    PAY_METHOD_QUERY = "pay_method_query"
    PAY_WALLET_BIND = "pay_wallet_bind"
    PAY_WALLET_UNBIND = "pay_wallet_unbind"
    PAY_CONTACTLESS = "pay_contactless"
    PAY_LARGE_VERIFY = "pay_large_verify"
    PAY_ONLINE_SET = "pay_online_set"
    PAY_PASSWORD_ONLINE = "pay_password_online"
    PAY_PAUSE = "pay_pause"
    PAY_MAGNETIC_ISSUE = "pay_magnetic_issue"

    # ── 1.9 费用与费率域 (fee, 13) ──
    FEE_ANNUAL = "fee_annual"
    FEE_INTEREST = "fee_interest"
    FEE_PENALTY = "fee_penalty"
    FEE_OVERLIMIT = "fee_overlimit"
    FEE_SERVICE = "fee_service"
    FEE_OVERSEAS = "fee_overseas"
    FEE_CASH = "fee_cash"
    FEE_TRANSFER = "fee_transfer"
    FEE_CARD_MATERIAL = "fee_card_material"
    FEE_RATE_QUERY = "fee_rate_query"
    FEE_SETTLE_INQUIRY = "fee_settle_inquiry"
    FEE_CHARGED_QUERY = "fee_charged_query"
    FEE_APPEAL = "fee_appeal"

    # ── 1.10 风险管理域 (risk, 13) ──
    RISK_FRAUD_REPORT = "risk_fraud_report"
    RISK_CASH_ADVANCE_WARN = "risk_cash_advance_warn"
    RISK_MONEY_LAUNDRY = "risk_money_laundry"
    RISK_ACCOUNT_FREEZE = "risk_account_freeze"
    RISK_CONTACT_WARN = "risk_contact_warn"
    RISK_FRAUD_HOTLINE = "risk_fraud_hotline"
    RISK_ATM_ANOMALY = "risk_atm_anomaly"
    RISK_POS_ANOMALY = "risk_pos_anomaly"
    RISK_KYC = "risk_kyc"
    RISK_SMS_VERIFY = "risk_sms_verify"
    RISK_PIN_LEAK = "risk_pin_leak"
    RISK_OVERSEAS_TRAVEL = "risk_overseas_travel"
    RISK_WALLET_SAFETY = "risk_wallet_safety"

    # ── 1.11 争议与投诉域 (dispute, 12) ──
    DISPUTE_SUBMIT = "dispute_submit"
    DISPUTE_STATUS = "dispute_status"
    DISPUTE_APPEAL = "dispute_appeal"
    DISPUTE_CHARGEBACK = "dispute_chargeback"
    DISPUTE_REGULATE = "dispute_regulate"
    DISPUTE_HOTLINE = "dispute_hotline"
    DISPUTE_URGE = "dispute_urge"
    DISPUTE_WITHDRAW = "dispute_withdraw"
    DISPUTE_MATERIAL = "dispute_material"
    DISPUTE_COMPENSATION = "dispute_compensation"
    DISPUTE_CLOSE = "dispute_close"
    DISPUTE_POLICY = "dispute_policy"

    # ── 1.12 转人工与人工服务域 (handoff, 8) ──
    # transfer_agent 即旧 flat 值 (identity), 已在上方别名区声明
    HANDOFF_QUEUE_QUERY = "handoff_queue_query"
    HANDOFF_HOURS_QUERY = "handoff_hours_query"
    HANDOFF_END = "handoff_end"
    HANDOFF_RESTART = "handoff_restart"
    HANDOFF_SCHEDULE = "handoff_schedule"
    HANDOFF_HOTLINE = "handoff_hotline"
    HANDOFF_VERIFY = "handoff_verify"

    # ── 1.13 知识问答与政策域 (faq, 9) ──
    FAQ_PRODUCT = "faq_product"
    FAQ_CREDIT_REPORT = "faq_credit_report"
    FAQ_CONTRACT = "faq_contract"
    FAQ_NOTICE = "faq_notice"
    FAQ_COMPLIANCE = "faq_compliance"
    FAQ_DATA = "faq_data"
    FAQ_CHANNEL = "faq_channel"
    FAQ_ACCOUNT_POLICY = "faq_account_policy"
    FAQ_ANY = "faq_any"

    # ── 1.14 非业务域 (nonbusiness, 3) ──
    NB_CHITCHAT = "nb_chitchat"
    NB_NOISE = "nb_noise"
    NB_HELP = "nb_help"


# 旧 flat 意图值 -> 归一化后的主意图 (draft-0.3 §3.1 兼容映射)。
# 存量 Redis/PG/回流样本里的旧字符串在此归一化; 未知字符串兜底 FAQ。
_INTENT_NORMALIZATION: dict[str, IntentLabel] = {
    "faq": IntentLabel.FAQ_PRODUCT,
    "bill_query": IntentLabel.ACCOUNT_BILL_QUERY,
    "transaction_query": IntentLabel.TXN_QUERY,
    "limit_query": IntentLabel.LIMIT_QUERY,  # identity
    "installment_inquiry": IntentLabel.INST_PARAM_QUERY,
    "reward_query": IntentLabel.POINTS_BALANCE_QUERY,
    "card_loss": IntentLabel.CARD_LOSS_REPORT,
    "complaint": IntentLabel.DISPUTE_SUBMIT,
    "transfer_agent": IntentLabel.TRANSFER_AGENT,  # identity
    "chitchat": IntentLabel.NB_CHITCHAT,
}


def normalize_intent(value: str) -> IntentLabel:
    """把 (旧/新) 意图字符串归一化为主意图 IntentLabel (draft-0.3 §3.2)。

    - 旧 flat 值 → 归一化到主名 (存量兼容, 查 _INTENT_NORMALIZATION)
    - 已是主名/合法枚举值 → 直接返回
    - 未知字符串 → 兜底 FAQ, 不抛异常
    """
    canonical = _INTENT_NORMALIZATION.get(value)
    if canonical is not None:
        return canonical
    try:
        return IntentLabel(value)
    except ValueError:
        pass
    return IntentLabel.FAQ


# 敏感写意图: 命中必须紧急转人工 / assist URGENT, 不允许走工具或 RAG 兜底 (合规底线)。
# draft-0.3 主表 ⚠️ 集合 + 旧 flat 别名 (分类器重训前仍输出旧值, 集合双世界兼容),
# 使用 in {集合} 而非点对点精确比较。
SENSITIVE_INTENTS: frozenset[IntentLabel] = frozenset(
    {
        # 旧 flat 别名
        IntentLabel.CARD_LOSS,
        IntentLabel.COMPLAINT,
        # draft-0.3 ⚠️ 主名集合
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
)

# 主动转人工意图: 用户明确要求转人工。
TRANSFER_INTENTS: frozenset[IntentLabel] = frozenset({IntentLabel.TRANSFER_AGENT})


class SentimentLabel(StrEnum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    ANGRY = "angry"


class AlertLevel(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertCategory(StrEnum):
    COMPLIANCE = "compliance"
    EMOTION = "emotion"
    SILENCE = "silence"
    PROCESS = "process"


class TransferTriggerLevel(StrEnum):
    L1 = "L1"  # 关键词触发
    L2 = "L2"  # 语义识别
    L3 = "L3"  # 连续低置信度


class DegradationLevel(StrEnum):
    """LLM 降级级别"""

    NORMAL = "normal"  # LLM 可用，正常调用
    DEGRADED = "degraded"  # LLM 降级，跳过 LLM 用检索摘要
    FALLBACK = "fallback"  # LLM 不可用，跳过检索直接用模板


class RiskActionEnum(StrEnum):
    """风控动作"""

    PASS = "PASS"
    WARN = "WARN"
    BLOCK = "BLOCK"


class AssistEngineState(StrEnum):
    """编排引擎状态"""

    IDLE = "IDLE"
    EVALUATING = "EVALUATING"
    DISPATCHING = "DISPATCHING"
    WAITING_RESULTS = "WAITING_RESULTS"
    COMPLETED = "COMPLETED"


# ── 状态转换白名单 ──

VALID_TRANSITIONS: dict[tuple[str, str], set[str]] = {
    ("bot", "bot:active"): {"agent:queued", "ended"},
    # FIX-8: agent:assigned (振铃) 由外部 chat-svc 驱动, lumio 本地无触发路径 —
    # 保留表项兼容外部回调, 但不设本地超时守卫 (见 session_timeout._get_timeout)
    ("agent", "agent:queued"): {"agent:assigned", "bot:active", "ended"},
    ("agent", "agent:assigned"): {"agent:active", "agent:queued", "ended"},
    ("agent", "agent:active"): {"agent:on_hold", "agent:assigned", "agent:reviewing", "bot:active", "ended"},
    ("agent", "agent:on_hold"): {"agent:active", "ended"},
    ("agent", "agent:reviewing"): {"ended"},
}

_SUB_PHASE_TO_PHASE: dict[SessionSubPhase, SessionPhase] = {
    SessionSubPhase.BOT_ACTIVE: SessionPhase.BOT,
    SessionSubPhase.AG_QUEUED: SessionPhase.AGENT,
    SessionSubPhase.AG_ASSIGNED: SessionPhase.AGENT,
    SessionSubPhase.AG_ACTIVE: SessionPhase.AGENT,
    SessionSubPhase.AG_ON_HOLD: SessionPhase.AGENT,
    SessionSubPhase.AG_REVIEWING: SessionPhase.AGENT,
}


def validate_transition(phase: SessionPhase, sub_phase: SessionSubPhase, target_sub: SessionSubPhase) -> bool:
    """校验状态转换是否合法"""
    key = (phase.value, sub_phase.value)
    allowed = VALID_TRANSITIONS.get(key)
    if allowed is None:
        return False
    return target_sub.value in allowed


# ── 基础数据结构 ──


class Entity(BaseModel):
    """抽取的实体"""

    entity_type: str
    value: str
    start: int | None = None
    end: int | None = None
    confidence: float = 1.0


class IntentResult(BaseModel):
    """意图分类结果"""

    # exclude: 内部透传字段, 不随 model_dump 进决策日志/对外响应.
    primary_intent: IntentLabel
    primary_confidence: float
    alternatives: list[IntentLabel] = Field(default_factory=list)
    # 次选意图分数 (与 alternatives 按下标对齐)。会话 22ad 复盘: 次选只带标签不带
    # 分数, 路由策略只能"有业务次选就放行" — LLM/BERT 的对冲性弱次选 (softmax 第
    # 二三名, 常 <0.3) 也能挡掉闲聊短路。空列表 = 无分数, 调用方按保守语义处理。
    alternative_scores: list[float] = Field(default_factory=list)
    # P1: 本次分类的 energy-OOD 分 (-logsumexp). 随本对象按次透传给上层噪声闸,
    # 避免各 session 并发时共享 classifier._last_energy 造成跨会话串线.
    energy: float | None = Field(default=None, exclude=True)
    # P0 快慢分歧信号: LLM 慢路径覆盖快路径时, 透传 BERT 快路径的意图与置信,
    # 供噪声门/转人工派发门判"两路分歧"(乱码被慢路径高置信幻觉成 bill_query@0.7
    # 而快路径只给 limit_query@0.39, 见会话 e33d1fa8). None = 无慢路径覆盖.
    fast_conf: float | None = Field(default=None, exclude=True)
    fast_intent: IntentLabel | None = Field(default=None, exclude=True)
    # 单次结构化裁决 (架构整改): LLM 慢路径分类与"业务/闲聊/噪声"仲裁合并为同一次
    # 调用, 此字段即仲裁结论 ("business"|"chitchat"|"noise"). None = 本次结果未经
    # LLM 慢路径 (快路径短路/慢路径失败兜底), 噪声门需要仲裁时再独立调用兜底。
    llm_input_class: str | None = Field(default=None, exclude=True)
    # 分类状态 (意图体系拆分·路由层): "bert"|"vector"|"rule"|"rule:query"|"llm" =
    # 真识别; "fallback"|"bert:lowconf"|"bert:ood" = 弱识别/兜底; None = 分类器
    # 异常未识别。兜底轮的 faq 标签是存储兼容残差, 不代表"识别为知识咨询"。
    classification_source: str | None = Field(default=None, exclude=True)


class SentimentResult(BaseModel):
    """情感分析结果"""

    label: SentimentLabel
    score: float


class EmotionVector(BaseModel):
    """情绪向量（带时间衰减）

    对应设计文档 §3.1 情绪向量衰减公式:
    emotion_vector(t) = emotion_raw × exp(-λ × Δt)
    λ = 0.005 (半衰期约 2.3 分钟, 适配客服对话节奏)
    """

    label: SentimentLabel
    score: float = Field(ge=0.0, le=1.0)
    decay_lambda: float = 0.005
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def decayed_score(self, delta_seconds: float) -> float:
        """计算衰减后的情绪分数"""
        import math

        return self.score * math.exp(-self.decay_lambda * delta_seconds)


class DialogueTurn(BaseModel):
    """对话轮次

    包含完整决策上下文，支持事后回溯 Bot 推理链。
    """

    turn_id: str
    session_id: str
    speaker: Literal["customer", "agent", "bot"]
    content: str
    emotion_label: SentimentLabel | None = None
    emotion_score: float | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))

    # 决策上下文（Bot 轮次填充，支持回溯推理链）
    intent: IntentLabel | None = None
    confidence: float | None = None
    entities: list[Entity] = Field(default_factory=list)
    response_source: str = ""  # rag / fallback / template / bank_api
    retrieval_context: str = ""  # RAG 检索到的知识片段摘要


# ── 会话状态 ──


class PendingAction(BaseModel):
    """待用户确认的敏感工具调用

    敏感工具（如挂失、调额、账单分期）在 LLM 请求调用后不立即执行，
    而是暂存于此，返回确认话术，等待用户下一轮回复「确认/取消」。
    """

    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    tool_call_id: str = ""  # 关联 LLM 返回的 tool_call id
    confirm_prompt: str = ""  # 已生成的确认话术
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expires_at: datetime | None = None  # 过期时间，超时需重新发起
    trace_id: str = ""  # 链路追踪 id
    unclear_count: int = 0  # 确认窗口内无法判定的次数, ≥3 自动取消并放行新消息
    # 身份核验状态机 (会话 564db34d 复盘): 敏感写工具执行前先经前端身份核验弹框
    # none -> pending(已发起核验, 等前端回传) -> verified(核验通过, 等客户确认)
    verification_state: str = "none"  # none | pending | verified
    verification_token: str = ""  # 前端核验回传凭据


class VerificationRequest(BaseModel):
    """前端身份核验弹框信号 (后端 -> 前端)

    poll 响应带该结构化字段时, 前端据此弹出核验框(短信/人脸/交易密码)。
    """

    token: str  # 核验会话令牌, 前端核验完成后原样回传
    type: str = "sms"  # sms | face | password
    title: str = "身份核验"
    description: str = ""  # 待办理业务摘要(如"办理 3 期账单分期, 金额 8000 元")
    business: str = ""  # 待办理工具名


class VerificationResult(BaseModel):
    """前端身份核验结果回传 (前端 -> 后端, 经 /chat/send)"""

    token: str
    status: str = "success"  # success | cancel | failed


class SlotValue(BaseModel):
    """槽位已填值（随会话 meta 持久化，跨意图保留）

    name 为规范槽名（amount/period/card_tail/phone_number/card_number/card_type/issue_detail）。
    与静态 per-intent 槽位定义（SlotTracker._INTENT_SLOTS）分离：此处只存"已收集到的值"，
    是否必填、追问话术由当前意图的静态定义计算，两者互不耦合。
    """

    name: str
    value: str
    source: str = "entity"  # entity | derived | message
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class TopicRequestStatus(StrEnum):
    """会话内诉求生命周期 (多轮会话管理, 2026-09-04)

    断档/带偏同根源根治: 系统此前没有"客户有哪些进行中诉求"的表示 —
    挂失说了没办完, 切话题后诉求蒸发 (断档); 旧话题又过度影响新轮
    判定 (带偏)。诉求跟踪器让两边都有显式状态可依。
    """

    OPEN = "open"  # 已提出, 未开始处理
    WAITING_INFO = "waiting_info"  # 处理中, 等客户补参数/确认
    FULFILLED = "fulfilled"  # 已办结 (工具执行/知识回答完成)
    DROPPED = "dropped"  # 客户明确放弃 / 会话结束


class TopicRequest(BaseModel):
    """进行中的客户诉求 (会话级, 跨轮持久)"""

    id: str
    intent: str  # IntentLabel value
    label_zh: str  # 回访话术用中文名 ("挂失")
    urgency: str = "normal"  # high: 挂失/投诉/转人工域 → 切话题后回访
    status: TopicRequestStatus = TopicRequestStatus.OPEN
    raised_turn: int = 0
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    revisit_count: int = 0  # 回访次数 (防骚扰上限)


class SessionState(BaseModel):
    """会话状态对象

    对应概要设计 §2.2，存储在 Redis 中。
    """

    session_id: str
    customer_id: str | None = None
    channel_type: ChannelType = ChannelType.WEB
    current_phase: SessionPhase = SessionPhase.BOT
    sub_phase: SessionSubPhase | None = SessionSubPhase.BOT_ACTIVE
    end_reason: str | None = None  # completed/timeout/cust_disconnect/agent_disconnect/system_error

    # 客户画像
    vip_level: str = "普通"
    card_types: list[str] = Field(default_factory=list)
    risk_tolerance: str = "R2"
    # D0 衰减: 每个画像字段的最近更新时间戳 (Unix seconds)
    # 缺失时 fallback 0.0 → 触发 999 天 → 强制降级 (D0 设计)
    vip_level_updated_at: float = 0.0
    risk_tolerance_updated_at: float = 0.0
    card_types_updated_at: float = 0.0

    # 对话历史
    turns: list[DialogueTurn] = Field(default_factory=list)
    turn_count: int = 0

    # 机器人阶段状态
    last_intent: IntentLabel | None = None
    last_entities: list[Entity] = Field(default_factory=list)
    confidence_history: list[float] = Field(default_factory=list)
    low_confidence_streak: int = 0
    human_request_score: int = 0

    # 槽位已填值（生产级: 随会话 meta 持久化, 单一真相源, 跨意图保留）
    slot_values: dict[str, SlotValue] = Field(default_factory=dict)

    # 槽位等待快照（P0 会话 1fb54681 复盘）: bot 发出槽位追问/参数索取的那一轮把
    # "我在等什么槽"写进来, 下一轮短回复("3"/"3000"/裸卡号)无论被分类成什么,
    # 噪声门都读这份上文快照判回话, 不再依赖本轮意图(短填充必然 faq@0.00)重算 tracker。
    # 结构: {"intent": "installment_inquiry", "slots": [("amount","分期金额"), ...]}
    # 只存槽名/标签等元信息, 不含敏感值; 下轮放行/正常流转后整体覆写清空。
    awaiting_slots: dict[str, Any] = Field(default_factory=dict)

    # 进行中诉求 (多轮会话管理): 客户已表达且未办结/未放弃的诉求清单,
    # 每轮按意图 upsert、按回复来源流转状态; 高紧急未办结诉求切话题后回访
    active_requests: list[TopicRequest] = Field(default_factory=list)

    # 对话摘要压缩（长对话场景）
    conversation_summary: str = ""  # 被裁剪轮次的摘要
    summary_turn_count: int = 0  # 已纳入摘要的轮次数（近似值，LTRIM 后可能不准）
    last_summarized_turn_id: str = ""  # 最后一个被摘要的 turn_id（精确追踪）

    # 坐席阶段状态
    agent_id: str | None = None
    # 编排层扩展字段（对应设计文档 §3.2）
    intent_stack: list[IntentLabel] = Field(default_factory=list)
    entity_pool: list[Entity] = Field(default_factory=list)
    emotion_vector: EmotionVector | None = None
    suppress_flag: bool = False  # 营销压制标记（单向门 false→true，对应文档 §3.2 覆写规则）
    node_position: str = ""  # DAG 节点位置
    risk_pending_audit: bool = False  # 风控待审标记
    transfer_reason: str | None = None
    transfer_summary: str | None = None

    # 工具确认状态机：存在未过期 pending_action 时，下一轮拦截判定确认/取消
    pending_action: PendingAction | None = None

    # 元数据
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    last_active_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    version: int = 1


# ── 知识库元数据 ──


class CategoryEnum(StrEnum):
    """知识文档业务分类"""

    FAQ = "FAQ"
    FEE = "费率"
    POINTS = "积分"
    ANNUAL_FEE = "年费"
    REGULATIONS = "章程"
    REPAYMENT = "还款"
    SECURITY = "安全"
    ACTIVITY = "活动"
    OTHER = "OTHER"


class DocumentMetadata(BaseModel):
    """文档元数据，入库管道使用"""

    doc_id: str
    category: str
    doc_type: str
    keywords: list[str] = Field(default_factory=list)
    card_type: str | None = None
    customer_tier: str | None = None
    effective_date: str | None = None
    expiry_date: str | None = None
    security_level: str = "internal"
    version: str = "1.0"


class RerankResult(BaseModel):
    """重排序结果"""

    index: int
    relevance_score: float
    text: str


# ── 检索结果 ──


class RetrievedChunk(BaseModel):
    """检索到的知识块"""

    chunk_id: str
    content: str
    score: float
    source_doc: str
    metadata: dict = Field(default_factory=dict)


class RetrieveRequest(BaseModel):
    """检索请求"""

    query: str
    top_k: int = 5
    filters: dict = Field(default_factory=dict)
    rerank: bool = True
    search_type: Literal["hybrid", "bm25_only", "vector_only"] = "hybrid"
    rrf_k: int | None = None  # 覆盖 RRF k 参数；None 时使用配置默认值

    # 银行合规: 权限 + 时间过滤
    user_role: str | None = None  # 调用者角色，用于权限过滤
    include_expired: bool = False  # 是否包含已过期政策（默认仅返回生效中的）


class RetrieveResponse(BaseModel):
    """检索响应"""

    results: list[RetrievedChunk] = Field(default_factory=list)
    total_candidates: int = 0
    latency_ms: int = 0


# ── 坐席辅助推送 ──


class ScriptCard(BaseModel):
    """话术卡片"""

    script_id: str
    content: str
    tags: list[str] = Field(default_factory=list)
    priority: int = 1


class KnowledgeSnippet(BaseModel):
    """知识片段"""

    chunk_id: str
    summary: str
    source: str
    confidence: Literal["high", "medium", "low"] = "medium"


class AlertObject(BaseModel):
    """告警对象"""

    level: AlertLevel
    category: AlertCategory
    message: str
    suggestion: str = ""


class ProductRecommendation(BaseModel):
    """产品推荐"""

    product_id: str
    product_name: str
    reason: str
    script_suggestion: str
    risk_tip: str
    eligibility_match: bool = True


class AssistPushPayload(BaseModel):
    """坐席辅助推送载荷"""

    scripts: list[ScriptCard] = Field(default_factory=list)
    knowledge: list[KnowledgeSnippet] = Field(default_factory=list)
    alerts: list[AlertObject] = Field(default_factory=list)
    recommendations: list[ProductRecommendation] = Field(default_factory=list)


class AssistPushMessage(BaseModel):
    """坐席辅助推送消息"""

    type: str = "assist_push"
    session_id: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    trigger: str = ""
    payload: AssistPushPayload = Field(default_factory=AssistPushPayload)


class ExecutorResult(BaseModel):
    """执行器结果"""

    executor_id: str
    ui_schema: dict = Field(default_factory=dict)
    latency_ms: int = 0
    success: bool = True
    degraded: bool = False
    degradation_type: str = ""
    risk_action: RiskActionEnum | None = None
    trace_id: str = ""


class ArbitrationResult(BaseModel):
    """仲裁结果"""

    primary_card: dict | None = None
    risk_badge: dict | None = None
    marketing_slot: dict | None = None
    fusion_type: str = "service_only"
    trace_id: str = ""


class OrchestrationState(BaseModel):
    """编排引擎状态（每次 OE 调度周期的快照）"""

    session_id: str
    oe_state: AssistEngineState = AssistEngineState.IDLE
    d1_activated: bool = False
    d2_activated: bool = False
    d3_activated: bool = True  # 风控始终激活
    d1_cooldown_remaining: int = 0
    d2_cooldown_remaining: int = 0
    activation_history: list[dict] = Field(default_factory=list)
    global_timeout_ms: int = 5000


class FeedbackSignal(BaseModel):
    """隐式反馈信号

    对应设计文档 §3.6 反馈闭环层:
    - 直接发送 → accept, confidence 1.0
    - 修改后发送 → modify, confidence 0.5
    - 复制部分内容 → partial_accept, confidence 0.3
    - 忽略 → reject, confidence 0.0
    """

    session_id: str
    agent_id: str
    action: Literal["accept", "modify", "partial_accept", "reject"] = "reject"
    confidence: float = 0.0
    modify_fields: list[str] = Field(default_factory=list)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


# ── 话后小结 ──


class CallSummary(BaseModel):
    """话后小结"""

    summary_id: str
    session_id: str
    customer_demand: str = ""
    problem_category: str = ""
    solution_provided: str = ""
    resolution_status: str = ""
    key_info: dict = Field(default_factory=dict)
    sentiment: SentimentLabel = SentimentLabel.NEUTRAL
    confidence: float = 0.0


# ── API 请求/响应 ──


class ChatRequest(BaseModel):
    """机器人聊天请求"""

    session_id: str | None = Field(default=None, max_length=128)
    customer_id: str | None = Field(default=None, max_length=128)
    customer_name: str | None = Field(default=None, max_length=64)
    # P3-7 整改: message 加 max_length=2000 防 DoS (单条消息 1MB 直接进 Redis Stream + LLM 浪费 token)
    message: str = Field(..., max_length=2000)
    channel: ChannelType = ChannelType.WEB
    # FIX-7: 客户端幂等键 — 双击/重试时携带同一 client_message_id, 服务端只处理一次
    client_message_id: str | None = Field(default=None, max_length=64)
    # 身份核验结果回传 (前端弹框完成后回传, 复用 /chat/send 通道)
    verification_result: VerificationResult | None = None


class ChatResponse(BaseModel):
    """机器人聊天响应"""

    session_id: str
    reply: str
    intent: IntentLabel | None = None
    confidence: float = 0.0
    source: str = "rag"  # rag / fallback / bank_api
    is_transfer: bool = False


# ── 长轮询 ──


class ChatSendRequest(ChatRequest):
    """客户端发送消息请求（复用 ChatRequest 字段）"""

    pass


class ChatSendResponse(BaseModel):
    """发送消息响应"""

    accepted: bool = True
    message_id: str
    session_id: str


class PollResponse(BaseModel):
    """长轮询响应"""

    has_message: bool = False
    session_id: str = ""
    reply: str = ""
    intent: IntentLabel | None = None
    confidence: float = 0.0
    source: str = "rag"
    is_transfer: bool = False
    transfer_url: str = ""
    transfer_reason: str = ""
    # 身份核验弹框信号: 非空时前端弹出核验框 (短信/人脸/交易密码)
    verification: VerificationRequest | None = None


class SessionUpdateRequest(BaseModel):
    """会话状态更新请求（chat-svc 回调）"""

    session_id: str
    phase: Literal["AGENT", "ENDED", "agent", "ended"]
    sub_phase: str | None = None
    agent_id: str | None = None
    end_reason: str | None = None


class SessionUpdateResponse(BaseModel):
    """会话状态更新响应"""

    status: str = "ok"
