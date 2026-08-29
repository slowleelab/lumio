<template>
  <div class="rag-metrics-page">
    <div class="page-header">
      <h2>RAG 指标监控</h2>
      <div class="header-controls">
        <span v-if="livePolling" class="live-indicator"><span class="dot" /> 实时</span>
        <el-switch
          :model-value="livePolling"
          active-text="实时刷新"
          inline-prompt
          @update:model-value="(v: boolean) => (v ? startLive() : stopLive())"
        />
        <el-radio-group v-model="days" size="small" @change="loadSummary">
          <el-radio-button :value="7">近 7 天</el-radio-button>
          <el-radio-button :value="14">近 14 天</el-radio-button>
          <el-radio-button :value="30">近 30 天</el-radio-button>
        </el-radio-group>
        <el-button :loading="summaryLoading" @click="loadSummary">刷新</el-button>
      </div>
    </div>

    <!-- 实时指标卡 -->
    <el-row :gutter="12" class="stat-cards">
      <el-col :span="4" v-for="card in statCards" :key="card.label">
        <el-card shadow="never" class="stat-card">
          <div class="stat-label">{{ card.label }}</div>
          <div class="stat-value" :class="card.cls ?? ''">{{ card.value }}</div>
          <div class="stat-sub">{{ card.sub }}</div>
        </el-card>
      </el-col>
    </el-row>

    <!-- 图表区 -->
    <el-row :gutter="12" style="margin-top: 12px">
      <el-col :span="16">
        <el-card shadow="never">
          <template #header>响应来源趋势</template>
          <div ref="sourceChartEl" class="chart-lg" />
        </el-card>
      </el-col>
      <el-col :span="8">
        <el-card shadow="never">
          <template #header>回复来源占比</template>
          <div ref="sourcePieEl" class="chart-lg" />
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="12" style="margin-top: 12px">
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>
            FAQ 命中率
            <el-tag size="small" class="header-tag" :type="faqRateType">
              {{ faqHitRate != null ? `${Math.round(faqHitRate * 100)}%` : "无数据" }}
            </el-tag>
          </template>
          <div ref="faqChartEl" class="chart-md" />
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>对话量趋势</template>
          <div ref="volumeChartEl" class="chart-md" />
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="12" style="margin-top: 12px">
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>意图 TOP {{ summary?.intent_top.length ?? 10 }}</template>
          <div ref="intentChartEl" class="chart-md" />
        </el-card>
      </el-col>
      <el-col :span="12">
        <el-card shadow="never">
          <template #header>决策延迟（avg / p95 ms）</template>
          <el-table :data="summary?.decision_latency ?? []" stripe size="small">
            <el-table-column prop="agent" label="Agent" min-width="110" />
            <el-table-column prop="action" label="动作" min-width="110" show-overflow-tooltip />
            <el-table-column prop="count" label="次数" width="80" align="center" />
            <el-table-column label="平均" width="100" align="right">
              <template #default="{ row }">{{ fmtMs(row.avg_ms) }}</template>
            </el-table-column>
            <el-table-column label="P95" width="100" align="right">
              <template #default="{ row }">{{ fmtMs(row.p95_ms) }}</template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onScopeDispose, ref } from "vue"
import type { EChartsCoreOption } from "echarts/core"
import { getRagQualitySummary, getRagLiveMetrics, type RagQualitySummary, type RagLiveMetrics } from "@/api/console"
import { useChart } from "@/composables/useChart"

const days = ref(7)
const summary = ref<RagQualitySummary | null>(null)
const summaryLoading = ref(false)
const live = ref<RagLiveMetrics | null>(null)
const livePolling = ref(false)
let liveTimer: ReturnType<typeof setInterval> | null = null

// 实时指标 30s 轮询（走 axios client, 自带鉴权头; usePolling 是裸 fetch 不带 Authorization）
function startLive() {
  if (liveTimer) return
  livePolling.value = true
  loadLive()
  liveTimer = setInterval(loadLive, 30000)
}
function stopLive() {
  if (liveTimer) {
    clearInterval(liveTimer)
    liveTimer = null
  }
  livePolling.value = false
}
onScopeDispose(stopLive)

const SOURCE_LABELS: Record<string, string> = {
  knowledge: "知识问答",
  tool: "工具编排",
  llm: "LLM 直答",
  template: "模板",
  fallback: "兜底",
  clarify: "澄清",
  human: "人工",
}
const SOURCE_COLORS: Record<string, string> = {
  knowledge: "#409eff",
  tool: "#67c23a",
  llm: "#9254de",
  template: "#909399",
  fallback: "#f56c6c",
  clarify: "#e6a23c",
  human: "#13c2c2",
}
const MATCH_LABELS: Record<string, string> = { exact: "精确命中", semantic: "语义命中", miss: "未命中" }

