<template>
  <div class="opslog-page">
    <div class="page-header">
      <h2>操作审计</h2>
      <div class="header-controls">
        <el-input
          v-model="filters.actor_id"
          placeholder="操作者"
          clearable
          size="small"
          style="width: 140px"
          @change="load"
        />
        <el-input
          v-model="filters.action"
          placeholder="操作类型 (如 session.transition)"
          clearable
          size="small"
          style="width: 220px"
          @change="load"
        />
        <el-select
          v-model="filters.target_type"
          placeholder="目标类型"
          clearable
          size="small"
          style="width: 120px"
          @change="load"
        >
          <el-option v-for="t in targetOptions" :key="t" :label="t" :value="t" />
        </el-select>
        <el-input
          v-model="filters.path_contains"
          placeholder="路径包含"
          clearable
          size="small"
          style="width: 180px"
          @change="load"
        />
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

    <el-table :data="logs" v-loading="loading" stripe style="margin-top: 16px">
      <el-table-column label="时间" width="170">
        <template #default="{ row }">{{ formatTime(row.timestamp) }}</template>
      </el-table-column>
      <el-table-column prop="actor_id" label="操作者" width="120" show-overflow-tooltip />
      <el-table-column prop="actor_role" label="角色" width="90">
        <template #default="{ row }">
          <el-tag size="small" :type="roleType(row.actor_role)">{{ row.actor_role }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="action" label="操作" min-width="180" show-overflow-tooltip />
      <el-table-column prop="method" label="方法" width="70" align="center" />
      <el-table-column prop="path" label="路径" min-width="200" show-overflow-tooltip />
      <el-table-column label="状态码" width="90" align="center">
        <template #default="{ row }">
          <span :class="row.status_code >= 400 ? 'status-error' : 'status-ok'">{{ row.status_code }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="ip_address" label="IP" width="130" show-overflow-tooltip />
      <el-table-column label="操作" width="80" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click="viewDetail(row)">详情</el-button>
        </template>
      </el-table-column>
    </el-table>

    <el-pagination
      v-model:current-page="page"
      v-model:page-size="pageSize"
      :total="total"
      :page-sizes="[20, 50, 100, 200]"
      layout="total, sizes, prev, pager, next"
      style="margin-top: 16px; justify-content: flex-end"
      @current-change="load"
      @size-change="onSizeChange"
    />

    <el-dialog v-model="detailVisible" title="操作详情" width="640px">
      <pre v-if="detail" class="detail-block">{{ JSON.stringify(detail, null, 2) }}</pre>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue"
import { listOperationLogs, type OperationLog } from "@/api/console"

const logs = ref<OperationLog[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(50)
const loading = ref(false)

const filters = ref({
  actor_id: "",
  action: "",
  target_type: "",
  path_contains: "",
})
const dateRange = ref<[string, string] | null>(null)

const targetOptions = ["session", "document", "faq", "feedback", "config", "user"]
const detailVisible = ref(false)
const detail = ref<OperationLog | null>(null)

async function load() {
  loading.value = true
  try {
    const res = await listOperationLogs({
      actor_id: filters.value.actor_id || undefined,
      action: filters.value.action || undefined,
      target_type: filters.value.target_type || undefined,
      path_contains: filters.value.path_contains || undefined,
      start: dateRange.value?.[0] ? `${dateRange.value[0]}T00:00:00` : undefined,
      end: dateRange.value?.[1] ? `${dateRange.value[1]}T23:59:59` : undefined,
      limit: pageSize.value,
      offset: (page.value - 1) * pageSize.value,
    })
    logs.value = res.logs
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

function viewDetail(row: OperationLog) {
  detail.value = row
  detailVisible.value = true
}

function roleType(r: string): "success" | "warning" | "danger" | "info" {
  const m: Record<string, "success" | "warning" | "danger" | "info"> = {
    admin: "danger",
    agent: "warning",
    customer: "success",
    service: "info",
  }
  return m[r] ?? "info"
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
.status-ok { color: var(--color-success, #67c23a); }
.status-error { color: var(--color-danger, #f56c6c); font-weight: 600; }
.detail-block {
  margin: 0;
  padding: var(--space-3);
  background: var(--color-bg-page);
  border-radius: var(--radius-md);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: var(--fs-sm);
  max-height: 420px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
}
:deep(.el-pagination) {
  display: flex;
}
</style>
