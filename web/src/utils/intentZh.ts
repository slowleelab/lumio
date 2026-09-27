// intent 枚举 → 业务中文名 (公共单一事实源)
// 问题治理/案例工作台/决策链等页面共用 — 新增 intent 时在此补一行,
// 禁止各页面再散落私有映射 (问题治理曾因私有映射缺项裸显 complaint 英文)
export const INTENT_ZH: Record<string, string> = {
  bill_query: "账单查询",
  account_bill_query: "账单查询",
  transaction_query: "交易明细查询",
  txn_query: "交易明细查询",
  limit_query: "额度查询",
  installment_inquiry: "分期咨询",
  installment_manage: "分期办理",
  reward_query: "积分相关",
  faq: "知识问答",
  knowledge_qa: "知识问答",
  faq_product: "产品咨询",
  chitchat: "闲聊",
  nb_chitchat: "闲聊",
  nb_noise: "无效输入",
  complaint: "投诉",
  transfer_agent: "要求转人工",
  card_loss: "卡片挂失",
  card_loss_report: "卡片挂失",
  card_reissue: "挂失补卡",
  card_activation: "卡片激活",
  card_limit_adjust: "额度调整",
  bill_dispute: "账单争议",
  fraud_report: "欺诈举报",
  credit_apply: "信贷申请",
  unclassified: "未分类业务",
}

export function intentZh(v: string | null | undefined): string {
  return INTENT_ZH[v ?? ""] ?? v ?? ""
}
