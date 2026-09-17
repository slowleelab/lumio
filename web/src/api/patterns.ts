/**
 * 问题治理 API (共性聚合: 业务×根因 聚成治理专项, 方案 + 批量执行)
 */

import { client } from "./client"

export interface PatternGroup {
  group_key: string
  intent_label: string | null
  root_cause_layer: string | null
  case_count: number
  pending_count: number
  unconfirmed_count: number
  session_count: number
  fixing_count: number
  last_seen: string | null
  has_plan: boolean
  plan_status: "none" | "planned" | "fixing"
  plan_owner: string
  fix_table: string | null
  title: string
}

export interface PatternGroupsResponse {
  items: PatternGroup[]
  unattributed: number
}

export interface PatternCase {
  id: string
  user_input: string
  bot_output: string | null
  session_id: string
  intent_label: string | null
  root_cause_layer: string | null
  needs_human_review: boolean
  human_confirmed_layer: string | null
  fix_status: string
  fix_table: string | null
  session_time: string | null
}

export interface PatternDetail {
  group_key: string
  intent_label: string | null
  root_cause_layer: string | null
  case_count: number
  unconfirmed_count: number
  fix_table: string | null
  plan_text: string
  plan_owner: string
  plan_template: string
  cases: PatternCase[]
}

export interface PlanPayload {
  intent_label: string | null
  root_cause_layer: string | null
  fix_table: string | null
  plan_text: string
  plan_owner: string
}

export interface BatchResult {
  done: number
  total: number
  results: { id: string; ok: boolean; message?: string }[]
}

export function listPatterns() {
  return client.get<PatternGroupsResponse>("/admin/patterns")
}

export function getPatternDetail(groupKey: string) {
  return client.get<PatternDetail>(`/admin/patterns/${encodeURIComponent(groupKey)}`)
}

export function savePatternPlan(groupKey: string, payload: PlanPayload) {
  return client.put(`/admin/patterns/${encodeURIComponent(groupKey)}/plan`, payload)
}

export function batchTransition(groupKey: string, fixStatus: string, caseIds?: string[]) {
  return client.post<BatchResult>(`/admin/patterns/${encodeURIComponent(groupKey)}/cases/batch`, {
    fix_status: fixStatus,
    case_ids: caseIds ?? null,
  })
}
