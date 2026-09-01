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
              <span class="intent-name" :class="{ 'intent-deprecated': data.state === 'deprecated' }">{{ data.label }}</span>
              <span class="intent-slug">{{ data.slug }}</span>
              <el-tag v-if="data.source === 'registry'" size="small" type="warning" effect="plain" class="tree-reg">运营</el-tag>
              <el-tag v-if="data.state === 'shadow'" size="small" type="info" effect="plain" class="tree-reg">影子</el-tag>
              <el-tag v-else-if="data.state === 'deprecated'" size="small" type="info" effect="plain" class="tree-reg">已下线</el-tag>
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
          属性表为只读总览（五域/组/交易性质）；出厂意图修改需代码变更，运营新增意图在「注册表」页签管理。
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
          <el-table-column label="来源" width="80" align="center">
            <template #default="{ row }">
              <el-tag v-if="row.source === 'registry'" size="small" type="warning" effect="plain">运营</el-tag>
              <span v-else class="muted">出厂</span>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>

      <!-- 运营意图注册表 (流派二生命周期) -->
      <el-tab-pane name="registry">
        <template #label>
          意图注册表
          <el-badge v-if="registryEntries.length" :value="registryEntries.length" class="tab-badge" />
        </template>

        <!-- 索引状态卡 -->
        <div class="index-card">
          <div class="index-info">
            <span class="index-title">L2 向量索引</span>
            <el-tag size="small" :type="indexStatus.running ? 'warning' : indexStatus.error ? 'danger' : 'success'">
              {{ indexStatus.running ? "重建中…" : indexStatus.error ? "异常" : "就绪" }}
            </el-tag>
            <span class="muted">版本 v{{ indexStatus.version || "-" }}</span>
            <span class="muted">{{ indexStatus.entities }} 条种子</span>
            <span v-if="indexStatus.error" class="index-error">{{ indexStatus.error }}</span>
          </div>
          <div class="index-actions">
            <el-button size="small" :loading="indexStatus.running" @click="doRebuildIndex">蓝绿重建</el-button>
            <el-popconfirm title="回滚到上一版本索引？" confirm-button-text="回滚" @confirm="doRollbackIndex">
              <template #reference>
                <el-button size="small" type="warning" plain>回滚上一版</el-button>
              </template>
            </el-popconfirm>
          </div>
        </div>

        <div class="registry-controls">
          <el-select v-model="registryStateFilter" placeholder="按状态过滤" clearable size="small" style="width: 160px" @change="loadRegistry">
            <el-option v-for="(label, s) in STATE_LABELS" :key="s" :label="label" :value="s" />
          </el-select>
          <span class="seed-total">共 {{ registryEntries.length }} 条</span>
          <el-button size="small" type="primary" @click="openCreateDialog">新增意图</el-button>
        </div>

        <el-table :data="registryEntries" v-loading="registryLoading" stripe size="small">
          <el-table-column prop="name_zh" label="中文名" width="120" show-overflow-tooltip />
          <el-table-column prop="slug" label="slug" min-width="160" show-overflow-tooltip />
          <el-table-column label="五域" width="110">
            <template #default="{ row }">
              <span>{{ DOMAIN_LABELS[row.domain] ?? row.domain }}</span>
            </template>
          </el-table-column>
          <el-table-column label="状态" width="110">
            <template #default="{ row }">
              <el-tag size="small" :type="STATE_TAG[row.state] ?? 'info'">{{ STATE_LABELS[row.state] ?? row.state }}</el-tag>
            </template>
          </el-table-column>
          <el-table-column label="种子" width="60" align="center">
            <template #default="{ row }">{{ row.seeds.length }}</template>
          </el-table-column>
          <el-table-column label="命中 (影子/生效)" width="120" align="center">
            <template #default="{ row }">{{ row.shadow_hits }} / {{ row.active_hits }}</template>
          </el-table-column>
          <el-table-column prop="created_by" label="登记人" width="100" show-overflow-tooltip />
          <el-table-column label="评测" width="200">
            <template #default="{ row }">
              <template v-if="row.eval_report">
                <el-tag size="small" :type="row.eval_report.passed ? 'success' : 'danger'">
                  {{ row.eval_report.passed ? "通过" : "未过" }}
                </el-tag>
                <span v-if="row.eval_report.overlap?.max_similarity != null" class="muted eval-detail">
                  重叠{{ row.eval_report.overlap.max_similarity }}
                </span>
                <span v-if="row.eval_report.golden?.drop != null" class="muted eval-detail">
                  回归{{ row.eval_report.golden.drop }}
                </span>
                <span v-else-if="row.eval_report.error" class="index-error">{{ row.eval_report.error }}</span>
              </template>
              <span v-else class="muted">未评测</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="290" fixed="right">
            <template #default="{ row }">
              <el-button v-if="row.state === 'draft'" size="small" type="primary" link @click="doSubmit(row)">提交评审</el-button>
              <el-button v-if="row.state === 'draft' || row.state === 'eval_failed'" size="small" link @click="openEditDialog(row)">编辑</el-button>
              <el-button v-if="row.state === 'pending_review'" size="small" type="success" link @click="doReview(row, true)">通过</el-button>
              <el-button v-if="row.state === 'pending_review'" size="small" type="danger" link @click="doReview(row, false)">驳回</el-button>
              <el-button v-if="row.state === 'eval_failed'" size="small" type="warning" link @click="doEvaluate(row)">重跑评测</el-button>
              <el-button v-if="row.state === 'shadow'" size="small" type="success" link @click="doActivate(row)">激活生效</el-button>
              <el-button v-if="row.state === 'shadow'" size="small" type="danger" link @click="doReject(row)">驳回</el-button>
              <el-button v-if="row.state === 'active'" size="small" type="danger" link @click="doDeprecate(row)">下线</el-button>
              <el-button size="small" link @click="openHistory(row)">历史</el-button>
            </template>
          </el-table-column>
        </el-table>
      </el-tab-pane>
    </el-tabs>

    <!-- 新增/编辑意图弹窗 -->
    <el-dialog v-model="createVisible" :title="editingSlug ? '编辑意图（' + editingSlug + '）' : '新增意图（登记 → 评审 → 评测 → 影子 → 生效）'" width="620px">
      <el-form :model="intentForm" label-width="92px" size="default">
        <el-form-item label="slug" required>
          <el-input v-model="intentForm.slug" :disabled="!!editingSlug" placeholder="小写字母开头 a-z0-9_，3-64 位，如 fx_rate_query" />
        </el-form-item>
        <el-form-item label="中文名" required>
          <el-input v-model="intentForm.name_zh" placeholder="如：外汇牌价查询" />
        </el-form-item>
        <el-form-item label="定义">
          <el-input v-model="intentForm.definition" placeholder="一句话定义（悬停树节点可见）" />
        </el-form-item>
        <el-form-item label="五域" required>
          <el-select v-model="intentForm.domain" :disabled="!!editingSlug" style="width: 100%">
            <el-option v-for="(label, d) in DOMAIN_LABELS" :key="d" :label="label" :value="d" />
          </el-select>
        </el-form-item>
        <el-form-item label="子域组">
          <el-select v-model="intentForm.group" :disabled="!!editingSlug" clearable style="width: 100%">
            <el-option v-for="(label, g) in GROUP_LABELS" :key="g" :label="label" :value="g" />
          </el-select>
        </el-form-item>
        <el-form-item label="交易性质">
          <el-select v-model="intentForm.traffic_class" clearable placeholder="默认按域推导" style="width: 100%">
            <el-option label="金融交易" value="financial_transaction" />
            <el-option label="只读查询" value="read_only_query" />
            <el-option label="高风险(转人工)" value="high_risk" />
          </el-select>
        </el-form-item>
        <el-form-item label="种子样本" required>
          <el-input
            v-model="intentForm.seedsText"
            type="textarea"
            :rows="8"
            placeholder="每行一条用户问法，最少 10 条（建议 20 条）。&#10;评审通过后自动跑评测闸门：重叠检测 + 金标回归"
          />
          <div class="seed-count-hint" :class="{ 'seed-count-bad': seedLineCount < 10 }">
            当前 {{ seedLineCount }} 条（最少 10 条）
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="createLoading" @click="doCreateOrUpdate">{{ editingSlug ? "保存" : "登记 (draft)" }}</el-button>
      </template>
    </el-dialog>

    <!-- 审计历史抽屉 -->
    <el-drawer v-model="historyVisible" :title="'生命周期历史 — ' + (historyEntry?.slug ?? '')" size="460px">
      <el-timeline v-if="historyEntry">
        <el-timeline-item
          v-for="(h, i) in [...historyEntry.history].reverse()"
          :key="i"
          :timestamp="formatTs(h.ts)"
          :type="h.action === 'gates_fail' || h.action === 'reject' ? 'danger' : h.action === 'activate' ? 'success' : 'primary'"
        >
          <div>
            <b>{{ ACTION_LABELS[h.action] ?? h.action }}</b>
            <span class="muted"> by {{ h.actor }}</span>
          </div>
          <div v-if="h.note" class="history-note">{{ h.note }}</div>
        </el-timeline-item>
      </el-timeline>
    </el-drawer>

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
import { computed, onMounted, onUnmounted, ref, watch } from "vue"
import { ElMessage, ElMessageBox } from "element-plus"
import { InfoFilled } from "@element-plus/icons-vue"
import {
  getIntentTree,
  listSeeds,
  addSeed,
  deleteSeed,
  getAttributeTable,
  listRegistry,
  createRegistryIntent,
  updateRegistryIntent,
  submitRegistryIntent,
  reviewRegistryIntent,
  evaluateRegistryIntent,
  activateRegistryIntent,
  deprecateRegistryIntent,
  rejectRegistryIntent,
  getIndexStatus,
  rebuildIndex,
  rollbackIndex,
  type SeedExample,
  type AttributeRow,
  type RegistryEntry,
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
  source?: "factory" | "registry"
  state?: string
}

