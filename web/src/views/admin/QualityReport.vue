<template>
  <div class="quality-report-page">
    <div class="page-header">
      <h2>质量监控报表 <span class="page-subtitle">质检结果 · 趋势 · 闭环运营 (30 秒自动刷新)</span></h2>
      <div class="header-actions">
        <el-radio-group v-model="trendWindow" size="small" @change="reloadTrend">
          <el-radio-button :value="7">近 7 天</el-radio-button>
          <el-radio-button :value="14">近 14 天</el-radio-button>
          <el-radio-button :value="30">近 30 天</el-radio-button>
        </el-radio-group>
        <el-button size="small" :loading="refreshing" @click="refreshAll">立即刷新</el-button>
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
        <el-tooltip placement="left" effect="light">
          <template #content>
            <div class="judge-tip">
              <b>全量质检巡检</b><br />
              所有会话从<b>原始对话内容</b>过 GLM 裁判质检,<br />
              不依赖置信度/差评信号 — 高置信但答非所问也逃不掉。<br />
              fail 自动采集进待复核队列; 合格率见下方卡片。
            </div>
          </template>
          <el-button size="small" type="success" plain :loading="scan.running" @click="doQualityScan">
            {{ scan.running ? `全量质检中 ${scan.done}/${scan.total}` : "全量质检 · 扫描全部会话" }}
          </el-button>
        </el-tooltip>
      </div>
    </div>

    <!-- 数据时效提示: 巡检停止超过 1 天即提示覆盖缺口 -->
    <el-alert
      v-if="scanStaleDays >= 1 && !scan.running"
      type="warning"
      show-icon
      :closable="false"
      style="margin-top: 10px"
      :title="`上轮全量质检为 ${scanStaleDays} 天前 (${fmtTime(scan.lastRun?.finished_at)})，之后的新会话尚未质检 — 覆盖率将持续下降，建议触发全量质检`"
    />

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

    <!-- GLM 批量归因进行中: 实时进度 -->
    <el-progress
      v-if="batch.running"
      :percentage="batchPct"
      :stroke-width="10"
      striped
      striped-flow
      style="margin-top: 10px"
    >
      <template #default>
        <span class="progress-text">
          GLM 裁判批量归因中 {{ batch.done }}/{{ batch.total }}
          <template v-if="batch.failed"> (失败 {{ batch.failed }})</template>
        </span>
      </template>
    </el-progress>

    <!-- ══ 质检结果 (近 30 天) ══ -->
    <div class="section-label">质检结果 <span class="muted">近 30 天 · 点击卡片进入对应会话列表</span></div>
    <div class="stat-cards">
      <div class="stat-card clickable" @click="gotoCategory('unscanned')">
        <span class="label">覆盖率</span>
        <span class="num">{{ fmtPct(coverage?.coverage) }}</span>
        <span class="hint">已检 {{ coverage?.scanned_sessions ?? "-" }} / 应检 {{ coverage?.total_sessions ?? "-" }} · 未检 {{ unscannedCount }} 会话</span>
      </div>
      <div class="stat-card clickable" @click="gotoCategory('pass')">
        <span class="label">合格率</span>
        <span class="num ok">
          {{ fmtPct(coverage?.pass_rate) }}
          <span v-if="passRateDelta != null" class="delta" :class="passRateDelta >= 0 ? 'up' : 'down'" :title="`近 ${trendWindow} 天 vs 前 ${trendWindow} 天`">
            {{ passRateDelta >= 0 ? "▲" : "▼" }} {{ Math.abs(passRateDelta).toFixed(1) }}pct
          </span>
        </span>
        <span class="hint">合格 {{ coverage?.by_verdict?.pass ?? "-" }} 会话</span>
      </div>
      <div class="stat-card clickable" @click="gotoCategory('fail')">
        <span class="label">不合格</span>
        <span class="num danger">{{ coverage?.by_verdict?.fail ?? "-" }}</span>
        <span class="hint">另有提醒级 {{ coverage?.by_verdict?.warn ?? 0 }} (轻问题, 不合格另计)</span>
      </div>
      <div class="stat-card clickable" @click="gotoCategory('pending_review')">
        <span class="label">待复核</span>
        <span class="num warn">{{ stats?.pending_review ?? "-" }}</span>
        <span class="hint">待人工判定会话 (问题已发现, 等人确认)</span>
      </div>
    </div>

    <!-- ══ 趋势图 ══ -->
    <el-card shadow="never" class="trend-card">
      <template #header>
        <div class="card-head">
          <span>质检判定趋势 <span class="muted">近 {{ trendWindow }} 天 · 按会话时间 · 柱=判定数 线=当日合格率</span></span>
          <span v-if="trendLoaded" class="muted trend-meta">
            期初 → 期末: 日均质检 {{ trendAvgRecent }} 会话
          </span>
        </div>
      </template>
      <div ref="chartEl" class="trend-chart"></div>
    </el-card>

    <!-- ══ 闭环运营 ══ -->
    <div class="section-label">闭环运营 <span class="muted">问题案例的确认、修复与上线进度</span></div>
    <div class="stat-cards">
      <div class="stat-card">
        <span class="label">今日新增案例</span>
        <span class="num">{{ stats?.today_new ?? "-" }}</span>
        <span class="hint">近 24 小时采集</span>
      </div>
      <div class="stat-card">
        <span class="label">已上线</span>
        <span class="num ok">{{ stats?.deployed ?? "-" }}</span>
        <span class="hint">修复完成并上线</span>
      </div>
      <div class="stat-card">
        <span class="label">LLM 直通率</span>
        <span class="num">{{ fmtPct(stats?.llm_pass_rate ?? null) }}</span>
        <span class="hint">归因免人工确认占比</span>
      </div>
      <div class="funnel-card">
        <span class="label">案例流转盘点 <span class="muted">(各状态案例数 · 占采集总数比例)</span></span>
        <div class="funnel-rows">
          <div v-for="f in funnel" :key="f.label" class="funnel-row">
            <span class="funnel-label">{{ f.label }}</span>
            <div class="dist-track"><div class="dist-fill" :class="f.cls" :style="{ width: f.width }"></div></div>
            <span class="dist-count">{{ f.count }}</span>
            <span class="funnel-pct muted">{{ f.pct }}</span>
          </div>
        </div>
      </div>
    </div>

    <!-- ══ 分布 ══ -->
    <el-row :gutter="12" class="dist-row-cards">
      <el-col :span="12">
        <el-card shadow="never">
          <template #header><span>根因层分布 <span class="muted">全部问题案例 · 点击进入智能质检处置</span></span></template>
          <div class="dist-bars">
            <div v-for="d in layerDist" :key="d.key" class="dist-row clickable" :title="`${d.label}: ${d.count} (${d.pct})`" @click="gotoScan">
              <span class="dist-label">{{ d.label }}</span>
              <div class="dist-track"><div class="dist-fill" :style="{ width: d.width }"></div></div>
              <span class="dist-count">{{ d.count }}</span>
              <span class="funnel-pct muted">{{ d.pct }}</span>
            </div>
            <div v-if="!layerDist.length" class="muted dist-empty">暂无归因数据 — 先在智能质检页跑批量归因</div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="never">
          <template #header><span>信号来源分布 <span class="muted">问题案例从哪些渠道被发现</span></span></template>
          <div class="dist-bars">
            <div v-for="d in signalDist" :key="d.key" class="dist-row" :title="`${d.label}: ${d.count} (${d.pct})`">
              <span class="dist-label">{{ d.label }}</span>
              <div class="dist-track"><div class="dist-fill warn" :style="{ width: d.width }"></div></div>
              <span class="dist-count">{{ d.count }}</span>
              <span class="funnel-pct muted">{{ d.pct }}</span>
            </div>
            <div v-if="!signalDist.length" class="muted dist-empty">暂无案例信号</div>
          </div>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue"
