/**
 * 对话模拟器 API (客户端模拟 Agent 启停/状态/场景清单)
 */

import { client } from "./client"

export interface ScenarioInfo {
  key: string
  name_zh: string
  turns: number
  variants: number
  final_feedback: string
  tags: string[]
}

export interface SimulatorStats {
  started_at: number
  sessions: number
  turns: number
  expect_hits: number
  expect_checks: number
  feedbacks: number
  errors: number
  abandoned: number
  latency_avg_ms: number
  latency_p95_ms: number
}

export interface TurnRecord {
  ts: number
  scenario: string
  turn: number
  text: string
  reply: string
  latency_ms: number
  expect_ok: boolean | null
}

export interface SimulatorStatus {
  running: boolean
  config: { scenario_keys: string[]; users: number; interval: number }
  stats: SimulatorStats
  recent: TurnRecord[]
}

export function listScenarios(): Promise<{ total: number; scenarios: ScenarioInfo[] }> {
  return client.get("/admin/simulator/scenarios")
}

export function startSimulator(body: {
  scenario_keys: string[]
  users: number
  interval: number
}): Promise<SimulatorStatus> {
  return client.post("/admin/simulator/start", body)
}

export function stopSimulator(): Promise<SimulatorStatus> {
  return client.post("/admin/simulator/stop")
}

export function getSimulatorStatus(): Promise<SimulatorStatus> {
  return client.get("/admin/simulator/status")
}
