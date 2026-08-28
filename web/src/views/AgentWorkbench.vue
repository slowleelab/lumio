<template>
  <div class="agent-workbench">
    <SessionList />
    <ConversationPanel />
    <AssistPanel />
  </div>
</template>

<script setup lang="ts">
import { onMounted } from "vue"
import SessionList from "@/components/assist/SessionList.vue"
import ConversationPanel from "@/components/assist/ConversationPanel.vue"
import AssistPanel from "@/components/assist/AssistPanel.vue"
import { useAssistStore } from "@/stores/assist"

// 坐席身份: 演示环境固定为 demo 坐席 agent-1(王客服, 与 chat-svc 转人工自动分配一致)。
// 生产应在坐席登录时写入当前登录坐席的 id, 这里只作为验收演示的默认值。
const assistStore = useAssistStore()
onMounted(() => {
  if (!assistStore.currentAgentId) assistStore.currentAgentId = "agent-1"
})
</script>

<style scoped>
.agent-workbench {
  display: flex;
  height: 100%;
  background: #f0f2f5;
}
</style>