import { useRouter } from "vue-router"
import { ElMessage } from "element-plus"
import type { EChartsCoreOption } from "echarts/core"
import {
  getBadcaseStats,
  getQualityCoverage,
  getQualityScanStatus,
  getQualityTrend,
  startQualityScan,
  startBatchAttribution,
  getBatchAttributionStatus,
  type QualityScanStatus,
  type QualityTrendPoint,
} from "@/api/closedLoop"
import { useChart } from "@/composables/useChart"

const router = useRouter()

const refreshing = ref(false)
const stats = ref<Awaited<ReturnType<typeof getBadcaseStats>> | null>(null)
const coverage = ref<Awaited<ReturnType<typeof getQualityCoverage>> | null>(null)

// ── 趋势: 取双倍窗口, 后半段=本期, 前半段=基线 (合格率环比) ──
const trendWindow = ref(7)
const trend = ref<QualityTrendPoint[]>([])
const trendLoaded = ref(false)
async function reloadTrend() {
  try {
    const r = await getQualityTrend(trendWindow.value * 2)
    trend.value = r.days
    trendLoaded.value = true
  } catch {
    /* handled */
  }
}

const recentTrend = computed(() => trend.value.slice(-trendWindow.value))
function rateOf(list: QualityTrendPoint[]): number | null {
  const judged = list.reduce((s, d) => s + d.pass + d.warn + d.fail, 0)
  if (!judged) return null
  return (list.reduce((s, d) => s + d.pass, 0) / judged) * 100
}
const passRateDelta = computed(() => {
  if (trend.value.length < trendWindow.value * 2) return null
  const base = rateOf(trend.value.slice(0, trendWindow.value))
  const cur = rateOf(recentTrend.value)
  if (base == null || cur == null) return null
  return cur - base
})
const trendAvgRecent = computed(() => {
  const n = recentTrend.value.reduce((s, d) => s + d.pass + d.warn + d.fail, 0)
  return Math.round(n / Math.max(1, recentTrend.value.length))
})

