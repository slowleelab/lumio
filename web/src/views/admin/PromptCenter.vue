<template>
  <div class="prompt-page">
    <div class="page-header">
      <h2>
        提示词中心
        <span class="page-subtitle">版本管理 · 发布/回滚即时生效 · 修复分流表 D·模型 的落地页</span>
      </h2>
      <div class="header-actions">
        <el-button size="small" @click="load">刷新</el-button>
      </div>
    </div>

    <el-alert type="info" :closable="false" class="hint">
      <template #title>
        对话生成类可在线编辑（草稿 → 发布，回滚即指针回拨，60 秒内全实例生效）；
        裁判口径 / 分类基线 / 安全红线为工程锁定，随代码发版——后台只读，防判定口径漂移与合规红线误改。
      </template>
    </el-alert>

    <el-table v-loading="loading" :data="filtered" size="small" class="prompt-table" @row-click="openDetail">
      <el-table-column label="提示词" min-width="170">
        <template #default="{ row }">
          <div class="prompt-name">{{ row.name }}</div>
        </template>
      </el-table-column>
      <el-table-column label="描述" min-width="320">
        <template #default="{ row }">
          <div class="prompt-desc">{{ row.description }}</div>
        </template>
      </el-table-column>
      <el-table-column label="运用场景" min-width="240">
        <template #default="{ row }">
          <div class="prompt-scene">{{ row.scene || "-" }}</div>
        </template>
      </el-table-column>
      <el-table-column label="类别" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="categoryType(row.category)" effect="plain">{{ categoryLabel(row.category) }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="当前版本" width="200">
        <template #default="{ row }">
          <span v-if="row.source === 'db'">
            v{{ row.active_version }}
            <span class="muted"> · {{ row.active_changelog || "-" }}</span>
          </span>
          <span v-else class="muted">随代码发版</span>
        </template>
      </el-table-column>
      <el-table-column label="管理方式" width="120">
        <template #default="{ row }">
          <el-tag v-if="row.editable" size="small" type="success" effect="plain">在线可管理</el-tag>
          <el-tag v-else size="small" type="info" effect="plain">工程锁定</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="版本数" width="80" prop="version_count" />
      <el-table-column label="更新时间" width="160">
        <template #default="{ row }">{{ fmtTime(row.updated_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="90">
        <template #default="{ row }">
          <el-button size="small" link type="primary" @click.stop="openDetail(row)">管理</el-button>
        </template>
      </el-table-column>
    </el-table>

    <!-- 详情抽屉 -->
    <el-dialog v-model="detailVisible" width="76%" top="5vh" :title="detail?.name ?? ''" class="prompt-center-dialog" :close-on-click-modal="false">
      <template v-if="detail">
        <div class="drawer-meta">
          <el-tag size="small" :type="categoryType(detail.category)" effect="plain">{{ categoryLabel(detail.category) }}</el-tag>
          <el-tag v-if="detail.editable" size="small" type="success" effect="plain">在线可管理</el-tag>
          <el-tag v-else size="small" type="info" effect="plain">工程锁定 · 只读</el-tag>
          <span class="muted">{{ detail.description }}</span>
        </div>
        <div v-if="detail.scene" class="drawer-meta scene-line">
          <span class="scene-label">运用场景</span>
          <span class="muted">{{ detail.scene }}</span>
        </div>

        <!-- 工程锁定: 只读内容 -->
        <template v-if="!detail.editable">
          <el-alert type="warning" :closable="false" class="hint">
            <template #title>
              该提示词属工程锁定类（裁判口径 / 分类基线 / 安全红线 / 高风险话术）。变更会使历史判定不可比或触碰合规红线，
              需走代码评审发版；此处仅作集中查阅。
            </template>
          </el-alert>
          <pre class="prompt-content">{{ detail.active_content }}</pre>
        </template>

        <!-- 可管理: 版本时间线 + 编辑 -->
        <template v-else>
          <div class="section-title">版本时间线 <span class="muted section-hint">(内容一经发布不可变; 回滚 = 指针回拨)</span></div>
          <el-timeline class="version-timeline">
            <el-timeline-item
              v-for="v in detail.versions"
              :key="v.id"
              :type="v.status === 'published' ? 'success' : v.status === 'draft' ? 'warning' : 'info'"
              :hollow="v.status !== 'published'"
            >
              <div class="version-head">
                <span class="version-no">v{{ v.version }}</span>
                <el-tag size="small" :type="v.status === 'published' ? 'success' : v.status === 'draft' ? 'warning' : 'info'" effect="plain">
                  {{ v.status === "published" ? "生效中" : v.status === "draft" ? "草稿" : "已归档" }}
                </el-tag>
                <span class="muted version-meta">{{ v.changelog || "（无变更说明）" }} · {{ v.created_by || "-" }} · {{ fmtTime(v.created_at) }}</span>
                <el-button v-if="v.status === 'draft'" size="small" link type="success" :loading="acting" @click="publish(v)">发布</el-button>
                <el-button v-else-if="v.status === 'archived'" size="small" link type="warning" :loading="acting" @click="rollback(v)">回滚到此版</el-button>
                <el-button v-if="v.status === 'draft'" size="small" link type="danger" :loading="acting" @click="removeDraft(v)">删除</el-button>
                <el-button v-if="v.version !== detail.active_version" size="small" link type="primary" @click="toggleDiff(v)">
                  {{ diffWith === v.version ? "收起对比" : "对比当前" }}
                </el-button>
              </div>
              <div v-if="diffWith === v.version" class="diff-box">
                <div
                  v-for="(line, i) in diffLines(v.content, detail.active_content)"
                  :key="i"
                  class="diff-line"
                  :class="line.op === '+' ? 'plus' : 'minus'"
                >{{ line.op === "+" ? "+ " : "- " }}{{ line.text }}</div>
              </div>
            </el-timeline-item>
          </el-timeline>

          <div class="section-title">编辑新版本 <span class="muted section-hint">(基于当前生效内容; 保存为草稿, 不立即生效)</span></div>
          <el-input v-model="draftContent" type="textarea" :rows="14" :disabled="acting" spellcheck="false" />
          <div class="draft-row">
            <el-input v-model="draftChangelog" placeholder="变更说明 (必填, 便于回溯)" size="small" style="width: 300px" />
            <div class="draft-stats muted">
              {{ draftContent.length }} 字符 · 约 {{ tokensEst }} tokens
              <span v-if="previewIssues.length" class="issue">⚠ {{ previewIssues[0] }}</span>
            </div>
          </div>
          <div class="draft-actions">
            <el-button size="small" :loading="acting" :disabled="!draftChangelog.trim()" @click="saveDraft">保存草稿</el-button>
            <el-button size="small" @click="draftContent = detail.active_content">重置为当前版本</el-button>
          </div>
          <el-alert type="warning" :closable="false" class="hint">
            <template #title>安全红线段（身份/凭证/能力边界）随代码发版拼接在生成内容之后，不受此编辑影响。</template>
          </el-alert>
        </template>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue"
import { ElMessage, ElMessageBox } from "element-plus"
import {
  createDraft,
  deleteDraft,
  getPromptDetail,
  listPrompts,
  previewPrompt,
  publishVersion,
  rollbackVersion,
  type PromptDetail,
  type PromptItem,
  type PromptVersionItem,
} from "@/api/prompts"

const loading = ref(false)
const items = ref<PromptItem[]>([])
const detailVisible = ref(false)
const detail = ref<PromptDetail | null>(null)
const acting = ref(false)
const draftContent = ref("")
const draftChangelog = ref("")
const diffWith = ref<number | null>(null)
const previewIssues = ref<string[]>([])
const tokensEst = ref(0)

const CATEGORY_LABELS: Record<string, string> = {
  generation: "对话生成",
  judge: "裁判口径",
  classify: "分类基线",
  safety: "安全红线",
  script: "固定话术",
}
const CATEGORY_TYPES: Record<string, string> = {
  generation: "success",
  judge: "warning",
  classify: "primary",
  safety: "danger",
  script: "info",
}

function categoryLabel(c: string) {
  return CATEGORY_LABELS[c] ?? c
}
function categoryType(c: string) {
  return CATEGORY_TYPES[c] ?? "info"
}

const filtered = computed(() => items.value)

async function load() {
  loading.value = true
  try {
    const data = await listPrompts()
    items.value = data.items
  } finally {
    loading.value = false
  }
}

async function openDetail(row: PromptItem) {
  diffWith.value = null
  const data = await getPromptDetail(row.name)
  detail.value = data
  draftContent.value = data.active_content
  draftChangelog.value = ""
  previewIssues.value = []
  detailVisible.value = true
}

// 编辑区实时预览校验 (防抖走 watch)
let previewTimer: ReturnType<typeof setTimeout> | null = null
watch(draftContent, (v) => {
  if (!detail.value?.editable) return
  if (previewTimer) clearTimeout(previewTimer)
  previewTimer = setTimeout(async () => {
    if (!detail.value) return
    try {
      const data = await previewPrompt(detail.value.name, v)
      previewIssues.value = data.issues
      tokensEst.value = data.tokens_est
    } catch {
      /* 预览失败不打断编辑 */
    }
  }, 500)
})

async function saveDraft() {
  if (!detail.value) return
  acting.value = true
  try {
    await createDraft(detail.value.name, {
      content: draftContent.value,
      changelog: draftChangelog.value.trim(),
    })
    ElMessage.success("草稿已保存 (未生效)")
    draftChangelog.value = ""
    await reloadDetail()
  } finally {
    acting.value = false
  }
}

async function publish(v: PromptVersionItem) {
  await ElMessageBox.confirm(
    `发布 v${v.version} 后立即生效 (当前客户下一轮起使用新版本), 原 v${detail.value?.active_version} 归档。确认发布?`,
    "发布确认",
    { type: "warning" }
  )
  acting.value = true
  try {
    if (!detail.value) return
    const data = await publishVersion(detail.value.name, v.id)
    ElMessage.success(`已发布: v${data.active_version} 生效中`)
    await reloadDetail()
  } finally {
    acting.value = false
  }
}

async function rollback(v: PromptVersionItem) {
  await ElMessageBox.confirm(
    `指针回拨到 v${v.version}, 当前 v${detail.value?.active_version} 归档。确认回滚?`,
    "回滚确认",
    { type: "warning" }
  )
  acting.value = true
  try {
    if (!detail.value) return
    const data = await rollbackVersion(detail.value.name, v.id)
    ElMessage.success(`已回滚: v${data.active_version} 生效中`)
    await reloadDetail()
  } finally {
    acting.value = false
  }
}

async function removeDraft(v: PromptVersionItem) {
  await ElMessageBox.confirm(`删除草稿 v${v.version}? (历史版本不可删, 仅草稿可删)`, "删除草稿", { type: "warning" })
  acting.value = true
  try {
    if (!detail.value) return
    await deleteDraft(detail.value.name, v.id)
    ElMessage.success("草稿已删除")
    await reloadDetail()
  } finally {
    acting.value = false
  }
}

async function reloadDetail() {
  if (!detail.value) return
  const data = await getPromptDetail(detail.value.name)
  detail.value = data
  draftContent.value = data.active_content
}

function toggleDiff(v: PromptVersionItem) {
  diffWith.value = diffWith.value === v.version ? null : v.version
}

// 行级 diff (LCS): old = 历史版本, cur = 当前生效; "-" 旧版独有(将恢复), "+" 当前独有(将移除)
function diffLines(oldText: string, curText: string): { op: "+" | "-"; text: string }[] {
  const a = oldText.split("\n")
  const b = curText.split("\n")
  const n = a.length
  const m = b.length
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array<number>(m + 1).fill(0))
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }
  const out: { op: "+" | "-"; text: string }[] = []
  let i = 0
  let j = 0
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      i++
      j++
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      out.push({ op: "-", text: a[i++] })
    } else {
      out.push({ op: "+", text: b[j++] })
    }
  }
  while (i < n) out.push({ op: "-", text: a[i++] })
  while (j < m) out.push({ op: "+", text: b[j++] })
  return out
}

