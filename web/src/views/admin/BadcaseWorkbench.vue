<template>
  <div class="badcase-page">
    <div class="page-header">
      <h2>Badcase 工作台</h2>
      <div class="header-stats">
        <span class="stat">今日新增 <b>{{ stats?.today_new ?? "-" }}</b></span>
        <span class="stat">待复核 <b class="warn">{{ stats?.pending_review ?? "-" }}</b></span>
        <span class="stat">已确认 <b>{{ stats?.confirmed ?? "-" }}</b></span>
        <span class="stat">LLM 直通率 <b>{{ fmtPct(stats?.llm_pass_rate ?? null) }}</b></span>
      </div>
    </div>

    <div class="filters">
      <el-select v-model="filters.signal_source" placeholder="信号源" clearable size="small" style="width: 160px" @change="load">
        <el-option label="负面反馈" value="negative_feedback" />
        <el-option label="转人工" value="transfer" />
        <el-option label="人工撤回" value="agent_revoke" />
        <el-option label="行为异常" value="behavior_anomaly" />
        <el-option label="合规告警" value="compliance_alert" />
      </el-select>
      <el-select v-model="filters.root_cause_layer" placeholder="根因层" clearable size="small" style="width: 140px" @change="load">
        <el-option v-for="l in 7" :key="l" :label="`layer_${l}`" :value="`layer_${l}`" />
        <el-option label="uncertain" value="uncertain" />
      </el-select>
      <el-select v-model="filters.fix_status" placeholder="修复状态" clearable size="small" style="width: 130px" @change="load">
        <el-option label="待修" value="pending" />
        <el-option label="修复中" value="fixing" />
        <el-option label="已灰度" value="canary" />
        <el-option label="已全量" value="deployed" />
      </el-select>
      <el-button size="small" @click="load">刷新</el-button>
    </div>

    <el-table :data="badcases" v-loading="loading" stripe style="margin-top: 16px" @row-click="openDetail">
      <el-table-column label="用户输入" min-width="220" show-overflow-tooltip>
        <template #default="{ row }">{{ row.user_input }}</template>
      </el-table-column>
      <el-table-column label="信号源" width="110">
        <template #default="{ row }">
          <el-tag size="small" :type="signalType(row.signal_source)">{{ signalLabel(row.signal_source) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="根因层" width="110">
        <template #default="{ row }">
          <span v-if="row.root_cause_layer">{{ row.root_cause_layer }}</span>
          <span v-else class="muted">未归因</span>
        </template>
      </el-table-column>
      <el-table-column label="置信" width="70" align="center">
        <template #default="{ row }">
          {{ row.attribution_confidence != null ? row.attribution_confidence.toFixed(2) : "-" }}
        </template>
      </el-table-column>
      <el-table-column label="复核" width="80" align="center">
        <template #default="{ row }">
          <el-tag v-if="row.needs_human_review" size="small" type="warning">待人工</el-tag>
          <el-tag v-else size="small" type="success">已确认</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="分流表" width="110">
        <template #default="{ row }">{{ row.fix_table || "-" }}</template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="fixStatusType(row.fix_status)">{{ fixStatusLabel(row.fix_status) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="时间" width="160">
        <template #default="{ row }">{{ row.created_at?.slice(0, 16).replace("T", " ") }}</template>
      </el-table-column>
      <el-table-column label="操作" width="180" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click.stop="openDetail(row)">详情</el-button>
          <el-button
            v-if="row.needs_human_review && row.root_cause_layer"
            link type="primary" size="small"
            @click.stop="runAttribution(row)"
          >LLM 归因</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      v-model:current-page="page"
      v-model:page-size="pageSize"
      :total="total"
      :page-sizes="[20, 50, 100]"
      layout="total, sizes, prev, pager, next"
      style="margin-top: 16px; justify-content: flex-end"
      @current-change="load"
      @size-change="onSizeChange"
    />

    <!-- 详情抽屉 -->
    <el-drawer v-model="detailVisible" :title="detail?.user_input?.slice(0, 30)" size="55%" destroy-on-close>
      <div v-if="detail" class="detail-body">
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="会话">{{ detail.session_id }}</el-descriptions-item>
          <el-descriptions-item label="信号源">{{ signalLabel(detail.signal_source) }}</el-descriptions-item>
          <el-descriptions-item label="根因层">{{ detail.root_cause_layer || "未归因" }}</el-descriptions-item>
          <el-descriptions-item label="根因类别">{{ detail.root_cause_category || "-" }}</el-descriptions-item>
          <el-descriptions-item label="分流表">{{ detail.fix_table || "-" }}</el-descriptions-item>
          <el-descriptions-item label="修复状态">{{ detail.fix_status }}</el-descriptions-item>
        </el-descriptions>
        <div class="section-title">用户输入</div>
        <div class="bubble-user">{{ detail.user_input }}</div>
        <div class="section-title">Bot 回复</div>
        <div class="bubble-bot">{{ detail.bot_output || "-" }}</div>
        <template v-if="detail.attribution_evidence">
          <div class="section-title">LLM 归因证据</div>
          <div class="evidence">{{ detail.attribution_evidence }}</div>
          <el-button
            v-if="!detail.needs_human_review"
            type="success" size="small" style="margin-top: 8px"
            @click="confirmAttribution(detail); detailVisible = false"
          >确认归因并进入修复</el-button>
        </template>
        <div class="section-title">八层现场快照</div>
        <pre class="snapshot">{{ JSON.stringify(detail.snapshot, null, 2) }}</pre>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue"
import { ElMessage } from "element-plus"
import {
  listBadcases,
  attributeBadcase,
  resolveBadcase,
  getClosedLoopHealth,
  type Badcase,
  type ClosedLoopHealth,
} from "@/api/closedLoop"

const badcases = ref<Badcase[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(50)
const loading = ref(false)
const filters = ref({ signal_source: "", root_cause_layer: "", fix_status: "" })
const detailVisible = ref(false)
const detail = ref<Badcase | null>(null)
const health = ref<ClosedLoopHealth | null>(null)

const stats = ref<{ today_new: number; pending_review: number; confirmed: number; llm_pass_rate: number | null } | null>(null)

async function load() {
  loading.value = true
  try {
    const res = await listBadcases({
      signal_source: filters.value.signal_source || undefined,
      root_cause_layer: filters.value.root_cause_layer || undefined,
      fix_status: filters.value.fix_status || undefined,
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

async function loadStats() {
  try {
    const { getBadcaseStats } = await import("@/api/closedLoop")
    stats.value = await getBadcaseStats()
  } catch {
    /* handled */
  }
}

async function confirmAttribution(row: Badcase) {
  try {
    await resolveBadcase(row.id, {
      fix_status: "fixing",
      fix_table: row.fix_table || undefined,
      human_confirmed_layer: row.root_cause_layer || undefined,
      note: "归因确认",
    })
    ElMessage.success("归因已确认")
    load()
  } catch {
    /* handled */
  }
}

async function runAttribution(row: Badcase) {
  try {
    const r = (await attributeBadcase(row.id)) as { root_cause_layer?: string; needs_human_review?: boolean }
    ElMessage.success(`归因完成: ${r.root_cause_layer ?? "-"}`)
    load()
  } catch {
    /* handled */
  }
}

function onSizeChange() {
  page.value = 1
  load()
}

function openDetail(row: Badcase) {
  detail.value = row
  detailVisible.value = true
}

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
  const m: Record<string, string> = {
    negative_feedback: "负面反馈",
    transfer: "转人工",
    agent_revoke: "人工撤回",
    behavior_anomaly: "行为异常",
    compliance_alert: "合规告警",
  }
  return m[s] ?? s
}

function fixStatusLabel(s: string) {
  const m: Record<string, string> = {
    pending: "待修",
    fixing: "修复中",
    canary: "已灰度",
    deployed: "已全量",
    rejected: "已驳回",
  }
  return m[s] ?? s
}

function fixStatusType(s: string): string {
  const m: Record<string, string> = {
    pending: "info",
    fixing: "warning",
    canary: "warning",
    deployed: "success",
    rejected: "danger",
  }
  return m[s] ?? "info"
}

function fmtPct(v: number | null | undefined) {
  return v == null ? "-" : `${Math.round(v * 100)}%`
}

async function loadHealth() {
  try {
    health.value = await getClosedLoopHealth()
  } catch {
    /* handled */
  }
}

onMounted(() => {
  load()
  loadStats()
  loadHealth()
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
.header-stats {
  display: flex;
  gap: 16px;
  font-size: var(--fs-sm);
  .stat b {
    font-size: 16px;
    margin-left: 4px;
  }
  .stat b.warn {
    color: var(--color-warning, #e6a23c);
  }
}
.filters {
  display: flex;
  gap: var(--space-2);
  margin-top: 12px;
  flex-wrap: wrap;
}
.muted { color: var(--color-text-secondary); }
.detail-body { padding: 0 4px; }
.section-title {
  font-weight: 600;
  margin: 14px 0 6px;
  font-size: var(--fs-sm);
}
.bubble-user {
  background: var(--el-color-primary-light-9, #ecf5ff);
  padding: 8px 12px;
  border-radius: 6px;
  white-space: pre-wrap;
}
.bubble-bot {
  background: var(--color-bg-page, #f5f7fa);
  padding: 8px 12px;
  border-radius: 6px;
  white-space: pre-wrap;
}
.evidence, .snapshot {
  background: var(--color-bg-page, #f5f7fa);
  padding: 10px;
  border-radius: 6px;
  font-size: var(--fs-xs, 12px);
  white-space: pre-wrap;
  word-break: break-all;
}
</style>
