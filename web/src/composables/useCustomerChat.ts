import { ref, onUnmounted } from "vue"
import { pollChatSvcMessages, sendChatSvcMessage, closeChatSvcSession } from "@/api/chat-svc"
import { getToken } from "@/api/client"

function authHeaders(): Record<string, string> {
  const token = getToken()
  const headers: Record<string, string> = { "Content-Type": "application/json" }
  if (token) headers.Authorization = `Bearer ${token}`
  return headers
}

export interface ChatMsg {
  id: string
  role: "customer" | "bot" | "agent" | "system"
  content: string
  time: Date
}

export function useCustomerChat() {
  const sessionId = ref("")
  const transferSid = ref("") // chat-svc 生成的会话 id (session-xxxx)，转人工后轮询依据
  const customerId = ref("")
  const customerName = ref("")
  const messages = ref<ChatMsg[]>([])

  function setCustomer(id: string, name: string) {
    customerId.value = id
    customerName.value = name
  }
  const connected = ref(false)
  const polling = ref(false)
  const inQueue = ref(false)
  const queuePosition = ref(0)
  const agentName = ref("")
  // 转人工后的 chat-svc 消息轮询是否进行中。独立于 inQueue(排队中)——
  // 排队结束后仍要继续轮询坐席后续回复，不能因首条欢迎语到达就退出。
  const svcPolling = ref(false)

  let pollAbort: AbortController | null = null
  let svcPollAbort: AbortController | null = null
  let msgCounter = 0

  function addMsg(role: ChatMsg["role"], content: string) {
    messages.value.push({ id: `m${++msgCounter}`, role, content, time: new Date() })
  }

  async function sendMessage(text: string) {
    if (!text.trim()) return
    addMsg("customer", text)

    // 已转人工: 消息直接发往 chat-svc, 坐席端实时收到
    if (transferSid.value) {
      try {
        await sendChatSvcMessage(transferSid.value, { sender: "customer", content: text })
        return
      } catch {
        addMsg("system", "人工消息发送失败，请稍后重试")
        return
      }
    }

    try {
      const resp = await fetch("/api/chat/send", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({
          session_id: sessionId.value || undefined,
          customer_id: customerId.value || undefined,
          customer_name: customerName.value || undefined,
          message: text,
        }),
      })
      if (!resp.ok) {
        addMsg("system", `服务暂时不可用 (${resp.status})，请稍后重试`)
        return
      }
      const data = await resp.json()
      if (data.session_id) sessionId.value = data.session_id
    } catch {
      addMsg("system", "网络连接失败，请检查网络后重试")
    }
  }

  async function startPolling() {
    if (!sessionId.value) return
    polling.value = true
    pollAbort = new AbortController()

    while (polling.value) {
      try {
        const url = `/api/chat/poll?session_id=${sessionId.value}&timeout=25`
        const resp = await fetch(url, { signal: pollAbort.signal, headers: authHeaders() })
        const data = await resp.json()

        if (data.status === "done" && data.reply) {
          addMsg(data.is_transfer ? "system" : "bot", data.reply)
          if (data.is_transfer) {
            addMsg("system", "正在转接人工客服...")
            inQueue.value = true
            // 用 chat-svc 真实会话 id (session-xxxx) 轮询，而非 Lumio bot 的 session_id
            transferSid.value = data.transfer_sid || ""
            // 开始轮询 chat-svc
            startChatSvcPolling()
            return
          }
        } else if (data.status === "queued") {
          queuePosition.value = data.position || 0
          inQueue.value = true
        } else if (data.status === "processing") {
          // 等待中
        }
      } catch (e: any) {
        if (e.name === "AbortError") break
        await new Promise(r => setTimeout(r, 1000))
      }
    }
  }

  async function startChatSvcPolling() {
    const sid = transferSid.value
    let lastSeq = 0
    svcPolling.value = true
    svcPollAbort = new AbortController()
    while (svcPolling.value && transferSid.value) {
      try {
        const msgs = await pollChatSvcMessages(sid, lastSeq, 25000, svcPollAbort.signal)
        for (const m of msgs || []) {
          if (m.seq > lastSeq) lastSeq = m.seq
          if (m.sender === "agent") {
            addMsg("agent", m.content)
            if (!connected.value) connected.value = true
            inQueue.value = false
          }
        }
      } catch (e: any) {
        if (e?.name === "AbortError" || !svcPolling.value) break
        await new Promise(r => setTimeout(r, 1000))
      }
    }
    svcPolling.value = false
  }

  function stopPolling() {
    polling.value = false
    svcPolling.value = false
    svcPollAbort?.abort()
    svcPollAbort = null
    pollAbort?.abort()
    pollAbort = null
  }

  function clearChat() {
    stopPolling()
    sessionId.value = ""
    transferSid.value = ""
    messages.value = []
    connected.value = false
    inQueue.value = false
    agentName.value = ""
  }

  // 客户主动结束会话: 已转人工则关闭 chat-svc 会话(坐席列表移除该客户)，
  // 否则结束 bot 会话。关闭失败不阻塞, 依旧清空本地回到登录态。
  async function endSession() {
    try {
      if (transferSid.value) {
        await closeChatSvcSession(transferSid.value)
      } else if (sessionId.value) {
        await fetch("/api/chat/end", {
          method: "POST",
          headers: authHeaders(),
          body: JSON.stringify({ session_id: sessionId.value }),
        })
      }
    } catch {
      // 忽略关闭失败, 仍要清空本地
    }
    clearChat()
  }

  onUnmounted(() => stopPolling())

  return { sessionId, messages, connected, polling, inQueue, queuePosition, agentName, sendMessage, startPolling, stopPolling, clearChat, endSession, addMsg, setCustomer, customerName }
}
