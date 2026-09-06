<template>
  <div class="sim-page">
    <div class="page-header">
      <h2>
        对话模拟
        <span class="page-subtitle">虚拟客户黑盒压测 — 喂闭环 / 验链路 / 测延迟</span>
      </h2>
      <div class="header-actions">
        <el-button v-if="!status.running" type="primary" :loading="starting" @click="doStart">
          启动模拟
        </el-button>
        <el-button v-else type="danger" :loading="stopping" @click="doStop">停止模拟</el-button>
      </div>
    </div>

    <div class="status-card" :class="{ running: status.running }">
      <div class="status-left">
        <span class="status-dot" :class="status.running ? 'on' : 'off'"></span>
        <b>{{ status.running ? "运行中" : "已停止" }}</b>
        <span v-if="status.running" class="muted">
          {{ status.config.users }} 个虚拟客户 · 每 {{ status.config.interval }}s 一轮 · 已跑
          {{ Math.round((Date.now() / 1000 - status.stats.started_at) / 60) }} 分钟
        </span>
      </div>
      <div class="stat-grid">
        <div class="stat"><b>{{ status.stats.sessions }}</b><span>会话</span></div>
        <div class="stat"><b>{{ status.stats.turns }}</b><span>轮次</span></div>
        <div class="stat">
          <b>{{ status.stats.expect_checks ? Math.round((status.stats.expect_hits / status.stats.expect_checks) * 100) : 0 }}%</b>
          <span>期望命中</span>
        </div>
        <div class="stat"><b>{{ status.stats.latency_avg_ms || "-" }}</b><span>均延迟 ms</span></div>
        <div class="stat"><b>{{ status.stats.latency_p95_ms || "-" }}</b><span>P95 ms</span></div>
        <div class="stat"><b class="warn">{{ status.stats.feedbacks }}</b><span>差评(喂闭环)</span></div>
        <div class="stat"><b>{{ status.stats.abandoned }}</b><span>中途挂断</span></div>
        <div class="stat"><b class="warn">{{ status.stats.timeouts ?? 0 }}</b><span>超时自动结束</span></div>
        <div class="stat"><b class="warn">{{ status.stats.errors }}</b><span>错误</span></div>
      </div>
    </div>

    <el-row :gutter="12">
      <el-col :span="9">
        <div class="panel">
          <div class="panel-title">场景剧本（勾选后启动生效）</div>
          <el-checkbox-group v-model="selected">
            <div v-for="s in scenarios" :key="s.key" class="scenario-row">
              <el-checkbox :value="s.key" :disabled="status.running">
                <span class="sc-name">{{ s.name_zh }}</span>
                <span class="muted sc-meta">{{ s.turns }} 轮 · {{ s.variants }} 变体</span>
                <el-tag v-if="s.final_feedback === 'down'" size="small" type="danger" effect="plain">差评</el-tag>
                <el-tag v-for="t in s.tags.slice(0, 2)" :key="t" size="small" type="info" effect="plain">{{ t }}</el-tag>
              </el-checkbox>
            </div>
          </el-checkbox-group>
          <div class="panel-controls">
            <span class="muted">并发客户</span>
            <el-input-number v-model="users" :min="1" :max="10" size="small" :disabled="status.running" />
            <span class="muted">间隔(s)</span>
            <el-input-number v-model="interval" :min="2" :max="120" size="small" :disabled="status.running" />
          </div>
        </div>
      </el-col>
      <el-col :span="15">
        <div class="panel">
          <div class="panel-title">最近轮次（实时）</div>
          <el-table :data="recentRows" size="small" height="480">
            <el-table-column label="时间" width="80">
              <template #default="{ row }">{{ fmtTime(row.ts) }}</template>
            </el-table-column>
            <el-table-column prop="scenario" label="场景" width="130" />
            <el-table-column prop="text" label="客户输入" min-width="160" show-overflow-tooltip />
            <el-table-column prop="reply" label="Bot 回复" min-width="200" show-overflow-tooltip />
            <el-table-column label="延迟" width="80" align="center">
              <template #default="{ row }">{{ row.latency_ms }}</template>
            </el-table-column>
            <el-table-column label="命中" width="60" align="center">
              <template #default="{ row }">
                <el-tag v-if="row.expect_ok === true" size="small" type="success">✓</el-tag>
                <el-tag v-else-if="row.expect_ok === false" size="small" type="danger">✗</el-tag>
                <span v-else class="muted">-</span>
              </template>
            </el-table-column>
          </el-table>
        </div>
      </el-col>
    </el-row>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue"
