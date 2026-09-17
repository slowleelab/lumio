<template>
  <div class="flow-chain" :class="{ compact }">
    <!-- 主链: 待处置 → 修复中 → 已灰度 → 已上线 → 已验证 -->
    <div class="chain-line">
      <template v-for="(node, i) in chain" :key="node.key">
        <div
          class="flow-node"
          :class="nodeClass(node)"
          :title="nodeTitle(node)"
          @click="onNodeClick(node)"
        >
          <div class="node-dot">
            <span v-if="nodeState(node) === 'done'" class="node-check">✓</span>
            <span v-else-if="counts && counts[node.key]" class="node-count">{{ counts[node.key] }}</span>
            <span v-else-if="node.key === 'rejected'" class="node-x">✕</span>
            <span v-else class="node-inner"></span>
          </div>
          <div class="node-label">{{ node.label }}</div>
          <div v-if="nodeState(node) === 'current' && node.key === current" class="node-here">当前</div>
          <div v-if="nodeState(node) === 'next'" class="node-act">{{ node.actionLabel }}</div>
        </div>
        <div v-if="i < chain.length - 1" class="chain-link" :class="{ done: linkDone(i) }">
          <span class="link-arrow">→</span>
        </div>
      </template>
    </div>

    <!-- 分支: 复检回环 / 驳回 -->
    <div v-if="showBranches" class="chain-branches muted">
      <template v-if="current === 'reopened'">
        <span class="branch-loop">⟲ 复检未过 — 回到「修复中」重新修复</span>
        <el-button size="small" link type="warning" :loading="loading" @click="$emit('advance', 'fixing')">执行: 重新修复</el-button>
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
      >驳回</el-button>
    </div>
  </div>
</template>

<script setup lang="ts">
/**
 * 状态流转节点链 — 状态机可视化 + 流转动作挂在节点上 (点下一节点即流转)
 *
 * 与后端 badcase_store._FIX_TRANSITIONS 同构:
 *   pending → fixing → canary → deployed → verified(终)
 *   各态 → rejected(终); deployed → reopened → fixing(回环)
 * advance 事件只对合法转移目标触发, 后端状态机仍是最终守门。
 */
import { computed } from "vue"

type Status = "pending" | "fixing" | "canary" | "deployed" | "verified" | "reopened" | "rejected"

const props = withDefaults(
  defineProps<{
    current: Status
    /** pending→fixing 的前置 (未归因时锁定, 提示先归因) */
    blocked?: boolean
    /** 节点计数徽标 (批量场景: 该状态案例数) */
    counts?: Record<string, number> | null
    loading?: boolean
    compact?: boolean
    allowReject?: boolean
    rejectNote?: string
  }>(),
  { blocked: false, counts: null, loading: false, compact: false, allowReject: true, rejectNote: "" },
)

const emit = defineEmits<{ (e: "advance", to: Status): void }>()

const CHAIN: { key: Status; label: string; actionLabel: string; hint: string }[] = [
  { key: "pending", label: "待处置", actionLabel: "", hint: "案例已立案待处理" },
  { key: "fixing", label: "修复中", actionLabel: "确认根因，转修复", hint: "修复进行中" },
  { key: "canary", label: "已灰度", actionLabel: "修复完成，转灰度", hint: "灰度环境生效" },
  { key: "deployed", label: "已上线", actionLabel: "灰度通过，上线", hint: "全量生效" },
  { key: "verified", label: "已验证", actionLabel: "重放验证", hint: "重放验证通过 · 销项 (终态)" },
]

// 与后端 _FIX_TRANSITIONS 同构
const TRANSITIONS: Record<Status, Status[]> = {
  pending: ["fixing", "rejected"],
  fixing: ["canary", "rejected"],
  canary: ["deployed", "rejected"],
  deployed: ["verified", "reopened", "rejected"],
  reopened: ["fixing", "rejected"],
  verified: [],
  rejected: [],
}

const chain = computed(() => CHAIN)
const terminal = computed(() => props.current === "verified" || props.current === "rejected")

// reopened 在主链上的落点 = fixing (回环); rejected 落点 = 链外分支
const mainPosition = computed<number>(() => {
  if (props.current === "reopened") return 1
  const idx = CHAIN.findIndex((n) => n.key === props.current)
  return idx >= 0 ? idx : 0
})

function nodeState(node: { key: Status }) {
  const idx = CHAIN.findIndex((n) => n.key === node.key)
  if (idx < mainPosition.value) return "done"
  if (node.key === props.current || (props.current === "reopened" && node.key === "fixing")) return "current"
  if (idx === mainPosition.value + 1 && TRANSITIONS[props.current]?.includes(node.key)) {
    if (node.key === "fixing" && props.current === "pending" && props.blocked) return "locked"
    return "next"
  }
  return "future"
}

function nodeClass(node: { key: Status }) {
  return {
    done: nodeState(node) === "done",
    current: nodeState(node) === "current",
    next: nodeState(node) === "next",
    locked: nodeState(node) === "locked",
    future: nodeState(node) === "future",
    clickable: nodeState(node) === "next",
    reopened: props.current === "reopened" && node.key === "fixing",
  }
}

function nodeTitle(node: { key: Status; hint: string }) {
  const st = nodeState(node)
  if (st === "next") return `点击流转: ${node.actionLabel}`
  if (st === "locked") return "先完成归因 (GLM 裁判归因) 再推进"
  return node.hint
}

function linkDone(i: number) {
  return i < mainPosition.value
}

function onNodeClick(node: { key: Status }) {
  if (nodeState(node) !== "next" || props.loading) return
  emit("advance", node.key)
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
  gap: 0;
}

.flow-node {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  min-width: 86px;
  cursor: default;
  position: relative;
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
  transition: all 0.2s;
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

.node-act {
  font-size: 10px;
  color: var(--el-color-primary);
  white-space: nowrap;
}

/* 已过 */
.flow-node.done .node-dot {
  border-color: var(--el-color-success);
  background: var(--el-color-success);
  color: #fff;
}

.flow-node.done .node-label {
  color: var(--color-text-secondary);
}

/* 当前 */
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

/* 可流转的下一节点 = 动作入口 */
.flow-node.clickable {
  cursor: pointer;
}

.flow-node.clickable .node-dot {
  border-style: dashed;
  border-color: var(--el-color-primary);
  color: var(--el-color-primary);
}

.flow-node.clickable:hover .node-dot {
  transform: translateY(-2px);
  box-shadow: 0 3px 8px var(--el-color-primary-light-7);
}

.flow-node.locked .node-dot {
  border-style: dotted;
  cursor: not-allowed;
}

/* 计数徽标 (批量场景) */
.node-count {
  font-size: 11px;
  font-weight: 700;
}

.chain-link {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  height: 26px;
  color: var(--el-border-color);
  font-size: 14px;
}

.chain-link.done {
  color: var(--el-color-success);
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
  min-width: 70px;
}
</style>
