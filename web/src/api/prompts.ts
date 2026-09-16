/**
 * 提示词中心 API (PromptOps: 版本 append-only + 指针切换发布/回滚)
 */

import { client } from "./client"

export interface PromptItem {
  name: string
  category: string
  description: string
  scene?: string
  editable: boolean
  source: "db" | "code"
  active_version: number
  active_changelog: string
  version_count: number
  updated_at: string | null
  source_case_id: string | null
}

export interface PromptVersionItem {
  id: string
  version: number
  status: "draft" | "published" | "archived"
  changelog: string
  created_by: string
  rollout_pct: number
  source_case_id: string | null
  created_at: string | null
  content: string
}

export interface PromptDetail extends PromptItem {
  active_content: string
  versions: PromptVersionItem[]
}

export interface DraftPayload {
  content: string
  changelog?: string
  source_case_id?: string | null
}

export interface PreviewResult {
  chars: number
  tokens_est: number
  issues: string[]
}

export function listPrompts() {
  return client.get<{ items: PromptItem[] }>("/admin/prompts")
}

export function getPromptDetail(name: string) {
  return client.get<PromptDetail>(`/admin/prompts/${name}`)
}

export function createDraft(name: string, payload: DraftPayload) {
  return client.post<{ id: string; version: number; status: string }>(`/admin/prompts/${name}/draft`, payload)
}

export function publishVersion(name: string, versionId: string) {
  return client.post<{ name: string; active_version: number }>(`/admin/prompts/${name}/versions/${versionId}/publish`, {})
}

export function rollbackVersion(name: string, versionId: string) {
  return client.post<{ name: string; active_version: number }>(`/admin/prompts/${name}/rollback/${versionId}`, {})
}

export function deleteDraft(name: string, versionId: string) {
  return client.delete<{ deleted: string }>(`/admin/prompts/${name}/versions/${versionId}`)
}

export function previewPrompt(name: string, content: string) {
  return client.post<PreviewResult>(`/admin/prompts/${name}/preview`, { content })
}
