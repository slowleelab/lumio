<template>
  <div class="audit-page">
    <div class="page-header">
      <h2>对话审计</h2>
      <div class="header-controls">
        <el-input
          v-model="filters.session_id"
          placeholder="会话 ID"
          clearable
          size="small"
          style="width: 180px"
          @change="load"
        />
        <el-input
          v-model="filters.customer_id"
          placeholder="客户 ID"
          clearable
          size="small"
          style="width: 140px"
          @change="load"
        />
        <el-input
          v-model="filters.intent"
          placeholder="意图"
          clearable
          size="small"
          style="width: 140px"
          @change="load"
        />
        <el-select
          v-model="filters.response_source"
          placeholder="回复来源"
          clearable
          size="small"
          style="width: 130px"
          @change="load"
        >
          <el-option v-for="s in sourceOptions" :key="s" :label="s" :value="s" />
        </el-select>
        <el-date-picker
          v-model="dateRange"
          type="daterange"
          range-separator="→"
          start-placeholder="开始"
          end-placeholder="结束"
          size="small"
          style="width: 240px"
          value-format="YYYY-MM-DD"
          @change="load"
        />
        <el-button :loading="loading" @click="load">查询</el-button>
      </div>
    </div>

    <el-table :data="conversations" v-loading="loading" stripe style="margin-top: 16px" @row-click="openReplay">
      <el-table-column prop="session_id" label="会话" min-width="220" show-overflow-tooltip />
      <el-table-column prop="customer_id" label="客户" width="120" show-overflow-tooltip />
      <el-table-column prop="channel_type" label="渠道" width="80" />
      <el-table-column prop="turns" label="轮次" width="70" align="center" />
      <el-table-column prop="top_intent" label="主意图" width="160" show-overflow-tooltip>
        <template #default="{ row }">
          <el-tag v-if="row.top_intent" size="small" type="info">{{ row.top_intent }}</el-tag>
          <span v-else>-</span>
        </template>
      </el-table-column>
      <el-table-column label="平均置信度" width="110" align="center">
        <template #default="{ row }">
          <span :class="confClass(row.avg_bot_confidence)">{{ fmtConf(row.avg_bot_confidence) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="错误" width="70" align="center">
        <template #default="{ row }">
          <el-badge v-if="row.errors > 0" :value="row.errors" type="danger" />
          <span v-else>0</span>
        </template>
      </el-table-column>
      <el-table-column label="开始时间" width="160">
        <template #default="{ row }">{{ formatTime(row.started_at) }}</template>
      </el-table-column>
      <el-table-column label="最后活跃" width="160">
        <template #default="{ row }">{{ formatTime(row.last_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="90" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click.stop="openReplay(row)">回放</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      v-model:current-page="page"
      v-model:page-size="pageSize"
      :total="total"
      :page-sizes="[10, 20, 50, 100]"
      layout="total, sizes, prev, pager, next"
      style="margin-top: 16px; justify-content: flex-end"
      @current-change="load"
      @size-change="onSizeChange"
    />

    <!-- 会话回放抽屉 -->
    <el-drawer
      v-model="replayVisible"
      :title="`会话回放 — ${replay?.session_id ?? ''}`"
      size="72%"
      destroy-on-close
    >
      <div v-loading="replayLoading">
        <template v-if="replay">
          <el-tabs v-model="activeTab">
            <el-tab-pane label="对话记录" name="turns">
              <div class="turn-list">
                <div
                  v-for="t in replay.turns"
                  :key="t.turn_id + t.timestamp"
                  class="turn-row"
                  :class="t.speaker === 'customer' ? 'turn-customer' : 'turn-bot'"
                >
                  <div class="bubble">
                    <div class="bubble-content">{{ t.content }}</div>
                    <div class="bubble-meta">
                      <el-tag v-if="t.intent" size="small" type="info">{{ t.intent }}</el-tag>
                      <el-tag v-if="t.response_source" size="small">{{ sourceLabel(t.response_source) }}</el-tag>
                      <span v-if="t.confidence != null" class="meta-num">置信 {{ fmtConf(t.confidence) }}</span>
                      <span class="meta-time">{{ formatTime(t.timestamp) }}</span>
                    </div>
                  </div>
                </div>
                <el-empty v-if="replay.turns.length === 0" description="无对话记录" />
              </div>
            </el-tab-pane>

            <el-tab-pane :label="`决策链 (${replay.decisions.length})`" name="decisions">
              <div v-for="g in turnGroups" :key="g.turnId" class="turn-group">
                <div class="turn-group-head">
                  <span class="turn-badge" :class="{ 'turn-badge-legacy': g.legacy }">
                    {{ g.legacy ? "历史记录 · 未分轮" : `第 ${g.newIndex} 轮` }}
                  </span>
                  <span class="turn-time">{{ formatTime(g.decisions[0].created_at) }}</span>
                  <span v-if="g.totalMs != null" class="turn-total">全程 {{ g.totalMs >= 1000 ? (g.totalMs / 1000).toFixed(1) + 's' : Math.round(g.totalMs) + 'ms' }}</span>
                  <span class="turn-steps">{{ g.decisions.length }} 步</span>
                </div>
                <el-timeline>
                  <el-timeline-item
                    v-for="d in g.decisions"
                    :key="d.decision_id"
                    :type="decisionMeta(d.action).dot"
                    :color="decisionMeta(d.action).color"
                    placement="top"
                  >
                    <div class="decision-card">
                      <div class="decision-head">
                        <el-tag size="small" :type="decisionMeta(d.action).tag" effect="light">
                          {{ decisionMeta(d.action).label }}
                        </el-tag>
                        <span v-if="d.latency_ms != null && d.latency_ms > 0" class="meta-num">{{ Math.round(d.latency_ms) }}ms</span>
                        <span class="decision-agent">{{ agentLabel(d.agent_name) }}</span>
                      </div>
                      <div class="decision-explain">{{ decisionExplain(d) }}</div>
                      <div class="decision-reason">技术记录：{{ d.reasoning }}</div>
                      <div v-if="evidenceSummary(d.evidence).length" class="decision-kv">
                        <span v-for="kv in evidenceSummary(d.evidence)" :key="kv.k" class="kv-item">
                          <span class="kv-k">{{ kv.k }}</span>
                          <span class="kv-v" :class="{ 'kv-bad': kv.bad }">{{ kv.v }}</span>
                        </span>
                      </div>
                      <el-collapse v-if="d.evidence && Object.keys(d.evidence).length" class="decision-raw">
                        <el-collapse-item :title="`原始数据 (${Object.keys(d.evidence).length} 字段)`">
                          <pre class="decision-evidence">{{ JSON.stringify(d.evidence, null, 2) }}</pre>
                        </el-collapse-item>
                      </el-collapse>
                    </div>
                  </el-timeline-item>
                </el-timeline>
              </div>
              <el-empty v-if="replay.decisions.length === 0" description="无决策记录" />
            </el-tab-pane>

            <el-tab-pane :label="`处理记录 (${replay.messages.length})`" name="messages">
              <el-table :data="replay.messages" stripe size="small">
                <el-table-column label="时间" width="160">
                  <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
                </el-table-column>
                <el-table-column prop="content" label="消息" min-width="220" show-overflow-tooltip />
                <el-table-column prop="intent" label="意图" width="140" show-overflow-tooltip />
                <el-table-column label="状态" width="90" align="center">
                  <template #default="{ row }">
                    <el-tag size="small" :type="statusType(row.processing_status)">
                      {{ statusText(row.processing_status) }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="耗时" width="90" align="right">
                  <template #default="{ row }">
                    {{ row.processing_duration_ms != null ? row.processing_duration_ms + "ms" : "-" }}
                  </template>
                </el-table-column>
                <el-table-column prop="source" label="来源" width="110" />
                <el-table-column prop="error_message" label="错误" min-width="160" show-overflow-tooltip />
              </el-table>
            </el-tab-pane>
          </el-tabs>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue"
import { useRoute } from "vue-router"
import {
  listConversations,
  getConversationReplay,
  type ConversationItem,
  type ReplayResponse,
} from "@/api/console"

const conversations = ref<ConversationItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)

const filters = ref({
  session_id: "",
  customer_id: "",
  intent: "",
  response_source: "",
})
const dateRange = ref<[string, string] | null>(null)

const sourceOptions = ["knowledge", "tool", "llm", "template", "fallback", "clarify", "human"]

const replayVisible = ref(false)
const replayLoading = ref(false)
const replay = ref<ReplayResponse | null>(null)
const activeTab = ref("turns")

async function load() {
  loading.value = true
  try {
    const res = await listConversations({
      session_id: filters.value.session_id || undefined,
      customer_id: filters.value.customer_id || undefined,
      intent: filters.value.intent || undefined,
      response_source: filters.value.response_source || undefined,
      start: dateRange.value?.[0] ? `${dateRange.value[0]}T00:00:00` : undefined,
      end: dateRange.value?.[1] ? `${dateRange.value[1]}T23:59:59` : undefined,
      limit: pageSize.value,
      offset: (page.value - 1) * pageSize.value,
    })
    conversations.value = res.conversations
    total.value = res.total
  } catch {
    /* handled */
  } finally {
    loading.value = false
  }
}

function onSizeChange() {
  page.value = 1
  load()
}

async function openReplay(row: ConversationItem) {
  replayVisible.value = true
  replayLoading.value = true
  replay.value = null
  activeTab.value = "turns"
  try {
    replay.value = await getConversationReplay(row.session_id)
  } catch {
    /* handled */
  } finally {
    replayLoading.value = false
  }
}

function statusType(s: string | null): "success" | "warning" | "danger" | "info" {
  const m: Record<string, "success" | "warning" | "danger" | "info"> = {
    done: "success",
    queued: "info",
    processing: "warning",
    skipped: "info",
    error: "danger",
  }
  return (s && m[s]) || "info"
}

function statusText(s: string | null) {
  const m: Record<string, string> = {
    done: "完成",
    queued: "排队",
    processing: "处理中",
    skipped: "跳过",
    error: "错误",
  }
  return (s && m[s]) || s || "-"
}

function sourceLabel(s: string) {
  const m: Record<string, string> = {
    knowledge: "知识",
    tool: "工具",
    llm: "LLM",
    template: "模板",
    fallback: "兜底",
    clarify: "澄清",
    human: "人工",
  }
  return m[s] ?? s
}

function fmtConf(v: number | null) {
  return v == null ? "-" : `${Math.round(v * 100)}%`
}

function confClass(v: number | null) {
  if (v == null) return ""
  if (v < 0.5) return "conf-low"
  if (v < 0.7) return "conf-mid"
  return "conf-high"
}

// ── 决策链可读化: action 中文化+语义配色 / evidence 关键字段摘要 ──
const ACTION_META: Record<string, { label: string; tag: string; dot: string; color?: string }> = {
  turn_start: { label: "消息出队", tag: "info", dot: "" },
  route_decision: { label: "路由决策", tag: "warning", dot: "warning" },
  intent_classify: { label: "意图分类", tag: "primary", dot: "primary" },
  tool_call: { label: "工具执行", tag: "success", dot: "success" },
  rag_retrieve: { label: "知识检索", tag: "primary", dot: "primary" },
  llm_generate: { label: "回复生成", tag: "warning", dot: "warning" },
  chain_complete: { label: "链路完成", tag: "info", dot: "" },
  faq_direct: { label: "FAQ 直出", tag: "success", dot: "success" },
  noise_blocked: { label: "噪声拦截", tag: "danger", dot: "danger", color: "#f56c6c" },
  transfer_agent: { label: "转人工", tag: "warning", dot: "warning" },
  user_confirm: { label: "客户确认", tag: "info", dot: "" },
  injection_blocked: { label: "注入拦截", tag: "danger", dot: "danger", color: "#f56c6c" },
  guard_denied: { label: "护栏拦截", tag: "danger", dot: "danger", color: "#f56c6c" },
  cache_hit: { label: "缓存命中", tag: "success", dot: "success" },
  outbound_guard: { label: "出站拦截", tag: "danger", dot: "danger", color: "#f56c6c" },
  context_reply_pass: { label: "回话放行", tag: "info", dot: "" },
  mis_kill_candidate: { label: "误杀排查", tag: "warning", dot: "warning" },
  topic_track: { label: "诉求跟踪", tag: "info", dot: "" },
}
const AGENT_LABELS: Record<string, string> = {
  bot_agent: "编排大脑",
  query_chain: "查询链路",
  tool_executor: "工具执行器",
}
function decisionMeta(action: string) {
  return ACTION_META[action] ?? { label: action, tag: "info", dot: "" }
}
function agentLabel(name: string) {
  return AGENT_LABELS[name] ?? name
}
// ── 决策链按轮分组: 同一 turn_id 的决策归为一轮 (turn_id 由消息出队时绑定贯穿) ──
// 存量兼容: 修复前的决策每条独立 uuid4 (无 turn_start 特征), 按轮分组会把
// 一轮的 N 步拆成 N 个假轮次 — 合并为一个「历史记录 · 未分轮」组如实展示。
const turnGroups = computed(() => {
  const decisions = replay.value?.decisions ?? []
  type Group = { turnId: string; decisions: typeof decisions; totalMs: number | null; legacy: boolean; newIndex: number }
  const byTurn = new Map<string, typeof decisions>()
  for (const d of decisions) {
    const key = d.turn_id || "-"
    if (!byTurn.has(key)) byTurn.set(key, [])
    byTurn.get(key)!.push(d)
  }
  const legacy: typeof decisions = []
  const fresh: Group[] = []
  for (const [turnId, ds] of byTurn) {
    if (ds.some((x) => x.action === "turn_start")) {
      const done = ds.find((x) => x.action === "chain_complete" && typeof x.latency_ms === "number")
      fresh.push({ turnId, decisions: ds, totalMs: done ? done.latency_ms : null, legacy: false, newIndex: 0 })
    } else {
      legacy.push(...ds)
    }
  }
  const out: Group[] = []
  if (legacy.length) {
    const done = legacy.find((x) => x.action === "chain_complete" && typeof x.latency_ms === "number")
    out.push({ turnId: "legacy", decisions: legacy, totalMs: done ? done.latency_ms : null, legacy: true, newIndex: 0 })
  }
  fresh.forEach((g, i) => {
    g.newIndex = i + 1
    out.push(g)
  })
  return out
})

// evidence 关键字段 → 人话摘要 (只挑审阅者关心的; 原始 JSON 仍可展开)
const EV_KEYS: Record<string, string> = {
  intent: "意图",
  confidence: "置信度",
  tool: "工具",
  arguments: "参数",
  cache_hit: "缓存",
  is_error: "失败",
  result_preview: "结果",
  hit: "命中",
  query: "查询",
  citations: "引用",
  traffic_class: "链路",
  domain: "域",
  composite: "复合意图",
  direct: "直连",
  chitchat_redirect: "闲聊引导",
  fast_conf: "快路径置信",
  fast_intent: "快路径意图",
  missing_params: "缺参",
  queue_wait_ms: "排队",
  mcp_ms: "工具调用",
  summarize_ms: "摘要生成",
  route: "路由",
  reason: "原因",
}
const VALUE_ZH: Record<string, string> = {
  // 域 (domain)
  query: "查询", business: "业务办理", knowledge: "知识咨询", fallback: "闲聊/兜底",
  risk: "风险操作", complain: "投诉", transfer: "转人工", consulting: "咨询",
  transaction: "交易", service: "人工服务", chitchat: "闲聊",
  // 链路 (traffic_class / chain)
  read_only_query: "查询直达", financial_transaction: "交易办理", high_risk: "高风险→人工",
}
function fmtVal(k: string, v: unknown): string {
  if (typeof v === "string" && VALUE_ZH[v]) return VALUE_ZH[v]
  if (typeof v === "number") {
    if (k === "confidence" || k === "fast_conf") return `${Math.round(v * 100)}%`
    if (k.endsWith("_ms")) return v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${Math.round(v)}ms`
    return String(Math.round(v * 100) / 100)
  }
  if (typeof v === "boolean") return v ? "是" : "否"
  if (Array.isArray(v)) return v.slice(0, 3).join("、") + (v.length > 3 ? ` 等${v.length}项` : "")
  if (typeof v === "object") return JSON.stringify(v).slice(0, 60)
  if (v == null) return "-"
  return String(v).slice(0, 40)
}
function evidenceSummary(ev: Record<string, unknown> | null): Array<{ k: string; v: string; bad?: boolean }> {
  if (!ev) return []
  const priority = ["intent", "confidence", "queue_wait_ms", "tool", "arguments", "cache_hit", "hit", "traffic_class", "is_error", "result_preview", "missing_params", "mcp_ms", "summarize_ms", "citations", "query", "direct"]
  const out: Array<{ k: string; v: string; bad?: boolean }> = []
  const seen = new Set<string>()
  for (const k of priority) {
    if (k in ev && ev[k] != null) {
      out.push({ k: EV_KEYS[k] ?? k, v: fmtVal(k, ev[k]), bad: k === "is_error" && ev[k] === true })
      seen.add(k)
    }
  }
  for (const [k, v] of Object.entries(ev)) {
    if (!seen.has(k) && out.length < 8 && k !== "alternatives" && v != null) {
      out.push({ k: EV_KEYS[k] ?? k, v: fmtVal(k, v) })
    }
  }
  return out
}

// ── 决策通俗解释: 按 action + evidence 生成面向审阅者的一句话说明 ──
const INTENT_ZH: Record<string, string> = {
  bill_query: "账单查询", account_bill_query: "账单查询", transaction_query: "交易明细查询",
  txn_query: "交易明细查询", limit_query: "额度查询", installment_inquiry: "分期咨询",
  reward_query: "积分相关", faq: "常见咨询", faq_product: "产品咨询", chitchat: "闲聊/无明确业务",
  nb_chitchat: "闲聊/无明确业务", nb_noise: "无效输入", complaint: "投诉", transfer_agent: "要求转人工",
  card_loss: "卡片挂失", card_loss_report: "卡片挂失",
}
const TRAFFIC_ZH: Record<string, string> = {
  read_only_query: "查询直达链路（直接查系统，不走 AI 对话）",
  financial_transaction: "交易办理链路（调用业务工具）",
  high_risk: "高风险诉求（优先转人工）",
}
const NOISE_ZH: Record<string, string> = {
  low_confidence: "系统无法识别这句话的含义",
  ood_unknown: "这句话不属于客服知识范围",
  noise: "输入内容像乱码或误触",
  fast_slow_disagreement: "两套识别结果互相矛盾，稳妥起见不作答",
  subword_ambiguous: "只输入了孤立的词语（如"信用"），看不出具体诉求",
}
function intentZh(v: unknown): string {
  const t = String(v ?? "")
  return INTENT_ZH[t] ?? t
}
function decisionExplain(d: { action: string; reasoning: string; evidence?: Record<string, unknown> | null }): string {
  const ev = d.evidence ?? {}
  const conf = typeof ev.confidence === "number" ? `${Math.round(ev.confidence * 100)}%` : null
  switch (d.action) {
    case "route_decision": {
      // 两级路由判定 (新动作): 决策一交易性质 / 决策二咨询分流 / 闲聊短路
      if (ev.chitchat_redirect) {
        return "识别为闲聊或无效输入，直接用固定话术引导客户说明业务需求（不检索、不 AI 生成）"
      }
      if (ev.traffic_class != null) {
        return `根据识别结果选择处理方式：${TRAFFIC_ZH[String(ev.traffic_class)] ?? String(ev.traffic_class)}`
      }
      return "意图属于咨询类，进入知识问答流程（检索知识库 + AI 组织回答）"
    }
    case "intent_classify": {
      // 路由预备决策 (traffic_class 存在) vs 纯意图决策
      if ("traffic_class" in ev && ev.traffic_class != null) {
        return `识别结果为「${intentZh(ev.intent)}」，判定走${TRAFFIC_ZH[String(ev.traffic_class)] ?? String(ev.traffic_class)}`
      }
      if ("traffic_class" in ev && ev.traffic_class == null && "composite" in ev) {
        return "意图属于咨询类，进入知识问答流程（检索知识库 + AI 组织回答）"
      }
      if (ev.chitchat_redirect) {
        return "识别为闲聊或无效输入，直接用固定话术引导客户说明业务需求（不检索、不 AI 生成）"
      }
      if (ev.direct) {
        return `识别为「${intentZh(ev.intent)}」且把握很高（${conf}），跳过 AI 决策直接调用对应业务工具`
      }
      let out = `系统识别客户意图为「${intentZh(ev.intent)}」，把握 ${conf ?? "未知"}`
      const alts = Array.isArray(ev.alternatives) ? (ev.alternatives as string[]).filter(Boolean) : []
      if (alts.length) out += `；也考虑过：${alts.slice(0, 2).map(intentZh).join("、")}`
      return out
    }
    case "tool_call": {
      // 三种工具决策: 查询直达 / 高置信办理直连 / 编排循环内的工具执行
      if (ev.direct) {
        return `识别把握很高，跳过 AI 决策环节，直接调用「${ev.tool}」为客户办理（更快更稳定）`
      }
      if (ev.chain === "B" || ev.route === "query") {
        const parts = [`直接调用查询工具「${ev.tool ?? "?"}」查系统数据（不走 AI 对话）`]
        parts.push(ev.cache_hit ? "，结果来自近期缓存，未重复查询" : "")
        if (Array.isArray(ev.missing_params) && ev.missing_params.length) parts.push(`；还缺信息：${(ev.missing_params as string[]).join("、")}`)
        return parts.join("")
      }
      if (ev.result_preview !== undefined) {
        const ok = ev.is_error !== true
        const args = ev.arguments && typeof ev.arguments === "object" ? Object.entries(ev.arguments as Record<string, unknown>).map(([k, v]) => `${k}=${String(v).slice(0, 12)}`).join(" ") : ""
        return `AI 编排过程中调用了工具「${ev.tool}」${args ? `（${args}）` : ""}，${ok ? "执行成功" : "执行失败"}`
      }
      if (ev.traffic_class != null) return `根据识别结果选择处理方式：${TRAFFIC_ZH[String(ev.traffic_class)] ?? String(ev.traffic_class)}`
      if (ev.traffic_class === null && "composite" in ev) return "意图属于咨询类，进入知识问答流程"
      return d.reasoning
    }
    case "rag_retrieve": {
      if (ev.hit) {
        const n = Array.isArray(ev.citations) ? (ev.citations as unknown[]).length : 0
        return `从知识库检索到相关内容（引用 ${n} 个知识来源），供下一步 AI 生成回答时参考`
      }
      return "知识库中未找到与这句话相关的内容"
    }
    case "llm_generate":
      return ev.rag_used ? "AI 参考检索到的知识内容组织回复（非凭空生成）" : "AI 直接生成回复（无知识库参考）"
    case "faq_direct":
      return `命中常见问题「${String(ev.question ?? "").slice(0, 30)}」，直接返回人工审核过的标准答案（非 AI 生成）`
    case "chain_complete":
      return `本轮处理结束，客户收到「${sourceZh(ev.source)}」类型的回复`
    case "noise_blocked": {
      const why = NOISE_ZH[String(ev.reason ?? "")] ?? "内容不适合自动作答"
      return `${why}。已用固定澄清话术回应，没有让 AI 猜测作答（防止答非所问）`
    }
    case "transfer_agent":
      return "触发转人工流程，已为客户分配人工客服"
    case "injection_blocked":
      return "检测到输入中疑似包含诱导指令，已拦截（安全防线）"
    case "guard_denied":
      return "护栏规则拦截了本次请求（内容不适合自动处理）"
    case "outbound_guard": {
      const why: Record<string, string> = {
        sensitive_solicitation: "回复中出现索要卡号/密码等敏感信息的话术",
        sensitive_solicitation_stripped: "回复中部分话术不当（索要敏感信息），已自动删去该句、保留合规内容",
        ungrounded_numbers: "回复中的数字没有知识依据（疑似 AI 编造），已被替换",
        fabricated_execution: "回复声称已办理业务但实际未执行（AI 编造办理结果），已被替换",
        sensitive_words: "回复包含敏感词，已被替换",
      }
      const reason = String((ev as { reason?: string }).reason ?? "")
      return `${why[reason] ?? "回复内容未通过出站合规检查"}，客户收到的是安全话术`
    }
    case "context_reply_pass":
      return "客户这句话是在回答上一轮的提问（如补充卡号/日期），正常放行继续处理"
    case "mis_kill_candidate":
      return "系统连续两次没听懂客户，已标记为疑似误判案例，等待人工复核"
    default:
      return d.reasoning
  }
}
function sourceZh(v: unknown): string {
  const m: Record<string, string> = {
    tool: "系统查询结果", knowledge: "知识问答", faq: "标准答案", template: "固定话术",
    clarify: "澄清引导", fallback: "降级话术", llm: "AI 生成", retrieval: "检索原文",
  }
  return m[String(v ?? "")] ?? String(v ?? "")
}

function formatTime(s: string | null) {
  return s?.slice(0, 19).replace("T", " ") || "-"
}

const route = useRoute()
onMounted(() => {
  const q = route.query.session_id as string | undefined
  if (q && filters.value) filters.value.session_id = q
  load()
})
</script>

<style scoped lang="scss">
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--space-2);
}
.header-controls {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.conf-high { color: var(--color-success, #67c23a); }
.conf-mid { color: var(--color-warning, #e6a23c); }
.conf-low { color: var(--color-danger, #f56c6c); }

.turn-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  max-height: calc(100vh - 260px);
  overflow-y: auto;
  padding: var(--space-2);
}
.turn-row {
  display: flex;
  &.turn-customer { justify-content: flex-start; }
  &.turn-bot { justify-content: flex-end; }
}
.bubble {
  max-width: 72%;
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
  background: var(--color-bg-page);
}
.turn-customer .bubble {
  background: var(--el-color-primary-light-9, #ecf5ff);
  border-top-left-radius: 4px;
}
.turn-bot .bubble {
  background: var(--el-color-success-light-9, #f0f9eb);
  border-top-right-radius: 4px;
}
.bubble-content {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: var(--fs-sm);
}
.bubble-meta {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  margin-top: var(--space-1);
  flex-wrap: wrap;
}
.meta-num {
  font-size: var(--fs-xs, 12px);
  color: var(--color-text-secondary);
}
.meta-time {
  font-size: var(--fs-xs, 12px);
  color: var(--color-text-secondary);
}
// 决策链按轮分组: 每轮一个卡片块 (轮次徽标 + 全程耗时 + 该轮步骤时间线)
.turn-group {
  margin-bottom: var(--space-4);
  padding: var(--space-3);
  background: var(--color-bg-page);
  border-radius: var(--radius-md);
}
.turn-group-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-2);
}
.turn-badge {
  padding: 1px 8px;
  font-size: var(--fs-xs, 12px);
  font-weight: 600;
  color: var(--color-primary);
  background: var(--color-primary-light-9, rgba(64, 158, 255, 0.1));
  border-radius: 10px;
}
.turn-badge-legacy {
  color: var(--color-text-secondary);
  background: var(--color-fill, rgba(0, 0, 0, 0.06));
}
.turn-time {
  font-size: var(--fs-xs, 12px);
  color: var(--color-text-secondary);
}
.turn-total {
  font-size: var(--fs-xs, 12px);
  color: var(--color-success, #67c23a);
}
.turn-steps {
  margin-left: auto;
  font-size: var(--fs-xs, 12px);
  color: var(--color-text-secondary);
}
.decision-card {
  padding: var(--space-2);
  background: var(--color-bg-page);
  border-radius: var(--radius-md);
}
.decision-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.decision-kv {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  margin-top: 6px;
}
.kv-item {
  font-size: 12px;
  display: inline-flex;
  gap: 4px;
  align-items: baseline;
}
.kv-k {
  color: var(--el-text-color-secondary);
}
.kv-v {
  font-weight: 600;
  color: var(--el-text-color-primary);
}
.kv-bad {
  color: var(--el-color-danger);
}
.decision-agent {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-left: auto;
}
.decision-raw :deep(.el-collapse-item__header) {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  height: 28px;
  line-height: 28px;
  background: transparent;
  border: none;
}
.decision-raw :deep(.el-collapse-item__wrap) {
  background: transparent;
}
.decision-explain {
  font-size: 13px;
  font-weight: 500;
  color: var(--el-text-color-primary);
  margin-top: 6px;
  line-height: 1.6;
}

.decision-reason {
  margin-top: var(--space-1);
  font-size: var(--fs-sm);
  color: var(--color-text-primary);
}
.decision-evidence {
  margin: var(--space-2) 0 0;
  padding: var(--space-2);
  background: var(--color-bg-page, #f5f7fa);
  border-radius: var(--radius-sm);
  font-size: var(--fs-xs, 12px);
  max-height: 200px;
  overflow: auto;
}
:deep(.el-pagination) {
  display: flex;
}
</style>