function fmtTime(t: string | null) {
  if (!t) return "-"
  return t.slice(0, 16).replace("T", " ")
}

onMounted(load)
</script>

<style scoped>
.prompt-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
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

.prompt-table :deep(.el-table__row) {
  cursor: pointer;
}

.prompt-name {
  font-family: var(--font-mono, monospace);
  font-size: 13px;
}

.prompt-desc {
  font-size: 12px;
  line-height: 1.5;
}

.prompt-scene {
  font-size: 12px;
  color: var(--color-text-muted, #909399);
  line-height: 1.5;
}

.drawer-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 14px;
  flex-wrap: wrap;
}

.scene-line {
  margin-bottom: 6px;
}

.scene-label {
  font-size: 12px;
  font-weight: 600;
  color: var(--el-color-primary);
}

.section-title {
  font-weight: 600;
  margin: 18px 0 10px;
}

.section-hint {
  font-weight: normal;
  font-size: 12px;
}

.prompt-content {
  background: var(--color-bg-surface, #f5f7fa);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 14px;
  font-size: 12px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 60vh;
  overflow: auto;
  margin: 0;
}

.version-timeline {
  padding-left: 4px;
}

.version-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.version-no {
  font-weight: 600;
  font-family: var(--font-mono, monospace);
}

.version-meta {
  font-size: 12px;
}

.diff-box {
  margin-top: 8px;
  background: var(--color-bg-surface, #f5f7fa);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 10px;
  font-family: var(--font-mono, monospace);
  font-size: 12px;
  line-height: 1.6;
  max-height: 320px;
  overflow: auto;
}

.diff-line {
  white-space: pre-wrap;
  word-break: break-word;
}

.diff-line.plus {
  color: var(--el-color-success);
  background: var(--el-color-success-light-9);
}

.diff-line.minus {
  color: var(--el-color-danger);
  background: var(--el-color-danger-light-9);
}

.draft-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin: 10px 0;
}

.draft-stats {
  font-size: 12px;
}

.draft-stats .issue {
  color: var(--el-color-danger);
}

.draft-actions {
  display: flex;
  gap: 10px;
  margin-bottom: 14px;
}

.muted {
  color: var(--color-text-muted, #909399);
}
.prompt-center-dialog :deep(.el-dialog__body) {
  height: 76vh;
  overflow: auto;
  padding-top: 12px;
}
</style>
