/**
 * 管理控制台 API（对话审计 + RAG 指标监控）
 * 后端: agent/lumio/services/common/console_router.py (/api/admin/*)
 */
import { client } from "./client"

// ── 类型 ──

export interface ConversationItem {
  session_id: string
  customer_id: string | null
  channel_type: string | null
  turns: number
  messages: number
  top_intent: string | null
  avg_bot_confidence: number | null
  errors: number
  avg_duration_ms: number | null
  started_at: string | null
  last_at: string | null
}

export interface ConversationListResponse {
  total: number
  conversations: ConversationItem[]
}

export interface ReplayTurn {
  turn_id: string
  speaker: string
  content: string
  intent: string | null
  confidence: number | null
  entities: unknown[] | null
  response_source: string | null
  emotion_label: string | null
  emotion_score: number | null
  retrieval_context: string | null
  timestamp: string | null
}

export interface ReplayDecision {
  decision_id: string
  turn_id: string
  agent_name: string
  action: string
  reasoning: string
  evidence: Record<string, unknown> | null
  latency_ms: number | null
  created_at: string | null
}

export interface ReplayMessage {
  message_id: string
  content: string
  intent: string | null
  processing_status: string | null
  processing_duration_ms: number | null
  source: string | null
  error_message: string | null
  created_at: string | null
}

export interface ReplayResponse {
  session_id: string
  turns: ReplayTurn[]
  decisions: ReplayDecision[]
  messages: ReplayMessage[]
}

export interface OperationLog {
  id: string
  timestamp: string | null
  actor_id: string
  actor_role: string
  action: string
  target_type: string
  target_id: string | null
  method: string
  path: string
  status_code: number
  ip_address: string | null
  detail: Record<string, unknown> | null
}

export interface OperationLogsResponse {
  total: number
  logs: OperationLog[]
}

export interface RagQualitySummary {
  days: number
  daily_volume: { date: string; turns: number; sessions: number }[]
  response_source: {
    daily: { date: string; source: string; count: number }[]
    total: { source: string; count: number }[]
  }
  faq: {
    daily: { date: string; match_type: string; count: number }[]
    total: { match_type: string; count: number }[]
    hit_rate: number | null
  }
  intent_top: { intent: string; count: number; avg_confidence: number | null }[]
  confidence: {
    threshold: number
    bot_turns: number
    avg_bot_confidence: number | null
    low_confidence_share: number | null
  }
  decision_latency: { agent: string; action: string; count: number; avg_ms: number | null; p95_ms: number | null }[]
}

export interface HistStats {
  count: number
  sum: number
  avg: number | null
}

export interface RagLiveMetrics {
  retrieval: Record<string, HistStats>
  answer_latency: HistStats
  degradation_level: number
  agent_responses: Record<string, number>
  agent_timeouts: Record<string, number>
  fast_reply_total: number
  eval_regression_pass_rate: Record<string, number>
  bad_cases_total: number
  rag_cache_ops: Record<string, number>
  rerank_degradation: Record<string, number>
  faq_match: Record<string, number>
  circuit_breakers: Record<string, number>
  injection: { attempts_total: number; blocked_total: number }
}

// ── 会话审计 ──

export function listConversations(params?: {
  session_id?: string
  customer_id?: string
  intent?: string
  response_source?: string
  start?: string
  end?: string
  limit?: number
  offset?: number
}): Promise<ConversationListResponse> {
  return client.get("/admin/conversations", { params })
}

export function getConversationReplay(sessionId: string, includeContext = false): Promise<ReplayResponse> {
  return client.get(`/admin/conversations/${encodeURIComponent(sessionId)}/replay`, {
    params: includeContext ? { include_context: true } : {},
  })
}

// ── 操作审计 ──

export function listOperationLogs(params?: {
  actor_id?: string
  action?: string
  target_type?: string
  path_contains?: string
  status_code?: number
  start?: string
  end?: string
  limit?: number
  offset?: number
}): Promise<OperationLogsResponse> {
  return client.get("/admin/operation-logs", { params })
}

// ── RAG 指标 ──

export function getRagQualitySummary(days = 7, topN = 10): Promise<RagQualitySummary> {
  return client.get("/admin/rag/quality-summary", { params: { days, top_n: topN } })
}

export function getRagLiveMetrics(): Promise<RagLiveMetrics> {
  return client.get("/admin/rag/live-metrics")
}
