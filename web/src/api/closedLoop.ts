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
