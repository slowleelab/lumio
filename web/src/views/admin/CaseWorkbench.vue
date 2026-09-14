<template>
  <div class="case-page">
    <div class="page-header">
      <h2>案例工作台 <span class="page-subtitle">问题案例处置队列 · 每案独立闭环</span></h2>
      <div class="header-actions">
        <el-tooltip placement="left" effect="light">
          <template #content>
            <div class="judge-tip">
              <b>GLM-5.3-Flash 裁判 · 批量归因</b><br />
              对全部「未归因」坏例逐条跑 LLM 裁判 (n=3 多数票),<br />
              每条约 20-40 秒后台执行, 完成后自动刷新。<br />
              采集落库后不会自动归因 —— 由你在此触发。
            </div>
          </template>
          <el-button size="small" type="primary" plain :loading="batch.running" @click="doBatchAttribution">
            {{ batch.running ? `GLM 裁判中 ${batch.done}/${batch.total}` : "GLM 裁判 · 批量归因待归因项" }}
          </el-button>
        </el-tooltip>
      </div>
    </div>

    <el-progress v-if="batch.running" :percentage="batchPct" :stroke-width="10" striped striped-flow style="margin-top: 10px">
      <template #default>
        <span class="batch-progress-text">
          GLM 裁判批量归因中 {{ batch.done }}/{{ batch.total }}
          <template v-if="batch.failed"> (失败 {{ batch.failed }})</template>
        </span>
      </template>
    </el-progress>

    <div class="filters">
      <el-select v-model="caseFilters.fix_status" placeholder="处置状态" clearable size="small" style="width: 120px" @change="reloadCases">
        <el-option v-for="(label, key) in fixStatusLabelMap" :key="key" :label="label" :value="key" />
      </el-select>
      <el-select v-model="caseFilters.root_cause_layer" placeholder="根因层" clearable size="small" style="width: 120px" @change="reloadCases">
        <el-option v-for="(label, key) in LAYER_LABELS" :key="key" :label="label" :value="key" />
      </el-select>
      <el-input
        v-model="caseFilters.keyword"
        placeholder="搜索问题句 / 会话 ID…"
        clearable
        size="small"
        style="width: 240px"
        :prefix-icon="Search"
        @keyup.enter="reloadCases"
        @clear="reloadCases"
      />
      <el-button size="small" @click="reloadCases">查询</el-button>
      <el-button v-if="caseFilters.fix_status || caseFilters.root_cause_layer || caseFilters.keyword" size="small" link @click="clearFilters">清除筛选</el-button>
      <div class="filter-spacer"></div>
      <template v-if="selected.length">
        <el-button size="small" type="success" plain @click="batchConfirm">批量确认 ({{ selected.length }})</el-button>
      </template>
    </div>

    <el-table :data="caseRows" v-loading="casesLoading" stripe size="small" style="margin-top: 12px" @selection-change="onSelection">
      <el-table-column type="selection" width="40" :selectable="(row: Badcase) => row.fix_status === 'pending' && !!row.root_cause_layer && row.root_cause_layer !== 'uncertain'" />
      <el-table-column label="问题句" min-width="220">
        <template #default="{ row }">
          <span :title="row.user_input">{{ (row.user_input || "").slice(0, 40) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="根因" width="110" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.human_confirmed_layer" size="small" type="success">{{ layerLabel(row.human_confirmed_layer) }}</el-tag>
          <el-tag v-else-if="row.root_cause_layer === 'uncertain'" size="small" type="warning">待确认根因</el-tag>
          <el-tag v-else-if="row.root_cause_layer" size="small" type="primary">{{ layerLabel(row.root_cause_layer) }}</el-tag>
          <span v-else class="muted">未归因</span>
        </template>
      </el-table-column>
      <el-table-column label="处置" width="86" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.fix_status" size="small" :type="fixStatusType(row.fix_status)">{{ fixStatusLabel(row.fix_status) }}</el-tag>
          <span v-else class="muted">-</span>
        </template>
      </el-table-column>
      <el-table-column label="信号" width="86" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.signal_source" size="small" :type="signalType(row.signal_source)">{{ signalLabel(row.signal_source) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="出现" width="56" align="center">
        <template #default="{ row }">{{ row.occurrences ?? 1 }}</template>
      </el-table-column>
      <el-table-column label="来源会话" width="150">
        <template #default="{ row }">
          <el-link type="primary" :underline="false" @click="gotoAudit(row)">{{ (row.session_id || "").slice(0, 18) }}…</el-link>
        </template>
      </el-table-column>
      <el-table-column label="会话时间" width="150">
        <template #default="{ row }">{{ fmtTime(row.session_time || row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="70" align="center">
        <template #default="{ row }">
          <el-button size="small" link type="primary" @click="openDetail(row)">详情</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      v-model:current-page="casePage"
      v-model:page-size="casePageSize"
      :total="caseTotal"
      :page-sizes="[20, 50, 100]"
      layout="total, sizes, prev, pager, next"
      style="margin-top: 14px; justify-content: flex-end"
      @current-change="loadCases"
      @size-change="reloadCases"
    />

    <el-drawer v-model="detailVisible" size="58%" destroy-on-close>
      <template #header>
        <div class="drawer-title">
          <el-tag :type="fixStatusType(detail?.fix_status || '')" effect="dark">{{ fixStatusLabel(detail?.fix_status || "") }}</el-tag>
          <el-tag size="small" :type="signalType(detail?.signal_source || '')">{{ signalLabel(detail?.signal_source || "") }}</el-tag>
          <span class="muted" style="font-size: 12px">出现 {{ detail?.occurrences ?? 1 }} 次</span>
          <el-button size="small" link :disabled="!nextIdx" @click="openNext">下一条 ›</el-button>
        </div>
      </template>
      <div v-if="detail" class="detail-body">
        <!-- 整改进度步骤条: 处置状态机一眼可见 -->
        <el-steps :active="fixStepActive" align-center size="small" finish-status="success" class="fix-steps">
          <el-step title="归因" :description="detail.root_cause_layer ? layerLabel(detail.root_cause_layer) : '待裁判'" />
          <el-step title="人工确认" :description="detail.needs_human_review ? '待复核' : detail.root_cause_layer ? '已确认' : '-'" />
          <el-step title="修复" :description="{ fixing: '修复中', canary: '已灰度', deployed: '已上线', reopened: '复检未过', rejected: '已驳回' }[detail.fix_status] || '-'" />
          <el-step title="验证" :description="{ verified: '复检通过 · 已销项', deployed: '可复检', canary: '灰度可复检' }[detail.fix_status] || '待上线'" />
        </el-steps>
        <el-alert v-if="detail.fix_status === 'rejected'" type="info" :closable="false" class="reject-alert" :title="`已驳回 — ${detail.fix_note || ''}`" />
        <el-descriptions :column="3" border size="small">
          <el-descriptions-item label="信号源">{{ signalLabel(detail.signal_source) }}</el-descriptions-item>
          <el-descriptions-item label="出现次数">{{ detail.occurrences ?? 1 }} 次</el-descriptions-item>
          <el-descriptions-item label="采集时间">{{ fmtTime(detail.created_at) }}</el-descriptions-item>
          <el-descriptions-item label="会话时间">
            <span :title="`会话最后一轮对话时间; 采集时间是信号落库/巡检时刻`">{{ fmtTime(detail.session_time) }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="会话">
            <el-link type="primary" :underline="false" @click="gotoAudit(detail)">{{ detail.session_id?.slice(0, 24) }}…</el-link>
          </el-descriptions-item>
          <el-descriptions-item label="裁判模型">{{ detail.attribution_model || "-" }}</el-descriptions-item>
          <el-descriptions-item label="修复状态">
            <el-tag size="small" :type="fixStatusType(detail.fix_status)">{{ fixStatusLabel(detail.fix_status) }}</el-tag>
          </el-descriptions-item>
        </el-descriptions>

        <div class="section-title">对话现场</div>
        <div v-if="contextLoading" class="muted context-loading">加载会话上下文…</div>
        <template v-else>
          <div v-for="(m, i) in contextMessages" :key="i" class="ctx-row" :class="m.speaker === 'customer' ? 'ctx-user' : 'ctx-bot'">
            <span class="ctx-speaker">{{ m.speaker === "customer" ? "客户" : "Bot" }}</span>
            <span class="ctx-content">{{ m.content }}</span>
          </div>
          <div v-if="!contextMessages.length" class="muted">会话历史已过期 (仅存现场轮)</div>
        </template>
        <div class="ctx-row ctx-user highlight">
          <span class="ctx-speaker">客户</span>
          <span class="ctx-content">{{ detail.user_input }}</span>
        </div>
        <div class="ctx-row ctx-bot highlight">
          <span class="ctx-speaker">Bot</span>
          <span class="ctx-content">{{ detail.bot_output || "-" }}</span>
        </div>

        <template v-if="detail.root_cause_layer">
          <div class="section-title">根因归因 <span class="muted section-hint">(裁判结论 · 可人工改判后确认)</span></div>
          <div class="attrib-row">
            <el-select v-model="judgedLayer" size="small" style="width: 150px" :placeholder="detail.root_cause_layer === 'uncertain' ? '选择根因层' : '根因 (可改判)'">
              <el-option v-for="(label, key) in LAYER_LABELS" :key="key" :label="label" :value="key" :disabled="key === 'uncertain'" />
            </el-select>
            <el-select v-model="judgedTable" size="small" style="width: 170px" placeholder="修复分流表">
              <el-option v-for="(label, key) in FIX_TABLE_LABELS" :key="key" :label="label" :value="key">
                <el-tooltip :content="FIX_TABLE_TIPS[key] || ''" placement="right" :disabled="!FIX_TABLE_TIPS[key]">
                  <span>{{ label }}</span>
                </el-tooltip>
              </el-option>
            </el-select>
            <span class="muted attrib-meta">
              {{ categoryLabel(detail.root_cause_category) }} · 置信 {{ Math.round((detail.attribution_confidence ?? 0) * 100) }}%
            </span>
          </div>
          <div class="evidence">{{ detail.attribution_evidence }}</div>
          <!-- 修复指引: 分流表 → 去哪里改什么 -->
          <el-alert v-if="fixGuide" type="success" :closable="false" class="fix-guide">
            <template #title>
              修复指引 · {{ FIX_TABLE_LABELS[judgedTable || detail.fix_table || ""] || "分流表" }}:
              {{ fixGuide.text }}
              <el-link v-if="fixGuide.to" type="primary" :underline="false" style="margin-left: 6px" @click="router.push(fixGuide.to!)">前往处理 ›</el-link>
            </template>
          </el-alert>
        </template>
        <div v-else class="section-title muted">尚未归因 — 点击下方「GLM 裁判归因」开始 (约 20-40 秒)</div>

        <!-- 质检判定 (qa_scan 采集的案例: 裁判指出的具体问题项) -->
        <template v-if="qaVerdict">
          <div class="section-title">
            质检判定
            <span class="muted section-hint">(全量质检巡检 · 与人工坐席质检同口径)</span>
          </div>
          <div class="qa-verdict">
            <el-tag :type="verdictType(qaVerdict)" size="small">{{ verdictLabel(qaVerdict) }}</el-tag>
            <span v-if="qaSummary" class="qa-summary">{{ qaSummary }}</span>
          </div>
          <div v-for="(p, i) in qaProblems" :key="i" class="qa-problem">
            <el-tag size="small" type="danger" effect="plain">{{ problemLabel(p.type) }}</el-tag>
            <span class="qa-reason"><template v-if="p.turn">第 {{ p.turn }} 轮 · </template>{{ p.reason || "-" }}</span>
          </div>
        </template>

        <div class="section-title">
          采集现场快照
          <span class="muted section-hint">(按问答链路分层展示; 原始数据可折叠查看)</span>
        </div>
        <el-descriptions v-if="snapRows.length" :column="2" border size="small">
          <el-descriptions-item v-for="r in snapRows" :key="r.label" :label="r.label">{{ r.value }}</el-descriptions-item>
        </el-descriptions>
        <div v-if="snapTurnsMeta.length" class="turns-meta">
          <div class="turns-meta-title">逐轮元数据 (对话走向: 每轮意图与回复来源)</div>
          <div v-for="(t, i) in snapTurnsMeta" :key="i" class="turn-meta-row">
            <span class="turn-idx">{{ i + 1 }}</span>
            <span class="turn-speaker" :class="{ 'is-customer': t.speaker === 'customer' }">{{ t.speaker === "customer" ? "客户" : "Bot" }}</span>
            <span class="turn-intent">意图: {{ t.intent || "-" }}</span>
            <span class="turn-src">来源: {{ t.src || "-" }}</span>
          </div>
        </div>
        <pre v-if="snapTranscript" class="snapshot transcript">{{ snapTranscript }}</pre>
        <el-collapse v-if="detail.snapshot && Object.keys(detail.snapshot).length" class="raw-snap">
          <el-collapse-item name="raw">
            <template #title><span class="muted">查看原始快照数据 (调试用)</span></template>
            <pre class="snapshot">{{ snapshotPretty }}</pre>
          </el-collapse-item>
        </el-collapse>
        <div v-if="!detail.snapshot || !Object.keys(detail.snapshot).length" class="muted">(采集时未携带快照)</div>

        <div class="section-title">处理操作 <span class="muted section-hint">(按状态机流转: 待处置 → 修复中 → 已灰度 → 已上线 → 已验证; 终态不可逆)</span></div>
        <div class="action-grid">
          <!-- 主推进: 按状态机只亮当前态的合法转移 (与后端 _FIX_TRANSITIONS 同构) -->
          <el-button v-if="!detail.root_cause_layer || detail.root_cause_layer === 'uncertain'" size="small" type="warning" :loading="acting" @click="runAttribution(detail)">GLM 裁判归因{{ detail.root_cause_layer === "uncertain" ? " (重试)" : "" }}</el-button>
          <el-button v-if="detail.fix_status === 'pending' && detail.root_cause_layer" size="small" type="success" :loading="acting" @click="confirmResolve">
            确认根因 → 修复中{{ detail.root_cause_layer === "uncertain" ? " (待选根因)" : "" }}
          </el-button>
          <el-button v-if="detail.fix_status === 'fixing'" size="small" type="warning" :loading="acting" @click="transition('canary')">修复完成 → 已灰度</el-button>
          <el-button v-if="detail.fix_status === 'canary'" size="small" type="success" :loading="acting" @click="transition('deployed')">灰度通过 → 已上线</el-button>
          <!-- 验证闭环: 重放原会话 (当前代码重新回答) → 按新判定自动流转 已验证/已重开 -->
          <el-button
            v-if="detail.fix_status === 'canary' || detail.fix_status === 'deployed'"
            size="small" type="primary" plain :loading="recheckState.running" @click="recheckFromBadcase"
          >{{ recheckState.running ? `复检重放中 ${recheckState.done}/${recheckState.total || "…"}` : `复检原会话 (重放${detail.fix_status === "deployed" ? "" : "·灰度"})` }}</el-button>
          <el-button v-if="detail.fix_status === 'reopened'" size="small" type="warning" :loading="acting" @click="transition('fixing')">重新修复 → 修复中</el-button>
          <!-- 次要操作 -->
          <el-button size="small" @click="addToGolden(detail)">扩充金标集</el-button>
          <el-button size="small" @click="gotoAudit(detail)">会话审计</el-button>
          <el-button v-if="detail.fix_status !== 'verified' && detail.fix_status !== 'rejected'" size="small" type="danger" plain :loading="acting" @click="rejectCase">驳回 (误采集)</el-button>
          <span v-else class="muted action-hint">终态 · 不可流转</span>
        </div>

        <template v-if="detail.fix_note">
          <div class="section-title">最近处理记录</div>
          <div class="fix-note">{{ detail.fix_note }} <span class="muted" v-if="detail.resolved_at">· {{ fmtTime(detail.resolved_at) }}</span></div>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue"
import { useRoute, useRouter } from "vue-router"
import { ElMessage, ElMessageBox } from "element-plus"
import { Search } from "@element-plus/icons-vue"
import {
  attributeBadcase,
  resolveBadcase,
  startBatchAttribution,
  getBatchAttributionStatus,
  expandGoldenSet,
  getBadcase,
  listBadcases,
  replayQualitySession,
  getReplayStatus,
  type Badcase,
  type QualityProblem,
} from "@/api/closedLoop"
import { getConversationReplay, type ReplayResponse } from "@/api/console"

const router = useRouter()
const route = useRoute()

// ── 案例队列 (整改负责人的工作列表; 案例=处置工单, 会话核查在智能质检页) ──
const caseRows = ref<Badcase[]>([])
const caseTotal = ref(0)
const casePage = ref(1)
const casePageSize = ref(50)
const casesLoading = ref(false)
const caseFilters = ref<{ fix_status: string; root_cause_layer: string; keyword: string }>({ fix_status: "", root_cause_layer: "", keyword: "" })
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
  agent_revoke: "人工撤回",
  behavior_anomaly: "行为异常",
  compliance_alert: "合规告警",
  qa_scan: "质检巡检",
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


const FIX_TABLE_LABELS: Record<string, string> = {
  A_knowledge: "A · 知识库",
  B_intent: "B · 意图库",
  C_rule: "C · 规则",
  D_model: "D · 模型",
  none: "无需修复",
}

// ── 列表 ──

const selected = ref<Badcase[]>([])

function onSelection(rows: Badcase[]) {
  selected.value = rows
}

function fixTableFor(_row: Badcase): string | undefined {
  return undefined
}

const fixStatusLabelMap: Record<string, string> = {
  pending: "待处置",
  fixing: "修复中",
  canary: "已灰度",
  deployed: "已上线",
  verified: "已验证",
  reopened: "已重开",
  rejected: "已驳回",
}

async function loadCases() {
  casesLoading.value = true
  try {
    const res = await listBadcases({
      fix_status: caseFilters.value.fix_status || undefined,
      root_cause_layer: caseFilters.value.root_cause_layer || undefined,
      keyword: caseFilters.value.keyword || undefined,
      limit: casePageSize.value,
      offset: (casePage.value - 1) * casePageSize.value,
    })
    caseRows.value = res.badcases ?? []
    caseTotal.value = res.total
  } catch {
    /* handled */
  } finally {
    casesLoading.value = false
  }
}

function reloadCases() {
  casePage.value = 1
  loadCases()
}

function clearFilters() {
  caseFilters.value = { fix_status: "", root_cause_layer: "", keyword: "" }
  reloadCases()
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

function gotoAuditSession(sessionId: string) {
  router.push({ path: "/admin/audit", query: { session_id: sessionId } })
}



const batch = ref({
  running: false,
  total: 0,
  done: 0,
  failed: 0,
})
let batchTimer: ReturnType<typeof setInterval> | null = null

const batchPct = computed(() => (batch.value.total > 0 ? Math.round((batch.value.done / batch.value.total) * 100) : 0))

async function pollBatch() {
  try {
    const st = await getBatchAttributionStatus()
    batch.value = { running: st.running, total: st.total, done: st.done, failed: st.failed }
    if (!st.running) {
      if (batchTimer) {
        clearInterval(batchTimer)
        batchTimer = null
      }
      if (st.total > 0) {
        ElMessage.success(`批量归因完成: 成功 ${st.done} / 失败 ${st.failed} / 共 ${st.total}`)
        await loadCases()
      }
    }
  } catch {
    /* handled */
  }
}

async function doBatchAttribution() {
  const scope: { keyword?: string } = {}
  if (qcFilters.value.keyword) scope.keyword = qcFilters.value.keyword
  const scopeText = Object.keys(scope).length ? " (按当前搜索范围)" : ""
  try {
    await startBatchAttribution(200, scope)
    ElMessage.success(`GLM 裁判批量归因已启动${scopeText}, 每条约 20-40 秒`)
    if (!batchTimer) batchTimer = setInterval(pollBatch, 4000)
  } catch {
    /* handled */
  }
}


function openNext() {
  const row = caseRows.value[nextIdx.value!]
  if (row?.badcase_id) openBadcaseById(row.badcase_id)
}

function openDetail(row: Badcase) {
  detail.value = row
  judgedLayer.value = row.human_confirmed_layer || (row.root_cause_layer !== "uncertain" ? row.root_cause_layer : "") || ""
  judgedTable.value = row.fix_table || ""
  detailVisible.value = true
  loadContext(row)
}

async function loadContext(row: Badcase) {
  contextMessages.value = []
  contextLoading.value = true
  try {
    // 管理端会话回放接口 (此前误调客户侧 /sessions/*: admin token + 已归档会话下 404)
    const { getConversationReplay } = await import("@/api/console")
    const r = await getConversationReplay(row.session_id)
    const all: { speaker: string; content: string }[] = (r.turns ?? []).map((t) => ({
      speaker: t.speaker,
      content: t.content,
    }))
    // 只显示现场轮之前的上文 (最后两条是本坏例现场, 模板里高亮单独渲染)
    contextMessages.value = all.slice(0, -2).slice(-4)
  } catch {
    contextMessages.value = [] // 会话已归档/过期时静默降级, 仅显示现场轮
  } finally {
    contextLoading.value = false
  }
}

const snapshotPretty = computed(() => {
  const snap = detail.value?.snapshot
  return snap && Object.keys(snap).length ? JSON.stringify(snap, null, 2) : "(采集时未携带快照)"
})

// ── 快照中文分层对照: 采集时的链路状态字段 → 人话 ──
const SNAP_FIELD_LABELS: Record<string, string> = {
  intent: "命中意图",
  confidence: "意图置信度",
  traffic_class: "流量分类",
  response_source: "回复来源",
  rag_hit: "RAG 检索",
  context_len: "上下文轮数",
  guard_reason: "护栏动作",
  stage_detail: "阶段明细",
}
function snapFieldValue(key: string, v: unknown): string {
  if (key === "confidence") return typeof v === "number" ? `${Math.round(v * 100)}%` : String(v ?? "-")
  if (key === "rag_hit") return v ? "已命中知识库/工具" : "未命中"
  if (v == null || v === "") return "-"
  return typeof v === "object" ? JSON.stringify(v) : String(v)
}
const snapRows = computed(() => {
  const snap = detail.value?.snapshot
  if (!snap) return []
  const rows: { label: string; value: string }[] = []
  for (const [key, label] of Object.entries(SNAP_FIELD_LABELS)) {
    if (key in snap) rows.push({ label, value: snapFieldValue(key, snap[key]) })
  }
  // 未收录的标量字段也照常显示 (兜底, 防止新增字段被吞)
  for (const [key, v] of Object.entries(snap)) {
    if (key in SNAP_FIELD_LABELS || key === "transcript" || key === "turns_meta") continue
    if (typeof v !== "object") rows.push({ label: key, value: snapFieldValue(key, v) })
  }
  return rows
})
const snapTurnsMeta = computed(() => {
  const meta = detail.value?.snapshot?.turns_meta
  return Array.isArray(meta) ? (meta as { speaker: string; intent?: string; src?: string }[]) : []
})
const snapTranscript = computed(() => {
  const t = detail.value?.snapshot?.transcript
  return typeof t === "string" && t.trim() ? t : ""
})

// ── 质检判定 (qa_scan 采集的案例: signal_detail 里裁判结论) ──
const qaDetail = computed(() => detail.value?.signal_detail as { verdict?: string; summary?: string; problems?: QualityProblem[] } | null)
const qaVerdict = computed(() => qaDetail.value?.verdict ?? "")
const qaSummary = computed(() => qaDetail.value?.summary ?? "")
const qaProblems = computed<QualityProblem[]>(() => qaDetail.value?.problems ?? [])

async function refreshAfterAction(msg: string) {
  ElMessage.success(msg)
  detailVisible.value = false
  await loadCases()
}

async function runAttribution(row: Badcase) {
  acting.value = true
  try {
    const r = (await attributeBadcase(row.id)) as { root_cause_layer?: string; needs_human_review?: boolean }
    if (r.root_cause_layer === "uncertain") {
      ElMessage.warning("归因完成: 裁判证据不足 — 常见于问题轮无决策链中间产物, 请人工定根因", { duration: 7000 })
    } else {
      ElMessage.success(`归因完成: ${layerLabel(r.root_cause_layer) || "-"}`)
    }
    await loadCases()
    if (detailVisible.value && detail.value?.id === row.id) {
      const fresh = await getBadcase(row.id)
      if (fresh) openDetail(fresh)
    }
  } catch {
    /* handled */
  } finally {
    acting.value = false
  }
}

const fixStepActive = computed(() => {
  const d = detail.value
  if (!d) return 0
  if (d.fix_status === "verified") return 4
  if (d.fix_status === "deployed" || d.fix_status === "canary") return 3
  if (d.fix_status === "fixing") return 2
  if (d.root_cause_layer && !d.needs_human_review) return 2
  if (d.root_cause_layer) return 1
  return 0
})

// 分流表说明 (下拉 tooltip) 与修复指引 (去哪里改什么)
const FIX_TABLE_TIPS: Record<string, string> = {
  A_knowledge: "知识缺口/检索不命中 → 补知识内容",
  B_intent: "意图/语义误判 → 意图库与种子语料",
  C_rule: "规则/路由/会话配置 → 规则与映射表",
  D_model: "生成幻觉/Prompt 失效 → 提示词与模型",
  none: "无需改表 (流程/工程问题)",
}
const FIX_TABLE_GUIDES: Record<string, { text: string; to?: string }> = {
  A_knowledge: { text: "到 FAQ 管理补标准问答对, 或在文档管理补充知识文档并重新摄入", to: "/admin/faq" },
  B_intent: { text: "到意图库管理页维护规则词/种子语料, 重跑评测闸门后激活", to: "/admin/intent-library" },
  C_rule: { text: "核对意图注册表与流量分类映射 (归并表), 走代码评审", to: "/admin/intent-library" },
  D_model: { text: "调整回复生成系统提示词与出站闸门话术, 走代码评审", to: "" },
  none: { text: "流程或工程问题, 在代码/配置侧定位处理", to: "" },
}
const fixGuide = computed(() => {
  const key = judgedTable.value || detail.value?.fix_table || ""
  return FIX_TABLE_GUIDES[key] ?? null
})


async function transition(status: string) {
  if (!detail.value) return
  let note = ""
  try {
    const r = await ElMessageBox.prompt("流转备注 (可选, 默认状态名):", `流转 → ${fixStatusLabel(status)}`, {
      inputPlaceholder: "如: 已补 FAQ 词条 / 种子语料已重训",
      inputValue: "",
    })
    note = (r.value || "").trim()
  } catch {
    return /* 取消 */
  }
  acting.value = true
  try {
    await resolveBadcase(detail.value.id, {
      fix_status: status,
      note: note ? `${fixStatusLabel(status)} · ${note}` : `状态流转 → ${fixStatusLabel(status)}`,
    })
    await refreshAfterAction(`已${fixStatusLabel(status)}`)
  } catch {
    /* handled */
  } finally {
    acting.value = false
  }
}

// 复检 (从整改闭环侧验证修复效果): 重放 = 当前代码重新回答原会话全部问题。
// rescan 只是对原对话历史重新打分 — bot 回答是历史固定的, 验证不了修复本身。
// 重放完成 (status=done 时自动质检已落库) → 按新会话判定自动流转:

const recheckState = ref({ running: false, done: 0, total: 0 })
let recheckTimer: ReturnType<typeof setInterval> | null = null

async function recheckFromBadcase() {
  if (!detail.value?.session_id) return
  recheckState.value = { running: true, done: 0, total: 0 }
  try {
    const r = await replayQualitySession(detail.value.session_id)
    recheckState.value.total = r.total_rounds
    if (!recheckTimer) recheckTimer = setInterval(() => void pollRecheck(r.new_session_id), 2000)
  } catch {
    ElMessage.error("复检 (重放) 启动失败")
    recheckState.value.running = false
  }
}

async function pollRecheck(newSid: string) {
  try {
    const st = await getReplayStatus(newSid)
    recheckState.value.done = st.done
    if (st.status === "running") return
    if (recheckTimer) {
      clearInterval(recheckTimer)
      recheckTimer = null
    }
    recheckState.value.running = false
    if (st.status !== "done" || st.error) {
      ElMessage.warning(`复检未完成: ${st.error || "重放中断"} — 案例状态未变, 可稍后重试`)
      return
    }
    const res = await listQcSessions({ keyword: newSid, limit: 1 })
    const verdict = res.sessions?.[0]?.verdict || ""
    if (!verdict) {
      ElMessage.warning("复检重放完成但新会话尚未出质检判定, 稍后可查看新会话手动流转")
      return
    }
    const ok = verdict === "pass" || verdict === "warn"
    await resolveBadcase(detail.value!.id, {
      fix_status: ok ? "verified" : "reopened",
      note: `复检重放 ${newSid}: ${verdictLabel(verdict)} — ${ok ? "验证通过, 销项" : "未通过, 打回重修"}`,
    })
    ElMessage[ok ? "success" : "warning"](`复检 ${verdictLabel(verdict)} — 已自动${ok ? "销项 (已验证)" : "打回 (已重开)"}`)
    await refreshAfterAction(ok ? "复检通过, 案例已验证" : "复检未通过, 案例已重开")
  } catch {
    /* handled */
  }
}

async function confirmResolve() {
  if (!detail.value) return
  // uncertain 不可被确认为根因 — 人工确认的意义就是给出确定层
  if (!judgedLayer.value || judgedLayer.value === "uncertain") {
    ElMessage.warning("请先在上方「根因归因」区选择根因层 (uncertain 不能作为确认值)")
    return
  }
  acting.value = true
  try {
    await resolveBadcase(detail.value.id, {
      fix_status: "fixing",
      fix_table: judgedTable.value || detail.value.fix_table || undefined,
      human_confirmed_layer: judgedLayer.value,
      note: `人工确认${judgedLayer.value !== detail.value.root_cause_layer ? " (改判)" : ""}`,
    })
    await refreshAfterAction("归因已确认，进入修复跟踪")
  } catch {
    /* handled */
  } finally {
    acting.value = false
  }
}

async function rejectCase() {
  if (!detail.value) return
  try {
    const { value } = await ElMessageBox.prompt("驳回原因 (必填):", "驳回坏例", { inputPlaceholder: "如: 误采集 / 重复提交" })
    if (!value.trim()) {
      ElMessage.warning("驳回需填原因")
      return
    }
    acting.value = true
    await resolveBadcase(detail.value.id, { fix_status: "rejected", note: `驳回: ${value}` })
    await refreshAfterAction("已驳回")
  } catch {
    return
  } finally {
    acting.value = false
  }
}


async function batchConfirm() {
  // 状态机口径: 待处置 + GLM 已给出明确根因 → 可批量确认进修复;
  // uncertain 是"等人定根因"——必须单笔进详情人工选层, 批量会把"不确定"确认为根因
  const rows = selected.value.filter(
    (r) => r.fix_status === "pending" && r.root_cause_layer && r.root_cause_layer !== "uncertain",
  )
  const skipped = selected.value.length - rows.length
  if (!rows.length) {
    ElMessage.warning(
      skipped
        ? `选中 ${skipped} 条为待归因/待确认根因/非待处置 — uncertain 案例需单笔进详情选根因`
        : "选中项中没有可确认的 (需待处置且已有明确根因)",
    )
    return
  }
  try {
    await ElMessageBox.confirm(`将 ${rows.length} 条按机器归因结果批量确认进入修复？`, "批量确认", { type: "warning" })
  } catch {
    return
  }
  let ok = 0
  for (const r of rows) {
    try {
      await resolveBadcase(r.id!, { fix_status: "fixing", fix_table: fixTableFor(r), human_confirmed_layer: r.root_cause_layer!, note: "批量确认" })
      ok++
    } catch {
      /* skip */
    }
  }
  ElMessage.success(`批量确认完成: ${ok}/${rows.length}`)
  await loadCases()
}

async function batchTransition(status: string) {
  const rows = selected.value
  if (!rows.length) return
  let ok = 0
  for (const r of rows) {
    try {
      await resolveBadcase(r.id!, { fix_status: status, note: "批量流转" })
      ok++
    } catch {
      /* skip */
    }
  }
  ElMessage.success(`批量流转完成: ${ok}/${rows.length}`)
  await loadCases()
}

async function addToGolden(row: Badcase) {
  try {
    const r = await expandGoldenSet([row.user_input])
    ElMessage.success(`金标扩充完成: 生成 ${r.variants.length} 条变体`)
  } catch {
    /* handled */
  }
}

function gotoAudit(row: Badcase) {
  gotoAuditSession(row.session_id)
}

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
  // 质检页/报表跳转带入筛选: fix_status (处置) / root_cause_layer (根因) / keyword (问题句或会话)
  const fs = route.query.fix_status as string | undefined
  if (fs && fixStatusLabelMap[fs]) caseFilters.value.fix_status = fs
  const rl = route.query.root_cause_layer as string | undefined
  if (rl) caseFilters.value.root_cause_layer = rl
  const kw = (route.query.keyword || route.query.session_id) as string | undefined
  if (kw) caseFilters.value.keyword = kw
  loadCases()
  pollBatch() // 恢复可能进行中的批量归因进度
})
onUnmounted(() => {
  if (recheckTimer) clearInterval(recheckTimer)
  if (batchTimer) clearInterval(batchTimer)
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

