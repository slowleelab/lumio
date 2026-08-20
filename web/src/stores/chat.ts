import { defineStore } from "pinia"
import { ref } from "vue"
import { sendMessage, pollReply } from "@/api/bot"
import { sendChatSvcMessage, pollChatSvcMessages } from "@/api/chat-svc"
import type { ChatMessage, ChatRequest, PollResponse } from "@/api/types"

// 长轮询参数：单次 poll 25s；总时限 40s（覆盖 worker 20s 编排预算 + 排队的余量）
const POLL_PER_POLL_MS = 25
const POLL_TOTAL_MS = 40000

export const useChatStore = defineStore("chat", () => {
  const messages = ref<ChatMessage[]>([])
  const sessionId = ref<string | null>(null)
  const isLoading = ref(false)
  const replyStatus = ref<string>("")  // bot 轮询期间的进度提示：排队中/处理中/超时重试
  const transferUrl = ref<string | null>(null)  // chat-svc 轮询地址
  const agentConnected = ref(false)

  let msgCounter = 0
  const seenMessageIds = new Set<string>()

  // 续轮询：queued/processing 保持等待不判死；首个 timeout 继续轮询，总时限到仍未 done
  // 才落一条占位气泡（避免静默丢回复）。只有 done 返回真实 result。
  async function pollUntilDone(sid: string, deadline: number): Promise<PollResponse | null> {
    for (;;) {
      const remaining = Math.min(
        POLL_PER_POLL_MS,
        Math.max(1, Math.ceil((deadline - Date.now()) / 1000)),
      )
      const resp = await pollReply(sid, remaining)
      if (resp.status === "done") return resp
      replyStatus.value =
        resp.status === "queued"
          ? `排队中${resp.position ? `，前方 ${resp.position} 位` : ""}`
          : resp.status === "processing"
            ? "处理中…"
            : "回复超时，重试中…"
      if (Date.now() >= deadline) {
        replyStatus.value = "服务繁忙，请稍后重试"
        return { status: "timeout", has_message: false } as PollResponse
      }
    }
  }

  async function send(text: string) {
    const userMsg: ChatMessage = {
      id: `msg-${++msgCounter}`,
      role: "customer" as const,
      content: text,
      timestamp: new Date(),
    }
    messages.value.push(userMsg)
    isLoading.value = true
    replyStatus.value = ""

    try {
      // 如果已转人工，发消息到 chat-svc (走 axios 包装, 自动 Bearer + 错误拦截)
      if (transferUrl.value) {
        const sid = transferUrl.value.match(/session_id=([^&]+)/)?.[1]
        if (sid) {
          try {
            await sendChatSvcMessage(sid, { sender: "customer", content: text })
          } catch { /* 错误已 toast */ }
        }
        isLoading.value = false
        return
      }

      // Bot 阶段：发送 + 续轮询（queued/processing 保持等待，don't 判死在首个 timeout）
      const request: ChatRequest = {
        message: text,
        session_id: sessionId.value ?? undefined,
      }
      const sendResp = await sendMessage(request)
      sessionId.value = sendResp.session_id

      const pollDeadline = Date.now() + POLL_TOTAL_MS
      const pollResp = await pollUntilDone(sendResp.session_id, pollDeadline)
      if (pollResp && pollResp.status === "done") {
        const botMsg: ChatMessage = {
          id: `msg-${++msgCounter}`,
          role: "bot",
          content: pollResp.reply || "抱歉，我暂时无法处理。",
          timestamp: new Date(),
          intent: pollResp.intent,
          confidence: pollResp.confidence,
          isTransfer: pollResp.is_transfer,
        }
        messages.value.push(botMsg)

        // 转人工：记录 transfer_url，开始轮询 chat-svc
        if (pollResp.is_transfer && pollResp.transfer_url) {
          transferUrl.value = pollResp.transfer_url
          agentConnected.value = true
          startAgentPolling()
        }
      }
    } catch {
      // silent
    } finally {
      isLoading.value = false
    }
  }

  let agentPollActive = false
  let lastSeq = 0  // seq 游标：只拉取该序号之后的消息（单调有序）

  async function startAgentPolling() {
    if (agentPollActive) return
    agentPollActive = true
    while (agentPollActive && transferUrl.value) {
      try {
        const sid = transferUrl.value.match(/session_id=([^&]+)/)?.[1]
        if (!sid) break
        const msgs = await pollChatSvcMessages(sid, lastSeq, 25000)
        for (const m of msgs) {
          if (m.seq > lastSeq) {
            lastSeq = m.seq
          }
          if (m.sender === "agent" && !seenMessageIds.has(m.messageId)) {
            seenMessageIds.add(m.messageId)
            messages.value.push({
              id: m.messageId,
              role: "agent",
              content: m.content,
              timestamp: new Date(m.timestamp),
            })
          }
        }
        // 有消息立即继续轮询，无消息等 500ms 避免空转
      } catch { await new Promise(r => setTimeout(r, 1000)) }
    }
  }

  function clearSession() {
    messages.value = []
    sessionId.value = null
    transferUrl.value = null
    agentConnected.value = false
    agentPollActive = false
    replyStatus.value = ""
    seenMessageIds.clear()
  }

  return { messages, sessionId, isLoading, replyStatus, transferUrl, agentConnected, send, clearSession }
})
