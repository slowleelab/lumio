<template>
  <div class="pattern-page">
    <!-- 飞轮漏斗: 发现→处置→修复→上线→根治 一屏判读 (P0 打通全链条) -->
    <div class="flywheel" v-if="funnel">
      <div class="fw-stages">
        <template v-for="(st, i) in funnel.stages" :key="st.key">
          <div v-if="i" class="fw-arrow">›</div>
          <div
            class="fw-stage"
            :class="{ clickable: stageQuery[st.key] }"
            :title="stageQuery[st.key] ? `点击下钻到案例工作台 (${st.label})` : st.label"
            @click="stageQuery[st.key] && router.push({ path: '/admin/cases', query: stageQuery[st.key]! })"
          >
            <span class="fw-count">{{ st.count }}<em v-if="stageQuery[st.key]" class="fw-drill">↧</em></span>
            <span class="fw-label">{{ st.label }}</span>
          </div>
        </template>
        <template v-if="funnel.reopened > 0">
          <div class="fw-arrow fw-back">↩</div>
          <div class="fw-stage fw-reopened" title="复检未过, 重开重新治理">
            <span class="fw-count">{{ funnel.reopened }}</span>
            <span class="fw-label">回流</span>
          </div>
        </template>
      </div>
      <div class="fw-meta muted">
        已根治平均周期 {{ funnel.avg_cycle_days != null ? funnel.avg_cycle_days + " 天" : "—" }}
        · 复发案例自动继承组状态 · 上线后组级重放一键根治验证
      </div>
    </div>
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
          :data="visibleGroups"
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
          <el-table-column label="案例 / 待处置" width="112" align="center">
            <template #default="{ row }">
              <span class="case-count">{{ row.case_count }}</span>
              <span class="muted count-sep"> / </span>
              <span :class="{ 'pending-hot': row.pending_count > 0 }" title="待处置">{{ row.pending_count }}</span>
            </template>
          </el-table-column>
          <el-table-column label="方案" width="86" align="center">
            <template #default="{ row }">
              <el-tag v-if="row.plan_status === 'none'" size="small" type="info" effect="plain">未定方案</el-tag>
              <el-tag v-else-if="row.plan_status === 'planned'" size="small" type="primary" effect="plain">方案已定</el-tag>
              <el-tag v-else size="small" type="warning" effect="plain">修复中</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="最近发生" width="76" align="center">
            <template #default="{ row }">{{ fmtTime(row.last_seen) }}</template>
          </el-table-column>
        </el-table>
      </div>

      <!-- 右: 组详情 -->
      <div class="detail-panel" v-loading="detailLoading">
        <template v-if="detail">
          <!-- 统计头: 组名 + 态势数字徽标 — 一眼掌握组规模与推进状态 -->
          <div class="detail-hero">
            <div class="hero-title">
              <span class="group-intent big">{{ intentLabel(detail.intent_label) }}</span>
              <span class="muted">×</span>
              <span class="big">{{ layerLabel(detail.root_cause_layer) }}</span>
              <el-tag size="small" :type="fixTableType(detail.fix_table)" effect="light">{{ fixTableLabel(detail.fix_table) }}</el-tag>
            </div>
            <div class="hero-stats">
              <span class="stat"><b>{{ detail.case_count }}</b> 案例</span>
              <span v-if="detailPending > 0" class="stat warn"><b>{{ detailPending }}</b> 待处置</span>
              <span v-if="detail.unconfirmed_count > 0" class="stat warn"><b>{{ detail.unconfirmed_count }}</b> 根因待确认</span>
              <span class="stat"><b>{{ detail.case_count - detail.unconfirmed_count }}</b> 已确认</span>
              <span v-if="detail.plan_owner" class="stat">方案 · {{ detail.plan_owner }}</span>
            </div>
          </div>

          <div class="section-title">
            修复方案
            <el-tooltip content="方案落定 = 治理人确认组内共性根因, 是批量执行的把关前置" placement="top">
              <span class="muted section-hint">怎么修 · 谁负责 ⓘ</span>
            </el-tooltip>
          </div>
          <div class="plan-row">
            <el-select v-model="planTable" size="small" style="width: 150px" placeholder="修复分流表">
              <el-option v-for="opt in planTableOptions" :key="opt.key" :label="opt.label" :value="opt.key">
                <span>
                  {{ opt.label }}
                  <span v-if="opt.recommended" class="rec-mark">推荐</span>
                </span>
              </el-option>
            </el-select>
            <el-input v-model="planOwner" placeholder="负责人" size="small" style="width: 130px" />
            <el-button size="small" type="primary" :loading="acting" @click="savePlan">保存方案</el-button>
            <el-button size="small" link @click="planText = detail.plan_template || ''">按分流表生成建议</el-button>
            <el-link
              v-if="fixGuideTo"
              type="primary"
              :underline="false"
              style="margin-left: 4px"
              @click="router.push(fixGuideTo)"
            >前往处理 ›</el-link>
          </div>
          <el-input v-model="planText" type="textarea" :rows="4" placeholder="修复方案: 做什么、在哪做、验收标准 (可用「生成建议」出模板后修改)" />

          <div class="section-title">
            批量执行
            <el-button
              v-if="detail.cases.some((c: PatternCase) => c.fix_status === 'deployed')"
              size="small" type="success" plain :loading="recheck.running" style="margin-left: 10px"
              @click="runGroupRecheck"
            >{{ recheck.running ? `根治验证中 ${recheck.done}/${recheck.total}` : "组级重放验证 (一键根治)" }}</el-button>
            <el-tooltip content="节点带各状态案例数, 点击下一节点批量流转; 逐例走状态机守门, 单例失败不阻断" placement="top">
              <span class="muted section-hint">点节点流转 ⓘ</span>
            </el-tooltip>
          </div>
          <StatusFlowChain
            :current="batchChainCurrent"
            :counts="batchCounts"
            :loading="acting"
            :allow-reject="false"
            batch-mode
            @advance="(to: string) => runBatch(to)"
          />

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
            <el-table-column label="会话时间" width="86" align="center">
              <template #default="{ row }">{{ fmtTime(row.session_time) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="76">
              <template #default="{ row }">
                <el-button size="small" link type="primary" @click="gotoQc(row)">核查</el-button>
                <el-button size="small" link type="primary" @click="router.push({ path: '/admin/cases', query: { case_id: row.id } })">处置</el-button>
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

