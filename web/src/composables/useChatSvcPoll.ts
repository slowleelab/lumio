import { ref, watch, onScopeDispose, type Ref } from "vue"
import { pollChatSvcMessages, type ChatSvcMessageRaw } from "@/api/chat-svc"

export type ChatSvcMsg = ChatSvcMessageRaw

export interface UseChatSvcPollOptions {
  /** 当前激活的 sessionId; 切换即重置游标并重启 */
  sessionId: Ref<string | null | undefined>
  /** 收到新消息时回调 (父组件负责落库 / 渲染) */
  onMessage: (msg: ChatSvcMsg) => void
  /** 是否激活 (false 时不轮询, 不报错误) */
  active?: Ref<boolean>
  /** 长轮询服务端超时 (ms), 默认 25s */
  timeoutMs?: number
}

/** 坐席侧 HTTP 长轮询 chat-svc 获取新消息 (B2: 走 axios 包装, 自动 Bearer)
 *
 *  - seq 游标: 只拉取 lastSeq 之后的消息 (单调有序, 消除同毫秒丢/重/乱), 切换 session 时重置
 *  - sessionId 变化: 立即停旧的, 启动新的
 *  - 组件卸载: 自动停止
 *  - 错误: 1s 后重试 (silently)
 */
export function useChatSvcPoll(opts: UseChatSvcPollOptions) {
  const isPolling = ref(false)
  let lastSeq = 0
  let stopFlag = false

  async function loop(sid: string) {
    isPolling.value = true
    while (!stopFlag) {
      try {
        const msgs = await pollChatSvcMessages(sid, lastSeq, opts.timeoutMs ?? 25000)
        for (const m of msgs || []) {
          if (m.seq > lastSeq) lastSeq = m.seq
          opts.onMessage(m)
        }
      } catch {
        await new Promise((r) => setTimeout(r, 1000))
      }
    }
    isPolling.value = false
  }

  function start(sid: string) {
    stopFlag = false
    lastSeq = 0  // 新会话从 0 开始
    loop(sid)
  }

  function stop() {
    stopFlag = true
    isPolling.value = false
  }

  watch(
    () => opts.sessionId.value,
    (newSid) => {
      stop()
      const isActive = opts.active?.value ?? true
      if (newSid && isActive) start(newSid)
    },
    { immediate: true },
  )

  onScopeDispose(stop)

  return { isPolling, stop, start }
}
