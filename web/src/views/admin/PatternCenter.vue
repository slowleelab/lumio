<template>
  <div class="pattern-page">
    <div class="page-header">
      <h2>
        问题治理
        <span class="page-subtitle">共性问题聚成专项 · 一次修复覆盖整组 — 与案例工作台分工: 那边管单案例流转, 这里管模式治理</span>
      </h2>
      <div class="header-actions">
        <el-button size="small" @click="load">刷新</el-button>
      </div>
    </div>

    <el-alert type="info" :closable="false" class="hint">
      <template #title>
        问题组由未结案例按「业务分类 × 根因层」实时聚合 — 新案例自动入组, 治理完成后同类复发自动重开组。
        <template v-if="unattributed > 0">
          另有 <b>{{ unattributed }}</b> 例待归因案例未入组（归因需人工把关, 到
          <el-link type="primary" :underline="false" @click="router.push('/admin/cases')">案例工作台</el-link> 处理）。
        </template>
      </template>
    </el-alert>

    <div class="pattern-layout">
      <!-- 左: 问题组列表 -->
      <div class="group-panel">
        <el-table
          v-loading="loading"
          :data="groups"
          size="small"
          highlight-current-row
          @row-click="(row: PatternGroup) => openGroup(row)"
        >
          <el-table-column label="问题组" min-width="180">
            <template #default="{ row }">
              <div class="group-name">
                <span class="group-intent">{{ intentLabel(row.intent_label) }}</span>
                <span class="muted">×</span>
                <span>{{ layerLabel(row.root_cause_layer) }}</span>
              </div>
              <el-tag size="small" :type="fixTableType(row.fix_table)" effect="plain" class="group-ft">{{ fixTableLabel(row.fix_table) }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="案例" width="70" align="center">
            <template #default="{ row }">
              <span class="case-count">{{ row.case_count }}</span>
            </template>
          </el-table-column>
          <el-table-column label="待处置" width="64" align="center">
            <template #default="{ row }">
              <span :class="{ 'pending-hot': row.pending_count > 0 }">{{ row.pending_count }}</span>
            </template>
          </el-table-column>
          <el-table-column label="方案" width="86" align="center">
            <template #default="{ row }">
              <el-tag v-if="row.plan_status === 'none'" size="small" type="info" effect="plain">未定方案</el-tag>
              <el-tag v-else-if="row.plan_status === 'planned'" size="small" type="primary" effect="plain">方案已定</el-tag>
              <el-tag v-else size="small" type="warning" effect="plain">修复中</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="最近发生" width="96">
            <template #default="{ row }">{{ fmtTime(row.last_seen) }}</template>
          </el-table-column>
        </el-table>
      </div>

      <!-- 右: 组详情 -->
      <div class="detail-panel" v-loading="detailLoading">
        <template v-if="detail">
          <div class="detail-hero">
            <div class="hero-title">
              <span class="group-intent big">{{ intentLabel(detail.intent_label) }}</span>
              <span class="muted">×</span>
              <span class="big">{{ layerLabel(detail.root_cause_layer) }}</span>
            </div>
            <div class="hero-meta muted">
              {{ detail.case_count }} 个案例 · 组内案例批量治理
              <template v-if="detail.unconfirmed_count > 0"> · {{ detail.unconfirmed_count }} 例根因待人工确认（批量确认随方案执行）</template>
            </div>
          </div>

          <div class="section-title">
            修复方案
            <span class="muted section-hint">(方案落定 = 治理人确认组内共性根因, 是批量执行的把关前置)</span>
          </div>
          <div class="plan-row">
            <el-select v-model="planTable" size="small" style="width: 140px" placeholder="修复分流表">
              <el-option v-for="(label, key) in FIX_TABLE_LABELS" :key="key" :label="label" :value="key" />
            </el-select>
            <el-input v-model="planOwner" placeholder="负责人" size="small" style="width: 130px" />
            <el-button size="small" @click="planText = detail.plan_template || ''">按分流表生成建议</el-button>
            <el-button size="small" type="primary" :loading="acting" @click="savePlan">保存方案</el-button>
            <el-link
              v-if="fixGuideTo"
              type="primary"
              :underline="false"
              style="margin-left: 4px"
              @click="router.push(fixGuideTo)"
            >前往处理 ›</el-link>
          </div>
          <el-input v-model="planText" type="textarea" :rows="4" placeholder="修复方案: 做什么、在哪做、验收标准 (可用上方按钮生成建议模板后修改)" />

          <div class="section-title">
            批量执行
            <span class="muted section-hint">(逐例走状态机守门, 单例失败不阻断; 确认根因随方案批量落人工确认)</span>
          </div>
          <div class="batch-row">
            <el-button size="small" type="success" :loading="acting" :disabled="!canBatch('fixing')" @click="runBatch('fixing')">
              批量确认根因 → 修复中 ({{ pendingCount }})
            </el-button>
            <el-button size="small" type="warning" :loading="acting" :disabled="!canBatch('canary')" @click="runBatch('canary')">
              批量转灰度 ({{ fixingCount }})
            </el-button>
            <el-button size="small" :loading="acting" :disabled="!canBatch('deployed')" @click="runBatch('deployed')">
              批量上线 ({{ canaryCount }})
            </el-button>
          </div>

          <div class="section-title">组内案例 <span class="muted section-hint">({{ detail.cases.length }} 例, 按会话时间倒序)</span></div>
          <el-table :data="detail.cases" size="small" class="case-table" max-height="420">
            <el-table-column label="问题句" min-width="240">
              <template #default="{ row }">
                <div class="case-q">{{ row.user_input }}</div>
              </template>
            </el-table-column>
            <el-table-column label="根因确认" width="96" align="center">
              <template #default="{ row }">
                <el-tag v-if="row.needs_human_review" size="small" type="warning" effect="plain">待确认</el-tag>
                <el-tag v-else size="small" type="success" effect="plain">已确认</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="处置" width="86" align="center">
              <template #default="{ row }">
                <el-tag size="small" :type="fixStatusType(row.fix_status)" effect="plain">{{ fixStatusLabel(row.fix_status) }}</el-tag>
              </template>
            </el-table-column>
            <el-table-column label="会话时间" width="100">
              <template #default="{ row }">{{ fmtTime(row.session_time) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="76">
              <template #default="{ row }">
                <el-button size="small" link type="primary" @click="gotoQc(row)">核查</el-button>
              </template>
            </el-table-column>
          </el-table>
        </template>
        <div v-else-if="!detailLoading" class="muted empty-hint">← 从左侧选择问题组</div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue"
import { useRouter } from "vue-router"
import { ElMessage, ElMessageBox } from "element-plus"
import {
  batchTransition,
  getPatternDetail,
  listPatterns,
  savePatternPlan,
  type PatternDetail,
  type PatternGroup,
} from "@/api/patterns"

const router = useRouter()
const loading = ref(false)
const detailLoading = ref(false)
const acting = ref(false)
const groups = ref<PatternGroup[]>([])
const unattributed = ref(0)
const detail = ref<PatternDetail | null>(null)
const planText = ref("")
const planOwner = ref("")
const planTable = ref("")

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
const FIX_TABLE_LABELS: Record<string, string> = {
  A_knowledge: "A · 知识库",
  B_intent: "B · 意图库",
  C_rule: "C · 规则",
  D_model: "D · 模型",
  none: "无需修复",
}
const FIX_GUIDE_TO: Record<string, string> = {
  A_knowledge: "/admin/faq",
  B_intent: "/admin/intent-library",
  C_rule: "/admin/intent-library",
  D_model: "/admin/prompts",
}
const INTENT_FALLBACK: Record<string, string> = {
  faq: "常见咨询",
  bill_query: "账单查询",
  card_loss: "挂失",
  installment_inquiry: "分期咨询",
  limit_query: "额度",
}

const FIX_STATUS: Record<string, { label: string; type: string }> = {
  pending: { label: "待处置", type: "info" },
  fixing: { label: "修复中", type: "warning" },
  canary: { label: "已灰度", type: "primary" },
  deployed: { label: "已上线", type: "success" },
  reopened: { label: "复检未过", type: "danger" },
}

function intentLabel(v: string | null) {
  if (!v) return "未分类业务"
  return INTENT_FALLBACK[v] ?? v
}
function layerLabel(v: string | null) {
  return LAYER_LABELS[v ?? ""] ?? v ?? "-"
}
function fixTableLabel(v: string | null) {
  return FIX_TABLE_LABELS[v ?? ""] ?? "待映射"
}
function fixTableType(v: string | null) {
  return { A_knowledge: "success", B_intent: "primary", C_rule: "warning", D_model: "danger" }[v ?? ""] ?? "info"
}
function fixStatusLabel(v: string) {
  return FIX_STATUS[v]?.label ?? v
}
function fixStatusType(v: string) {
  return FIX_STATUS[v]?.type ?? "info"
}
function fmtTime(t: string | null) {
  return t ? t.slice(5, 16).replace("T", " ") : "-"
}

const fixGuideTo = computed(() => FIX_GUIDE_TO[planTable.value || detail.value?.fix_table || ""] ?? null)
const pendingCount = computed(() => detail.value?.cases.filter((c) => c.fix_status === "pending" || c.fix_status === "reopened").length ?? 0)
const fixingCount = computed(() => detail.value?.cases.filter((c) => c.fix_status === "fixing").length ?? 0)
const canaryCount = computed(() => detail.value?.cases.filter((c) => c.fix_status === "canary").length ?? 0)

function canBatch(target: string) {
  if (target === "fixing") return pendingCount.value > 0
  if (target === "canary") return fixingCount.value > 0
  if (target === "deployed") return canaryCount.value > 0
  return false
}

async function load() {
  loading.value = true
  try {
    const data = await listPatterns()
    groups.value = data.items
    unattributed.value = data.unattributed
  } finally {
    loading.value = false
  }
}

async function openGroup(row: PatternGroup) {
  detailLoading.value = true
  try {
    const data = await getPatternDetail(row.group_key)
    detail.value = data
    planText.value = data.plan_text
    planOwner.value = data.plan_owner
    planTable.value = data.fix_table ?? ""
  } finally {
    detailLoading.value = false
  }
}

async function reloadDetail() {
  if (!detail.value) return
  const data = await getPatternDetail(detail.value.group_key)
  detail.value = data
  await load()
}

async function savePlan() {
  if (!detail.value) return
  if (!planText.value.trim()) {
    ElMessage.warning("方案内容不能为空")
    return
  }
  acting.value = true
  try {
    await savePatternPlan(detail.value.group_key, {
      intent_label: detail.value.intent_label,
      root_cause_layer: detail.value.root_cause_layer,
      fix_table: planTable.value || null,
      plan_text: planText.value,
      plan_owner: planOwner.value,
    })
    ElMessage.success("治理方案已保存")
    await reloadDetail()
  } finally {
    acting.value = false
  }
}

async function runBatch(target: string) {
  if (!detail.value) return
  const verb = { fixing: "确认根因并转入修复中", canary: "批量转灰度", deployed: "批量上线" }[target] ?? target
  const n = target === "fixing" ? pendingCount.value : target === "canary" ? fixingCount.value : canaryCount.value
  await ElMessageBox.confirm(
    target === "fixing"
      ? `将组内 ${n} 个待处置案例的根因批量确认为「${layerLabel(detail.value.root_cause_layer)}」并转入修复中。方案落定即人工把关, 确认执行?`
      : `将组内 ${n} 个案例${verb}。确认执行?`,
    "批量执行确认",
    { type: "warning" }
  )
  acting.value = true
  try {
    const res = await batchTransition(detail.value.group_key, target)
    const failed = res.results.filter((r) => !r.ok)
    ElMessage.success(`批量完成: ${res.done}/${res.total} 成功${failed.length ? `, ${failed.length} 例失败(状态机守门拦截)` : ""}`)
    await reloadDetail()
  } finally {
    acting.value = false
  }
}

function gotoQc(row: { session_id: string }) {
  router.push({ path: "/admin/badcase", query: { keyword: row.session_id.slice(0, 24) } })
}

onMounted(load)
</script>

<style scoped>
.pattern-page {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.page-header h2 {
  margin: 0;
  font-size: 18px;
}

.page-subtitle {
  font-size: 12px;
  color: var(--color-text-muted, #909399);
  font-weight: normal;
  margin-left: 8px;
}

.hint {
  margin: 0;
}

.pattern-layout {
  display: grid;
  grid-template-columns: 420px 1fr;
  gap: 14px;
  align-items: start;
}

.group-panel :deep(.el-table__row) {
  cursor: pointer;
}

.group-name {
  font-size: 13px;
}

.group-intent {
  font-weight: 600;
}

.group-intent.big {
  font-size: 16px;
}

.group-ft {
  margin-top: 2px;
}

.case-count {
  font-weight: 700;
}

.pending-hot {
  color: var(--el-color-danger);
  font-weight: 600;
}

.detail-panel {
  min-height: 320px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 16px;
}

.detail-hero {
  margin-bottom: 6px;
}

.hero-title {
  font-size: 15px;
}

.hero-meta {
  font-size: 12px;
  margin-top: 2px;
}

.section-title {
  font-weight: 600;
  margin: 16px 0 8px;
}

.section-hint {
  font-weight: normal;
  font-size: 12px;
}

.plan-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  flex-wrap: wrap;
}

.batch-row {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}

.case-q {
  font-size: 12px;
  line-height: 1.4;
}

.empty-hint {
  text-align: center;
  padding: 60px 0;
}

.muted {
  color: var(--color-text-muted, #909399);
}
</style>