const chartEl = ref<HTMLElement | null>(null)
const chartOption = computed<EChartsCoreOption | null>(() => {
  const s = recentTrend.value
  if (!s.length) return null
  return {
    tooltip: { trigger: "axis" },
    legend: { data: ["合格", "提醒", "不合格", "合格率"], top: 0, textStyle: { fontSize: 11 } },
    grid: { left: 40, right: 46, top: 40, bottom: 24 },
    xAxis: { type: "category", data: s.map((d) => d.date.slice(5)) },
    yAxis: [
      { type: "value", name: "会话", minInterval: 1 },
      { type: "value", name: "合格率", min: 0, max: 100, axisLabel: { formatter: "{value}%" } },
    ],
    series: [
      { name: "合格", type: "bar", stack: "v", data: s.map((d) => d.pass), itemStyle: { color: "#67c23a" }, barMaxWidth: 26 },
      { name: "提醒", type: "bar", stack: "v", data: s.map((d) => d.warn), itemStyle: { color: "#e6a23c" } },
      { name: "不合格", type: "bar", stack: "v", data: s.map((d) => d.fail), itemStyle: { color: "#f56c6c" } },
      {
        name: "合格率",
        type: "line",
        yAxisIndex: 1,
        smooth: true,
        connectNulls: true,
        itemStyle: { color: "#409eff" },
        data: s.map((d) => {
          const t = d.pass + d.warn + d.fail
          return t ? Math.round((d.pass / t) * 100) : null
        }),
      },
    ],
  }
})
useChart(chartEl, chartOption)

// ── 全量质检巡检 (本页触发, 后台任务轮询) ──
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

const scanStaleDays = computed(() => {
  const t = scan.value.lastRun?.finished_at
  if (!t) return 0
  const ms = Date.now() - new Date(t).getTime() // ISO 带时区偏移, 原生可解析
  return Number.isFinite(ms) ? Math.floor(ms / 86_400_000) : 0
})

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
      if (st.total > 0) {
        ElMessage.success(`全量质检完成: 不合格 ${st.n_fail} 已采入待复核 (合格率 ${((st.last_run?.pass_rate ?? 0) * 100).toFixed(1)}%)`)
      }
      await Promise.all([loadStats(), loadCoverage(), reloadTrend()])
    }
  } catch {
    /* handled */
  }
}

async function doQualityScan() {
  try {
    await startQualityScan({ limit: 5000 }) // 全量补扫: 后端批次循环至无未检会话
    ElMessage.success("全量质检已启动, 后台逐会话审查原始对话")
    if (!scanTimer) scanTimer = setInterval(pollScan, 4000)
  } catch {
    /* handled */
  }
}

// ── GLM 裁判批量归因 (本页触发, 处理全部未归因坏例) ──
const batch = ref({ running: false, total: 0, done: 0, failed: 0 })
const batchPct = computed(() => (batch.value.total > 0 ? Math.round((batch.value.done / batch.value.total) * 100) : 0))
let batchTimer: ReturnType<typeof setInterval> | null = null

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
        await Promise.all([loadStats(), reloadTrend()])
      }
    }
  } catch {
    /* handled */
  }
}