const treeData = ref<TreeNode[]>([])
const treeLoading = ref(false)
const treeFilter = ref("")
const treeRef = ref()

const DOMAIN_LABELS: Record<string, string> = {
  query: "查询域（只读查询）",
  transaction: "交易域（资金/账户变更）",
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
        const intents = (
          gv as {
            intents: {
              intent: string
              name_zh?: string
              definition?: string
              source?: "factory" | "registry"
              state?: string
            }[]
          }
        ).intents
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
            source: it.source,
            state: it.state,
          })),
        })
        domNode.count = (domNode.count ?? 0) + intents.length
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

// ── 运营意图注册表 (流派二生命周期) ──

const STATE_LABELS: Record<string, string> = {
  draft: "草稿",
  pending_review: "待评审",
  evaluating: "评测中",
  eval_failed: "评测未过",
  shadow: "影子观察",
  active: "已生效",
  deprecated: "已下线",
  rejected: "已驳回",
}

const STATE_TAG: Record<string, string> = {
  draft: "info",
  pending_review: "warning",
  evaluating: "warning",
  eval_failed: "danger",
  shadow: "primary",
  active: "success",
  deprecated: "info",
  rejected: "danger",
}

const ACTION_LABELS: Record<string, string> = {
  create: "登记",
  submit: "提交评审",
  approve: "评审通过",
  reject: "驳回",
  evaluate: "重跑评测",
  gates_pass: "评测通过",
  gates_fail: "评测未过",
  activate: "激活生效",
  deprecate: "下线",
  update: "编辑",
}

