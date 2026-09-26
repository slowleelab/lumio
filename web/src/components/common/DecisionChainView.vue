<template>
  <div class="decision-chain-view">
    <!-- 每轮: 首行概览 (轮次+客户输入+总耗时+步骤胶囊流) + 次行内容详情 (回复全文+每步人话解释, 可折叠) -->
    <div v-for="g in turnGroups" :key="g.turnId" class="chain-row">
      <div class="row-main">
        <div class="row-head">
          <span class="turn-badge" :class="{ 'turn-badge-legacy': g.legacy }">
            {{ g.legacy ? "历史·未分轮" : `第 ${g.newIndex} 轮` }}
          </span>
          <span class="row-input" :title="g.input || formatTime(g.decisions[0].created_at)">
            {{ g.input || formatTime(g.decisions[0].created_at) }}
          </span>
          <span v-if="g.totalMs != null" class="row-total">
            {{ g.totalMs >= 1000 ? (g.totalMs / 1000).toFixed(1) + "s" : Math.round(g.totalMs) + "ms" }}
          </span>
        </div>
        <div class="row-steps">
          <template v-for="(d, i) in g.decisions" :key="d.decision_id">
            <span v-if="i" class="step-sep">›</span>
            <el-popover placement="top" trigger="click" :width="420" popper-class="chain-step-pop">
              <template #reference>
                <span class="step-chip" :class="'chip-' + decisionMeta(d.action).tag">
                  {{ decisionMeta(d.action).label
                  }}<em v-if="d.latency_ms != null && d.latency_ms > 0">{{
                    d.latency_ms >= 1000 ? (d.latency_ms / 1000).toFixed(1) + "s" : Math.round(d.latency_ms) + "ms"
                  }}</em>
                </span>
              </template>
              <div class="pop-title">
                <el-tag size="small" :type="decisionMeta(d.action).tag" effect="light">{{ decisionMeta(d.action).label }}</el-tag>
                <span class="pop-agent">{{ agentLabel(d.agent_name) }}</span>
              </div>
              <div class="pop-explain">{{ decisionExplain(d) }}</div>
              <div class="pop-reason">技术记录：{{ d.reasoning }}</div>
              <div v-if="evidenceSummary(d.evidence).length" class="pop-kvs">
                <span v-for="kv in evidenceSummary(d.evidence)" :key="kv.k" class="kv-item">
                  <span class="kv-k">{{ kv.k }}</span>
                  <span class="kv-v" :class="{ 'kv-bad': kv.bad }">{{ kv.v }}</span>
                </span>
              </div>
              <pre v-if="d.evidence && Object.keys(d.evidence).length" class="pop-raw">{{ JSON.stringify(d.evidence, null, 2) }}</pre>
            </el-popover>
          </template>
          <button class="detail-toggle" :class="{ open: !collapsedTurns.has(g.turnId) }" @click="toggleTurn(g.turnId)">
            {{ collapsedTurns.has(g.turnId) ? "展开内容" : "收起内容" }}
          </button>
        </div>
      </div>
      <!-- 内容详情层: 回复全文 + 每步人话解释直接可见 (悬停弹层在移动端不可用) -->
      <div v-if="!collapsedTurns.has(g.turnId)" class="turn-detail">
        <template v-if="g.reply">
          <div class="detail-label">
            本轮回复
            <el-tag v-if="g.replySource" size="small" effect="plain" type="info">{{ sourceZh(g.replySource) }}</el-tag>
          </div>
          <div class="reply-block">{{ g.reply }}</div>
        </template>
        <template v-else>
          <div class="detail-label muted">本轮回复 — 未记录 (历史会话或拦截链路无回复落档)</div>
        </template>
        <div class="detail-label">处理过程</div>
        <div class="explain-list">
          <div v-for="d in g.decisions" :key="'x' + d.decision_id" class="explain-item">
            <span class="explain-chip" :class="'chip-' + decisionMeta(d.action).tag">{{ decisionMeta(d.action).label }}</span>
            <span class="explain-text">{{ decisionExplain(d) }}</span>
          </div>
        </div>
      </div>
    </div>
    <el-empty v-if="!decisions.length" description="无决策记录" />
  </div>
