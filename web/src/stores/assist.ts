import { defineStore } from "pinia"
import { ref, computed, reactive, watch } from "vue"
import type { SessionInfo, AssistPushPayload, AssistPushMessage, ChatMessage } from "@/api/types"
import { getChatSvcMessages, type ChatSvcSessionRaw } from "@/api/chat-svc"
import { getToken } from "@/api/client"

export const useAssistStore = defineStore("assist", () => {
  const sessions = ref<SessionInfo[]>([])

  const currentAgentId = ref<string | null>(null)  // 按坐席建连的 WS
  const activeSessionId = ref<string | null>(null)
  const wsStatus = ref<"connecting" | "connected" | "disconnected" | "error">("disconnected")

  // 按 session 分组存储推送数据
  const pushDataMap = ref<Map<string, AssistPushPayload>>(new Map())

  // 按 session 分组存储对话消息
  const messagesMap = ref<Map<string, ChatMessage[]>>(new Map())

  // 跨组件注入的草稿片段：AssistPanel 的"采纳"按钮通过 store 推送给 ConversationPanel 输入框
  // nonce 字段用于在同一文本连续点击时强制触发 watch (避免文本相同时 watcher 不触发)
  const pendingInsert = ref<{ text: string; nonce: number } | null>(null)

  const activePushData = computed(() => {
    if (!activeSessionId.value) return null
    return pushDataMap.value.get(activeSessionId.value) ?? null
  })

  const activeMessages = computed(() => {
    if (!activeSessionId.value) return []
    return messagesMap.value.get(activeSessionId.value) ?? []
  })

  const activeSession = computed(() => {
    if (!activeSessionId.value) return null
    return sessions.value.find((s) => s.sessionId === activeSessionId.value) ?? null
  })

  // 未读计数 (sessionId → 条数), 供 SessionList 显示红点; selectSession 时清零
  const unread = reactive<Record<string, number>>({})

  let msgCounter = 0

  // 会话列表按最近活跃降序排列, 新转来的客户置顶
  function sortSessions() {
    sessions.value.sort((a, b) => (b.lastActiveAt?.getTime() ?? 0) - (a.lastActiveAt?.getTime() ?? 0))
  }

  function onPushMessage(msg: AssistPushMessage) {
    pushDataMap.value.set(msg.session_id, msg.payload)
    const session = sessions.value.find((s) => s.sessionId === msg.session_id)
    if (session) {
      session.lastActiveAt = new Date(msg.timestamp)
      sortSessions()
    }
  }

  function addMessage(sessionId: string, role: ChatMessage["role"], content: string, extra?: Partial<ChatMessage>) {
    const messages = messagesMap.value.get(sessionId) ?? []
    messages.push({
      id: `msg-${++msgCounter}`,
      role,
      content,
      timestamp: new Date(),
      ...extra,
    })
    messagesMap.value.set(sessionId, messages)
  }

  function selectSession(id: string) {
    activeSessionId.value = id
    unread[id] = 0
    // 首次激活该会话时载入存量历史 (此后靠 WS 实时累积)
    loadHistory(id)
  }

  function setWsStatus(status: "connecting" | "connected" | "disconnected" | "error") {
    wsStatus.value = status
  }

  // 草稿注入：从 AssistPanel 的"采纳/修改"按钮推到 ConversationPanel 输入框
  function insertDraftText(text: string) {
    pendingInsert.value = { text, nonce: Date.now() }
  }

  // ConversationPanel 消费后清空，避免下次进入会话时重复触发
  function consumePendingInsert() {
    pendingInsert.value = null
  }

  // ===== 坐席实时通道 (WebSocket → chat-svc) =====
  // 聊天框所有数据交互统一走 /api/chat-svc/ws/agent/{agentId}：
  //  收客户消息 {type:"message",...}、收会话/客户进线 {type:"session",...}、发送坐席消息。
  let chatWs: WebSocket | null = null
  let chatWsReconnectTimer: ReturnType<typeof setTimeout> | null = null
  let chatWsHeartbeatTimer: ReturnType<typeof setInterval> | null = null
  let chatWsDelay = 1000

  function chatWsUrl(agentId: string): string {
    if (typeof window === "undefined") return ""
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:"
    const token = getToken()
    // 握手经 query param 携带 token，供 chat-svc 端到端鉴权（浏览器原生 WS 无法自定义 header）
    return `${proto}//${window.location.host}/api/chat-svc/ws/agent/${agentId}?token=${encodeURIComponent(token ?? "")}`
  }

  // 追加服务端消息；带 messageId 去重，避免历史加载与实时推送重复
  function pushServerMessage(
    sid: string,
    role: "customer" | "agent",
    content: string,
    messageId?: string,
    timestampMs?: number,
  ) {
    const list = messagesMap.value.get(sid) ?? []
    if (messageId && list.some((m) => m.id === messageId)) return
    list.push({
      id: messageId || `msg-${++msgCounter}`,
      role,
      content,
      timestamp: new Date(timestampMs ?? Date.now()),
    })
    messagesMap.value.set(sid, list)
  }

  // 会话激活时载入存量历史（仅第一次；之后靠 WS 实时累积）
  async function loadHistory(sid: string) {
    if (messagesMap.value.get(sid)?.length) return
    try {
      const msgs = await getChatSvcMessages(sid)
      for (const m of msgs) {
        pushServerMessage(sid, m.sender === "agent" ? "agent" : "customer", m.content, m.messageId, m.timestamp)
      }
    } catch {
      // chat-svc 不可用时静默失败
    }
  }

  function handleChatWsMessage(ev: MessageEvent) {
    let frame: Record<string, unknown>
    try {
      frame = JSON.parse(ev.data)
    } catch {
      return
    }
    if (frame.type === "message") {
      const sid = String(frame.session_id ?? "")
      if (!sid) return
      if (sid === activeSessionId.value) {
        pushServerMessage(sid, frame.sender === "agent" ? "agent" : "customer", String(frame.content ?? ""), frame.messageId as string, frame.timestamp as number)
      } else {
        // 非当前会话的客户消息 → 计入未读红点
        const session = sessions.value.find((s) => s.sessionId === sid)
        if (session && session.phase === "agent") unread[sid] = (unread[sid] ?? 0) + 1
      }
      return
    }
    if (frame.type === "session") {
      applySessionRaw(frame as unknown as ChatSvcSessionRaw)
    }
  }

  function startChatWsHeartbeat() {
    stopChatWsHeartbeat()
    chatWsHeartbeatTimer = setInterval(() => {
      if (chatWs?.readyState === WebSocket.OPEN) chatWs.send(JSON.stringify({ type: "PING" }))
    }, 30000)
  }

  function stopChatWsHeartbeat() {
    if (chatWsHeartbeatTimer) {
      clearInterval(chatWsHeartbeatTimer)
      chatWsHeartbeatTimer = null
    }
  }

  function scheduleChatWsReconnect() {
    chatWsReconnectTimer = setTimeout(() => {
      chatWsDelay = Math.min(chatWsDelay * 2, 30000)
      connectChatSvcWs()
    }, chatWsDelay)
  }

  // 连接坐席实时通道；agentId 变化时断开重建。连接即收到服务端会话快照。
  function connectChatSvcWs() {
    if (typeof window === "undefined" || !currentAgentId.value) return
    closeChatSvcWs()
    chatWs = new WebSocket(chatWsUrl(currentAgentId.value))
    chatWs.onopen = () => {
      chatWsDelay = 1000
      startChatWsHeartbeat()
    }
    chatWs.onmessage = handleChatWsMessage
    chatWs.onclose = (ev) => {
      stopChatWsHeartbeat()
      if (!ev.wasClean) scheduleChatWsReconnect()
    }
    chatWs.onerror = () => {
      // onclose 会跟进重连
    }
  }

  function closeChatSvcWs() {
    if (chatWsReconnectTimer) {
      clearTimeout(chatWsReconnectTimer)
      chatWsReconnectTimer = null
    }
    stopChatWsHeartbeat()
    if (chatWs) {
      chatWs.onclose = null
      chatWs.close()
      chatWs = null
    }
  }

  /** 坐席发送消息：乐观落本地消息 + 走 WS 上行 */
  function sendAgentMessage(sid: string, content: string) {
    addMessage(sid, "agent", content)
    if (chatWs?.readyState === WebSocket.OPEN) {
      chatWs.send(JSON.stringify({ type: "agent_message", session_id: sid, content }))
    }
  }

  // ===== 会话/客户进线实时推送 (经坐席 WS 通道) =====
  // 客户进线 / 会话状态变化时, chat-svc 通过坐席 WebSocket 下发 {type:"session"} 事件,
  // 前端即时增删/重排列表, 不再依赖轮询或 SSE。连接即先收到全量快照。
  function applySessionRaw(raw: ChatSvcSessionRaw) {
    if (raw.status === "CLOSED") {
      const i = sessions.value.findIndex((s) => s.sessionId === raw.sessionId)
      if (i >= 0) sessions.value.splice(i, 1)
      return
    }
    const info: SessionInfo = {
      sessionId: raw.sessionId,
      phase: raw.status === "ACTIVE" ? "agent" as const : "bot" as const,
      lastActiveAt: new Date(raw.createTime),
      customerName: raw.customerName || raw.customerId || "访客",
      agentId: raw.agentId || undefined,
    }
    const i = sessions.value.findIndex((s) => s.sessionId === raw.sessionId)
    if (i >= 0) sessions.value[i] = info
    else sessions.value.push(info)
    sortSessions()
  }
  // agentId 就绪即连接坐席实时通道 (store 常驻; 断线自动重连 + 重连快照对账)
  watch(currentAgentId, (id) => {
    if (id) connectChatSvcWs()
  }, { immediate: true })

  // 页面卸载时清理坐席 WS 连接
  if (typeof window !== "undefined") {
    window.addEventListener("beforeunload", () => closeChatSvcWs())
  }

  return {
    currentAgentId,
    sessions, activeSessionId, wsStatus, activePushData, activeMessages, activeSession,
    unread,
    pendingInsert,
    onPushMessage, addMessage, selectSession, setWsStatus, insertDraftText, consumePendingInsert,
    sendAgentMessage, connectChatSvcWs,
  }
})
