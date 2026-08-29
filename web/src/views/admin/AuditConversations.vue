<template>
  <div class="audit-page">
    <div class="page-header">
      <h2>对话审计</h2>
      <div class="header-controls">
        <el-input
          v-model="filters.session_id"
          placeholder="会话 ID"
          clearable
          size="small"
          style="width: 180px"
          @change="load"
        />
        <el-input
          v-model="filters.customer_id"
          placeholder="客户 ID"
          clearable
          size="small"
          style="width: 140px"
          @change="load"
        />
        <el-input
          v-model="filters.intent"
          placeholder="意图"
          clearable
          size="small"
          style="width: 140px"
          @change="load"
        />
        <el-select
          v-model="filters.response_source"
          placeholder="回复来源"
          clearable
          size="small"
          style="width: 130px"
          @change="load"
        >
          <el-option v-for="s in sourceOptions" :key="s" :label="s" :value="s" />
        </el-select>
        <el-date-picker
          v-model="dateRange"
          type="daterange"
          range-separator="→"
          start-placeholder="开始"
          end-placeholder="结束"
          size="small"
          style="width: 240px"
          value-format="YYYY-MM-DD"
          @change="load"
        />
        <el-button :loading="loading" @click="load">查询</el-button>
      </div>
    </div>

    <el-table :data="conversations" v-loading="loading" stripe style="margin-top: 16px" @row-click="openReplay">
      <el-table-column prop="session_id" label="会话" min-width="220" show-overflow-tooltip />
      <el-table-column prop="customer_id" label="客户" width="120" show-overflow-tooltip />
      <el-table-column prop="channel_type" label="渠道" width="80" />
      <el-table-column prop="turns" label="轮次" width="70" align="center" />
      <el-table-column prop="top_intent" label="主意图" width="160" show-overflow-tooltip>
        <template #default="{ row }">
          <el-tag v-if="row.top_intent" size="small" type="info">{{ row.top_intent }}</el-tag>
          <span v-else>-</span>
        </template>
      </el-table-column>
      <el-table-column label="平均置信度" width="110" align="center">
        <template #default="{ row }">
          <span :class="confClass(row.avg_bot_confidence)">{{ fmtConf(row.avg_bot_confidence) }}</span>
        </template>
      </el-table-column>
      <el-table-column label="错误" width="70" align="center">
        <template #default="{ row }">
          <el-badge v-if="row.errors > 0" :value="row.errors" type="danger" />
          <span v-else>0</span>
        </template>
      </el-table-column>
      <el-table-column label="开始时间" width="160">
        <template #default="{ row }">{{ formatTime(row.started_at) }}</template>
      </el-table-column>
      <el-table-column label="最后活跃" width="160">
        <template #default="{ row }">{{ formatTime(row.last_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="90" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click.stop="openReplay(row)">回放</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      v-model:current-page="page"
      v-model:page-size="pageSize"
      :total="total"
      :page-sizes="[10, 20, 50, 100]"
      layout="total, sizes, prev, pager, next"
      style="margin-top: 16px; justify-content: flex-end"
      @current-change="load"
      @size-change="onSizeChange"
    />

    <!-- 会话回放抽屉 -->
    <el-drawer
      v-model="replayVisible"
      :title="`会话回放 — ${replay?.session_id ?? ''}`"
      size="72%"
      destroy-on-close
    >
      <div v-loading="replayLoading">
        <template v-if="replay">
          <el-tabs v-model="activeTab">
            <el-tab-pane label="对话记录" name="turns">
              <div class="turn-list">
                <div
                  v-for="t in replay.turns"
                  :key="t.turn_id + t.timestamp"
                  class="turn-row"
                  :class="t.speaker === 'customer' ? 'turn-customer' : 'turn-bot'"
                >
                  <div class="bubble">
                    <div class="bubble-content">{{ t.content }}</div>
                    <div class="bubble-meta">
                      <el-tag v-if="t.intent" size="small" type="info">{{ t.intent }}</el-tag>
                      <el-tag v-if="t.response_source" size="small">{{ sourceLabel(t.response_source) }}</el-tag>
                      <span v-if="t.confidence != null" class="meta-num">置信 {{ fmtConf(t.confidence) }}</span>
                      <span class="meta-time">{{ formatTime(t.timestamp) }}</span>
                    </div>
                  </div>
                </div>
                <el-empty v-if="replay.turns.length === 0" description="无对话记录" />
              </div>
            </el-tab-pane>

            <el-tab-pane :label="`决策链 (${replay.decisions.length})`" name="decisions">
              <el-timeline>
                <el-timeline-item
                  v-for="d in replay.decisions"
                  :key="d.decision_id"
                  :timestamp="formatTime(d.created_at)"
                  placement="top"
                >
                  <div class="decision-card">
                    <div class="decision-head">
                      <el-tag size="small">{{ d.agent_name }}</el-tag>
                      <el-tag size="small" type="warning">{{ d.action }}</el-tag>
                      <span v-if="d.latency_ms != null" class="meta-num">{{ d.latency_ms }}ms</span>
                    </div>
                    <div class="decision-reason">{{ d.reasoning }}</div>
                    <pre v-if="d.evidence" class="decision-evidence">{{ JSON.stringify(d.evidence, null, 2) }}</pre>
                  </div>
                </el-timeline-item>
              </el-timeline>
              <el-empty v-if="replay.decisions.length === 0" description="无决策记录" />
            </el-tab-pane>

            <el-tab-pane :label="`处理记录 (${replay.messages.length})`" name="messages">
              <el-table :data="replay.messages" stripe size="small">
                <el-table-column label="时间" width="160">
                  <template #default="{ row }">{{ formatTime(row.created_at) }}</template>
                </el-table-column>
                <el-table-column prop="content" label="消息" min-width="220" show-overflow-tooltip />
                <el-table-column prop="intent" label="意图" width="140" show-overflow-tooltip />
                <el-table-column label="状态" width="90" align="center">
                  <template #default="{ row }">
                    <el-tag size="small" :type="statusType(row.processing_status)">
                      {{ statusText(row.processing_status) }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="耗时" width="90" align="right">
                  <template #default="{ row }">
                    {{ row.processing_duration_ms != null ? row.processing_duration_ms + "ms" : "-" }}
                  </template>
                </el-table-column>
                <el-table-column prop="source" label="来源" width="110" />
                <el-table-column prop="error_message" label="错误" min-width="160" show-overflow-tooltip />
              </el-table>
            </el-tab-pane>
          </el-tabs>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue"
import {
  listConversations,
  getConversationReplay,
  type ConversationItem,
  type ReplayResponse,
} from "@/api/console"

const conversations = ref<ConversationItem[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const loading = ref(false)

const filters = ref({
  session_id: "",
  customer_id: "",
  intent: "",
  response_source: "",
})
const dateRange = ref<[string, string] | null>(null)

const sourceOptions = ["knowledge", "tool", "llm", "template", "fallback", "clarify", "human"]

const replayVisible = ref(false)
const replayLoading = ref(false)
const replay = ref<ReplayResponse | null>(null)
const activeTab = ref("turns")

async function load() {
  loading.value = true
  try {
    const res = await listConversations({
      session_id: filters.value.session_id || undefined,
      customer_id: filters.value.customer_id || undefined,
      intent: filters.value.intent || undefined,
      response_source: filters.value.response_source || undefined,
      start: dateRange.value?.[0] ? `${dateRange.value[0]}T00:00:00` : undefined,
      end: dateRange.value?.[1] ? `${dateRange.value[1]}T23:59:59` : undefined,
      limit: pageSize.value,
      offset: (page.value - 1) * pageSize.value,
    })
    conversations.value = res.conversations
    total.value = res.total
  } catch {
    /* handled */
  } finally {
    loading.value = false
  }
}

function onSizeChange() {
  page.value = 1
  load()
}

async function openReplay(row: ConversationItem) {
  replayVisible.value = true
  replayLoading.value = true
  replay.value = null
  activeTab.value = "turns"
  try {
    replay.value = await getConversationReplay(row.session_id)
  } catch {
    /* handled */
  } finally {
    replayLoading.value = false
  }
}

function statusType(s: string | null): "success" | "warning" | "danger" | "info" {
  const m: Record<string, "success" | "warning" | "danger" | "info"> = {
    done: "success",
    queued: "info",
    processing: "warning",
    skipped: "info",
    error: "danger",
  }
  return (s && m[s]) || "info"
}

function statusText(s: string | null) {
  const m: Record<string, string> = {
    done: "完成",
    queued: "排队",
    processing: "处理中",
    skipped: "跳过",
    error: "错误",
  }
  return (s && m[s]) || s || "-"
}

function sourceLabel(s: string) {
  const m: Record<string, string> = {
    knowledge: "知识",
    tool: "工具",
    llm: "LLM",
    template: "模板",
    fallback: "兜底",
    clarify: "澄清",
    human: "人工",
  }
  return m[s] ?? s
}

function fmtConf(v: number | null) {
  return v == null ? "-" : `${Math.round(v * 100)}%`
}

function confClass(v: number | null) {
  if (v == null) return ""
  if (v < 0.5) return "conf-low"
  if (v < 0.7) return "conf-mid"
  return "conf-high"
}

function formatTime(s: string | null) {
  return s?.slice(0, 19).replace("T", " ") || "-"
}

onMounted(load)
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
  flex-wrap: wrap;
}
.conf-high { color: var(--color-success, #67c23a); }
.conf-mid { color: var(--color-warning, #e6a23c); }
.conf-low { color: var(--color-danger, #f56c6c); }

.turn-list {
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  max-height: calc(100vh - 260px);
  overflow-y: auto;
  padding: var(--space-2);
}
.turn-row {
  display: flex;
  &.turn-customer { justify-content: flex-start; }
  &.turn-bot { justify-content: flex-end; }
}
.bubble {
  max-width: 72%;
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius-md);
  background: var(--color-bg-page);
}
.turn-customer .bubble {
  background: var(--el-color-primary-light-9, #ecf5ff);
  border-top-left-radius: 4px;
}
.turn-bot .bubble {
  background: var(--el-color-success-light-9, #f0f9eb);
  border-top-right-radius: 4px;
}
.bubble-content {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: var(--fs-sm);
}
.bubble-meta {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  margin-top: var(--space-1);
  flex-wrap: wrap;
}
.meta-num {
  font-size: var(--fs-xs, 12px);
  color: var(--color-text-secondary);
}
.meta-time {
  font-size: var(--fs-xs, 12px);
  color: var(--color-text-secondary);
}
.decision-card {
  padding: var(--space-2);
  background: var(--color-bg-page);
  border-radius: var(--radius-md);
}
.decision-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.decision-reason {
  margin-top: var(--space-1);
  font-size: var(--fs-sm);
  color: var(--color-text-primary);
}
.decision-evidence {
  margin: var(--space-2) 0 0;
  padding: var(--space-2);
  background: var(--color-bg-page, #f5f7fa);
  border-radius: var(--radius-sm);
  font-size: var(--fs-xs, 12px);
  max-height: 200px;
  overflow: auto;
}
:deep(.el-pagination) {
  display: flex;
}
</style>