const registryEntries = ref<RegistryEntry[]>([])
const registryLoading = ref(false)
const registryStateFilter = ref("")

const indexStatus = ref({ running: false, action: "", version: 0, entities: 0, error: "", started_at: 0, finished_at: 0 })
let indexPollTimer: ReturnType<typeof setInterval> | null = null

async function loadRegistry() {
  registryLoading.value = true
  try {
    const r = await listRegistry(registryStateFilter.value || undefined)
    registryEntries.value = r.entries
  } catch {
    /* handled */
  } finally {
    registryLoading.value = false
  }
}

async function loadIndexStatus() {
  try {
    indexStatus.value = await getIndexStatus()
    if (indexStatus.value.running && !indexPollTimer) {
      indexPollTimer = setInterval(async () => {
        try {
          indexStatus.value = await getIndexStatus()
        } catch {
          /* handled */
        }
        if (!indexStatus.value.running && indexPollTimer) {
          clearInterval(indexPollTimer)
          indexPollTimer = null
          loadRegistry()
        }
      }, 2000)
    }
  } catch {
    /* handled */
  }
}

async function doRebuildIndex() {
  try {
    await rebuildIndex()
    ElMessage.success("重建已调度（蓝绿：建新版本 → 校验 → 原子切换）")
    loadIndexStatus()
  } catch {
    /* handled */
  }
}

