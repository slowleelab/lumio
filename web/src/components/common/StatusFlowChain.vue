<template>
  <div class="flow-chain" :class="{ compact }">
    <!-- 主链: 节点(状态) —动作胶囊(流转触发)→ 节点 —… -->
    <div class="chain-line">
      <template v-for="(seg, i) in segments" :key="seg.node.key">
        <div class="flow-node" :class="nodeClass(seg.node)" :title="seg.node.hint">
          <div class="node-dot">
            <span v-if="nodeState(seg.node) === 'done'" class="node-check">✓</span>
            <span v-else-if="counts && counts[seg.node.key]" class="node-count">{{ counts[seg.node.key] }}</span>
            <span v-else class="node-inner"></span>
          </div>
          <div class="node-label">{{ seg.node.label }}</div>
          <div v-if="nodeState(seg.node) === 'current'" class="node-here">当前</div>
        </div>

        <!-- 流转边: 明确命名的动作触发 (当前态的出边可点; 前置不满足禁用并给原因) -->
        <div v-if="seg.edge" class="flow-edge" :class="edgeClass(seg.edge)">
          <span class="edge-line left"></span>
          <button
            class="edge-action"
            :disabled="edgeState(seg.edge) !== 'active' || loading"
            :title="edgeTitle(seg.edge)"
            @click.stop="onEdgeClick(seg.edge)"
          >
            <span v-if="loading && edgeState(seg.edge) === 'active'" class="edge-spin"></span>
            {{ edgeLabel(seg.edge) }}
          </button>
          <span class="edge-line right"></span>
        </div>
      </template>
    </div>

    <!-- 分支: 复检回环 / 驳回 (链外动作) -->
    <div v-if="showBranches" class="chain-branches muted">
      <template v-if="current === 'reopened'">
        <span class="branch-loop">⟲ 重放验证未过 — 回到修复中</span>
        <el-button size="small" link type="warning" :loading="loading" @click="$emit('advance', 'fixing')">重新修复</el-button>
      </template>
      <template v-if="current === 'rejected'">
        <span class="branch-reject">✕ 已驳回 (终态) — {{ rejectNote }}</span>
      </template>
      <el-button
        v-if="!terminal && allowReject"
        size="small"
        link
        type="danger"
        class="branch-reject-btn"
        @click="$emit('advance', 'rejected')"
      >驳回 (误采集)</el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 状态流转节点链 — 每段流转挂明确命名的动作触发 (边即按钮)
 *
 * 与后端 badcase_store._FIX_TRANSITIONS 同构:
 *   pending → fixing → canary → deployed → verified(终)
 *   各态 → rejected(终); deployed → reopened → fixing(回环)
 * 动作语义: 确认根因 / 修复完成 / 灰度通过 / 重放验证 — verified 只能经
 * 重放验证达成 (调用方在 advance 分发中路由到重放动作, 不做直接状态改写)。
 */
import { computed } from "vue"

type Status = "pending" | "fixing" | "canary" | "deployed" | "verified" | "reopened" | "rejected"

interface EdgeDef {
  from: Status
  to: Status
  label: string
  /** 前置条件不满足时的禁用说明 */
  blockedHint?: string
  /** 批量场景的标签覆盖 */
  batchLabel?: string
}

const props = withDefaults(
  defineProps<{
    current: Status
    /** pending→fixing 前置 (未归因锁定) */
    blocked?: boolean
    /** 节点计数徽标 (批量场景) */
    counts?: Record<string, number> | null
    loading?: boolean
    compact?: boolean
    allowReject?: boolean
    rejectNote?: string
    /** 批量场景: 动作标签加"批量"前缀且隐藏验证边 (重放验证需逐案执行) */
    batchMode?: boolean
  }>(),
  { blocked: false, counts: null, loading: false, compact: false, allowReject: true, rejectNote: "", batchMode: false },
)

const emit = defineEmits<{ (e: "advance", to: Status): void }>()

const NODES: { key: Status; label: string; hint: string }[] = [
  { key: "pending", label: "待处置", hint: "案例已立案待处理" },
  { key: "fixing", label: "修复中", hint: "修复进行中" },
  { key: "canary", label: "已灰度", hint: "灰度环境生效" },
  { key: "deployed", label: "已上线", hint: "全量生效" },
  { key: "verified", label: "已验证", hint: "重放验证通过 · 销项 (终态)" },
]

const EDGES: EdgeDef[] = [
  { from: "pending", to: "fixing", label: "确认根因", batchLabel: "批量确认根因", blockedHint: "先完成 GLM 裁判归因, 根因明确后解锁" },
  { from: "fixing", to: "canary", label: "修复完成", batchLabel: "批量转灰度" },
  { from: "canary", to: "deployed", label: "灰度通过", batchLabel: "批量上线" },
  { from: "deployed", to: "verified", label: "重放验证", batchLabel: "" },
]

const chainNodes = computed(() => (props.batchMode ? NODES.filter((n) => n.key !== "verified") : NODES))
const chainEdges = computed(() => (props.batchMode ? EDGES.filter((e) => e.to !== "verified") : EDGES))

const segments = computed(() =>
  chainNodes.value.map((node, i) => ({ node, edge: chainEdges.value[i] ?? null })),
)

const terminal = computed(() => props.current === "verified" || props.current === "rejected")
const mainPosition = computed<number>(() => {
  if (props.current === "reopened") return 1
  const idx = chainNodes.value.findIndex((n) => n.key === props.current)
  return idx >= 0 ? idx : 0
})