async function doBatchAttribution() {
  try {
    await startBatchAttribution(200)
    ElMessage.success("GLM 裁判批量归因已启动, 每条约 20-40 秒")
    if (!batchTimer) batchTimer = setInterval(pollBatch, 4000)
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
    await Promise.all([loadStats(), loadCoverage(), reloadTrend(), pollScan()])
  } finally {
    refreshing.value = false
  }
}

const unscannedCount = computed(() => {
  if (!coverage.value) return "-"
  return Math.max(0, coverage.value.total_sessions - coverage.value.scanned_sessions)
})

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
const funnel = computed(() => {
  const s = stats.value
  if (!s) return []
  const total = Math.max(1, s.total)
  const steps = [
    { label: "采集", count: s.total, cls: "" },
    { label: "待复核", count: s.pending_review, cls: "warn" },
    { label: "已确认", count: s.confirmed, cls: "ok" },
    { label: "已上线", count: s.deployed, cls: "done" },
  ]
  const max = Math.max(1, ...steps.map((x) => x.count))
  return steps.map((x) => ({
    ...x,
    width: `${Math.max(4, Math.round((x.count / max) * 100))}%`,
    pct: `${Math.round((x.count / total) * 100)}%`,
  }))
})

function fmtPct(v: number | null | undefined) {
  return v == null ? "-" : `${Math.round(v * 100)}%`
}
function fmtTime(iso?: string | null) {
  return iso ? iso.slice(0, 16).replace("T", " ") : "-"
}

onMounted(() => {
  refreshAll()
  pollBatch() // 恢复可能进行中的批量归因进度
  refreshTimer = setInterval(() => Promise.all([loadStats(), loadCoverage()]), 30_000)
})
onUnmounted(() => {
  if (scanTimer) clearInterval(scanTimer)
  if (batchTimer) clearInterval(batchTimer)
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
.header-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.progress-text {
  font-size: 12px;
  white-space: nowrap;
}
.muted { color: var(--color-text-secondary); }

.section-label {
  margin: 18px 0 8px;
  font-size: var(--fs-sm);
  font-weight: 600;
  color: var(--color-text-primary);
  .muted { font-weight: 400; margin-left: 6px; }
}

.stat-cards {
  display: grid;
  grid-template-columns: repeat(4, minmax(130px, 1fr));
  gap: 10px;
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
.delta {
  font-size: 12px;
  font-weight: 600;
  margin-left: 6px;
  &.up { color: var(--el-color-success); }
  &.down { color: var(--el-color-danger); }
}

.trend-card { margin-top: 12px; }
.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 6px;
  font-size: var(--fs-sm);
  font-weight: 600;
  .muted { font-weight: 400; }
}
.trend-meta { font-size: 12px; }
.trend-chart { height: 300px; width: 100%; }

.funnel-card {
  grid-column: span 2;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 10px 12px;
  background: var(--el-fill-color-extra-light);
  .label { font-size: var(--fs-sm); color: var(--color-text-secondary); .muted { font-weight: 400; } }
}
.funnel-rows { margin-top: 6px; display: flex; flex-direction: column; gap: 3px; }
.funnel-row { display: flex; align-items: center; gap: 6px; }
.funnel-label { font-size: 11px; width: 48px; color: var(--color-text-secondary); flex-shrink: 0; }
.funnel-pct { font-size: 11px; width: 34px; text-align: right; flex-shrink: 0; }

.dist-row-cards { margin-top: 12px; }
.dist-bars { display: flex; flex-direction: column; gap: 4px; }
.dist-row {
  display: flex;
  align-items: center;
  gap: 6px;
  &.clickable { cursor: pointer; &:hover .dist-label { color: var(--el-color-primary); } }
}
.dist-label { font-size: 11px; width: 72px; color: var(--color-text-secondary); flex-shrink: 0; }
.dist-track { flex: 1; height: 8px; border-radius: 4px; background: var(--el-fill-color); overflow: hidden; }
.dist-fill {
  height: 100%;
  border-radius: 4px;
  background: var(--el-color-primary-light-3);
  &.warn { background: var(--el-color-warning-light-5); }
  &.ok { background: var(--el-color-success-light-5); }
  &.done { background: var(--el-color-success); }
}
.dist-count { font-size: 11px; width: 30px; text-align: right; font-weight: 600; }
.dist-empty { font-size: var(--fs-sm); }
</style>
