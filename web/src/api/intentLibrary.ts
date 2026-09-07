/**
 * 意图库管理 API (目标架构 管理端模块②)
 */

import { client } from "./client"

export interface IntentTreeNode {
  intent: string
  name_zh?: string
  definition?: string
  domain: string
  group: string
  source?: "factory" | "registry"
  state?: string
}

export interface IntentTreeResponse {
  domains: Record<string, { groups: Record<string, { intents: IntentTreeNode[] }> }>
}

export interface SeedExample {
  text: string
  intent: string
}

export interface SeedListResponse {
  total: number
  examples: SeedExample[]
}

export interface AttributeRow {
  intent: string
  name_zh?: string
  domain: string
  group: string
  traffic_class: string | null
  touches_account: boolean
  source?: "factory" | "registry"
  state?: string
}

export interface RegistryHistoryItem {
  ts: number
  actor: string
  action: string
  from: string | null
  to: string
  note?: string
}

export interface RegistryEntry {
  slug: string
  domain: string
  group: string
  name_zh: string
  definition: string
  traffic_class: string
  seeds: string[]
  state: string
  created_by: string
  created_at: number
  updated_at: number
  history: RegistryHistoryItem[]
  eval_report?: {
    passed: boolean
    overlap?: { max_similarity?: number; warn_count?: number; hard_conflicts?: unknown[] }
    golden?: { baseline_accuracy?: number; with_candidate_accuracy?: number; drop?: number }
    error?: string
  } | null
  shadow_hits: number
  active_hits: number
}

export interface RegistryListResponse {
  total: number
  entries: RegistryEntry[]
}

export interface IndexStatus {
  running: boolean
  action: string
  version: number
  entities: number
  error: string
  started_at: number
  finished_at: number
}

export function getIntentTree(): Promise<IntentTreeResponse> {
  return client.get("/admin/intent-library/tree")
}

export function listSeeds(intent?: string): Promise<SeedListResponse> {
  return client.get("/admin/intent-library/seeds", { params: intent ? { intent } : {} })
}

export function addSeed(intent: string, text: string): Promise<{ created: number; duplicate?: boolean }> {
  return client.post("/admin/intent-library/seeds", { intent, text })
}

export function deleteSeed(intent: string, text: string): Promise<{ removed: number }> {
  return client.delete("/admin/intent-library/seeds", { params: { intent, text } })
}

export function getAttributeTable(): Promise<{ total: number; rows: AttributeRow[] }> {
  return client.get("/admin/intent-library/attributes")
}

// ── 运营意图注册表 (流派二生命周期) ──

export function listRegistry(state?: string): Promise<RegistryListResponse> {
  return client.get("/admin/intent-library/registry", { params: state ? { state } : {} })
}

export function createRegistryIntent(body: {
  slug: string
  domain: string
  name_zh: string
  definition?: string
  group?: string
  traffic_class?: string
  seeds: string[]
}): Promise<{ created: boolean; entry: RegistryEntry }> {
  return client.post("/admin/intent-library/registry", body)
}

export function updateRegistryIntent(
  slug: string,
  body: { seeds?: string[]; name_zh?: string; definition?: string; traffic_class?: string },
): Promise<{ updated: boolean; entry: RegistryEntry }> {
  return client.put(`/admin/intent-library/registry/${slug}`, body)
}

export function submitRegistryIntent(slug: string): Promise<{ entry: RegistryEntry }> {
  return client.post(`/admin/intent-library/registry/${slug}/submit`)
}

export function reviewRegistryIntent(
  slug: string,
  approve: boolean,
  note?: string,
): Promise<{ entry: RegistryEntry | null }> {
  return client.post(`/admin/intent-library/registry/${slug}/review`, { approve, note })
}

export function evaluateRegistryIntent(slug: string): Promise<{ evaluating: boolean }> {
  return client.post(`/admin/intent-library/registry/${slug}/evaluate`)
}

export function activateRegistryIntent(slug: string): Promise<{ entry: RegistryEntry; rebuild: string }> {
  return client.post(`/admin/intent-library/registry/${slug}/activate`)
}

export function deprecateRegistryIntent(slug: string): Promise<{ entry: RegistryEntry }> {
  return client.post(`/admin/intent-library/registry/${slug}/deprecate`)
}

export function rejectRegistryIntent(slug: string, note?: string): Promise<{ entry: RegistryEntry }> {
  return client.post(`/admin/intent-library/registry/${slug}/reject`, { note })
}

// ── L2 索引蓝绿重建 ──

export function getIndexStatus(): Promise<IndexStatus> {
  return client.get("/admin/intent-library/index/status")
}

export function rebuildIndex(): Promise<{ scheduled: boolean }> {
  return client.post("/admin/intent-library/index/rebuild")
}

export function rollbackIndex(): Promise<{ version: number }> {
  return client.post("/admin/intent-library/index/rollback")
}
