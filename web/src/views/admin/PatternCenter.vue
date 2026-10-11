<template>
  <div class="pattern-page">
    <!-- 页头: 标题 + 漏斗一行文字 -->
    <div class="page-header">
      <h2>问题治理</h2>
      <span v-if="funnel" class="flywheel-text muted">
        {{ funnel.stages.map(s => `${s.label} ${s.count}`).join(" → ") }}
        <template v-if="funnel.reopened > 0"> · 回流 {{ funnel.reopened }}</template>
        <template v-if="funnel.avg_cycle_days != null"> · 根治均 {{ funnel.avg_cycle_days }} 天</template>
      </span>
      <el-button size="small" @click="load">刷新</el-button>
    </div>

    <div class="main-split">
      <!-- 左: 问题组列表 -->
      <div class="group-list" v-loading="loading">
        <el-table :data="visibleGroups" size="small" highlight-current-row @row-click="(g: PatternGroup) => openGroup(g)">
          <el-table-column label="问题组" min-width="160">
            <template #default="{ row }">
              <div>{{ intentLabel(row.intent_label) }} × {{ layerLabel(row.root_cause_layer) }}</div>
              <span class="muted ft-label">{{ fixTableLabel(row.fix_table) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="案例" width="56" align="center">
            <template #default="{ row }">{{ row.case_count }}</template>
          </el-table-column>
          <el-table-column label="待处置" width="56" align="center">
            <template #default="{ row }">
              <span :class="{ 'pending-hot': row.pending_count > 0 }">{{ row.pending_count }}</span>
            </template>
          </el-table-column>
          <el-table-column label="方案" width="66" align="center">
            <template #default="{ row }">
              <span class="muted">{{ row.plan_status === 'planned' ? '已定' : row.plan_status === 'fixing' ? '修复中' : '—' }}</span>
            </template>
          </el-table-column>
        </el-table>
        <div v-if="unattributed > 0" class="unattributed-row muted">
          另有 {{ unattributed }} 例待归因 →
          <el-link type="primary" :underline="false" @click="router.push({ path: '/admin/quality-loop', query: { tab: 'cases' } })">去案例工作台</el-link>
        </div>
      </div>

      <!-- 右: 组详情 -->
      <div class="group-detail" v-if="detail" v-loading="detailLoading">
        <!-- 标题行 -->
        <div class="detail-head">
          <span class="detail-name">{{ intentLabel(detail.intent_label) }} × {{ layerLabel(detail.root_cause_layer) }}</span>
          <span class="muted detail-meta">{{ detail.case_count }} 例</span>
          <el-button v-if="detail.cases.some((c: PatternCase) => c.fix_status === 'deployed')" type="primary" size="small" :loading="recheck.running" @click="runGroupRecheck">
            {{ recheck.running ? `验证中 ${recheck.done}/${recheck.total}` : "根治验证" }}
          </el-button>
        </div>

        <!-- 方案表单 -->
        <div class="detail-section">
          <div class="section-label">修复方案</div>
          <div class="plan-form">
            <el-select v-model="planTable" size="small" style="width: 140px" placeholder="分流表">
              <el-option v-for="opt in planTableOptions" :key="opt.key" :label="opt.label" :value="opt.key">
                <span>{{ opt.label }}<span v-if="opt.recommended" class="rec-mark">荐</span></span>
              </el-option>
            </el-select>
            <el-input v-model="planOwner" placeholder="负责人" size="small" style="width: 100px" />
            <el-button size="small" @click="savePlan" :loading="acting">保存方案</el-button>
            <el-button size="small" link @click="planText = detail.plan_template || ''">生成建议</el-button>
            <el-link v-if="fixGuideTo" type="primary" :underline="false" @click="router.push(fixGuideTo)">前往 ›</el-link>
          </div>
          <el-input v-model="planText" type="textarea" :rows="2" placeholder="做什么、在哪做、验收标准" />
        </div>

        <!-- 批量执行 -->
        <div class="detail-section">
          <div class="section-label">批量执行</div>
          <StatusFlowChain :current="batchChainCurrent" :counts="batchCounts" :loading="acting" :allow-reject="false" batch-mode @advance="(to: string) => runBatch(to)" />
        </div>

        <!-- 案例表 -->
        <div class="detail-section">
          <div class="section-label">组内案例 ({{ detail.cases.length }})</div>
          <el-table :data="detail.cases" size="small">
            <el-table-column label="问题句" min-width="240">
              <template #default="{ row }">
                <div class="case-q">{{ row.user_input }}</div>
              </template>
            </el-table-column>
            <el-table-column label="根因" width="70" align="center">
              <template #default="{ row }">
                <span :class="row.needs_human_review ? 'pending-hot' : 'muted'">{{ row.needs_human_review ? "待确认" : "已确认" }}</span>
              </template>
            </el-table-column>
            <el-table-column label="处置" width="70" align="center">
              <template #default="{ row }">
                <span class="muted">{{ fixStatusLabel(row.fix_status) }}</span>
              </template>
            </el-table-column>
            <el-table-column label="时间" width="72" align="center">
              <template #default="{ row }">{{ fmtTime(row.session_time) }}</template>
            </el-table-column>
            <el-table-column label="" width="100" align="center">
              <template #default="{ row }">
                <el-button size="small" link type="primary" @click="gotoQc(row)">核查</el-button>
                <el-button size="small" link type="primary" @click="router.push({ path: '/admin/quality-loop', query: { tab: 'cases', case_id: row.id } })">处置</el-button>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </div>
      <div v-else class="muted no-selection">选择左侧问题组查看详情</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue"
import { useRoute, useRouter } from "vue-router"
import { ElMessage, ElMessageBox } from "element-plus"
import StatusFlowChain from "@/components/common/StatusFlowChain.vue"
import { intentZh } from "@/utils/intentZh"
import { getFunnelStats, getGroupRecheckStatus, startGroupRecheck, type FunnelStats } from "@/api/patterns"
import {
  batchTransition,
  getPatternDetail,
  listPatterns,
  savePatternPlan,
  type PatternDetail,
  type PatternGroup,
} from "@/api/patterns"

const router = useRouter()
const route = useRoute()
const loading = ref(false)
const detailLoading = ref(false)

const stageQuery: Record<string, Record<string, string> | null> = {
  total: null,
  pending: { fix_status: "pending" },
  fixing: { fix_status: "fixing" },
  deployed: { fix_status: "deployed" },
  verified: { fix_status: "verified" },
}

const funnel = ref<FunnelStats | null>(null)
async function loadFunnel() {
  try { funnel.value = await getFunnelStats() } catch { funnel.value = null }
}

const recheck = ref({ running: false, total: 0, done: 0, passed: 0 })
let recheckTimer: ReturnType<typeof setInterval> | null = null

async function runGroupRecheck() {
  if (!detail.value) return
  const n = detail.value.cases.filter((c: PatternCase) => c.fix_status === "deployed").length
  try {
    await ElMessageBox.confirm(
      `将对组内 ${n} 个已上线案例批量重放验证。确认执行?`,
      "根治验证",
      { type: "warning", confirmButtonText: "开始", cancelButtonText: "取消" }
    )
  } catch { return }
  try {
    const r = await startGroupRecheck(detail.value.group_key)
    recheck.value = { running: true, total: r.total, done: 0, passed: 0 }
    if (!recheckTimer) recheckTimer = setInterval(() => void pollRecheck(), 2500)
  } catch { /* handled */ }
}

async function resumeRecheckIfNeeded() {
  try {
    const st = await getGroupRecheckStatus()
    if (st.running) {
      recheck.value = { running: true, total: st.total, done: st.done, passed: st.passed }
      if (!recheckTimer) recheckTimer = setInterval(() => void pollRecheck(), 2500)
    }
  } catch { /* 静默 */ }
}

async function pollRecheck() {
  try {
    const st = await getGroupRecheckStatus()
    recheck.value = { running: st.running, total: st.total, done: st.done, passed: st.passed }
    if (!st.running) {
      if (recheckTimer) { clearInterval(recheckTimer); recheckTimer = null }
      ElMessage.success(`根治验证完成: 通过 ${st.passed}/${st.total}${st.failed ? `, ${st.failed} 例重开` : ""}`)
      await Promise.all([load(), loadFunnel(), detail.value ? reloadDetail() : Promise.resolve()])
    }
  } catch { /* 重试 */ }
}

// ── 缺陷下钻 ──
const DEFECT_TO_LAYER: Record<string, string> = {
  knowledge_missing: "layer_5", knowledge_outdated: "layer_5",
  intent_misread: "layer_3", intent_uncovered: "layer_3",
  rule_flaw: "layer_4", reply_quality: "layer_6",
  fallback_poor: "layer_4", compliance_risk: "layer_7",
}
const DEFECT_TO_TABLE: Record<string, string> = {
  knowledge_missing: "A_knowledge", knowledge_outdated: "A_knowledge",
  intent_misread: "B_intent", intent_uncovered: "B_intent",
  rule_flaw: "C_rule", reply_quality: "D_model",
  fallback_poor: "C_rule", compliance_risk: "C_rule",
}
const defectLayerFilter = ref("")
const visibleGroups = computed(() =>
  defectLayerFilter.value ? groups.value.filter((g) => g.root_cause_layer === defectLayerFilter.value) : groups.value
)

// ── 数据 ──
const groups = ref<PatternGroup[]>([])
const detail = ref<PatternDetail | null>(null)
const planText = ref("")
const planOwner = ref("")
const planTable = ref("")
const acting = ref(false)
const unattributed = ref(0)

async function load() {
  loading.value = true
  try {
    const data = await listPatterns()
    groups.value = data.items
    unattributed.value = data.unattributed
  } catch { /* handled */ } finally { loading.value = false }
}

async function openGroup(g: PatternGroup) {
  detailLoading.value = true
  try { await loadDetail(g.group_key) } finally { detailLoading.value = false }
}

async function loadDetail(groupKey: string) {
  detail.value = await getPatternDetail(groupKey)
  planText.value = detail.value?.plan_text ?? ""
  planOwner.value = detail.value?.plan_owner ?? ""
  const allowed = LAYER_ALLOWED_TABLES[detail.value?.root_cause_layer || ""] || []
  planTable.value = allowed.includes(detail.value?.fix_table ?? "") ? detail.value?.fix_table ?? "" : allowed[0] || ""
}

async function reloadDetail() {
  if (!detail.value) return
  await loadDetail(detail.value.group_key)
}

// ── 方案 ──
const LAYER_ALLOWED_TABLES: Record<string, string[]> = {
  layer_1: ["C_rule"], layer_2: ["C_rule"], layer_3: ["B_intent", "C_rule"],
  layer_4: ["C_rule"], layer_5: ["A_knowledge", "C_rule"], layer_6: ["D_model", "A_knowledge"], layer_7: ["C_rule"],
}
const planTableOptions = computed(() =>
  (LAYER_ALLOWED_TABLES[detail.value?.root_cause_layer || ""] || []).map((key, i) => ({
    key, label: FIX_TABLE_LABELS[key] ?? key, recommended: i === 0,
  }))
)
const FIX_TABLE_LABELS: Record<string, string> = {
  A_knowledge: "A·知识库", B_intent: "B·意图库", C_rule: "C·规则", D_model: "D·模型", none: "无需修复",
}

const fixGuideTo = computed(() => {
  const m: Record<string, string> = { A_knowledge: "/admin/faq", B_intent: "/admin/intent-library", C_rule: "/admin/intent-library", D_model: "/admin/prompts" }
  return m[planTable.value || detail.value?.fix_table || ""] ?? null
})

async function savePlan() {
  if (!detail.value) return
  if (!planText.value.trim()) { ElMessage.warning("方案内容不能为空"); return }
  const allowed = LAYER_ALLOWED_TABLES[detail.value.root_cause_layer] || []
  if (allowed.length && (!planTable.value || !allowed.includes(planTable.value))) { ElMessage.warning("修复表请从允许集内选择"); return }
  acting.value = true
  try {
    await savePatternPlan(detail.value.group_key, {
      intent_label: detail.value.intent_label,
      root_cause_layer: detail.value.root_cause_layer,
      fix_table: planTable.value || null,
      plan_text: planText.value,
      plan_owner: planOwner.value,
    })
    ElMessage.success("方案已保存")
    await reloadDetail()
  } catch { /* handled */ } finally { acting.value = false }
}

// ── 批量执行 ──
const batchCounts = computed(() => {
  const c: Record<string, number> = {}
  for (const cs of detail.value?.cases ?? []) c[cs.fix_status] = (c[cs.fix_status] || 0) + 1
  return c
})
const batchChainCurrent = computed(() => {
  const c = batchCounts.value
  if ((c.verified || 0) > 0) return "verified"
  if ((c.deployed || 0) > 0) return "deployed"
  if ((c.canary || 0) > 0) return "canary"
  if ((c.fixing || 0) > 0) return "fixing"
  return "pending"
})

async function runBatch(target: string) {
  if (!detail.value) return
  const verb = { fixing: "确认根因并转入修复中", canary: "批量转灰度", deployed: "批量上线" }[target] ?? target
  const n = batchCounts.value[target === "fixing" ? "pending" : target === "canary" ? "fixing" : "canary"] ?? 0
  await ElMessageBox.confirm(
    target === "fixing" ? `将组内 ${n} 个待处置案例的根因批量确认并转入修复中。确认执行?` : `将组内 ${n} 个案例${verb}。确认执行?`,
    "批量执行", { type: "warning" }
  )
  acting.value = true
  try {
    const res = await batchTransition(detail.value.group_key, target)
    const failed = res.results.filter((r) => !r.ok)
    ElMessage.success(`完成: ${res.done}/${res.total}${failed.length ? `, ${failed.length} 例失败` : ""}`)
    await reloadDetail()
  } catch { /* handled */ } finally { acting.value = false }
}

// ── 工具 ──
const INTENT_FALLBACK: Record<string, string> = { faq: "知识问答", bill_query: "账单查询", card_loss: "挂失", installment_inquiry: "分期咨询", limit_query: "额度" }
function intentLabel(v: string | null) { return intentZh(v) || INTENT_FALLBACK[v ?? ""] || "未分类" }
function layerLabel(v: string | null) {
  const m: Record<string, string> = { layer_1: "预处理", layer_2: "会话管理", layer_3: "意图识别", layer_4: "路由决策", layer_5: "RAG检索", layer_6: "回复生成", layer_7: "风控合规" }
  return m[v ?? ""] ?? v ?? "-"
}
const FIX_STATUS_LABELS: Record<string, string> = { pending: "待处置", fixing: "修复中", canary: "灰度", deployed: "已上线", verified: "已根治", reopened: "重开", rejected: "驳回" }
function fixStatusLabel(s: string) { return FIX_STATUS_LABELS[s] ?? s }
function fixTableLabel(v: string | null) { return FIX_TABLE_LABELS[v ?? ""] ?? "—" }
function fixTableType(v: string | null) { const m: Record<string, string> = { A_knowledge: "success", B_intent: "primary", C_rule: "warning", D_model: "danger" }; return m[v ?? ""] ?? "info" }
function fixStatusType(s: string) { const m: Record<string, string> = { pending: "info", fixing: "warning", canary: "primary", deployed: "success", verified: "success", reopened: "danger", rejected: "info" }; return m[s] ?? "info" }
function fmtTime(t: string | null) { return t ? t.slice(5, 10) : "-" }
function gotoQc(row: { session_id: string }) {
  router.push({ path: "/admin/quality-loop", query: { tab: "qc", keyword: row.session_id, session_id: row.session_id } })
}
function fixStatusTypeCss(s: string) { return fixStatusType(s) }

async function openGroupByKey(groupKey: string) {
  detailLoading.value = true
  try {
    await load()
    await loadDetail(groupKey)
  } finally { detailLoading.value = false }
}

onMounted(async () => {
  void loadFunnel()
  void resumeRecheckIfNeeded()
  const gk = route.query.group_key as string | undefined
  const defect = route.query.defect as string | undefined
  if (gk) { await openGroupByKey(gk) }
  else if (defect) { await load(); defectLayerFilter.value = DEFECT_TO_LAYER[defect] || "" }
  else { load() }
})

onUnmounted(() => { if (recheckTimer) clearInterval(recheckTimer) })
</script>

<style scoped lang="scss">
.pattern-page { padding: 0 4px; }

.page-header {
  display: flex; align-items: center; gap: 12px; margin-bottom: 12px;
  h2 { margin: 0; font-size: 16px; }
  .flywheel-text { flex: 1; font-size: 12px; }
}

.main-split {
  display: grid; grid-template-columns: 320px 1fr; gap: 14px; align-items: start;
  @media (max-width: 900px) { grid-template-columns: 1fr; }
}

/* 左: 列表 */
.group-list {
  .el-table { cursor: pointer; }
  .ft-label { font-size: 11px; }
  .pending-hot { color: var(--el-color-danger); font-weight: 600; }
  .unattributed-row { padding: 8px 12px; font-size: 12px; }
}

/* 右: 详情 */
.group-detail {
  display: flex; flex-direction: column; gap: 16px;
}

.detail-head {
  display: flex; align-items: center; gap: 10px;
  .detail-name { font-size: 15px; font-weight: 600; }
  .detail-meta { font-size: 12px; }
  .el-button { margin-left: auto; }
}

.detail-section {
  .section-label { font-size: 12px; font-weight: 600; color: var(--color-text-secondary); margin-bottom: 6px; }
}

.plan-form {
  display: flex; align-items: center; gap: 6px; margin-bottom: 6px; flex-wrap: wrap;
}

.rec-mark {
  margin-left: 4px; padding: 0 4px; font-size: 10px; color: var(--el-color-primary); background: var(--el-color-primary-light-9); border-radius: 2px;
}

.case-q { font-size: 12px; line-height: 1.5; word-break: break-all; }
.no-selection { text-align: center; padding: 60px 0; }
.muted { color: var(--color-text-muted, #909399); }
.pending-hot { color: var(--el-color-danger); }
</style>