</template>

<script setup lang="ts">
import { computed, ref } from "vue"
import type { ReplayDecision, ReplayTurn } from "@/api/console"

// 决策链统一渲染组件: 对话审计「决策链」页签与智能质检详情共用, 保证两处展示一致。
// turns (可选) 提供每轮 bot 回复全文 — customer/bot 行 turn_id 不同, 按时间顺序配对
// (customer 行开轮, 其后第一条 bot 行归属该轮)。
const props = defineProps<{ decisions: ReplayDecision[]; turns?: ReplayTurn[] | null }>()

// 内容详情层折叠状态: 默认全部展开 (审阅者要直接看内容), 折叠集记已收起的轮
const collapsedTurns = ref<Set<string>>(new Set())
function toggleTurn(turnId: string) {
  const next = new Set(collapsedTurns.value)
  if (next.has(turnId)) next.delete(turnId)
  else next.add(turnId)
  collapsedTurns.value = next
}

// ── 决策链可读化: action 中文化+语义配色 / evidence 关键字段摘要 ──
const ACTION_META: Record<string, { label: string; tag: string; dot: string; color?: string }> = {
  turn_start: { label: "消息出队", tag: "info", dot: "" },
  route_decision: { label: "路由决策", tag: "warning", dot: "warning" },
  query_rewrite: { label: "上下文改写", tag: "primary", dot: "primary" },
  intent_classify: { label: "意图分类", tag: "primary", dot: "primary" },
  tool_call: { label: "工具执行", tag: "success", dot: "success" },
  faq_retrieve: { label: "FAQ 检索", tag: "primary", dot: "primary" },
  rag_retrieve: { label: "文档检索 (RAG)", tag: "primary", dot: "primary" },
  llm_generate: { label: "回复生成", tag: "warning", dot: "warning" },
  chain_complete: { label: "链路完成", tag: "info", dot: "" },
  faq_direct: { label: "FAQ 检索·直出", tag: "success", dot: "success" },
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
  const decisions = props.decisions ?? []
  type Group = {
    turnId: string
    decisions: ReplayDecision[]
    totalMs: number | null
    legacy: boolean
    newIndex: number
    input: string
    reply: string
    replySource: string
  }
  // turns → bot 回复配对: decision 与 dialogue 的 turn_id 不同源 (chat 队列短 id vs
  // 独立 uuid), 无法按键关联 — 用 chain_complete 时间 ↔ bot 行落库时间就近匹配
  // (同轮毫秒级接近, 跨轮至少隔一次客户输入; 60s 阈值防误配)
  const botPool = (props.turns ?? [])
    .filter((t) => t.speaker === "bot" && t.timestamp && t.content)
    .map((t) => ({ at: new Date(t.timestamp as string).getTime(), reply: t.content || "", source: t.response_source || "", used: false }))
  function matchReply(doneAtMs: number | null): { reply: string; source: string } | null {
    if (doneAtMs == null || !botPool.length) return null
    let best: (typeof botPool)[number] | null = null
    let bestGap = Infinity
    for (const b of botPool) {
      if (b.used) continue
      const gap = Math.abs(b.at - doneAtMs)
      if (gap < bestGap) {
        bestGap = gap
        best = b
      }
    }
    if (best && bestGap <= 60_000) {
      best.used = true
      return { reply: best.reply, source: best.source }
    }
    return null
  }
  function doneAt(ds: ReplayDecision[]): number | null {
    const done = ds.find((x) => x.action === "chain_complete" && x.created_at)
    return done ? new Date(done.created_at as string).getTime() : null
  }
  const byTurn = new Map<string, ReplayDecision[]>()
  for (const d of decisions) {
    const key = d.turn_id || "-"
    if (!byTurn.has(key)) byTurn.set(key, [])
    byTurn.get(key)!.push(d)
  }
  const legacy: ReplayDecision[] = []
  const fresh: Group[] = []
  for (const [turnId, ds] of byTurn) {
    if (ds.some((x) => x.action === "turn_start")) {
      const fin = ds.find((x) => x.action === "chain_complete" && typeof x.latency_ms === "number")
      const input = String(ds.find((x) => x.action === "turn_start")?.evidence?.input_preview || "")
      const matched = matchReply(doneAt(ds))
      fresh.push({
        turnId,
        decisions: ds,
        totalMs: fin ? fin.latency_ms : null,
        legacy: false,
        newIndex: 0,
        input,
        reply: matched?.reply ?? "",
        replySource: matched?.source || (fin ? String(fin.evidence?.source ?? "") : ""),
      })
    } else {
      legacy.push(...ds)
    }
  }
  const out: Group[] = []
  if (legacy.length) {
    const fin = legacy.find((x) => x.action === "chain_complete" && typeof x.latency_ms === "number")
    out.push({ turnId: "legacy", decisions: legacy, totalMs: fin ? fin.latency_ms : null, legacy: true, newIndex: 0, input: "", reply: "", replySource: fin ? String(fin.evidence?.source ?? "") : "" })
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
  // 意图 (intent): faq 为旧意图名, 归一化主名 knowledge_qa — FAQ 是检索来源不是意图
  faq: "知识问答", knowledge_qa: "知识问答",
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
  reward_query: "积分相关", faq: "知识问答", knowledge_qa: "知识问答", faq_product: "产品咨询", chitchat: "闲聊/无明确业务",
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
  subword_ambiguous: "只输入了孤立的词语（如『信用』），看不出具体诉求",
}
function intentZh(v: unknown): string {
  const t = String(v ?? "")
  return INTENT_ZH[t] ?? t
}
function decisionExplain(d: { action: string; reasoning: string; evidence?: Record<string, unknown> | null }): string {
  const ev = d.evidence ?? {}
  const conf = typeof ev.confidence === "number" ? `${Math.round(ev.confidence * 100)}%` : null
  switch (d.action) {
    case "turn_start":
      return `客户消息进入处理队列${typeof ev.queue_wait_ms === "number" ? `（等待 ${Math.round(ev.queue_wait_ms)}ms）` : ""}`
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
    case "query_rewrite":
      return `客户在接上文追问 — 结合上下文将「${String(ev.original ?? "").slice(0, 20)}」改写为自包含问题「${String(ev.rewritten ?? "").slice(0, 30)}」再理解和检索${ev.context_answer ? "；上轮结果中已有答案，将直接复述" : ""}`
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
    case "faq_retrieve":
      return "在 FAQ 标准问答库三路检索（逐字精确/语义/BM25），未命中 —— 继续走文档知识库 RAG 检索"
    case "rag_retrieve": {
      if (ev.hit) {
        const n = Array.isArray(ev.citations) ? (ev.citations as unknown[]).length : 0
        return `从文档知识库检索到相关内容（引用 ${n} 个知识来源），供下一步 AI 生成回答时参考`
      }
      return "文档知识库中未找到与这句话相关的内容"
    }
    case "llm_generate":
      return ev.rag_used ? "AI 参考检索到的知识内容组织回复（非凭空生成）" : "AI 直接生成回复（无知识库参考）"
    case "faq_direct":
      return `在 FAQ 标准问答库命中「${String(ev.question ?? "").slice(0, 30)}」，直接返回人工审核过的标准答案（非 AI 生成）`
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
</script>

<style scoped lang="scss">
.chain-row {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 7px 10px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  margin-bottom: 8px;
  background: var(--color-bg-page);
}
.row-main {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}
.row-head {
  flex-shrink: 0;
  width: 268px;
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}
.turn-badge {
  flex-shrink: 0;
  padding: 1px 8px;
  font-size: var(--fs-xs, 12px);
  border-radius: 999px;
  background: var(--el-color-primary-light-8);
  color: var(--el-color-primary);
  font-weight: 600;
}
.turn-badge-legacy {
  background: var(--el-fill-color);
  color: var(--color-text-muted);
}
.row-input {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text-primary, #303133);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  border-left: 3px solid var(--el-color-primary);
  padding-left: 8px;
  line-height: 1.5;
}
.row-total {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--color-text-muted);
}
.row-steps {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 3px 0;
}
.step-sep {
  color: var(--el-border-color);
  margin: 0 4px;
  font-size: 11px;
}
.step-chip {
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
  padding: 2px 8px;
  font-size: 11.5px;
  border-radius: 6px;
  border: 1px solid var(--el-border-color);
  background: var(--el-fill-color-blank);
  color: var(--color-text-secondary);
  cursor: pointer;
  white-space: nowrap;
  transition: all 0.15s;
}
.step-chip:hover {
  border-color: var(--el-color-primary);
  color: var(--el-color-primary);
}
.step-chip em {
  font-style: normal;
  font-size: 10px;
  color: var(--color-text-muted);
}
.chip-primary {
  border-color: var(--el-color-primary-light-5);
  color: var(--el-color-primary);
}
.chip-success {
  border-color: var(--el-color-success-light-5);
  color: var(--el-color-success);
}
.chip-warning {
  border-color: var(--el-color-warning-light-5);
  color: var(--el-color-warning);
}
.chip-danger {
  border-color: var(--el-color-danger-light-5);
  color: var(--el-color-danger);
  background: var(--el-color-danger-light-9);
}
.detail-toggle {
  margin-left: 8px;
  padding: 1px 8px;
  font-size: 11px;
  border: none;
  border-radius: 999px;
  background: transparent;
  color: var(--color-text-muted);
  cursor: pointer;
  white-space: nowrap;
}
.detail-toggle:hover,
.detail-toggle.open {
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}
.turn-detail {
  border-top: 1px dashed var(--el-border-color-lighter);
  padding-top: 6px;
  margin-left: 0;
}
.detail-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11px;
  font-weight: 600;
  color: var(--color-text-muted);
  margin: 4px 0;
  &:first-child {
    margin-top: 0;
  }
}
.reply-block {
  font-size: 12.5px;
  line-height: 1.65;
  color: var(--color-text-primary, #303133);
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--el-fill-color-light);
  border-left: 3px solid var(--el-color-success-light-5);
  border-radius: 0 6px 6px 0;
  padding: 8px 10px;
  margin-bottom: 6px;
}
.explain-list {
  display: flex;
  flex-direction: column;
  gap: 3px;
}
.explain-item {
  display: flex;
  align-items: baseline;
  gap: 8px;
  font-size: 12px;
  line-height: 1.55;
}
.explain-chip {
  flex-shrink: 0;
  display: inline-flex;
  padding: 0 6px;
  font-size: 10.5px;
  border-radius: 4px;
  border: 1px solid var(--el-border-color);
  background: var(--el-fill-color-blank);
  color: var(--color-text-secondary);
  white-space: nowrap;
}
.explain-text {
  color: var(--color-text-secondary);
  min-width: 0;
}
</style>

<style lang="scss">
/* popper 挂 body, 需全局样式 */
.chain-step-pop {
  .pop-title {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 6px;
  }
  .pop-agent {
    font-size: 11px;
    color: var(--color-text-muted);
  }
  .pop-explain {
    font-size: 12.5px;
    line-height: 1.6;
    color: var(--color-text-primary, #303133);
  }
  .pop-reason {
    font-size: 11.5px;
    color: var(--color-text-muted);
    margin-top: 4px;
  }
  .pop-kvs {
    display: flex;
    flex-wrap: wrap;
    gap: 4px 10px;
    margin-top: 6px;
  }
  .kv-item {
    font-size: 11px;
  }
  .kv-k {
    color: var(--color-text-muted);
    margin-right: 3px;
  }
  .kv-v {
    font-weight: 600;
  }
  .kv-bad {
    color: var(--el-color-danger);
  }
  .pop-raw {
    max-height: 180px;
    overflow: auto;
    background: var(--color-bg-page);
    border-radius: 6px;
    padding: 8px;
    font-size: 10.5px;
    line-height: 1.5;
    margin: 8px 0 0;
  }
}
</style>
