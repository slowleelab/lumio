/**
 * 管理端 API 客户端 — 闭环 (Badcase 工作台) + 健康度指标
 */

import { client } from "./client"

export interface Badcase {
  id: string
  trace_id: string
  session_id: string
  customer_id: string | null
  signal_source: string
  signal_detail: Record<string, unknown> | null
  user_input: string
  bot_output: string | null
  root_cause_layer: string | null
  root_cause_category: string | null
  attribution_evidence: string | null
  attribution_confidence: number | null
  attribution_model: string | null
  needs_human_review: boolean
  human_confirmed_layer: string | null
  fix_table: string | null
  fix_status: string
  fix_note: string | null
  resolved_at: string | null
  snapshot: Record<string, unknown> | null
  created_at: string | null
  session_time?: string | null
  occurrences?: number
}

export interface BadcaseListResponse {
  total: number
  badcases: Badcase[]
}

export interface ClosedLoopHealth {
  window_days: number
  transfer_count_7d: number
  sessions_7d: number
  transfer_rate_7d: number | null
  avg_fix_days: number | null
  badcases_deployed: number
  recurrence_rate: null
  golden_pass_rate: null
}

// ── Badcase 工作台 ──

export function listBadcases(params?: {
  signal_source?: string
  root_cause_layer?: string
  fix_status?: string
  fix_table?: string
  needs_review?: boolean
  keyword?: string
  limit?: number
  offset?: number
}): Promise<BadcaseListResponse> {
  return client.get("/admin/closed-loop/badcases", { params })
}

export function getBadcase(badcaseId: string): Promise<Badcase> {
  return client.get(`/admin/closed-loop/badcases/${badcaseId}`)
}

export function attributeBadcase(badcaseId: string): Promise<Record<string, unknown>> {
  return client.post(`/admin/closed-loop/badcases/${badcaseId}/attribute`, {})
}

export function resolveBadcase(
  badcaseId: string,
  body: { fix_status: string; fix_table?: string; note?: string; human_confirmed_layer?: string },
): Promise<{ status: string }> {
  return client.post(`/admin/closed-loop/badcases/${badcaseId}/resolve`, body)
}

// ── 健康度指标 ──

export function getClosedLoopHealth(): Promise<ClosedLoopHealth> {
  return client.get("/admin/closed-loop/health-metrics")
}

export interface BadcaseStats {
  layer_dist?: Record<string, number>
  signal_dist?: Record<string, number>
  total: number
  today_new: number
  pending_review: number
  confirmed: number
  deployed: number
  llm_pass_rate: number | null
}

export function getBadcaseStats(): Promise<BadcaseStats> {
  return client.get("/admin/closed-loop/badcases/stats")
}

export interface BatchAttributionStatus {
  running: boolean
  total: number
  done: number
  failed: number
  started_at: number
  error: string
  scope?: { signal_source?: string; keyword?: string; layer?: string } | null
}

export function startBatchAttribution(
  limit = 50,
  scope?: { signal_source?: string; keyword?: string; layer?: string },
): Promise<{ scheduled: boolean; limit: number }> {
  return client.post("/admin/closed-loop/badcases/attribute-batch", { limit, ...scope })
}

export function getBatchAttributionStatus(): Promise<BatchAttributionStatus> {
  return client.get("/admin/closed-loop/badcases/attribute-batch/status")
}

export function expandGoldenSet(seeds: string[]): Promise<{ seed_count: number; variants: string[]; rejected_count: number }> {
  return client.post("/admin/closed-loop/golden/expand", { seeds })
}

// ── 全量质检巡检 (所有会话从原始对话内容过质检, 不依赖置信度/信号) ──

export interface QualityScanStatus {
  running: boolean
  total: number
  done: number
  n_pass: number
  n_warn: number
  n_fail: number
  n_error: number
  error_msg: string
  last_run?: {
    finished_at: string
    total: number
    n_pass: number
    n_warn: number
    n_fail: number
    n_error: number
    pass_rate: number | null
  } | null
}

export function startQualityScan(opts?: {
  limit?: number
  sample_rate?: number
  lookback_hours?: number
  reinspect?: boolean
}): Promise<{ scheduled: boolean; limit: number }> {
  return client.post("/admin/closed-loop/quality/scan", opts ?? {})
}

export function getQualityScanStatus(): Promise<QualityScanStatus> {
  return client.get("/admin/closed-loop/quality/scan/status")
}

// ── 质检记录 (每个被巡检会话一条判定, 按会话时间倒序) ──

export interface QualityProblem {
  type: string // A 答非所问 / B 幻觉编造 / C 越界承诺 / D 漏转人工 / E 未解决无引导
  turn?: number
  reason?: string
}

export interface QualityRecord {
  id: string
  session_id: string
  verdict: "pass" | "warn" | "fail"
  problems: QualityProblem[]
  summary: string | null
  preview: string | null
  judge_model: string | null
  turns: number | null
  session_time: string | null
  scanned_at: string | null
  badcase_id: string | null
}

export interface QualityRecordListResponse {
  total: number
  records: QualityRecord[]
}

export interface QcSessionRow {
  session_id: string
  verdict: "pass" | "warn" | "fail" | null
  problems: QualityProblem[] | null
  summary: string | null
  turns: number | null
  session_time: string | null
  scanned_at: string | null
  judge_model: string | null
  preview: string | null
  badcase_id: string | null
  signal_source: string | null
  root_cause_layer: string | null
  human_confirmed_layer: string | null
  fix_status: string | null
  needs_human_review: boolean | null
  attribution_confidence: number | null
  collected_at: string | null
  category: "pass" | "warn" | "fail" | "pending_review" | "unscanned"
}

export interface QcSessionListResponse {
  total: number
  sessions: QcSessionRow[]
}

export function listQcSessions(params?: {
  category?: string
  keyword?: string
  limit?: number
  offset?: number
}): Promise<QcSessionListResponse> {
  return client.get("/admin/closed-loop/quality/sessions", { params })
}

export function replayQualitySession(sessionId: string): Promise<{ status: string; new_session_id: string; total_rounds: number }> {
  return client.post("/admin/closed-loop/quality/replay", { session_id: sessionId })
}

export function endChatSession(sessionId: string): Promise<{ status: string }> {
  return client.post("/chat/end", { session_id: sessionId })
}

export function rescanQualitySession(sessionId: string): Promise<{
  status: string
  verdict?: string
  problems?: QualityProblem[]
  summary?: string
}> {
  return client.post("/admin/closed-loop/quality/rescan", { session_id: sessionId })
}

export function listQualityRecords(params?: {
  verdict?: string
  keyword?: string
  limit?: number
  offset?: number
}): Promise<QualityRecordListResponse> {
  return client.get("/admin/closed-loop/quality/records", { params })
}

export interface QualityCoverage {
  lookback_hours: number
  total_sessions: number
  scanned_sessions: number
  coverage: number | null
  by_verdict: Record<string, number>
  pass_rate: number | null
}

export function getQualityCoverage(lookbackHours = 720): Promise<QualityCoverage> {
  return client.get("/admin/closed-loop/quality/coverage", { params: { lookback_hours: lookbackHours } })
}