async function doRollbackIndex() {
  try {
    const r = await rollbackIndex()
    ElMessage.success(`已回滚到 v${r.version}`)
    loadIndexStatus()
  } catch {
    /* handled */
  }
}

// ── 生命周期动作 ──

async function refreshAll() {
  await Promise.all([loadRegistry(), loadTree(), loadAttr()])
}

async function doSubmit(row: RegistryEntry) {
  try {
    await submitRegistryIntent(row.slug)
    ElMessage.success("已提交评审（需另一位管理员复核）")
    loadRegistry()
  } catch {
    /* handled */
  }
}

async function doReview(row: RegistryEntry, approve: boolean) {
  let note = ""
  try {
    const { value } = await ElMessageBox.prompt(
      approve ? "评审意见（可选）：确认边界清晰、种子质量达标" : "驳回原因（必填）：",
      approve ? `通过 — ${row.name_zh}` : `驳回 — ${row.name_zh}`,
      { inputValue: "", inputPlaceholder: approve ? "可选" : "必填", type: approve ? "info" : "warning" },
    )
    note = value ?? ""
  } catch {
    return // 用户取消
  }
  if (!approve && !note.trim()) {
    ElMessage.warning("驳回需填写原因")
    return
  }
  try {
    await reviewRegistryIntent(row.slug, approve, note)
    ElMessage.success(approve ? "已通过，评测闸门运行中（重叠检测 + 金标回归）" : "已驳回")
    setTimeout(loadRegistry, 1500) // 等后台闸门推进状态
    setTimeout(loadRegistry, 4000)
  } catch {
    /* handled */
  }
}

async function doEvaluate(row: RegistryEntry) {
  try {
    await evaluateRegistryIntent(row.slug)
    ElMessage.success("评测已重新调度")
    setTimeout(loadRegistry, 1500)
    setTimeout(loadRegistry, 4000)
  } catch {
    /* handled */
  }
}

async function doActivate(row: RegistryEntry) {
  try {
    await ElMessageBox.confirm(
      `激活后「${row.name_zh}」种子将并入 L2 向量索引（蓝绿重建），L3 候选正式生效。继续？`,
      "激活生效",
      { confirmButtonText: "激活", type: "warning" },
    )
  } catch {
    return
  }
  try {
    await activateRegistryIntent(row.slug)
    ElMessage.success("已激活，索引蓝绿重建调度中")
    refreshAll()
    loadIndexStatus()
  } catch {
    /* handled */
  }
}

async function doDeprecate(row: RegistryEntry) {
  try {
    await ElMessageBox.confirm(
      `下线后「${row.name_zh}」种子退出索引（重建生效），条目保留供审计，不可恢复。继续？`,
      "下线意图",
      { confirmButtonText: "下线", type: "warning" },
    )
  } catch {
    return
  }
  try {
    await deprecateRegistryIntent(row.slug)
    ElMessage.success("已下线")
    refreshAll()
    loadIndexStatus()
  } catch {
    /* handled */
  }
}

