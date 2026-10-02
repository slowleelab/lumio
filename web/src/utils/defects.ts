// 缺陷类型枚举 → 业务中文名 (公共单一事实源, 与后端 badcase_loop.DEFECT_TYPES 同构)
// 处理链路/案例工作台等页面共用 — 禁止各页面散落私有映射
export const DEFECT_LABELS: Record<string, string> = {
  knowledge_missing: "知识缺失",
  knowledge_outdated: "知识过时",
  intent_misread: "意图理解错",
  intent_uncovered: "说法未覆盖",
  rule_flaw: "流程/规则缺陷",
  reply_quality: "回复质量差",
  fallback_poor: "兜底不当",
  compliance_risk: "合规风险",
}

export function defectZh(v: string | null | undefined): string {
  return DEFECT_LABELS[v ?? ""] ?? v ?? ""
}
