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