async function doReject(row: RegistryEntry) {
  let note = ""
  try {
    const { value } = await ElMessageBox.prompt("驳回原因（必填）：", `驳回 — ${row.name_zh}`, {
      inputPlaceholder: "必填",
    })
    note = value ?? ""
  } catch {
    return
  }
  if (!note.trim()) {
    ElMessage.warning("驳回需填写原因")
    return
  }
  try {
    await rejectRegistryIntent(row.slug, note)
    ElMessage.success("已驳回")
    loadRegistry()
  } catch {
    /* handled */
  }
}

// ── 新增/编辑弹窗 ──

const createVisible = ref(false)
const createLoading = ref(false)
const editingSlug = ref("")
const intentForm = ref({
  slug: "",
  name_zh: "",
  definition: "",
  domain: "consulting",
  group: "",
  traffic_class: "",
  seedsText: "",
})

const seedLineCount = computed(
  () => intentForm.value.seedsText.split("\n").filter((l) => l.trim()).length,
)

function openCreateDialog() {
  editingSlug.value = ""
  intentForm.value = { slug: "", name_zh: "", definition: "", domain: "consulting", group: "", traffic_class: "", seedsText: "" }
  createVisible.value = true
}

function openEditDialog(row: RegistryEntry) {
  editingSlug.value = row.slug
  intentForm.value = {
    slug: row.slug,
    name_zh: row.name_zh,
    definition: row.definition,
    domain: row.domain,
    group: row.group,
    traffic_class: row.traffic_class,
    seedsText: row.seeds.join("\n"),
  }
  createVisible.value = true
}

async function doCreateOrUpdate() {
  const f = intentForm.value
  const seeds = f.seedsText.split("\n").map((l) => l.trim()).filter(Boolean)
  if (!editingSlug.value) {
    if (!/^[a-z][a-z0-9_]{2,63}$/.test(f.slug)) {
      ElMessage.warning("slug 需为小写字母开头的 a-z0-9_，3-64 位")
      return
    }
    if (!f.name_zh.trim()) {
      ElMessage.warning("中文名必填")
      return
    }
  }
  if (seeds.length < 10) {
    ElMessage.warning(`种子样本至少 10 条（建议 20），当前 ${seeds.length} 条`)
    return
  }
  createLoading.value = true
  try {
    if (editingSlug.value) {
      await updateRegistryIntent(editingSlug.value, {
        seeds,
        name_zh: f.name_zh,
        definition: f.definition,
        traffic_class: f.traffic_class,
      })
      ElMessage.success("已保存（可重跑评测）")
    } else {
      await createRegistryIntent({
        slug: f.slug,
        domain: f.domain,
        name_zh: f.name_zh,
        definition: f.definition,
        group: f.group,
        traffic_class: f.traffic_class,
        seeds,
      })
      ElMessage.success("已登记为草稿，提交评审后走评测闸门")
    }
    createVisible.value = false
    loadRegistry()
  } catch {
    /* handled */
  } finally {
    createLoading.value = false
  }
}

// ── 历史抽屉 ──

const historyVisible = ref(false)
const historyEntry = ref<RegistryEntry | null>(null)

function openHistory(row: RegistryEntry) {
  historyEntry.value = row
  historyVisible.value = true
}

function formatTs(ts: number) {
  if (!ts) return ""
  return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false })
}

onMounted(() => {
  loadTree()
  loadSeeds()
  loadAttr()
  loadRegistry()
  loadIndexStatus()
})

onUnmounted(() => {
  if (indexPollTimer) {
    clearInterval(indexPollTimer)
    indexPollTimer = null
  }
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

.intent-deprecated {
  text-decoration: line-through;
  opacity: 0.55;
}
.tree-reg { transform: scale(0.82); }
.tab-badge { margin-left: 6px; transform: scale(0.85); }

.index-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  margin-bottom: 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-fill-color-extra-light);
}
.index-info {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  .index-title { font-weight: 600; }
  .index-error { color: var(--el-color-danger); font-size: var(--fs-sm); }
}
.registry-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}
.eval-detail { margin-left: 4px; }
.seed-count-hint {
  margin-top: 4px;
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  &.seed-count-bad { color: var(--el-color-danger); }
}
.history-note {
  margin-top: 2px;
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
}
</style>
