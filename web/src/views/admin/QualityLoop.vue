<template>
  <div class="quality-loop">
    <div class="page-header">
      <h2>质量闭环</h2>
      <span class="muted loop-sub">发现 → 归因 → 修复 → 根治 — 会话质检与案例处置合并为一条流水线</span>
    </div>

    <el-tabs v-model="activeTab" class="loop-tabs" @tab-change="onTabChange">
      <el-tab-pane name="qc">
        <template #label>
          会话质检
          <el-tag v-if="qcCount > 0" size="small" type="danger" effect="plain" class="tab-badge">{{ qcCount }}</el-tag>
        </template>
        <BadcaseWorkbench ref="qcRef" @qc-loaded="onQcLoaded" />
      </el-tab-pane>
      <el-tab-pane name="cases">
        <template #label>
          案例队列
          <el-tag v-if="caseCount > 0" size="small" type="warning" effect="plain" class="tab-badge">{{ caseCount }}</el-tag>
        </template>
        <CaseWorkbench ref="caseRef" @cases-loaded="onCasesLoaded" />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, watch } from "vue"
import { useRoute, useRouter } from "vue-router"
import BadcaseWorkbench from "@/views/admin/BadcaseWorkbench.vue"
import CaseWorkbench from "@/views/admin/CaseWorkbench.vue"

const router = useRouter()
const route = useRoute()
const activeTab = ref((route.query.tab as string) || "qc")
const qcRef = ref<InstanceType<typeof BadcaseWorkbench>>()
const caseRef = ref<InstanceType<typeof CaseWorkbench>>()
const qcCount = ref(0)
const caseCount = ref(0)

function onQcLoaded(total: number) { qcCount.value = total }
function onCasesLoaded(total: number) { caseCount.value = total }

function onTabChange(tab: string) {
  router.replace({ query: { ...route.query, tab } })
}

// 外部跳转带 tab 参数 → 切换到对应页签
watch(() => route.query.tab, (t) => {
  if (t && t !== activeTab.value) activeTab.value = t as string
})

onMounted(() => {
  if (route.query.tab) activeTab.value = route.query.tab as string
})
</script>

<style scoped lang="scss">
.quality-loop { padding: 0 4px; }
.page-header {
  display: flex; align-items: center; gap: 10px; margin-bottom: 8px;
  h2 { margin: 0; font-size: 16px; }
  .loop-sub { font-size: 12px; }
}
.loop-tabs {
  :deep(.el-tabs__header) { margin-bottom: 0; }
  :deep(.el-tabs__content) { padding-top: 8px; }
}
.tab-badge { margin-left: 4px; }
.muted { color: var(--color-text-muted, #909399); }
</style>