function nodeState(node: { key: Status }) {
  const idx = chainNodes.value.findIndex((n) => n.key === node.key)
  if (idx < mainPosition.value) return "done"
  if (node.key === props.current || (props.current === "reopened" && node.key === "fixing")) return "current"
  return "future"
}

function nodeClass(node: { key: Status }) {
  return {
    done: nodeState(node) === "done",
    current: nodeState(node) === "current",
    future: nodeState(node) === "future",
    reopened: props.current === "reopened" && node.key === "fixing",
  }
}

function edgeState(edge: EdgeDef): "active" | "blocked" | "idle" {
  if (edge.from !== props.current) return "idle"
  if (edge.from === "pending" && props.blocked) return "blocked"
  return "active"
}

function edgeLabel(edge: EdgeDef) {
  if (props.batchMode && edge.batchLabel !== undefined) return edge.batchLabel || edge.label
  return edge.label
}

function edgeClass(edge: EdgeDef) {
  return { active: edgeState(edge) === "active", blocked: edgeState(edge) === "blocked", idle: edgeState(edge) === "idle" }
}

function edgeTitle(edge: EdgeDef) {
  const st = edgeState(edge)
  if (st === "blocked") return edge.blockedHint || "前置条件未满足"
  if (st === "idle") return `流程到达「${nodeName(edge.from)}」后可执行`
  return `${edgeLabel(edge)} → ${nodeName(edge.to)}`
}

function nodeName(s: Status) {
  return NODES.find((n) => n.key === s)?.label ?? s
}

function onEdgeClick(edge: EdgeDef) {
  if (edgeState(edge) !== "active" || props.loading) return
  emit("advance", edge.to)
}

const showBranches = computed(() => !terminal.value || props.current === "rejected")
</script>

<style scoped>
.flow-chain {
  padding: 6px 0 2px;
}

.chain-line {
  display: flex;
  align-items: flex-start;
}

.flow-node {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  min-width: 76px;
}

.node-dot {
  width: 26px;
  height: 26px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  border: 2px solid var(--el-border-color);
  background: var(--color-bg-surface, #f5f7fa);
  color: var(--color-text-muted, #909399);
}

.node-label {
  font-size: 12px;
  color: var(--color-text-muted, #909399);
}

.node-here {
  font-size: 10px;
  color: var(--el-color-primary);
  font-weight: 600;
}

.flow-node.done .node-dot {
  border-color: var(--el-color-success);
  background: var(--el-color-success);
  color: #fff;
}

.flow-node.done .node-label {
  color: var(--color-text-secondary);
}

.flow-node.current .node-dot {
  border-color: var(--el-color-primary);
  box-shadow: 0 0 0 4px var(--el-color-primary-light-8);
  background: var(--el-color-primary);
  color: #fff;
}

.flow-node.current .node-label {
  color: var(--el-color-primary);
  font-weight: 700;
}

.flow-node.reopened .node-dot {
  border-color: var(--el-color-warning);
  background: var(--el-color-warning);
}

.node-count {
  font-size: 11px;
  font-weight: 700;
}

/* ── 流转边: 动作胶囊是唯一流转入口 ── */
.flow-edge {
  flex: 1;
  display: flex;
  align-items: center;
  margin-top: 4px;
  min-width: 0;
}

.edge-line {
  flex: 1;
  height: 2px;
  border-radius: 1px;
}

.flow-edge.active .edge-line {
  background: repeating-linear-gradient(90deg, var(--el-color-primary-light-5) 0 6px, transparent 6px 10px);
}

.flow-edge.idle .edge-line,
.flow-edge.blocked .edge-line {
  background: var(--el-border-color-lighter);
}

.edge-action {
  flex-shrink: 0;
  margin: 0 4px;
  padding: 3px 10px;
  font-size: 11px;
  border-radius: 999px;
  border: 1px solid var(--el-color-primary);
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  cursor: pointer;
  white-space: nowrap;
  transition: all 0.18s;
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.flow-edge.active .edge-action:hover:not(:disabled) {
  background: var(--el-color-primary);
  color: #fff;
  transform: translateY(-1px);
  box-shadow: 0 2px 6px var(--el-color-primary-light-7);
}

.flow-edge.active .edge-action:disabled {
  opacity: 0.7;
  cursor: wait;
}

.flow-edge.idle .edge-action {
  border-color: var(--el-border-color);
  color: var(--color-text-muted, #c0c4cc);
  background: transparent;
  cursor: default;
}

.flow-edge.blocked .edge-action {
  border-style: dashed;
  border-color: var(--el-color-warning-light-5);
  color: var(--el-color-warning);
  background: transparent;
  cursor: not-allowed;
}

.edge-spin {
  width: 10px;
  height: 10px;
  border: 2px solid var(--el-color-primary-light-5);
  border-top-color: var(--el-color-primary);
  border-radius: 50%;
  animation: chain-spin 0.8s linear infinite;
}

@keyframes chain-spin {
  to {
    transform: rotate(360deg);
  }
}

.chain-branches {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-top: 8px;
  font-size: 12px;
  flex-wrap: wrap;
}

.branch-loop {
  color: var(--el-color-warning);
}

.branch-reject {
  color: var(--el-color-danger);
}

.compact .flow-node {
  min-width: 62px;
}
</style>