// ── P0 飞轮: 漏斗统计 + 组级重放验证 ──
// 漏斗阶段 → 案例工作台下钻参数 (发现=全量不带筛选)
const stageQuery: Record<string, Record<string, string> | null> = {
  total: null,
  pending: { fix_status: "pending" },
  fixing: { fix_status: "fixing" },
  deployed: { fix_status: "deployed" },
  verified: { fix_status: "verified" },
}

const funnel = ref<FunnelStats | null>(null)
async function loadFunnel() {
  try {
    funnel.value = await getFunnelStats()
  } catch {
    funnel.value = null
  }
}
const recheck = ref({ running: false, total: 0, done: 0, passed: 0 })
let recheckTimer: ReturnType<typeof setInterval> | null = null
async function runGroupRecheck() {
  if (!detail.value) return
  const n = detail.value.cases.filter((c: PatternCase) => c.fix_status === "deployed").length
  try {
    await ElMessageBox.confirm(
      `将对组内 ${n} 个已上线案例逐个重放 (当前代码重新回答), 按新质检判定自动流转: 通过 → 已根治, 未通过 → 重开重新治理。全程约 ${Math.ceil(n * 0.75)} 分钟, 期间可离开页面。确认执行?`,
      "组级根治验证",
      { type: "warning", confirmButtonText: "开始验证", cancelButtonText: "再想想" }
    )
  } catch {
    return
  }
  try {
    const r = await startGroupRecheck(detail.value.group_key)
    recheck.value = { running: true, total: r.total, done: 0, passed: 0 }
    if (!recheckTimer) recheckTimer = setInterval(() => void pollRecheck(), 2500)
  } catch {
    /* handled */
  }
}

