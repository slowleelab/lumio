<template>
  <div class="quality-report-page">
    <div class="page-header">
      <h2>质量监控报表 <span class="page-subtitle">质检覆盖 · 判定分布 · 根因分布 (30 秒自动刷新)</span></h2>
      <div class="header-actions">
        <el-button size="small" :loading="refreshing" @click="refreshAll">立即刷新</el-button>
        <el-button size="small" type="success" plain @click="gotoScan">前往全量质检</el-button>
      </div>
    </div>

    <!-- 全量质检进行中: 实时进度 (巡检在智能质检页触发) -->
    <el-progress
      v-if="scan.running"
      :percentage="scanPct"
      :stroke-width="10"
      striped
      striped-flow
      status="success"
      style="margin-top: 10px"
    >
      <template #default>
        <span class="progress-text">
          全量质检中 {{ scan.done }}/{{ scan.total }}
          · 合格 {{ scan.n_pass }} / 提醒 {{ scan.n_warn }} / 不合格 {{ scan.n_fail }}
          <template v-if="scan.n_error"> (失败 {{ scan.n_error }})</template>
        </span>
      </template>
    </el-progress>
    <div v-else-if="scan.lastRun" class="scan-summary muted">
      上轮全量质检 {{ scan.lastRun.total }} 个会话 · 合格率 {{ ((scan.lastRun.pass_rate ?? 0) * 100).toFixed(1) }}%
      · 不合格 {{ scan.lastRun.n_fail }} 已采入待复核 ({{ scan.lastRun.finished_at?.slice(5, 16).replace("T", " ") }})
    </div>

    <div v-if="coverage" class="coverage-line">
      近 30 天应检会话 <b>{{ coverage.total_sessions }}</b> · 已质检 <b class="ok">{{ coverage.scanned_sessions }}</b>
      · 覆盖率 <b>{{ fmtPct(coverage.coverage) }}</b> · 合格率 <b>{{ fmtPct(coverage.pass_rate) }}</b>
      <span class="muted"> (不合格 {{ coverage.by_verdict.fail ?? 0 }} / 提醒 {{ coverage.by_verdict.warn ?? 0 }})</span>
    </div>

    <div class="stat-cards">
      <div class="stat-card clickable" @click="gotoCategory('pending_review')">
        <span class="label">待复核</span>
        <span class="num warn">{{ stats?.pending_review ?? "-" }}</span>
        <span class="hint">点击查看待人工判定会话</span>
      </div>
      <div class="stat-card clickable" @click="gotoCategory('fail')">
        <span class="label">不合格</span>
        <span class="num danger">{{ coverage?.by_verdict?.fail ?? "-" }}</span>
        <span class="hint">质检不合格会话 (另提醒级 {{ coverage?.by_verdict?.warn ?? 0 }})</span>
      </div>
      <div class="stat-card clickable" @click="gotoCategory('pass')">
        <span class="label">合格</span>
        <span class="num ok">{{ coverage?.by_verdict?.pass ?? "-" }}</span>
        <span class="hint">质检合格会话</span>
      </div>
      <div class="stat-card">
        <span class="label">今日新增案例</span>
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
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue"
import { useRouter } from "vue-router"
import {
  getBadcaseStats,
  getQualityCoverage,
  getQualityScanStatus,
  type QualityScanStatus,
} from "@/api/closedLoop"

const router = useRouter()

const refreshing = ref(false)
const stats = ref<Awaited<ReturnType<typeof getBadcaseStats>> | null>(null)
const coverage = ref<Awaited<ReturnType<typeof getQualityCoverage>> | null>(null)

// ── 全量质检巡检状态 (触发在智能质检页, 此处只读展示) ──
const scan = ref({
  running: false,
  total: 0,
  done: 0,
  n_pass: 0,
  n_warn: 0,
  n_fail: 0,
  n_error: 0,
  lastRun: null as QualityScanStatus["last_run"],
})
const scanPct = computed(() => (scan.value.total > 0 ? Math.round((scan.value.done / scan.value.total) * 100) : 0))
let scanTimer: ReturnType<typeof setInterval> | null = null
let refreshTimer: ReturnType<typeof setInterval> | null = null

async function pollScan() {
  try {
    const st = await getQualityScanStatus()
    scan.value = {
      running: st.running,
      total: st.total,
      done: st.done,
      n_pass: st.n_pass,
      n_warn: st.n_warn,
      n_fail: st.n_fail,
      n_error: st.n_error,
      lastRun: st.last_run ?? scan.value.lastRun,
    }
    if (st.running && !scanTimer) scanTimer = setInterval(pollScan, 4000)
    if (!st.running && scanTimer) {
      clearInterval(scanTimer)
      scanTimer = null
      await Promise.all([loadStats(), loadCoverage()])
    }
  } catch {
    /* handled */
  }
}

async function loadStats() {
  try {
    stats.value = await getBadcaseStats()
  } catch {
    /* handled */
  }
}

async function loadCoverage() {
  try {
    coverage.value = await getQualityCoverage()
  } catch {
    /* handled */
  }
}

async function refreshAll() {
  refreshing.value = true
  try {
    await Promise.all([loadStats(), loadCoverage(), pollScan()])
  } finally {
    refreshing.value = false
  }
}

function gotoCategory(category: string) {
  router.push({ path: "/admin/badcase", query: { category } })
}

function gotoScan() {
  router.push({ path: "/admin/badcase" })
}

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

function fmtPct(v: number | null | undefined) {
  return v == null ? "-" : `${Math.round(v * 100)}%`
}

onMounted(() => {
  refreshAll()
  refreshTimer = setInterval(() => Promise.all([loadStats(), loadCoverage()]), 30_000)
})
onUnmounted(() => {
  if (scanTimer) clearInterval(scanTimer)
  if (refreshTimer) clearInterval(refreshTimer)
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
}
.progress-text {
  font-size: 12px;
  white-space: nowrap;
}
.scan-summary {
  margin-top: 10px;
  font-size: var(--fs-sm);
}
.muted { color: var(--color-text-secondary); }

.coverage-line {
  margin-top: 10px;
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  padding: 4px 2px 0;
  b { color: var(--color-text-primary); margin: 0 2px; }
  b.ok { color: var(--el-color-success); }
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
  transition: border-color 0.2s;
  .label { font-size: var(--fs-sm); color: var(--color-text-secondary); }
  .num { font-size: 20px; font-weight: 600; }
  .num.ok { color: var(--el-color-success); }
  .num.warn { color: var(--el-color-warning); }
  .num.danger { color: var(--el-color-danger); }
  .hint { font-size: var(--fs-xs, 11px); color: var(--color-text-placeholder); }
  &.clickable {
    cursor: pointer;
    &:hover { border-color: var(--el-color-primary-light-5); }
  }
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
</style>
