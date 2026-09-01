<template>
  <div class="intent-lib-page">
    <div class="page-header">
      <h2>意图库管理</h2>
      <div class="header-actions">
        <el-button type="primary" size="small" @click="addVisible = true">新增种子样本</el-button>
      </div>
    </div>

    <el-tabs v-model="activeTab">
      <!-- 意图树 -->
      <el-tab-pane label="意图树" name="tree">
        <el-input
          v-model="treeFilter"
          placeholder="过滤意图（中文名或 slug）"
          size="small"
          clearable
          style="width: 280px; margin-bottom: 12px"
        />
        <el-tree
          v-loading="treeLoading"
          :data="treeData"
          :props="{ label: 'label', children: 'children' }"
          node-key="key"
          default-expand-all
          :filter-node-method="filterTreeNode"
          ref="treeRef"
        >
          <template #default="{ data }">
            <span v-if="data.type === 'intent'" class="tree-intent">
              <span class="intent-name">{{ data.label }}</span>
              <span class="intent-slug">{{ data.slug }}</span>
              <el-tooltip v-if="data.definition" :content="data.definition" placement="top">
                <el-icon class="intent-info"><InfoFilled /></el-icon>
              </el-tooltip>
            </span>
            <span v-else class="tree-group-label">
              {{ data.label }}
              <el-tag size="small" type="info" effect="plain" class="tree-count">{{ data.count }}</el-tag>
            </span>
          </template>
        </el-tree>
      </el-tab-pane>

      <!-- 种子样本 -->
      <el-tab-pane label="种子样本" name="seeds">
        <div class="seeds-controls">
          <el-select v-model="seedIntentFilter" placeholder="按意图过滤" clearable filterable size="small" style="width: 220px" @change="loadSeeds">
            <el-option v-for="i in allIntentLabels" :key="i" :label="i" :value="i" />
          </el-select>
          <span class="seed-total">共 {{ seedTotal }} 条</span>
          <el-popconfirm title="确认删除该种子样本？" confirm-button-text="删除" @confirm="doDelete">
            <template #reference>
              <el-button size="small" type="danger" plain>删除选中</el-button>
            </template>
          </el-popconfirm>
        </div>
        <el-table
          
          :data="seeds"
          v-loading="seedLoading"
          stripe size="small"
          @selection-change="onSeedSelection"
        >
          <el-table-column type="selection" width="40" />
          <el-table-column prop="text" label="文本" min-width="240" show-overflow-tooltip />
          <el-table-column prop="intent" label="意图" width="160" />
        </el-table>
      </el-tab-pane>

      <!-- 属性表 -->
      <el-tab-pane label="属性表" name="attributes">
        <div class="attr-warning">
          属性表为只读总览（五域/组/交易性质），修改需走代码变更 + 双人复核流程。
        </div>
        <el-table :data="attrRows" v-loading="attrLoading" stripe size="small" max-height="600">
          <el-table-column prop="name_zh" label="中文名" width="130" show-overflow-tooltip />
          <el-table-column prop="intent" label="意图 slug" min-width="200" show-overflow-tooltip sortable />
          <el-table-column prop="domain" label="五域" width="120" />
          <el-table-column prop="group" label="子域组" width="160" />
          <el-table-column label="交易性质" width="160">
            <template #default="{ row }">
              <el-tag v-if="row.traffic_class" size="small" :type="trafficTag(row.traffic_class)">
                {{ trafficLabel(row.traffic_class) }}
              </el-tag>
              <span v-else class="muted">咨询(无)</span>
            </template>
          </el-table-column>
          <el-table-column label="触碰账户" width="90" align="center">
            <template #default="{ row }">
              <el-tag v-if="row.touches_account" size="small" type="danger">是</el-tag>
              <el-tag v-else size="small" type="info">否</el-tag>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <!-- 新增种子弹窗 -->
    <el-dialog v-model="addVisible" title="新增种子样本" width="480px">
      <el-form label-width="80px" size="small">
        <el-form-item label="意图" required>
          <el-select v-model="newSeed.intent" filterable placeholder="选择或输入意图" style="width: 100%">
            <el-option v-for="i in allIntentLabels" :key="i" :label="i" :value="i" />
          </el-select>
        </el-form-item>
        <el-form-item label="文本" required>
          <el-input v-model="newSeed.text" placeholder="客户会说的原话" type="textarea" :rows="2" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="addVisible = false">取消</el-button>
        <el-button type="primary" :loading="addLoading" @click="doAdd">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue"
import { ElMessage } from "element-plus"
import { InfoFilled } from "@element-plus/icons-vue"
import {
  getIntentTree,
  listSeeds,
  addSeed,
  deleteSeed,
  getAttributeTable,
  type SeedExample,
  type AttributeRow,
} from "@/api/intentLibrary"

const activeTab = ref("tree")

// ── 意图树 (el-tree 三级: 域 → 组 → 叶子) ──
interface TreeNode {
  key: string
  label: string
  type: "domain" | "group" | "intent"
  children?: TreeNode[]
  slug?: string
  definition?: string
  count?: number
}

const treeData = ref<TreeNode[]>([])
const treeLoading = ref(false)
const treeFilter = ref("")
const treeRef = ref()

const DOMAIN_LABELS: Record<string, string> = {
  query: "查询域（非金融）",
  transaction: "交易域（金融类）",
  consulting: "咨询域",
  service: "服务域",
  chitchat: "闲聊域",
}