// 刷新/切页回来: 后台任务可能仍在跑 — 恢复进度与轮询 (与批量归因恢复同模式)
async function resumeRecheckIfNeeded() {
  try {
    const st = await getGroupRecheckStatus()
    if (st.running) {
      recheck.value = { running: true, total: st.total, done: st.done, passed: st.passed }
      if (!recheckTimer) recheckTimer = setInterval(() => void pollRecheck(), 2500)
    }
  } catch {
    /* 状态接口异常静默 */
  }
}
async function pollRecheck() {
  try {
    const st = await getGroupRecheckStatus()
    recheck.value = { running: st.running, total: st.total, done: st.done, passed: st.passed }
    if (!st.running) {
      if (recheckTimer) {
        clearInterval(recheckTimer)
        recheckTimer = null
      }
      ElMessage.success(`组级重放完成: 通过 ${st.passed}/${st.total}${st.failed ? `, ${st.failed} 例重开重新治理` : ""}`)
      await Promise.all([load(), loadFunnel(), detail.value ? reloadDetail() : Promise.resolve()])
    }
  } catch {
    /* 轮询失败静默重试 */
  }
}
const acting = ref(false)
const groups = ref<PatternGroup[]>([])
const unattributed = ref(0)
const detail = ref<PatternDetail | null>(null)
// 组态势: 待处置数从案例明细算 (列表接口有 pending_count, 详情没有)
const detailPending = computed(() =>
  (detail.value?.cases ?? []).filter((c) => c.fix_status === "pending" || c.fix_status === "reopened").length,
)
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
// 层 → 允许的修复表 (首位 = 推荐默认; 与后端 badcase_loop._LAYER_ALLOWED_FIX_TABLES 同构, 后端守门兜底)
const LAYER_ALLOWED_TABLES: Record<string, string[]> = {
  layer_1: ["C_rule"],
  layer_2: ["C_rule"],
  layer_3: ["B_intent", "C_rule"],
  layer_4: ["C_rule"],
  layer_5: ["A_knowledge", "C_rule"],
  layer_6: ["D_model", "A_knowledge"],
  layer_7: ["C_rule"],
}
// 组根因层固定 (group_key 组成部分), 表选项锁定本层允许集
const planTableOptions = computed(() =>
  (LAYER_ALLOWED_TABLES[detail.value?.root_cause_layer || ""] || []).map((key, i) => ({
    key,
    label: FIX_TABLE_LABELS[key] ?? key,
    recommended: i === 0,
  })),
)
const FIX_GUIDE_TO: Record<string, string> = {
  A_knowledge: "/admin/faq",
  B_intent: "/admin/intent-library",
  C_rule: "/admin/intent-library",
  D_model: "/admin/prompts",
}
const FIX_STATUS: Record<string, { label: string; type: string }> = {
  pending: { label: "待处置", type: "info" },
  fixing: { label: "修复中", type: "warning" },
  canary: { label: "灰度中 (可选)", type: "primary" },
  deployed: { label: "已上线", type: "success" },
  reopened: { label: "复检未过", type: "danger" },
}

function intentLabel(v: string | null) {
  return intentZh(v) || "未分类业务"
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
  return t ? t.slice(5, 10) : "-"
}

const fixGuideTo = computed(() => FIX_GUIDE_TO[planTable.value || detail.value?.fix_table || ""] ?? null)

// 批量节点链: 组内案例状态分布 → 计数徽标; 当前位置取组内"最靠前"状态 (批量从它推进)
const batchCounts = computed<Record<string, number>>(() => {
  const c: Record<string, number> = {}
  for (const k of ["pending", "fixing", "canary", "deployed", "verified"])
    c[k] = detail.value?.cases.filter((x) => x.fix_status === k).length ?? 0
  return c
})
const batchChainCurrent = computed<string>(() => {
  const c = batchCounts.value
  if ((c.fixing ?? 0) > 0 && (c.pending ?? 0) === 0) return c.canary ? "fixing" : "fixing"
  if ((c.canary ?? 0) > 0 && (c.fixing ?? 0) === 0 && (c.pending ?? 0) === 0) return "canary"
  return "pending"
})
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
    const allowed = LAYER_ALLOWED_TABLES[data.root_cause_layer] || []
    planTable.value = allowed.includes(data.fix_table ?? "") ? data.fix_table ?? "" : allowed[0] || "" // 存量非法归正推荐
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
  // 组层×表组合保险 (下拉已锁定允许集, 此处兜底)
  const allowed = LAYER_ALLOWED_TABLES[detail.value.root_cause_layer] || []
  if (allowed.length && (!planTable.value || !allowed.includes(planTable.value))) {
    ElMessage.warning(`根因层「${layerLabel(detail.value.root_cause_layer)}」的修复表请从允许集内选择`)
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
  const verb = { fixing: "确认根因并转入修复中", canary: "批量转灰度 (可选旁路)", deployed: "批量上线" }[target] ?? target
  const n = batchCounts.value[target === "fixing" ? "pending" : target === "canary" ? "fixing" : "canary"] ?? 0
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
  router.push({ path: "/admin/badcase", query: { keyword: row.session_id, session_id: row.session_id } })
}

