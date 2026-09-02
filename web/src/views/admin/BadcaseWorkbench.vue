<template>
  <div class="badcase-page">
    <div class="page-header">
      <h2>智能质检 <span class="page-subtitle">问题案例归因与整改闭环</span></h2>
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

    <!-- 统计卡 (可点击联动筛选) -->
    <!-- 批量归因进度条 -->
    <el-progress
      v-if="batch.running"
      :percentage="batchPct"
      :stroke-width="10"
      striped
      striped-flow
      style="margin-top: 10px"
    >
      <template #default>
        <span class="batch-progress-text">
          GLM 裁判批量归因中 {{ batch.done }}/{{ batch.total }}
          <template v-if="batch.failed"> (失败 {{ batch.failed }})</template>
          <template v-if="batch.scope?.signal_source || batch.scope?.keyword"> · 范围: {{ batchScopeText }}</template>
        </span>
      </template>
    </el-progress>

    <div class="stat-cards">
      <div class="stat-card" :class="{ active: filters.needs_review === true }" @click="toggleStatFilter('needs_review', true)">
        <span class="label">待复核</span>
        <span class="num warn">{{ stats?.pending_review ?? "-" }}</span>
        <span class="hint">点击查看待复核队列</span>
      </div>
      <div class="stat-card" :class="{ active: filters.needs_review === false }" @click="toggleStatFilter('needs_review', false)">
        <span class="label">已确认</span>
        <span class="num">{{ stats?.confirmed ?? "-" }}</span>
        <span class="hint">点击查看已归因确认</span>
      </div>
      <div class="stat-card">
        <span class="label">今日新增</span>
        <span class="num">{{ stats?.today_new ?? "-" }}</span>
        <span class="hint">近 24 小时采集</span>
      </div>
      <div class="stat-card">
        <span class="label">已全量</span>
        <span class="num">{{ stats?.deployed ?? "-" }}</span>
        <span class="hint">修复完成上线</span>
      </div>
      <div class="stat-card">
        <span class="label">LLM 直通率</span>
        <span class="num">{{ fmtPct(stats?.llm_pass_rate ?? null) }}</span>
        <span class="hint">免人工确认占比</span>
      </div>
      <!-- 根因分布条 -->
      <div class="dist-card">
        <span class="label">根因层分布</span>
        <div class="dist-bars">
          <div v-for="d in layerDist" :key="d.key" class="dist-row" :title="`${d.label}: ${d.count}`">
            <span class="dist-label">{{ d.label }}</span>
            <div class="dist-track"><div class="dist-fill" :style="{ width: distWidth(d.count) }"></div></div>
            <span class="dist-count">{{ d.count }}</span>
          </div>
          <div v-if="!layerDist.length" class="muted dist-empty">暂无归因数据 — 先跑批量归因</div>
        </div>
      </div>
    </div>

    <div class="filters">
      <el-select v-model="filters.signal_source" placeholder="信号源" clearable size="small" style="width: 140px" @change="reload">
        <el-option v-for="(label, key) in SIGNAL_LABELS" :key="key" :label="label" :value="key" />
      </el-select>
      <el-select v-model="filters.root_cause_layer" placeholder="根因层" clearable size="small" style="width: 140px" @change="reload">
        <el-option v-for="(label, key) in LAYER_LABELS" :key="key" :label="label" :value="key" />
      </el-select>
      <el-select v-model="filters.fix_status" placeholder="修复状态" clearable size="small" style="width: 120px" @change="reload">
        <el-option label="待修" value="pending" />
        <el-option label="修复中" value="fixing" />
        <el-option label="已灰度" value="canary" />
        <el-option label="已全量" value="deployed" />
        <el-option label="已驳回" value="rejected" />
      </el-select>
      <el-input
        v-model="filters.keyword"
        placeholder="搜索用户输入…"
        clearable
        size="small"
        style="width: 200px"
        :prefix-icon="Search"
        @keyup.enter="reload"
        @clear="reload"
      />
      <el-button size="small" @click="reload">查询</el-button>
      <el-button v-if="hasActiveFilter" size="small" link @click="clearFilters">清除筛选</el-button>
      <div class="filter-spacer"></div>
      <template v-if="selected.length">
        <el-button size="small" type="success" plain @click="batchConfirm">批量确认 ({{ selected.length }})</el-button>
        <el-button size="small" type="warning" plain @click="batchTransition('fixing')">批量转修复中</el-button>
      </template>
    </div>

    <el-table
      :data="badcases"
      v-loading="loading"
      stripe
      size="small"
      style="margin-top: 12px"
      @selection-change="onSelection"
      @row-click="openDetail"
    >
      <el-table-column type="selection" width="38" />
      <el-table-column label="用户输入" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="input-text">{{ row.user_input }}</span>
          <el-tag v-if="(row.occurrences ?? 1) > 1" size="small" type="warning" style="margin-left: 4px">×{{ row.occurrences }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="信号" width="92">
        <template #default="{ row }">
          <el-tag size="small" :type="signalType(row.signal_source)">{{ signalLabel(row.signal_source) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="根因层" width="112">
        <template #default="{ row }">
          <el-tag v-if="row.human_confirmed_layer" size="small" type="success">{{ layerLabel(row.human_confirmed_layer) }}</el-tag>
          <el-tag v-else-if="row.root_cause_layer" size="small" :type="row.root_cause_layer === 'uncertain' ? 'warning' : 'primary'">
            {{ layerLabel(row.root_cause_layer) }}
          </el-tag>
          <span v-else class="muted">未归因</span>
        </template>
      </el-table-column>
      <el-table-column label="置信" width="62" align="center">
        <template #default="{ row }">
          <span v-if="row.attribution_confidence != null">{{ Math.round(row.attribution_confidence * 100) }}%</span>
          <span v-else class="muted">-</span>
        </template>
      </el-table-column>
      <el-table-column label="裁判" width="96" show-overflow-tooltip>
        <template #default="{ row }">
          <span v-if="row.attribution_model" class="model-tag">{{ shortModel(row.attribution_model) }}</span>
          <span v-else class="muted">-</span>
        </template>
      </el-table-column>
      <el-table-column label="复核" width="74" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.needs_human_review" size="small" type="warning">待人工</el-tag>
          <el-tag v-else size="small" type="success">已确认</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="分流表" width="104">
        <template #default="{ row }">{{ fixTableLabel(row.fix_table) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="82">
        <template #default="{ row }">
          <el-tag size="small" :type="fixStatusType(row.fix_status)">{{ fixStatusLabel(row.fix_status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="时间" width="92">
        <template #default="{ row }">
          <span :title="row.created_at">{{ relTime(row.created_at) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click.stop="openDetail(row)">详情</el-button>
          <el-button v-if="!row.root_cause_layer" link type="warning" size="small" @click.stop="runAttribution(row)">GLM 裁判</el-button>
          <el-button
            v-else-if="row.needs_human_review"
            link type="success" size="small"
            @click.stop="openDetail(row)"
          >复核</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="暂无坏例 — 跑一轮对话模拟喂入信号，或清除筛选条件" :image-size="64" />
      </template>
    </el-table>

    <el-pagination
      v-model:current-page="page"
      v-model:page-size="pageSize"
      :total="total"
      :page-sizes="[20, 50, 100]"
      layout="total, sizes, prev, pager, next"
      style="margin-top: 14px; justify-content: flex-end"
      @current-change="load"
      @size-change="onSizeChange"
    />

    <!-- 详情抽屉 -->
    <el-drawer v-model="detailVisible" size="58%" destroy-on-close>
      <template #header>
        <div class="drawer-title">
          <span>{{ detail?.user_input?.slice(0, 28) }}</span>
          <el-button size="small" link :disabled="!nextIdx" @click="openNext">下一条 ›</el-button>
        </div>
      </template>
      <div v-if="detail" class="detail-body">
        <el-descriptions :column="3" border size="small">
          <el-descriptions-item label="信号源">{{ signalLabel(detail.signal_source) }}</el-descriptions-item>
          <el-descriptions-item label="出现次数">{{ detail.occurrences ?? 1 }} 次</el-descriptions-item>
          <el-descriptions-item label="采集时间">{{ fmtTime(detail.created_at) }}</el-descriptions-item>
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
          <div class="section-title">LLM 归因 <span class="muted section-hint">(可人工改判后确认)</span></div>
          <div class="attrib-row">
            <el-select v-model="judgedLayer" size="small" style="width: 150px">
              <el-option v-for="(label, key) in LAYER_LABELS" :key="key" :label="label" :value="key" />
            </el-select>
            <el-select v-model="judgedTable" size="small" style="width: 140px" placeholder="修复分流表">
              <el-option v-for="(label, key) in FIX_TABLE_LABELS" :key="key" :label="label" :value="key" />
            </el-select>
            <span class="muted attrib-meta">
              {{ categoryLabel(detail.root_cause_category) }} · 置信 {{ Math.round((detail.attribution_confidence ?? 0) * 100) }}%
            </span>
          </div>
          <div class="evidence">{{ detail.attribution_evidence }}</div>
        </template>
        <div v-else class="section-title muted">尚未归因 — 点击下方「GLM 裁判归因」开始 (约 20-40 秒)</div>

        <div class="section-title">八层现场快照</div>
        <pre class="snapshot">{{ snapshotPretty }}</pre>

        <div class="section-title">处理操作</div>
        <div class="action-grid">
          <el-button v-if="!detail.root_cause_layer" size="small" type="warning" :loading="acting" @click="runAttribution(detail)">GLM 裁判归因</el-button>
          <el-button v-if="detail.needs_human_review" size="small" type="success" :loading="acting" @click="confirmResolve">
            确认归因并进入修复
          </el-button>
          <el-button v-if="detail.fix_status === 'fixing'" size="small" type="warning" :loading="acting" @click="transition('canary')">转灰度</el-button>
          <el-button v-if="detail.fix_status === 'canary'" size="small" type="success" :loading="acting" @click="transition('deployed')">全量上线</el-button>
          <el-button size="small" @click="addToGolden(detail)">以该输入扩充金标集</el-button>
          <el-button size="small" @click="gotoAudit(detail)">会话审计</el-button>
          <el-button size="small" type="danger" plain :loading="acting" @click="rejectCase">驳回此例</el-button>
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
import { useRouter } from "vue-router"
import { ElMessage, ElMessageBox } from "element-plus"
import { Search } from "@element-plus/icons-vue"
import {
  listBadcases,
  attributeBadcase,
  resolveBadcase,
  startBatchAttribution,
  getBatchAttributionStatus,
  expandGoldenSet,
  getBadcaseStats,
  type Badcase,
} from "@/api/closedLoop"

const router = useRouter()

const badcases = ref<Badcase[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(50)
const loading = ref(false)
const selected = ref<Badcase[]>([])
const filters = ref<{ signal_source: string; root_cause_layer: string; fix_status: string; keyword: string; needs_review: boolean | null }>({
  signal_source: "",
  root_cause_layer: "",
  fix_status: "",
  keyword: "",
  needs_review: null,
})

const hasActiveFilter = computed(() =>
  Boolean(filters.value.signal_source || filters.value.root_cause_layer || filters.value.fix_status || filters.value.keyword || filters.value.needs_review !== null),
)

// ── 统计与分布 ──
const stats = ref<(Awaited<ReturnType<typeof getBadcaseStats>>) | null>(null)

const LAYER_LABELS: Record<string, string> = {
  layer_1: "① 预处理",
  layer_2: "② 会话管理",
  layer_3: "③ 意图识别",
  layer_4: "④ 路由决策",
  layer_5: "⑤ RAG 检索",
  layer_6: "⑥ 回复生成",
  layer_7: "⑦ 风控合规",
  uncertain: "待人工判定",
}
const SIGNAL_LABELS: Record<string, string> = {
  negative_feedback: "负面反馈",
  transfer: "转人工",
  agent_revoke: "人工撤回",
  behavior_anomaly: "行为异常",
  compliance_alert: "合规告警",
}
const CATEGORY_LABELS: Record<string, string> = {
  semantic: "语义误判",
  knowledge: "知识缺口",
  process: "流程缺陷",
  coverage: "覆盖不足",
  uncertain: "待定",
}
const FIX_TABLE_LABELS: Record<string, string> = {
  A_knowledge: "A · 知识库",
  B_intent: "B · 意图库",
  C_rule: "C · 规则",
  D_model: "D · 模型",
  none: "无需修复",
}

const layerDist = computed(() => {
  const dist = stats.value?.layer_dist ?? {}
  return Object.entries(dist)
    .map(([key, count]) => ({ key, count, label: LAYER_LABELS[key] ?? key }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 5)
})
const distMax = computed(() => Math.max(1, ...layerDist.value.map((d) => d.count)))
function distWidth(count: number) {
  return `${Math.max(4, Math.round((count / distMax.value) * 100))}%`
}

// ── 列表 ──
async function load() {
  loading.value = true
  try {
    const res = await listBadcases({
      signal_source: filters.value.signal_source || undefined,
      root_cause_layer: filters.value.root_cause_layer || undefined,
      fix_status: filters.value.fix_status || undefined,
      needs_review: filters.value.needs_review ?? undefined,
      keyword: filters.value.keyword || undefined,
      limit: pageSize.value,
      offset: (page.value - 1) * pageSize.value,
    })
    badcases.value = res.badcases
    total.value = res.total
  } catch {
    /* handled */
  } finally {
    loading.value = false
  }
}

function reload() {
  page.value = 1
  load()
}

function clearFilters() {
  filters.value = { signal_source: "", root_cause_layer: "", fix_status: "", keyword: "", needs_review: null }
  reload()
}

function toggleStatFilter(key: "needs_review", value: boolean) {
  filters.value[key] = filters.value[key] === value ? null : value
  reload()
}

async function loadStats() {
  try {
    stats.value = await getBadcaseStats()
  } catch {
    /* handled */
  }
}

function onSizeChange() {
  page.value = 1
  load()
}

function onSelection(rows: Badcase[]) {
  selected.value = rows
}

// ── 详情抽屉 ──
const detailVisible = ref(false)
const detail = ref<Badcase | null>(null)
const judgedLayer = ref("")
const judgedTable = ref("")
const acting = ref(false)
const contextMessages = ref<{ speaker: string; content: string }[]>([])
const contextLoading = ref(false)

const nextIdx = computed(() => {
  if (!detail.value) return null
  const i = badcases.value.findIndex((b) => b.id === detail.value!.id)
  return i >= 0 && i < badcases.value.length - 1 ? i + 1 : null
})

function openDetail(row: Badcase) {
  detail.value = row
  judgedLayer.value = row.human_confirmed_layer || row.root_cause_layer || "uncertain"
  judgedTable.value = row.fix_table || ""
  detailVisible.value = true
  loadContext(row)
}

function openNext() {
  if (nextIdx.value != null) openDetail(badcases.value[nextIdx.value])
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

async function refreshAfterAction(msg: string) {
  ElMessage.success(msg)
  detailVisible.value = false
  await Promise.all([load(), loadStats()])
}

async function runAttribution(row: Badcase) {
  acting.value = true
  try {
    const r = (await attributeBadcase(row.id)) as { root_cause_layer?: string; needs_human_review?: boolean }
    ElMessage.success(`归因完成: ${layerLabel(r.root_cause_layer) || "-"}`)
    await Promise.all([load(), loadStats()])
    if (detailVisible.value && detail.value?.id === row.id) {
      const fresh = badcases.value.find((b) => b.id === row.id)
      if (fresh) openDetail(fresh)
    }
  } catch {
    /* handled */
  } finally {
    acting.value = false
  }
}

async function confirmResolve() {
  if (!detail.value) return
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

async function transition(status: string) {
  if (!detail.value) return
  acting.value = true
  try {
    await resolveBadcase(detail.value.id, { fix_status: status, note: `状态流转 → ${fixStatusLabel(status)}` })
    await refreshAfterAction(`已${fixStatusLabel(status)}`)
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

// ── 批量操作 ──
async function batchConfirm() {
  const rows = selected.value.filter((r) => r.root_cause_layer && r.needs_human_review)
  if (!rows.length) {
    ElMessage.warning("选中项中没有可确认的 (需已归因且待复核)")
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
      await resolveBadcase(r.id, { fix_status: "fixing", fix_table: r.fix_table || undefined, human_confirmed_layer: r.root_cause_layer!, note: "批量确认" })
      ok++
    } catch {
      /* skip */
    }
  }
  ElMessage.success(`批量确认完成: ${ok}/${rows.length}`)
  await Promise.all([load(), loadStats()])
}

async function batchTransition(status: string) {
  const rows = selected.value
  if (!rows.length) return
  let ok = 0
  for (const r of rows) {
    try {
      await resolveBadcase(r.id, { fix_status: status, note: "批量流转" })
      ok++
    } catch {
      /* skip */
    }
  }
  ElMessage.success(`批量流转完成: ${ok}/${rows.length}`)
  await Promise.all([load(), loadStats()])
}

// ── 批量归因 (后台任务轮询, 跟随当前筛选范围) ──
const batch = ref({
  running: false,
  total: 0,
  done: 0,
  failed: 0,
  scope: null as { signal_source?: string; keyword?: string } | null,
})
let batchTimer: ReturnType<typeof setInterval> | null = null

const batchPct = computed(() => (batch.value.total > 0 ? Math.round((batch.value.done / batch.value.total) * 100) : 0))

const batchScopeText = computed(() => {
  const parts: string[] = []
  if (batch.value.scope?.signal_source) parts.push(signalLabel(batch.value.scope.signal_source))
  if (batch.value.scope?.keyword) parts.push(`"${batch.value.scope.keyword}"`)
  return parts.join(" + ") || "全部"
})

async function pollBatch() {
  try {
    const st = await getBatchAttributionStatus()
    batch.value = {
      running: st.running,
      total: st.total,
      done: st.done,
      failed: st.failed,
      scope: (st.scope as { signal_source?: string; keyword?: string }) ?? null,
    }
    if (!st.running) {
      if (batchTimer) {
        clearInterval(batchTimer)
        batchTimer = null
      }
      if (st.total > 0) {
        ElMessage.success(`批量归因完成: 成功 ${st.done} / 失败 ${st.failed} / 共 ${st.total}`)
        await Promise.all([load(), loadStats()])
      }
    }
  } catch {
    /* handled */
  }
}

async function doBatchAttribution() {
  const scope: { signal_source?: string; keyword?: string } = {}
  if (filters.value.signal_source) scope.signal_source = filters.value.signal_source
  if (filters.value.keyword) scope.keyword = filters.value.keyword
  const scopeText = Object.keys(scope).length ? " (按当前筛选范围)" : ""
  try {
    await startBatchAttribution(200, scope)
    ElMessage.success(`GLM 裁判批量归因已启动${scopeText}, 每条约 20-40 秒`)
    if (!batchTimer) batchTimer = setInterval(pollBatch, 4000)
  } catch {
    /* handled */
  }
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
  router.push({ path: "/admin/audit", query: { session_id: row.session_id } })
}

// ── 展示工具 ──
function signalType(s: string): string {
  const m: Record<string, string> = {
    negative_feedback: "danger",
    transfer: "warning",
    agent_revoke: "warning",
    behavior_anomaly: "info",
    compliance_alert: "danger",
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
function fixTableLabel(s?: string | null) {
  return s ? (FIX_TABLE_LABELS[s] ?? s) : "-"
}
function fixStatusLabel(s: string) {
  const m: Record<string, string> = { pending: "待修", fixing: "修复中", canary: "已灰度", deployed: "已全量", rejected: "已驳回" }
  return m[s] ?? s
}
function fixStatusType(s: string): string {
  const m: Record<string, string> = { pending: "info", fixing: "warning", canary: "warning", deployed: "success", rejected: "danger" }
  return m[s] ?? "info"
}
function shortModel(m?: string | null) {
  if (!m) return ""
  if (m.includes("GLM")) return "GLM 裁判"
  if (m.includes("qwen")) return "qwen 裁判"
  return m.length > 10 ? m.slice(0, 10) : m
}
function fmtPct(v: number | null | undefined) {
  return v == null ? "-" : `${Math.round(v * 100)}%`
}
function fmtTime(iso?: string | null) {
  return iso ? iso.slice(0, 19).replace("T", " ") : "-"
}
function relTime(iso?: string | null) {
  if (!iso) return "-"
  const diff = (Date.now() - new Date(iso).getTime()) / 1000
  if (diff < 60) return "刚刚"
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return `${Math.floor(diff / 86400)} 天前`
}

onMounted(() => {
  load()
  pollBatch()
  loadStats()
})
onUnmounted(() => {
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
.page-subtitle {
  font-size: var(--fs-sm);
  font-weight: 400;
  color: var(--color-text-secondary);
  margin-left: 8px;
}

.stat-cards {
  display: grid;
  grid-template-columns: repeat(5, minmax(110px, 1fr)) minmax(220px, 1.6fr);
  gap: 10px;
  margin-top: 12px;
}
.stat-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  background: var(--el-fill-color-extra-light);
  cursor: pointer;
  transition: border-color 0.2s;
  &:hover { border-color: var(--el-color-primary-light-5); }
  &.active { border-color: var(--el-color-primary); background: var(--el-color-primary-light-9); }
  .label { font-size: var(--fs-sm); color: var(--color-text-secondary); }
  .num { font-size: 20px; font-weight: 600; }
  .num.warn { color: var(--el-color-warning); }
  .hint { font-size: var(--fs-xs, 11px); color: var(--color-text-placeholder); }
}
.dist-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 8px 12px;
  background: var(--el-fill-color-extra-light);
  .label { font-size: var(--fs-sm); color: var(--color-text-secondary); }
  .dist-bars { margin-top: 4px; display: flex; flex-direction: column; gap: 3px; }
  .dist-row { display: flex; align-items: center; gap: 6px; }
  .dist-label { font-size: 11px; width: 62px; color: var(--color-text-secondary); flex-shrink: 0; }
  .dist-track { flex: 1; height: 8px; border-radius: 4px; background: var(--el-fill-color); overflow: hidden; }
  .dist-fill { height: 100%; border-radius: 4px; background: var(--el-color-primary-light-3); }
  .dist-count { font-size: 11px; width: 24px; text-align: right; }
  .dist-empty { font-size: var(--fs-sm); }
}

.batch-progress-text { font-size: var(--fs-sm); color: var(--color-text-secondary); }
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
</style>
