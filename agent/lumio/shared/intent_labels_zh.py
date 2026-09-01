"""意图中文标签表 (目标架构 管理端模块②)

149 个意图的中文名称与定义, 按 draft-0.3 五域骨架分类。
此前意图只有英文 slug, 审计/意图树/属性表无中文说明可看。
"""

from __future__ import annotations

# intent_slug → (name_zh, definition_zh)
INTENT_LABELS_ZH: dict[str, tuple[str, str]] = {
    # ── 1.1 账户与账单域 ──
    "account_bill_query": ("账单查询", "查询本期或历史账单、应还金额、最低还款"),
    "account_bill_alert_set": ("账单提醒设置", "设置账单出账/还款日提醒"),
    "account_bill_export": ("账单导出", "导出电子版账单文件"),
    "account_bill_repay_split_set": ("账单分期设置", "对已出账单申请分期还款"),
    "account_e_bill_set": ("电子账单设置", "开通/关闭/变更电子账单"),
    "account_forex_rate_query": ("外汇汇率查询", "查询购汇/结汇汇率"),
    "account_paper_bill_reissue": ("纸质账单补寄", "申请补寄纸质账单"),
    "account_stmt_query": ("交易流水查询", "查询账户交易流水"),
    "account_stmt_dispute": ("流水异议", "对交易流水存在异议"),
    "account_balance_query": ("余额查询", "查询信用卡可用额度/余额"),

    # ── 1.2 交易与消费域 ──
    "txn_query": ("交易明细查询", "查询具体交易记录/消费明细"),
    "txn_cash_advance_query": ("取现记录查询", "查询预借现金/取现记录"),
    "txn_auto_debit_set": ("自动还款设置", "设置/变更自动扣款"),
    "txn_auto_debit_query": ("自动还款查询", "查询自动扣款记录"),
    "txn_refund_query": ("退款查询", "查询退款到账状态"),
    "txn_receipt_get": ("交易凭证获取", "获取交易凭证/小票"),
    "txn_currency_set": ("交易币种设置", "设置记账币种"),
    "txn_overseas_lock": ("境外交易锁定", "锁定/解锁境外交易"),
    "txn_category_stat": ("消费分类统计", "按类目统计消费"),
        "txn_export": ("交易导出", "导出交易明细文件"),
    # ── 支付域 ──
    "pay_method_query": ("支付方式查询", "查询可用支付方式/渠道"),
    "pay_contactless": ("闪付", "闪付/免密支付咨询"),
    "pay_large_verify": ("大额验证", "大额交易验证方式"),
    "pay_magnetic_issue": ("磁条卡问题", "磁条卡交易受限"),
    "pay_online_set": ("线上支付设置", "开通/关闭线上支付"),
    "pay_password_online": ("线上支付密码", "线上支付密码设置/变更"),
    "pay_pause": ("暂停支付", "临时暂停支付功能"),
    "pay_wallet_bind": ("钱包绑卡", "第三方钱包绑卡/解绑"),
    "pay_wallet_unbind": ("钱包解绑", "第三方钱包解绑"),

    # ── 1.3 还款域 ──
    "repay_plan_query": ("还款计划查询", "查询分期还款计划"),
    "repay_record_query": ("还款记录查询", "查询历史还款记录"),
    "repay_calc": ("还款试算", "试算还款金额/利息"),
    "repay_method_query": ("还款方式查询", "查询可用还款渠道"),
    "repay_auto_set": ("自动还款", "设置/变更自动还款"),
    "repay_early": ("提前还款", "提前结清分期/欠款"),
    "repay_grace_period": ("宽限期查询", "查询还款宽限期政策"),
    "repay_overdue_query": ("逾期查询", "查询逾期状态/金额"),
    "repay_overdue_relief": ("逾期减免", "申请逾期费用减免"),
    "repay_overdue_plan": ("逾期还款计划", "协商逾期还款方案"),
    "repay_appointment": ("预约还款", "预约未来某日还款"),
    "repay_voucher": ("还款凭证", "获取/补发还款凭证"),
    "repay_settle": ("结清证明", "申请结清证明"),
    "repay_deduction_order": ("冲抵顺序查询", "查询还款冲抵顺序"),

    # ── 1.4 额度域 ──
    "limit_query": ("额度查询", "查询当前信用额度/可用额度"),
    "limit_apply_increase": ("提额申请", "申请提升信用额度"),
    "limit_apply_decrease": ("降额申请", "申请降低信用额度"),
    "limit_apply_status": ("提额进度查询", "查询额度调整审批进度"),
    "limit_policy_query": ("额度政策查询", "查询额度管理政策/规则"),
    "limit_history_query": ("额度变更历史", "查询历史额度调整记录"),
    "limit_tying_query": ("关联额度查询", "查询主卡/附属卡共享额度"),
        "limit_pool_query": ("额度池查询", "查询预审批额度池"),
    "limit_usage_alert_set": ("额度提醒设置", "设置额度使用预警"),

    # ── 1.5 分期域 ──
    "inst_apply": ("分期申请", "申请账单/消费分期"),
    "inst_param_query": ("分期参数查询", "查询分期期数/费率/额度"),
    "inst_calc": ("分期试算", "试算分期手续费/每期金额"),
    "inst_status_query": ("分期状态查询", "查询分期办理状态/剩余期数"),
    "inst_early_settle": ("分期提前结清", "提前结清剩余分期"),
    "inst_change_set": ("分期变更", "变更分期期数/金额"),
    "inst_cancel": ("分期取消", "取消未开始的分期"),
    "inst_refund_rule": ("分期退费规则", "查询分期退费/提前结清规则"),
    "inst_forex": ("分期外汇", "外币交易分期"),
    "inst_promotion": ("分期优惠", "查询分期费率优惠活动"),
    "inst_contract": ("分期合同", "获取分期合同/协议"),

    # ── 1.6 积分域 ──
    "points_balance_query": ("积分余额查询", "查询当前积分余额"),
    "points_redeem": ("积分兑换", "积分兑换商品/权益"),
    "points_expiry_query": ("积分有效期查询", "查询积分到期日"),
    "points_expiry_alarm_set": ("积分到期提醒", "设置积分过期提醒"),
    "points_transfer": ("积分转让", "积分转赠他人"),
    "points_rule_query": ("积分规则查询", "查询积分获取/使用规则"),
    "points_order_query": ("积分订单查询", "查询积分兑换订单"),
    "benefit_query": ("权益查询", "查询卡片附带的权益/优惠"),
    "benefit_claim": ("权益领取", "领取卡片权益/优惠券"),
    "benefit_reassign": ("权益重指派", "变更权益受益人"),
    "benefit_upgrade": ("权益升级", "升级权益等级"),

    # ── 1.7 卡片域 ──
    "card_apply_new": ("新卡申请", "申请新的信用卡"),
    "card_activate": ("卡片激活", "激活新卡"),
    "card_expiry_renew": ("卡片续期", "到期换卡"),
    "card_cancel": ("销卡", "注销信用卡账户"),
    "card_status_query": ("卡片状态查询", "查询卡片当前状态"),
    "card_freeze": ("卡片冻结", "临时冻结卡片"),
    "card_pin_set": ("密码设置", "设置/修改交易密码"),
    "card_pin_forgot": ("密码重置", "忘记密码需重置"),
    "card_info_query": ("卡片信息查询", "查询卡片基本信息"),
    "card_supplementary": ("附属卡", "申请/管理附属卡"),
    "card_upgrade": ("卡片升级", "升级卡片等级"),
    "card_gift_query": ("卡片礼品查询", "查询开卡礼品"),
    "card_loss_report": ("挂失", "卡片遗失/被盗后挂失"),
    "card_reissue": ("补卡", "申请补发新卡"),
    "card_limit_temporary": ("临时额度", "申请/查询临时额度"),

    # ── 1.8 风险域 ──
    "risk_fraud_report": ("欺诈上报", "报告可疑交易/盗刷"),
    "risk_account_freeze": ("账户冻结", "因风险冻结账户"),
    "risk_contact_warn": ("联系方式变更预警", "预留联系方式变更风险提示"),
    "risk_sms_verify": ("短信核验", "要求短信验证码核验"),
    "risk_pin_leak": ("密码泄露", "报告密码可能泄露"),
    "risk_money_laundry": ("洗钱嫌疑", "疑似洗钱相关风险"),
    "risk_overseas_travel": ("境外出行登记", "出境前卡片境外使用登记"),
    "risk_pos_anomaly": ("POS 异常", "POS 机交易异常反馈"),
        "risk_wallet_safety": ("钱包安全", "数字钱包绑定安全咨询"),
    "risk_atm_anomaly": ("ATM 异常", "ATM 机交易异常反馈"),
    "risk_cash_advance_warn": ("预借现金预警", "预借现金风险提示"),
    "risk_fraud_hotline": ("欺诈热线", "欺诈举报热线咨询"),
    "risk_kyc": ("KYC 核验", "实名认证/KYC 核验要求"),
    "risk_dispute": ("交易争议", "对交易存在争议"),

    # ── 1.9 客服域 ──
    "complaint": ("投诉", "客户投诉/不满"),
    "dispute_submit": ("争议提交", "提交交易争议"),
    "dispute_appeal": ("争议申诉", "申诉处理结果"),
    "dispute_chargeback": ("拒付/冲正", "申请交易拒付/冲正"),
    "fee_appeal": ("费用申诉", "对收费存在异议"),
    "transfer_agent": ("转人工", "要求转接人工客服"),
    "handoff_restart": ("重机会话", "人工服务重新开始"),
    "handoff_schedule": ("预约人工", "预约人工客服回电"),

    # ── 1.10 通用 ──
    "faq": ("常见咨询", "通用知识问答 (年费/免息期/账单日等)"),
    "faq_product": ("产品咨询", "产品功能/权益咨询"),
    "chitchat": ("闲聊", "与业务无关的日常对话"),
    "nb_chitchat": ("闲聊(边界)", "疑似闲聊的边界输入"),
    "nb_noise": ("噪声", "无意义/乱码输入"),
        "handoff_end": ("人工结束", "人工服务结束标记"),
    "handoff_hotline": ("客服热线", "客服热线号码咨询"),
    "handoff_hours_query": ("服务时间查询", "查询人工客服服务时间"),
    "handoff_queue_query": ("排队查询", "查询人工客服排队状态"),
    "handoff_verify": ("人工核验", "转人工前的身份核验"),
    "nb_help": ("求助", "客户表达需要帮助"),

    # ── 费用域 ──
    "fee_annual": ("年费", "信用卡年费标准/减免政策"),
    "fee_card_material": ("工本费", "卡片工本费/换卡费"),
    "fee_cash": ("取现手续费", "预借现金手续费标准"),
    "fee_charged_query": ("已收费查询", "查询已产生的费用"),
    "fee_interest": ("利息", "利息计算方式/利率"),
    "fee_overlimit": ("超限费", "超限使用额度的费用"),
    "fee_overseas": ("境外交易费", "境外交易货币转换费"),
    "fee_penalty": ("违约金", "逾期违约金标准/减免"),
    "fee_rate_query": ("费率查询", "各类费率标准查询"),
    "fee_service": ("服务费", "增值服务费咨询"),
    "fee_settle_inquiry": ("结算费用", "结算/结清相关费用"),
    "fee_transfer": ("转账手续费", "信用卡转账费用"),

    # ── 活动域 ──
    "campaign_query": ("活动查询", "营销活动规则/进度查询"),
    "campaign_signup": ("活动报名", "报名参加营销活动"),

    # ── 卡片补充 ──
    "card_expire_renew": ("到期换卡", "卡片到期后换新卡"),
    "card_loss_cancel": ("挂失撤销", "撤销之前的挂失"),

    # ── 争议补充 ──
    "dispute_close": ("争议关闭", "关闭已提交的争议"),
    "dispute_compensation": ("争议赔付", "争议处理赔付申请"),
    "dispute_hotline": ("争议热线", "争议处理专线咨询"),
    "dispute_material": ("争议材料", "争议处理所需材料"),
    "dispute_policy": ("争议政策", "争议处理规则/政策"),
    "dispute_regulate": ("争议调解", "争议调解/仲裁流程"),
    "dispute_status": ("争议进度", "争议处理进度查询"),
    "dispute_urge": ("争议催办", "催促争议处理进度"),
    "dispute_withdraw": ("争议撤回", "撤回已提交的争议"),

    # ── FAQ 补充 ──
    "faq_account_policy": ("账户政策", "账户管理政策咨询"),
    "faq_any": ("通用咨询", "通用银行业务咨询"),
    "faq_channel": ("办理渠道", "业务办理渠道/方式咨询"),
    "faq_compliance": ("合规咨询", "监管合规相关咨询"),
    "faq_contract": ("合同咨询", "领用合同/协议条款咨询"),
    "faq_credit_report": ("征信报告", "征信记录/报告咨询"),
    "faq_data": ("资料咨询", "办理业务所需资料咨询"),
    "faq_notice": ("公告", "银行公告/通知咨询"),
}


def intent_name_zh(slug: str) -> str:
    """查中文标签, 未收录时返回 slug 本身"""
    return INTENT_LABELS_ZH.get(slug, (slug, ""))[0]


def intent_desc_zh(slug: str) -> str:
    return INTENT_LABELS_ZH.get(slug, (slug, ""))[1]