import { ElMessage } from "element-plus"
import {
  listScenarios,
  startSimulator,
  stopSimulator,
  getSimulatorStatus,
  type ScenarioInfo,
  type SimulatorStatus,
} from "@/api/simulator"

const scenarios = ref<ScenarioInfo[]>([])
const selected = ref<string[]>([])
const users = ref(2)
const interval = ref(8)
const starting = ref(false)
const stopping = ref(false)
const status = ref<SimulatorStatus>({
  running: false,
  config: { scenario_keys: [], users: 2, interval: 8 },
  stats: {
    started_at: 0, sessions: 0, turns: 0, expect_hits: 0, expect_checks: 0,
    feedbacks: 0, errors: 0, abandoned: 0, timeouts: 0, latency_avg_ms: 0, latency_p95_ms: 0,
  },
  recent: [],
})

const recentRows = computed(() => [...status.value.recent].reverse())

let timer: ReturnType<typeof setInterval> | null = null

async function refresh() {
  try {
    status.value = await getSimulatorStatus()
  } catch {
    /* handled */
  }
}

async function doStart() {
  if (selected.value.length === 0) {
    ElMessage.warning("请至少勾选一个场景")
    return
  }
  starting.value = true
  try {
    status.value = await startSimulator({
      scenario_keys: selected.value,
      users: users.value,
      interval: interval.value,
    })
    ElMessage.success(`模拟已启动：${selected.value.length} 个场景 · ${users.value} 个虚拟客户`)
  } catch {
    /* handled */
  } finally {
    starting.value = false
  }
}

async function doStop() {
  stopping.value = true
  try {
    const final = await stopSimulator()
    status.value = final
    ElMessage.success(`已停止：共 ${final.stats.sessions} 会话 / ${final.stats.turns} 轮 / 差评 ${final.stats.feedbacks}`)
  } catch {
    /* handled */
  } finally {
    stopping.value = false
  }
}

function fmtTime(ts: number) {
  return new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false })
}

onMounted(async () => {
  try {
    const r = await listScenarios()
    scenarios.value = r.scenarios
    selected.value = r.scenarios.map((s) => s.key)
  } catch {
    /* handled */
  }
  refresh()
  timer = setInterval(refresh, 3000)
})

onUnmounted(() => {
  if (timer) clearInterval(timer)
})
</script>

<style scoped lang="scss">
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 14px;
}
.page-subtitle {
  font-size: var(--fs-sm);
  font-weight: 400;
  color: var(--color-text-secondary);
  margin-left: 8px;
}

.status-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 12px;
  padding: 12px 16px;
  margin-bottom: 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-fill-color-extra-light);
  &.running { border-color: var(--el-color-success-light-5); }
}
.status-left {
  display: flex;
  align-items: center;
  gap: 8px;
}
.status-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
  &.on { background: var(--el-color-success); box-shadow: 0 0 6px var(--el-color-success); }
  &.off { background: var(--el-color-info-light-5); }
}
.stat-grid {
  display: flex;
  gap: 22px;
  .stat {
    display: flex;
    flex-direction: column;
    align-items: center;
    b { font-size: 17px; }
    span { font-size: var(--fs-sm); color: var(--color-text-secondary); }
    .warn { color: var(--el-color-warning); }
  }
}

.panel {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 12px 14px;
  height: 540px;
  overflow: auto;
}
.panel-title {
  font-weight: 600;
  margin-bottom: 10px;
}
.panel-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 12px;
  padding-top: 10px;
  border-top: 1px dashed var(--el-border-color-lighter);
}
.scenario-row {
  padding: 3px 0;
  .sc-name { margin-right: 6px; }
  .sc-meta { margin-right: 6px; font-size: var(--fs-sm); }
  .el-tag { margin-right: 4px; transform: scale(0.85); }
}
.muted { color: var(--color-text-secondary); }
</style>
