package com.lumio.chatsvc.customerserver.controller;

import com.lumio.chatsvc.customerserver.websocket.SessionEventBroadcaster;
import org.springframework.http.MediaType;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RestController;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

/**
 * 会话变更实时推送接口 (SSE)。
 *
 * <p>坐席端客户列表订阅此流, 客户进线 / 会话状态变化时即时收到 {@code session} 事件,
 * 无需轮询。事件负载为 {@code SessionInfo} 的单条 JSON, 连接即先推当前快照。
 */
@RestController
@RequestMapping("/api")
public class SessionEventController {

    private final SessionEventBroadcaster broadcaster;

    public SessionEventController(SessionEventBroadcaster broadcaster) {
        this.broadcaster = broadcaster;
    }

    @GetMapping(value = "/sessions/events", produces = MediaType.TEXT_EVENT_STREAM_VALUE)
    public SseEmitter stream() {
        return broadcaster.register();
    }
}