function fmtMs(v: number | null) {
  return v == null ? "-" : `${Math.round(v)}ms`
}
function fmtInt(v: number | null | undefined) {
  return v == null ? "-" : Math.round(v).toLocaleString()
}
function fmtPct(v: number | null | undefined) {
  return v == null ? "-" : `${(v * 100).toFixed(1)}%`
}

// ── 实时指标卡 ──
const statCards = computed(() => {
  const l = live.value
  const ragStep = summary.value?.decision_latency.find((x) => x.action === "rag_retrieve")
  const cacheHit = l?.rag_cache_ops ?? {}
  const hit = cacheHit["hit"] ?? 0
  const miss = cacheHit["miss"] ?? 0
  const cacheRate = hit + miss > 0 ? hit / (hit + miss) : null
  const degrText = ["正常", "降级", "兜底"][l?.degradation_level ?? 0] ?? "未知"
  return [
    {
      label: "检索平均耗时",
      value: fmtMs(ragStep?.avg_ms ?? null),
      sub: `p95 ${fmtMs(ragStep?.p95_ms ?? null)} · 近 ${days.value} 天`,
    },
    {
      label: "FAQ 命中率",
      value: fmtPct(summary.value?.faq.hit_rate ?? null),
      sub: `近 ${days.value} 天窗口`,
    },
    {
      label: "检索缓存命中",
      value: fmtPct(cacheRate),
      sub: `hit ${fmtInt(hit)} / miss ${fmtInt(miss)}（进程启动后累计）`,
    },
    {
      label: "低置信占比",
      value: fmtPct(summary.value?.confidence.low_confidence_share ?? null),
      sub: `阈值 ${(summary.value?.confidence.threshold ?? 0).toFixed(2)}`,
    },
    {
      label: "系统状态",
      value: degrText,
      cls: (l?.degradation_level ?? 0) > 0 ? "stat-warn" : "stat-ok",
      sub: `Rerank 降级 ${fmtInt(Object.values(l?.rerank_degradation ?? {}).reduce((a, b) => a + b, 0))}`,
    },
    {
      label: "差评标记",
      value: fmtInt(l?.bad_cases_total),
      sub: `快答兜底 ${fmtInt(l?.fast_reply_total)}`,
    },
  ]
})

const faqHitRate = computed(() => summary.value?.faq.hit_rate ?? null)
const faqRateType = computed(() => (faqHitRate.value != null && faqHitRate.value >= 0.6 ? "success" : "warning"))

// ── 图表数据组装 ──
const sourceChartOpt = ref<EChartsCoreOption | null>(null)
const sourcePieOpt = ref<EChartsCoreOption | null>(null)
const faqChartOpt = ref<EChartsCoreOption | null>(null)
const volumeChartOpt = ref<EChartsCoreOption | null>(null)
const intentChartOpt = ref<EChartsCoreOption | null>(null)

const sourceChartEl = ref<HTMLElement | null>(null)
const sourcePieEl = ref<HTMLElement | null>(null)
const faqChartEl = ref<HTMLElement | null>(null)
const volumeChartEl = ref<HTMLElement | null>(null)
const intentChartEl = ref<HTMLElement | null>(null)

useChart(sourceChartEl, sourceChartOpt)
useChart(sourcePieEl, sourcePieOpt)
useChart(faqChartEl, faqChartOpt)
useChart(volumeChartEl, volumeChartOpt)
useChart(intentChartEl, intentChartOpt)