onMounted(async () => {
  void loadFunnel()
  void resumeRecheckIfNeeded()
  // 三页联动: 案例工作台/处理链路跳转带入 — group_key 直开组详情;
  // defect (缺陷枚举) 映射责任层过滤组列表 (高亮该缺陷的治理对象)
  const gk = route.query.group_key as string | undefined
  const defect = route.query.defect as string | undefined
  if (gk) {
    await openGroupByKey(gk)
  } else if (defect) {
    await load()
    defectLayerFilter.value = DEFECT_TO_LAYER[defect] || ""
  } else {
    load()
  }
})

async function openGroupByKey(groupKey: string) {
  detailLoading.value = true
  try {
    await load()
    detail.value = await getPatternDetail(groupKey)
    planText.value = detail.value?.plan_text ?? ""
    planOwner.value = detail.value?.plan_owner ?? ""
    const allowed = LAYER_ALLOWED_TABLES[detail.value?.root_cause_layer || ""] || []
    planTable.value = allowed.includes(detail.value?.fix_table ?? "") ? detail.value?.fix_table ?? "" : DEFECT_TO_TABLE[detail] || ""
    planTable.value = allowed.includes(planTable.value) ? planTable.value : allowed[0] || ""
  } finally {
    detailLoading.value = false
  }
}

// 缺陷 → 责任层/默认表 (与 utils/defects 及后端 DEFECT_TYPES 同构)
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

// 缺陷下钻过滤: 只看该责任层的组 (处理链路缺陷标签跳转进入时)
const visibleGroups = computed(() =>
  defectLayerFilter.value ? groups.value.filter((g) => g.root_cause_layer === defectLayerFilter.value) : groups.value
)
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

/* 飞轮漏斗 */
.flywheel {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 14px;
  border: 1px solid var(--el-color-success-light-7);
  border-radius: 10px;
  background: linear-gradient(120deg, var(--el-color-success-light-9), var(--el-color-primary-light-9));
  margin-bottom: 12px;
}
.fw-stages {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}
.fw-stage {
  display: flex;
  flex-direction: column;
  align-items: center;
  min-width: 58px;
  padding: 4px 10px;
  border-radius: 8px;
  background: var(--el-bg-color, #fff);
  .fw-count {
    font-size: 18px;
    font-weight: 700;
    color: var(--el-color-primary);
    line-height: 1.2;
  }
  .fw-label {
    font-size: 10.5px;
    color: var(--color-text-secondary);
  }
  &.fw-reopened .fw-count {
    color: var(--el-color-danger);
  }
}
.fw-arrow {
  color: var(--el-color-success);
  font-size: 15px;
  font-weight: 700;
  &.fw-back {
    color: var(--el-color-danger);
  }
}
.fw-meta {
  font-size: 11px;
}
.fw-stage.clickable {
  cursor: pointer;
  border: 1px solid var(--el-color-primary-light-5);
  transition: all 0.15s;
  .fw-drill {
    font-style: normal;
    font-size: 11px;
    color: var(--el-color-primary);
    margin-left: 2px;
  }
  &:hover {
    border-color: var(--el-color-primary);
    transform: translateY(-1px);
  }
}

.pattern-layout {
  display: grid;
  /* 左栏需容纳列表全部列 (问题组 180 + 案例/待处置 112 + 方案 86 + 时间 76 ≈ 470px) */
  grid-template-columns: 486px 1fr;
  gap: 14px;
  align-items: start;
}

@media (max-width: 1100px) {
  .pattern-layout {
    grid-template-columns: 1fr;
  }
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
  display: flex;
  align-items: center;
  gap: 8px;
}

.hero-stats {
  display: flex;
  align-items: center;
  gap: 14px;
  margin-top: 8px;

  .stat {
    font-size: 12px;
    color: var(--color-text-secondary);

    b {
      font-size: 16px;
      font-weight: 700;
      color: var(--color-text-primary);
      margin-right: 2px;
    }

    &.warn b {
      color: var(--el-color-danger);
    }
  }
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

.rec-mark {
  margin-left: 6px;
  padding: 0 5px;
  font-size: 11px;
  line-height: 16px;
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  border-radius: 3px;
}
.count-sep {
  padding: 0 2px;
  font-size: 11px;
}
</style>
