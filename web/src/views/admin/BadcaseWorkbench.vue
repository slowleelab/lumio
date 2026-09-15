<template>
  <div class="badcase-page">
    <div class="page-header">
      <h2>智能质检</h2>
    </div>


    <div class="filters">
      <el-select v-model="qcFilters.category" placeholder="判定" clearable size="small" style="width: 110px" @change="reloadQc">
        <el-option label="合格" value="pass" />
        <el-option label="提醒级" value="warn" />
        <el-option label="不合格" value="fail" />
        <el-option label="未质检" value="unscanned" />
      </el-select>
      <el-input
        v-model="qcFilters.keyword"
        placeholder="搜索会话 ID / 用户输入…"
        clearable
        size="small"
        style="width: 230px"
        :prefix-icon="Search"
        @keyup.enter="reloadQc"
        @clear="reloadQc"
      />
      <el-button size="small" @click="reloadQc">查询</el-button>
      <el-button v-if="qcFilters.category || qcFilters.keyword" size="small" link @click="clearQcFilters">清除筛选</el-button>
      <div class="filter-spacer"></div>
    </div>

    <el-table
      :data="qcRows"
      v-loading="qcLoading"
      stripe
      size="small"
      style="margin-top: 12px"
      @selection-change="onSelection"
      @row-click="openQcDetail"
    >
      <el-table-column label="会话时间" width="150">
        <template #default="{ row }">
          <span :title="sessionTimeTitle(row)">
            {{ fmtTime(row.session_time || row.scanned_at || row.collected_at) }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="会话 ID" width="150" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="session-id">{{ row.session_id }}</span>
        </template>
      </el-table-column>
      <el-table-column label="轮数" width="54" align="center">
        <template #default="{ row }">{{ row.turns ?? "-" }}</template>
      </el-table-column>
      <el-table-column label="判定" width="76" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.verdict === 'pass'" size="small" type="success">合格</el-tag>
          <el-tag v-else-if="row.verdict" size="small" type="danger" :title="row.verdict === 'warn' ? '提醒级问题 (原判定: 提醒)' : ''">不合格</el-tag>
          <span v-else class="muted">-</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="84" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.qc_status === 'human'" size="small" type="warning">人工质检</el-tag>
          <el-tag v-else-if="row.qc_status === 'ai'" size="small" type="primary">AI质检</el-tag>
          <el-tag v-else size="small" type="info">未质检</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="质检问题" min-width="160">
        <template #default="{ row }">
          <template v-if="row.problems?.length">
            <el-tooltip
              v-for="(p, i) in row.problems"
              :key="i"
              :content="`${p.turn ? `第 ${p.turn} 轮 · ` : ''}${p.reason || ''}`"
              placement="top"
            >
              <el-tag size="small" type="danger" effect="plain" class="problem-tag">{{ problemLabel(p.type) }}</el-tag>
            </el-tooltip>
          </template>
          <span v-else class="muted">-</span>
        </template>
      </el-table-column>
      <el-table-column label="来源" width="92">
        <template #default="{ row }">
          <el-tag v-if="row.signal_source" size="small" :type="signalType(row.signal_source)">{{ signalLabel(row.signal_source) }}</el-tag>
          <span v-else class="muted">-</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="70" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click.stop="openQcDetail(row)">详情</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="暂无会话 — 跑一轮全量质检或对话模拟; 新会话结束会自动质检" :image-size="64" />
      </template>
    </el-table>

    <el-pagination
      v-model:current-page="qcPage"
      v-model:page-size="qcPageSize"
      :total="qcTotal"
      :page-sizes="[20, 50, 100]"
      layout="total, sizes, prev, pager, next"
      style="margin-top: 14px; justify-content: flex-end"
      @current-change="loadQc"
      @size-change="reloadQc"
    />


    <!-- ══ 质检详情抽屉 · 工作台三段式: 结论 Hero → 证据 Tabs → 吸底行动区 ══ -->
    <el-drawer v-model="qcDetailVisible" size="58%" destroy-on-close class="qc-drawer">
      <template #header>
        <div class="drawer-title">
          <span class="session-id">{{ qcDetail?.session_id }}</span>
          <el-tag v-if="qcDetail?.qc_status === 'human'" size="small" type="warning">人工质检</el-tag>
          <el-tag v-if="(qcDetail?.case_count ?? 0) > 1" size="small" type="info" effect="plain">{{ qcDetail?.case_count }} 案</el-tag>
        </div>
      </template>

      <div v-if="qcDetail" class="qc-detail" v-loading="qcReplayLoading">
        <!-- ── 结论 Hero: 10 秒扫视 (判定 · 摘要 · 元信息) ── -->
        <div class="qc-hero" :class="`qc-hero--${qcDetail.verdict || 'none'}`">
          <div class="qc-hero-main">
            <div class="qc-hero-verdict">{{ verdictLabel(qcDetail.verdict || "") || "未质检" }}</div>
            <div class="qc-hero-meta">
              {{ shortModel(qcDetail.judge_model || "") || "AI 裁判" }} · {{ qcDetail.turns ?? "-" }} 轮对话 · {{ fmtTime(qcDetail.session_time || qcDetail.scanned_at) }}
              <template v-if="qcDetail.signal_source"> · {{ signalLabel(qcDetail.signal_source) }}</template>
            </div>
          </div>
          <div class="qc-hero-summary">{{ qcDetail.summary || "裁判未给出摘要" }}</div>
        </div>

        <!-- ── 证据: 三个视图共享同一空间, 切换替代滚动 ── -->
        <el-tabs v-model="qcEvidenceTab" class="qc-evidence">
          
          <el-tab-pane :label="`会话核查 (${qcPanorama.length} 轮)`" name="replay">
            <template v-if="qcPanorama.length">
              <div v-for="r in qcPanorama" :key="r.round" class="pano-round" :class="{ 'pano-problem': r.problem }">
                <span class="pano-round-no">{{ r.round }}</span>
                <div class="pano-text">
                  <div class="pano-customer">{{ r.customer }}</div>
                  <div class="pano-bot">
                    {{ r.bot }}
                    <el-tag v-if="r.source" size="small" type="info" class="scene-src">{{ r.source }}</el-tag>
                  </div>
                  <!-- 问题标注内联在对应回答下 (证据与发现同一现场); 支持人工编辑描述 -->
                  <div v-for="(p, pi) in r.problems" :key="pi" class="qc-inline-problem">
                    <div class="qc-inline-head">
                      <el-tag size="small" type="danger" effect="plain">{{ problemLabel(p.type) }}</el-tag>
                      <span class="muted">第 {{ r.round }} 轮对话 · 日志第 {{ p.turn }} 行</span>
                      <el-button size="small" link type="primary" @click="startEditProblem(p)">编辑</el-button>
                    </div>
                    <template v-if="editingProblemIdx !== (qcDetail.problems ?? []).indexOf(p)">
                      <div class="qc-inline-reason">{{ p.reason || "(未说明原因)" }}</div>
                    </template>
                    <template v-else>
                      <el-input v-model="problemDraft" type="textarea" :rows="2" size="small" placeholder="修订问题描述 (保存后追加人工判定记录, 原 AI 标注保留可追溯)" />
                      <div class="qc-inline-actions">
                        <el-button size="small" type="primary" :loading="annotating" @click="saveProblemEdit()">保存</el-button>
                        <el-button size="small" @click="editingProblemIdx = -1">取消</el-button>
                      </div>
                    </template>
                  </div>
                </div>
              </div>
            </template>
            <div v-else class="muted qc-evi-empty">无对话记录 (可能仅被信号采集, 尚未质检)</div>
          </el-tab-pane>

          <el-tab-pane :label="`决策链 (${qcReplay?.decisions.length ?? 0})`" name="chain">
            <DecisionChainView v-if="qcReplay?.decisions.length" :decisions="qcReplay.decisions" />
            <div v-else class="muted qc-evi-empty">无决策记录</div>
          </el-tab-pane>

          <el-tab-pane v-if="replayState.newSessionId || replayState.running" name="diff">
            <template #label>重放对比<el-tag v-if="replayChangedCount > 0" size="small" type="success" class="qc-tab-badge-tag">{{ replayChangedCount }} 变化</el-tag></template>
            <div v-if="replayState.running" class="replay-progress">
              <el-progress :percentage="replayProgressPct" :stroke-width="8" striped striped-flow status="success" />
              <span class="muted">
                重放中 {{ replayState.done }}/{{ replayState.total }} 轮{{ replayState.current ? ` · 正在发: ${replayState.current.slice(0, 16)}…` : "" }} · 新会话 {{ replayState.newSessionId?.slice(0, 24) }}…
              </span>
            </div>
            <template v-if="!replayState.running && replayCompare.length">
              <div class="replay-compare-head">
                <span>原会话 ({{ fmtTime(qcDetail.session_time) }})</span>
                <span>重放会话 ({{ fmtTime(replayState.finishedAt) }})</span>
              </div>
              <div v-for="c in replayCompare" :key="c.round" class="replay-row">
                <span class="pano-round-no" :class="{ 'is-problem': c.changed }">{{ c.round }}</span>
                <div class="replay-cells">
                  <div class="replay-cell">
                    <div class="replay-customer">{{ c.customer }}</div>
                    <div class="pano-bot">{{ c.oldBot }} <el-tag v-if="c.oldSource" size="small" type="info" class="scene-src">{{ c.oldSource }}</el-tag></div>
                  </div>
                  <div class="replay-cell" :class="{ 'replay-changed': c.changed }">
                    <div class="pano-bot">{{ c.newBot || "…" }} <el-tag v-if="c.newSource" size="small" :type="c.changed ? 'success' : 'info'" class="scene-src">{{ c.newSource }}</el-tag></div>
                  </div>
                </div>
              </div>
              <div class="replay-summary">
                <template v-if="replayChangedCount > 0">
                  <el-tag type="success" size="small">{{ replayChangedCount }}/{{ replayCompare.length }} 轮回复变化</el-tag>
                  已结束重放会话并触发质检 — 列表搜 <span class="session-id">{{ replayState.newSessionId }}</span> 查看新判定
                </template>
                <el-tag v-else type="info" size="small">回复与原会话一致</el-tag>
              </div>
            </template>
          </el-tab-pane>
        </el-tabs>
      </div>

      <!-- ── 吸底工具行: 本抽屉只核查会话质量; 案例是独立处置对象, 入口收敛为一行摘要 ── -->
      <template #footer>
        <div v-if="qcDetail" class="qc-actionbar">
          <div class="qc-cases-digest">
            <template v-if="(qcDetail.case_count ?? 0) > 0">
              <span class="qc-cases-digest-main">问题已立案 {{ qcDetail.case_count }} 项</span>
              <el-link type="primary" :underline="false" @click="router.push({ path: '/admin/cases', query: { keyword: qcDetail.session_id } })">去案例工作台 ›</el-link>
            </template>
            <template v-else-if="qcDetail.verdict === 'fail'">
              <span class="muted">判定不合格但未单独开案 — 同题已并入既有案例组 (30 天一案), 可在案例工作台按问题句搜索</span>
            </template>
            <template v-else><span class="muted">质检合格 · 无问题案例</span></template>
          </div>

          <div class="qc-actionbar-tools">
            <span class="muted action-hint">判定 (与案例处置正交)</span>
            <el-dropdown :disabled="humanJudging != null" @command="doHumanVerdict">
              <el-button size="small" type="primary" plain :loading="humanJudging != null">
                {{ qcDetail.qc_status === "human" ? "重新人工判定" : "人工判定" }} ▾
              </el-button>
              <template #dropdown>
                <el-dropdown-menu>
                  <el-dropdown-item command="pass">标记为合格</el-dropdown-item>
                  <el-dropdown-item command="fail">标记为不合格</el-dropdown-item>
                </el-dropdown-menu>
              </template>
            </el-dropdown>
            <el-tooltip content="对原对话重跑 AI 裁判, 覆盖为最新判定 (与重放验证不同: 不重新生成回复)" placement="top">
              <el-button size="small" :loading="qcRescanning" @click="doRescan">重新质检</el-button>
            </el-tooltip>
            <span class="qc-tools-sep"></span>
            <el-tooltip content="原客户消息按序重发当前链路 — 修复前后对比验证" placement="top">
              <el-button size="small" plain :disabled="replayState.running" @click="doReplay">
                {{ replayState.running ? `重放中 ${replayState.done}/${replayState.total}` : `重放验证 (${qcPanorama.length} 轮)` }}
              </el-button>
            </el-tooltip>
          </div>
        </div>
      </template>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from "vue"
import { useRoute, useRouter } from "vue-router"
import { ElMessage, ElMessageBox } from "element-plus"
import { ArrowRight, Search } from "@element-plus/icons-vue"
import {
  listQcSessions,
  rescanQualitySession,
  replayQualitySession,
  getReplayStatus,
  humanVerdictQualitySession,
  type QcSessionRow,
  type QualityProblem,
} from "@/api/closedLoop"
import { getConversationReplay, type ReplayResponse } from "@/api/console"
import DecisionChainView from "@/components/common/DecisionChainView.vue"

const router = useRouter()
const route = useRoute()

// ── 页签: 质检记录 (全量会话判定) / 问题案例 (归因整改闭环) ──
// ── 统一会话质检列表 (判定 ⟕ 问题案例, 会话维度一行) ──
const qcRows = ref<QcSessionRow[]>([])
const qcTotal = ref(0)
const qcPage = ref(1)
const qcPageSize = ref(50)
const qcLoading = ref(false)
const qcFilters = ref<{ category: string; keyword: string }>({ category: "", keyword: "" })
const selected = ref<QcSessionRow[]>([])

async function loadQc() {
  qcLoading.value = true
  try {
    const res = await listQcSessions({
      category: qcFilters.value.category || undefined,
      keyword: qcFilters.value.keyword || undefined,
      limit: qcPageSize.value,
      offset: (qcPage.value - 1) * qcPageSize.value,
    })
    qcRows.value = res.sessions
    qcTotal.value = res.total
  } catch {
    /* handled */
  } finally {
    qcLoading.value = false
  }
}

function reloadQc() {
  qcPage.value = 1
  loadQc()
}

function clearQcFilters() {
  qcFilters.value = { category: "", keyword: "" }
  reloadQc()
}

function sessionTimeTitle(row: QcSessionRow): string {
  const parts = [`会话 ${fmtTime(row.session_time)}`]
  if (row.scanned_at) parts.push(`质检 ${fmtTime(row.scanned_at)}`)
  if (row.collected_at) parts.push(`案例采集 ${fmtTime(row.collected_at)}`)
  return parts.join(" · ")
}

function verdictLabel(v: string) {
  return { pass: "合格", warn: "提醒", fail: "不合格" }[v] ?? v
}
function verdictType(v: string): string {
  return { pass: "success", warn: "warning", fail: "danger" }[v] ?? "info"
}
const PROBLEM_LABELS: Record<string, string> = {
  A: "答非所问",
  B: "幻觉编造",
  C: "越界承诺",
  D: "漏转人工",
  E: "未解决无引导",
}
function problemLabel(t?: string) {
  return t ? (PROBLEM_LABELS[t] ?? t) : "-"
}

// 质检记录行点击: fail 且已采入闭环 → 打开对应问题案例; 否则跳会话回放
const qcDetailVisible = ref(false)
const qcDetail = ref<QcSessionRow | null>(null)
const qcReplay = ref<ReplayResponse | null>(null)
const qcReplayLoading = ref(false)
const qcRescanning = ref(false)

async function openQcDetail(row: QcSessionRow) {
  qcDetail.value = row
  qcDetailVisible.value = true
  // 一通会话可能多方面问题 → 多案例: 拉全量供逐案处置
  qcReplay.value = null
  qcReplayLoading.value = true
  qcEvidenceTab.value = "replay"
  replayState.value = { running: false, newSessionId: null, total: 0, done: 0, finishedAt: null, current: "" }
  replayNewReplay.value = null
  try {
    qcReplay.value = await getConversationReplay(row.session_id)
  } catch {
    /* 回放拉取失败时抽屉降级为仅判定视图 */
  } finally {
    qcReplayLoading.value = false
  }
}

// 轮号口径: QA 裁判的 turn N 数的是对话日志行 (客户/客服各算一行)。
// rowRound(n) = 第 n 行(1-based)所属的客户轮序 (每遇客户行 +1)
function rowRound(n: number): number {
  const ts = qcReplay.value?.turns ?? []
  let r = 0
  for (let i = 0; i < n && i < ts.length; i++) if (ts[i].speaker === "customer") r++
  return r
}

// 问题轮现场: 定位第 N 行 → 向前取客户句 / 向后取客服句
function problemTurnDialog(turn?: number | null): { customer: string; bot: string; source: string | null } | null {
  if (!turn || !qcReplay.value) return null
  const ts = qcReplay.value.turns
  if (turn < 1 || turn > ts.length) return null
  let ci = ts[turn - 1].speaker === "customer" ? turn - 1 : -1
  for (let i = turn - 1; i >= 0 && ci < 0; i--) if (ts[i].speaker === "customer") ci = i
  let bi = ts[turn - 1].speaker === "bot" ? turn - 1 : -1
  for (let i = turn - 1; i < ts.length && bi < 0; i++) if (ts[i].speaker === "bot") bi = i
  if (ci < 0) return null
  return { customer: ts[ci].content, bot: bi >= 0 ? ts[bi].content : "", source: bi >= 0 ? ts[bi].response_source : null }
}

// 会话决策链按轮分组 (turn_start 起组, 组序 = 轮序); details 供链路节点悬停查看依据与耗时
const qcTurnChains = computed(() => {
  const ds = qcReplay.value?.decisions ?? []
  const groups: { round: number; steps: string[]; details: { label: string; title: string }[] }[] = []
  for (const d of ds) {
    if (d.action === "turn_start" || !groups.length) groups.push({ round: groups.length + 1, steps: [], details: [] })
    if (d.action !== "turn_start") {
      const label = ACTION_STEP[d.action] ?? d.action
      const g = groups[groups.length - 1]
      g?.steps.push(label)
      g?.details.push({
        label,
        title: [d.reasoning, d.latency_ms != null ? `${d.latency_ms}ms` : null].filter(Boolean).join(" · ") || label,
      })
    }
  }
  return groups
})
const ACTION_STEP: Record<string, string> = {
  intent_classify: "意图分类",
  route_decision: "路由",
  query_rewrite: "上下文改写",
  tool_call: "工具",
  rag_retrieve: "检索",
  llm_generate: "生成",
  noise_blocked: "噪声拦截",
  faq_direct: "FAQ直出",
  chain_complete: "完成",
  topic_track: "诉求跟踪",
  context_reply_pass: "回话放行",
  outbound_guard: "出站闸门",
}

// 会话全景: 客户轮 → 客服回复配对
const qcPanorama = computed(() => {
  const ts = qcReplay.value?.turns ?? []
  const rounds: {
    round: number
    customer: string
    bot: string
    source: string | null
    problem: boolean
    problems: QualityProblem[]
  }[] = []
  let round = 0
  for (const t of ts) {
    if (t.speaker === "customer") {
      round += 1
      const bot = ts[ts.indexOf(t) + 1]
      // 该轮的问题标注 (裁判发现内联到对应回答下, 代码审查 inline-comment 范式)
      const roundProblems = (qcDetail.value?.problems ?? []).filter((p) => p.turn != null && rowRound(p.turn) === round)
      rounds.push({
        round,
        customer: t.content,
        bot: bot?.speaker === "bot" ? bot.content : "",
        source: bot?.response_source ?? null,
        problem: roundProblems.length > 0,
        problems: roundProblems,
      })
    }
  }
  return rounds
})

// 证据视图: tabs 共享空间 (问题定位默认), 重放启动后自动切对比
const qcEvidenceTab = ref("replay")

// 重放执行: 原客户消息按序重发 → 轮询完成 → 结束会话触发质检 → 前后对比
const replayState = ref<{
  running: boolean
  newSessionId: string | null
  total: number
  done: number
  finishedAt: string | null
  current: string
}>({ running: false, newSessionId: null, total: 0, done: 0, finishedAt: null, current: "" })
const replayNewReplay = ref<ReplayResponse | null>(null)
let replayTimer: ReturnType<typeof setInterval> | null = null

const replayProgressPct = computed(() =>
  replayState.value.total ? Math.min(100, Math.round((replayState.value.done / replayState.value.total) * 100)) : 0,
)

const replayCompare = computed(() => {
  const oldRounds = qcPanorama.value
  const newTurns = replayNewReplay.value?.turns ?? []
  const newRounds: { customer: string; bot: string; source: string | null }[] = []
  let round = 0
  for (const t of newTurns) {
    if (t.speaker === "customer") {
      round += 1
      const bot = newTurns[newTurns.indexOf(t) + 1]
      newRounds.push({ customer: t.content, bot: bot?.speaker === "bot" ? bot.content : "", source: bot?.response_source ?? null })
    }
  }
  return oldRounds.map((o, i) => {
    const n = newRounds[i]
    return {
      round: o.round,
      customer: o.customer,
      oldBot: o.bot,
      oldSource: o.source,
      newBot: n?.bot ?? "",
      newSource: n?.source ?? null,
      changed: !!n && (n.bot !== o.bot || n.source !== o.source),
    }
  })
})
const replayChangedCount = computed(() => replayCompare.value.filter((c) => c.changed).length)

async function doReplay() {
  if (!qcDetail.value || replayState.value.running) return
  replayState.value = { running: true, newSessionId: null, total: 0, done: 0, finishedAt: null, current: "" }
  replayNewReplay.value = null
  qcEvidenceTab.value = "diff" // 启动即切对比视图, 进度与前后对照同屏
  try {
    const r = await replayQualitySession(qcDetail.value.session_id)
    replayState.value.newSessionId = r.new_session_id
    replayState.value.total = r.total_rounds
    let waited = 0
    // 轮询后端串行重放进度 (逐轮推进: 发一条 → 等回复 → 下一条; 完成后后端自动结束会话并触发质检)
    replayTimer = setInterval(async () => {
      waited += 2
      try {
        const st = await getReplayStatus(r.new_session_id)
        replayState.value.done = Math.min(st.done, r.total_rounds)
        replayState.value.current = st.current || ""
        if (st.status === "done" || st.status === "error" || waited > 600) {
          clearInterval(replayTimer!)
          replayTimer = null
          replayState.value.running = false
          replayState.value.finishedAt = new Date().toISOString()
          try {
            replayNewReplay.value = await getConversationReplay(r.new_session_id)
          } catch {
            /* 对比回放拉取失败, 进度结论仍有效 */
          }
          if (st.status === "error") {
            ElMessage.warning(`重放异常: ${st.error || "未知错误"} · 已完成 ${st.done}/${st.total || r.total_rounds} 轮`)
          } else if (st.timeouts > 0) {
            ElMessage.warning(`重放完成 (${st.timeouts} 轮等待回复超时已跳过) · 已结束新会话并触发质检`)
          } else if (waited > 600) {
            ElMessage.warning("重放超时收尾 (后台仍在执行, 稍后可搜新会话查看)")
          } else {
            ElMessage.success("重放完成, 已结束新会话并触发质检")
          }
        }
      } catch {
        /* 进度暂不可读, 继续等 */
      }
    }, 2000)
  } catch {
    replayState.value.running = false
    ElMessage.error("重放启动失败")
  }
}

// 人工判定: 追加 judge_model=人工判定 的质检记录, 状态列转为「人工质检」
// ── 问题标注内联编辑: 修订描述 → 追加人工判定记录 (append-only, 原 AI 标注可追溯) ──
const editingProblemIdx = ref(-1) // 在 qcDetail.problems 中的精确索引 (引用对位, 轮次重算在多问题时会错位)
const problemDraft = ref("")
const annotating = ref(false)

function startEditProblem(p: QualityProblem) {
  editingProblemIdx.value = (qcDetail.value?.problems ?? []).indexOf(p)
  problemDraft.value = p.reason || ""
}

async function saveProblemEdit() {
  if (!qcDetail.value) return
  annotating.value = true
  try {
    // 深拷贝当前标注列表, 仅改编辑中那条的描述
    const next = (qcDetail.value.problems ?? []).map((p) => ({ ...p }))
    if (editingProblemIdx.value >= 0 && editingProblemIdx.value < next.length) {
      next[editingProblemIdx.value].reason = problemDraft.value.trim()
    }
    await humanVerdictQualitySession(qcDetail.value.session_id, qcDetail.value.verdict === "pass" ? "pass" : "fail", undefined, next)
    ElMessage.success("标注已修订 — 追加人工判定记录 (原 AI 标注保留可追溯)")
    editingProblemIdx.value = -1
    await loadQc()
    const row = qcRows.value.find((x) => x.session_id === qcDetail.value?.session_id)
    if (row) qcDetail.value = row
  } catch {
    /* handled */
  } finally {
    annotating.value = false
  }
}

const humanJudging = ref<"pass" | "fail" | null>(null)
async function doHumanVerdict(v: "pass" | "fail") {
  if (!qcDetail.value) return
  const label = v === "pass" ? "合格" : "不合格"
  try {
    await ElMessageBox.confirm(
      `以人工质检身份将该会话判定为「${label}」? 判定记录追加落库, 原 AI 判定保留可追溯。`,
      "人工判定",
      { type: "warning", confirmButtonText: `判定为${label}`, cancelButtonText: "取消" },
    )
  } catch {
    return
  }
  humanJudging.value = v
  try {
    const r = await humanVerdictQualitySession(qcDetail.value.session_id, v)
    if (qcDetail.value) {
      qcDetail.value = {
        ...qcDetail.value,
        verdict: v,
        qc_status: "human",
        judge_model: "人工判定",
        problems: v === "pass" ? [] : qcDetail.value.problems,
      }
    }
    await loadQc()
    ElMessage.success(`人工判定完成: ${label}`)
    if (r?.open_badcase) {
      ElMessage.warning("该会话仍有待处置的问题案例 — 判定已改合格, 建议进整改闭环驳回该案例", { duration: 6000 })
    }
  } catch {
    ElMessage.error("人工判定失败")
  } finally {
    humanJudging.value = null
  }
}

async function doRescan() {
  if (!qcDetail.value) return
  qcRescanning.value = true
  try {
    const r = await rescanQualitySession(qcDetail.value.session_id)
    if (r.status === "ok" && qcDetail.value) {
      qcDetail.value = {
        ...qcDetail.value,
        verdict: (r.verdict as QcSessionRow["verdict"]) ?? qcDetail.value.verdict,
        problems: r.problems ?? qcDetail.value.problems,
        summary: r.summary ?? qcDetail.value.summary,
      }
      await loadQc()
      ElMessage.success(`重新质检完成: ${verdictLabel(r.verdict ?? "")}`)
    } else {
      ElMessage.info(r.status === "skipped" ? "对话不足 2 轮, 跳过" : "重新质检完成")
    }
  } catch {
    ElMessage.error("重新质检失败")
  } finally {
    qcRescanning.value = false
  }
}

// ── 展示工具 ──
const LAYER_LABELS: Record<string, string> = {
  layer_1: "预处理",
  layer_2: "会话管理",
  layer_3: "意图识别",
  layer_4: "路由决策",
  layer_5: "RAG 检索",
  layer_6: "回复生成",
  layer_7: "风控合规",
  uncertain: "待确认根因",
}
const SIGNAL_LABELS: Record<string, string> = {
  negative_feedback: "负面反馈",
  transfer: "转人工",
  agent_revoke: "坐席撤回",
  behavior_anomaly: "行为异常",
  compliance_alert: "合规告警",
  qa_scan: "质检不合格",
}

function distOf(dist: Record<string, number> | undefined, labels: Record<string, string>) {
  const entries = Object.entries(dist ?? {}).map(([key, count]) => ({ key, count, label: labels[key] ?? key }))
  const total = entries.reduce((s, e) => s + e.count, 0) || 1
  const max = Math.max(1, ...entries.map((e) => e.count))
  return entries
    .sort((a, b) => b.count - a.count)
    .map((e) => ({
      ...e,
      width: `${Math.max(4, Math.round((e.count / max) * 100))}%`,
      pct: `${Math.round((e.count / total) * 100)}%`,
    }))
}
const layerDist = computed(() => distOf(stats.value?.layer_dist, LAYER_LABELS))
const signalDist = computed(() => distOf(stats.value?.signal_dist, SIGNAL_LABELS))

// 修复闭环漏斗: 采集 → 待复核 → 已确认 → 已上线


function signalType(s: string): string {
  const m: Record<string, string> = {
    negative_feedback: "danger",
    transfer: "warning",
    agent_revoke: "warning",
    behavior_anomaly: "info",
    compliance_alert: "danger",
    qa_scan: "success",
  }
  return m[s] ?? "info"
}
function signalLabel(s: string) {
  return SIGNAL_LABELS[s] ?? s
}
function layerLabel(s?: string | null) {
  return s ? (LAYER_LABELS[s] ?? s) : ""
}
function categoryLabel(s?: string | null) {
  return s ? (CATEGORY_LABELS[s] ?? s) : ""
}
function fixStatusLabel(s: string) {
  const m: Record<string, string> = { pending: "待修", fixing: "修复中", canary: "已灰度", deployed: "已上线", verified: "已验证", reopened: "已重开", rejected: "已驳回" }
  return m[s] ?? s
}
function fixStatusType(s: string): string {
  const m: Record<string, string> = { pending: "info", fixing: "warning", canary: "warning", deployed: "success", verified: "success", reopened: "danger", rejected: "danger" }
  return m[s] ?? "info"
}
function shortModel(m?: string | null) {
  if (!m) return ""
  if (m.includes("GLM")) return "GLM 裁判"
  if (m.includes("qwen")) return "qwen 裁判"
  return m.length > 10 ? m.slice(0, 10) : m
}
function fmtTime(iso?: string | null) {
  return iso ? iso.slice(0, 19).replace("T", " ") : "-"
}
onMounted(() => {
  // 报表卡片跳转带入筛选: 判定域 query.category (兼容旧值 pending_review → 处置待处置) + 处置域 query.disposition
  const q = route.query.category as string | undefined
  if (q && ["pass", "warn", "fail", "unscanned"].includes(q)) {
    qcFilters.value.category = q
  }
  loadQc()
})
onUnmounted(() => {
  if (replayTimer) clearInterval(replayTimer)
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
.qa-tabs {
  margin-top: 8px;
  .tab-hint { font-size: var(--fs-xs, 11px); color: var(--color-text-placeholder); margin-left: 4px; }
}
.batch-progress-text { font-size: var(--fs-sm); color: var(--color-text-secondary); }
.problem-tag { margin-right: 4px; margin-bottom: 2px; cursor: default; }
.qa-verdict {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  .qa-summary { font-size: var(--fs-sm); color: var(--color-text-primary); }
}
.qa-problem {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 4px 8px;
  border-radius: 6px;
  background: var(--el-color-danger-light-9, #fef0f0);
  margin-bottom: 4px;
  font-size: var(--fs-sm);
  .qa-reason { color: var(--color-text-primary); }
}
.turns-meta {
  margin: 8px 0;
  .turns-meta-title { font-size: var(--fs-xs, 11px); margin-bottom: 4px; color: var(--color-text-secondary); }
  .turn-meta-row {
    display: flex;
    gap: 10px;
    font-size: var(--fs-xs, 12px);
    padding: 3px 8px;
    border-radius: 4px;
    &:nth-child(odd) { background: var(--el-fill-color-extra-light); }
    .turn-idx { width: 22px; color: var(--color-text-placeholder); }
    .turn-speaker { width: 30px; font-weight: 600; color: var(--color-text-secondary); &.is-customer { color: var(--el-color-primary); } }
    .turn-intent { width: 130px; }
    .turn-src, .turn-intent { color: var(--color-text-secondary); }
  }
}
.snapshot.transcript { margin-top: 8px; max-height: 260px; overflow: auto; }
.raw-snap { margin-top: 8px; }
.filters {
  display: flex;
  gap: var(--space-2);
  margin-top: 12px;
  flex-wrap: wrap;
  align-items: center;
}
.filter-spacer { flex: 1; }
.muted { color: var(--color-text-secondary); }
.input-text { cursor: pointer; }
.session-id {
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 11px;
  color: var(--color-text-secondary);
}
.model-tag {
  font-size: var(--fs-xs, 11px);
  border: 1px solid var(--el-border-color-light);
  border-radius: 4px;
  padding: 1px 4px;
  color: var(--color-text-secondary);
}

.drawer-title {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 15px;
}
.detail-body { padding: 0 4px; }
.section-title {
  font-weight: 600;
  margin: 14px 0 6px;
  font-size: var(--fs-sm);
  .section-hint { font-weight: 400; font-size: var(--fs-xs, 11px); }
}
.ctx-row {
  display: flex;
  gap: 8px;
  padding: 5px 10px;
  border-radius: 6px;
  margin-bottom: 4px;
  font-size: var(--fs-sm);
  .ctx-speaker { flex-shrink: 0; font-weight: 600; color: var(--color-text-secondary); }
  .ctx-content { white-space: pre-wrap; word-break: break-all; }
  &.ctx-user .ctx-content { color: var(--color-text-primary); }
  &.ctx-bot { background: var(--el-fill-color-light); }
  &.highlight { border: 1px solid var(--el-color-primary-light-7); }
  &.ctx-user.highlight { background: var(--el-color-primary-light-9); }
}
.context-loading { font-size: var(--fs-sm); padding: 6px 0; }
.attrib-row { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; flex-wrap: wrap; }
.attrib-meta { font-size: var(--fs-sm); }
.evidence, .snapshot {
  background: var(--color-bg-page, #f5f7fa);
  padding: 10px;
  border-radius: 6px;
  font-size: var(--fs-xs, 12px);
  white-space: pre-wrap;
  word-break: break-all;
}
.action-grid { display: flex; flex-wrap: wrap; gap: 8px; }
.fix-note {
  background: var(--el-color-success-light-9, #f0f9eb);
  padding: 8px 12px;
  border-radius: 6px;
  font-size: var(--fs-sm);
}
/* 质检详情抽屉 · 工作台三段式 */
.qc-hero {
  display: flex;
  align-items: stretch;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border-radius: var(--radius-md);
  background: var(--color-bg-page);
  border-left: 4px solid var(--color-text-placeholder);
  margin-bottom: var(--space-2);
  &--fail { border-left-color: var(--el-color-error, #f56c6c); }
  &--warn { border-left-color: var(--el-color-warning, #e6a23c); }
  &--pass { border-left-color: var(--el-color-success, #67c23a); }
}
.qc-hero-main { flex-shrink: 0; min-width: 96px; }
.qc-hero-verdict {
  font-size: 22px;
  font-weight: 700;
  letter-spacing: 2px;
  line-height: 1.2;
}
.qc-hero--fail .qc-hero-verdict { color: var(--el-color-error, #f56c6c); }
.qc-hero--warn .qc-hero-verdict { color: var(--el-color-warning, #e6a23c); }
.qc-hero--pass .qc-hero-verdict { color: var(--el-color-success, #67c23a); }
.qc-hero-meta {
  margin-top: 6px;
  font-size: 11px;
  color: var(--color-text-secondary);
  white-space: nowrap;
}
.qc-hero-summary {
  flex: 1;
  align-self: center;
  font-size: 13px;
  line-height: 1.65;
  color: var(--color-text-primary);
  border-left: 1px solid var(--color-border-light, #e4e7ed);
  padding-left: var(--space-3);
}
.qc-evidence {
  :deep(.el-tabs__header) { margin-bottom: 8px; }
  :deep(.el-tabs__content) { overflow: visible; }
  .qc-tab-badge { margin-left: 4px; transform: scale(0.85); }
  .qc-tab-badge-tag { margin-left: 6px; }
}
.qc-evi-empty { padding: var(--space-6) 0; text-align: center; }
.qc-problem {
  padding: var(--space-2) var(--space-3);
  margin-bottom: var(--space-2);
  background: var(--color-bg-page);
  border-radius: var(--radius-md);
}
.qc-problem-head {
  display: flex;
  align-items: center;
  flex-wrap: wrap; /* 轮次标注 (轮序+日志行号) 较长, 放不下时换行防截断 */
  gap: var(--space-2);
}
.qc-problem-reason {
  margin-top: 4px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--color-text-primary);
}
/* 吸底工具区: 案例摘要一行 (可展开只读明细), 处置操作在案例抽屉 */
.qc-actionbar {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
}
.qc-cases-digest {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  font-size: var(--fs-sm);
  cursor: pointer;
  user-select: none;
  padding: 2px 0;
}
.qc-cases-digest-main { font-weight: 600; }
.qc-cases-arrow {
  margin-left: auto;
  transition: transform 0.2s;
  &.open { transform: rotate(90deg); }
}
.qc-cases-body {
  max-height: 168px;
  overflow-y: auto;
  padding: 2px 6px 2px 2px; /* 右侧留白防「处理 ›」贴边裁切 */
}
.qc-case-row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  padding: 6px 8px;
  border-radius: 6px;
  & + & { margin-top: 4px; }
  &:hover { background: var(--color-fill-light, #f5f7fa); }
  .qc-case-input { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: var(--fs-sm); }
}
.qc-case-current { background: var(--el-color-primary-light-9, #ecf5ff); box-shadow: inset 2px 0 0 var(--el-color-primary, #409eff); }
.qc-actionbar-tools {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
  border-top: 1px solid var(--color-border-light, #e4e7ed);
  padding-top: var(--space-2);
}
.qc-tools-sep { flex: 1; }
.action-hint { font-size: var(--fs-xs, 12px); }
/* 问题标注内联卡 (证据与发现同一现场) */
.qc-inline-problem {
  margin-top: 6px;
  padding: 8px 10px;
  border-left: 3px solid var(--el-color-error, #f56c6c);
  background: var(--el-color-error-light-9, #fef0f0);
  border-radius: 6px;
}
.qc-inline-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-wrap: wrap;
}
.qc-inline-reason {
  margin-top: 4px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--color-text-primary);
}
.qc-inline-actions {
  margin-top: 6px;
  display: flex;
  gap: var(--space-2);
}

/* 问题轮现场还原 */
.scene-bubble {
  max-width: 92%;
  padding: 6px 10px;
  margin: 4px 0;
  font-size: 13px;
  line-height: 1.6;
  border-radius: 8px;
}
.scene-bubble.customer {
  align-self: flex-start;
  background: var(--color-fill, rgba(0, 0, 0, 0.05));
}
.scene-bubble.bot {
  align-self: flex-end;
  margin-left: auto;
  background: var(--color-primary-light-9, rgba(64, 158, 255, 0.1));
}
.scene-src { margin-left: 6px; }
.qc-turn-chain {
  margin-top: 6px;
  padding: 4px 8px;
  font-size: 12px;
  color: var(--color-text-secondary);
  background: var(--color-bg-page);
  border-radius: 6px;
}
.pano-round {
  display: flex;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 6px;
}
.pano-round.pano-problem {
  background: rgba(245, 108, 108, 0.08);
}
.pano-round-no {
  flex: none;
  width: 20px;
  height: 20px;
  font-size: 11px;
  line-height: 20px;
  text-align: center;
  color: var(--color-text-secondary);
  background: var(--color-fill, rgba(0, 0, 0, 0.05));
  border-radius: 50%;
}
.pano-problem .pano-round-no {
  color: #fff;
  background: var(--el-color-danger, #f56c6c);
}
.pano-customer { font-size: 13px; }
.pano-bot { margin-top: 2px; font-size: 12px; color: var(--color-text-secondary); }
/* 整改闭环抽屉优化 */
.fix-steps { margin-bottom: 16px; }
.reject-alert { margin-bottom: 12px; }
.fix-guide { margin-top: 8px; }
/* 重放验证 */
.replay-progress { margin-bottom: 10px; }
.replay-compare-head {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  padding: 4px 8px;
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-secondary);
}
.replay-row {
  display: flex;
  gap: 8px;
  padding: 6px 4px;
  border-bottom: 1px dashed var(--color-border-lighter, #ebeef5);
}
.replay-cells {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 8px;
  flex: 1;
}
.replay-cell {
  padding: 4px 8px;
  font-size: 13px;
  background: var(--color-bg-page);
  border-radius: 6px;
}
.replay-cell.replay-changed {
  background: rgba(103, 194, 58, 0.1);
}
.replay-customer { font-size: 13px; margin-bottom: 2px; }
.pano-round-no.is-problem { color: #fff; background: var(--el-color-success, #67c23a); }
.replay-summary {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 8px;
  font-size: 12px;
  color: var(--color-text-secondary);
}
</style>