function buildCharts(s: RagQualitySummary) {
  const dates = [...new Set(s.response_source.daily.map((d) => d.date))].sort()
  const sources = [...new Set(s.response_source.daily.map((d) => d.source))]
  sourceChartOpt.value = {
    tooltip: { trigger: "axis" },
    legend: { top: 0 },
    grid: { left: 40, right: 16, top: 32, bottom: 28 },
    xAxis: { type: "category", data: dates },
    yAxis: { type: "value" },
    series: sources.map((src) => ({
      name: SOURCE_LABELS[src] ?? src,
      type: "line",
      stack: "total",
      areaStyle: { opacity: 0.25 },
      data: dates.map((d) => s.response_source.daily.find((x) => x.date === d && x.source === src)?.count ?? 0),
      itemStyle: { color: SOURCE_COLORS[src] },
    })),
  }

  sourcePieOpt.value = {
    tooltip: { trigger: "item" },
    legend: { orient: "vertical", right: 0, top: "middle" },
    series: [
      {
        type: "pie",
        radius: ["40%", "70%"],
        center: ["38%", "50%"],
        label: { show: false },
        data: s.response_source.total.map((x) => ({
          name: SOURCE_LABELS[x.source] ?? x.source,
          value: x.count,
          itemStyle: { color: SOURCE_COLORS[x.source] },
        })),
      },
    ],
  }

  const faqDates = [...new Set(s.faq.daily.map((d) => d.date))].sort()
  const matchTypes = ["exact", "semantic", "miss"]
  faqChartOpt.value = {
    tooltip: { trigger: "axis" },
    legend: { top: 0 },
    grid: { left: 40, right: 16, top: 32, bottom: 28 },
    xAxis: { type: "category", data: faqDates },
    yAxis: { type: "value" },
    series: [
      {
        name: "命中率",
        type: "line",
        smooth: true,
        data: faqDates.map((d) => {
          const dayRows = s.faq.daily.filter((x) => x.date === d)
          const hit = dayRows.filter((x) => x.match_type !== "miss").reduce((a, b) => a + b.count, 0)
          const all = dayRows.reduce((a, b) => a + b.count, 0)
          return all ? Math.round((hit / all) * 1000) / 10 : null
        }),
        itemStyle: { color: "#67c23a" },
        yAxisIndex: 0,
      },
      ...matchTypes.map((mt, i) => ({
        name: MATCH_LABELS[mt] ?? mt,
        type: "bar",
        stack: "count",
        data: faqDates.map((d) => s.faq.daily.find((x) => x.date === d && x.match_type === mt)?.count ?? 0),
        itemStyle: { color: ["#409eff", "#67c23a", "#f56c6c"][i], opacity: 0.55 },
      })),
    ],
  }

  volumeChartOpt.value = {
    tooltip: { trigger: "axis" },
    legend: { top: 0 },
    grid: { left: 48, right: 16, top: 32, bottom: 28 },
    xAxis: { type: "category", data: s.daily_volume.map((d) => d.date) },
    yAxis: [
      { type: "value", name: "轮次" },
      { type: "value", name: "会话" },
    ],
    series: [
      { name: "轮次", type: "bar", data: s.daily_volume.map((d) => d.turns), itemStyle: { color: "#409eff" } },
      {
        name: "会话",
        type: "line",
        yAxisIndex: 1,
        smooth: true,
        data: s.daily_volume.map((d) => d.sessions),
        itemStyle: { color: "#e6a23c" },
      },
    ],
  }

  const top = [...s.intent_top].reverse()
  intentChartOpt.value = {
    tooltip: { trigger: "axis" },
    grid: { left: 140, right: 32, top: 8, bottom: 28 },
    xAxis: { type: "value" },
    yAxis: { type: "category", data: top.map((x) => x.intent) },
    series: [
      {
        name: "出现次数",
        type: "bar",
        data: top.map((x) => x.count),
        itemStyle: { color: "#409eff" },
        label: { show: true, position: "right" },
      },
    ],
  }
}

async function loadSummary() {
  summaryLoading.value = true
  try {
    const res = await getRagQualitySummary(days.value)
    summary.value = res
    buildCharts(res)
  } catch {
    /* handled */
  } finally {
    summaryLoading.value = false
  }
}

async function loadLive() {
  try {
    live.value = await getRagLiveMetrics()
  } catch {
    /* handled */
  }
}

onMounted(() => {
  loadSummary()
  startLive()
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
.header-controls {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.live-indicator {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: var(--fs-sm);
  color: var(--color-success, #67c23a);
  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--color-success, #67c23a);
    animation: blink 1.2s infinite;
  }
}
@keyframes blink {
  50% { opacity: 0.3; }
}
.stat-cards {
  margin-top: 12px;
}
.stat-card {
  text-align: center;
  :deep(.el-card__body) {
    padding: var(--space-3);
  }
}
.stat-label {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}
.stat-value {
  font-size: 22px;
  font-weight: 600;
  margin: 4px 0;
  &.stat-ok { color: var(--color-success, #67c23a); }
  &.stat-warn { color: var(--color-warning, #e6a23c); }
}
.stat-sub {
  font-size: var(--fs-xs, 12px);
  color: var(--color-text-secondary);
}
.chart-lg { height: 320px; }
.chart-md { height: 260px; }
.header-tag { margin-left: 8px; }
</style>
