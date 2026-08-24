<template>
  <div class="session-list" data-testid="session-list">
    <div class="list-header">
      <span class="title">会话列表</span>
      <el-badge :value="activeCount" :max="99" type="primary" />
    </div>
    <div class="list-body">
      <div
        v-for="session in assistStore.sessions"
        :key="session.sessionId"
        class="session-item" data-testid="session-item"
        :class="{ active: session.sessionId === assistStore.activeSessionId }"
        @click="assistStore.selectSession(session.sessionId)"
      >
        <div class="session-name">
          <span class="session-title">{{ session.customerName || session.sessionId }}</span>
          <span
            v-if="(assistStore.unread[session.sessionId] ?? 0) > 0"
            class="unread-dot"
            :title="`${assistStore.unread[session.sessionId]} 条未读`"
          >{{ assistStore.unread[session.sessionId] }}</span>
        </div>
        <div class="session-meta">
          <el-tag :type="phaseTagType(session.phase)" size="small">
            {{ phaseLabel(session.phase) }}
          </el-tag>
          <span class="session-time">{{ formatTime(session.lastActiveAt) }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue"
import { useAssistStore } from "@/stores/assist"
import type { SessionPhase } from "@/api/types"

const assistStore = useAssistStore()

const activeCount = computed(
  () => assistStore.sessions.filter((s) => s.phase !== "ended").length,
)

const phaseTagMap: Record<SessionPhase, { type: "" | "warning" | "success" | "danger"; label: string }> = {
  bot: { type: "", label: "机器人" },
  agent: { type: "success", label: "坐席辅助" },
  ended: { type: "danger", label: "已结束" },
}

function phaseTagType(phase: SessionPhase) {
  return phaseTagMap[phase].type
}

function phaseLabel(phase: SessionPhase) {
  return phaseTagMap[phase].label
}

function formatTime(date: Date): string {
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", second: "2-digit" })
}
</script>

<style scoped>
.session-list {
  width: 240px;
  display: flex;
  flex-direction: column;
  background: var(--color-bg-surface);
  border-right: 1px solid var(--color-border-lighter);
}

.list-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: var(--space-4);
  border-bottom: 1px solid var(--color-border-lighter);
}

.list-header .title {
  font-size: var(--fs-md);
  font-weight: 600;
  color: var(--color-text-primary);
}

.list-body {
  flex: 1;
  overflow-y: auto;
}

.session-item {
  padding: var(--space-3) var(--space-4);
  cursor: pointer;
  border-bottom: 1px solid var(--color-border-extra-light);
  transition: background var(--transition-fast);
}

.session-item:hover {
  background: var(--color-bg-page);
}

.session-item.active {
  background: var(--color-bg-hover);
}

.session-name {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: var(--fs-base);
  font-weight: 500;
  color: var(--color-text-primary);
  margin-bottom: 6px;
}

.session-title {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.unread-dot {
  flex-shrink: 0;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 9px;
  background: var(--color-danger, #f56c6c);
  color: #fff;
  font-size: var(--fs-xs);
  font-weight: 600;
  line-height: 18px;
  text-align: center;
}

.session-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.session-time {
  font-size: var(--fs-xs);
  color: var(--color-text-placeholder);
}
</style>