const GROUP_LABELS: Record<string, string> = {
  A1_account_query: "账户查询",
  A2_bill_query: "账单查询",
  A3_progress_query: "进度查询",
  B1_funds: "资金类",
  B2_account_change: "账户变更类",
  C1_product: "产品咨询",
  C2_business: "业务咨询",
  C3_dispute: "争议咨询",
  D1_transfer: "人工转接",
  D2_complaint: "投诉建议",
  E_chitchat: "闲聊",
}

async function loadTree() {
  treeLoading.value = true
  try {
    const res = await getIntentTree()
    const nodes: TreeNode[] = []
    for (const [dom, dv] of Object.entries(res.domains)) {
      const domNode: TreeNode = {
        key: dom,
        label: DOMAIN_LABELS[dom] ?? dom,
        type: "domain",
        children: [],
      }
      for (const [grp, gv] of Object.entries(dv.groups)) {
        const intents = (gv as { intents: { intent: string; name_zh?: string; definition?: string }[] }).intents
        domNode.children!.push({
          key: `${dom}/${grp}`,
          label: GROUP_LABELS[grp] ?? grp,
          type: "group",
          count: intents.length,
          children: intents.map((it) => ({
            key: `${dom}/${grp}/${it.intent}`,
            label: it.name_zh ? `${it.name_zh}` : it.intent,
            slug: it.intent,
            definition: it.definition,
            type: "intent" as const,
          })),
        })
      }
      nodes.push(domNode)
    }
    treeData.value = nodes
  } catch {
    /* handled */
  } finally {
    treeLoading.value = false
  }
}

function filterTreeNode(value: string, data: TreeNode) {
  if (!value) return true
  const v = value.toLowerCase()
  return (
    data.label.toLowerCase().includes(v) ||
    (data.slug ?? "").toLowerCase().includes(v)
  )
}

// ── 种子样本 ──
const seeds = ref<SeedExample[]>([])
const seedTotal = ref(0)
const seedLoading = ref(false)
const seedIntentFilter = ref("")
const selectedSeeds = ref<SeedExample[]>([])
const addVisible = ref(false)
const addLoading = ref(false)
const newSeed = ref({ intent: "", text: "" })

const allIntentLabels = computed(() => {
  const labels = new Set<string>()
  const walk = (nodes: TreeNode[]) => {
    for (const n of nodes) {
      if (n.type === "intent" && n.slug) labels.add(n.slug)
      if (n.children) walk(n.children)
    }
  }
  walk(treeData.value)
  return [...labels].sort()
})

function onSeedSelection(rows: SeedExample[]) {
  selectedSeeds.value = rows
}

async function loadSeeds() {
  seedLoading.value = true
  try {
    const res = await listSeeds(seedIntentFilter.value || undefined)
    seeds.value = res.examples
    seedTotal.value = res.total
  } catch {
    /* handled */
  } finally {
    seedLoading.value = false
  }
}

async function doAdd() {
  if (!newSeed.value.intent || !newSeed.value.text.trim()) {
    ElMessage.warning("意图和文本必填")
    return
  }
  addLoading.value = true
  try {
    const r = await addSeed(newSeed.value.intent, newSeed.value.text.trim())
    if (r.duplicate) {
      ElMessage.warning("该样本已存在，已跳过")
    } else {
      ElMessage.success("已添加")
    }
    addVisible.value = false
    loadSeeds()
  } catch {
    /* handled */
  } finally {
    addLoading.value = false
  }
}

async function doDelete() {
  if (selectedSeeds.value.length === 0) return
  try {
    for (const s of selectedSeeds.value) {
      await deleteSeed(s.intent, s.text)
    }
    ElMessage.success(`已删除 ${selectedSeeds.value.length} 条`)
    selectedSeeds.value = []
    loadSeeds()
  } catch {
    /* handled */
  }
}

// ── 属性表 ──
const attrRows = ref<AttributeRow[]>([])
const attrLoading = ref(false)

async function loadAttr() {
  attrLoading.value = true
  try {
    const r = await getAttributeTable()
    attrRows.value = r.rows
  } catch {
    /* handled */
  } finally {
    attrLoading.value = false
  }
}

// ── 通用 ──
function trafficLabel(s: string) {
  const m: Record<string, string> = {
    financial_transaction: "金融交易",
    read_only_query: "只读查询",
    high_risk: "高风险",
  }
  return m[s] ?? s
}

function trafficTag(s: string): string {
  const m: Record<string, string> = {
    financial_transaction: "danger",
    read_only_query: "",
    high_risk: "danger",
  }
  return m[s] ?? "info"
}

watch(treeFilter, (v) => {
  treeRef.value?.filter(v)
})

onMounted(() => {
  loadTree()
  loadSeeds()
  loadAttr()
})
</script>

<style scoped lang="scss">
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.header-actions {
  display: flex;
  gap: 8px;
}
.seeds-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}
.seed-total {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}
.attr-warning {
  padding: 8px 12px;
  margin-bottom: 12px;
  background: var(--el-color-warning-light-9, #fdf6ec);
  border-radius: 6px;
  font-size: var(--fs-sm);
  color: var(--el-color-warning-dark-2, #b8860b);
}
.muted { color: var(--color-text-secondary); }

.tree-intent {
  display: inline-flex;
  align-items: center;
  gap: 6px;

  .intent-name { font-size: 13px; }
  .intent-slug {
    font-size: 11px;
    color: var(--color-text-secondary);
    font-family: ui-monospace, Menlo, monospace;
  }
  .intent-info {
    color: var(--color-text-placeholder);
    cursor: help;
    font-size: 13px;
  }
}

.tree-group-label {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-weight: 500;
  .tree-count { transform: scale(0.85); }
}
</style>
