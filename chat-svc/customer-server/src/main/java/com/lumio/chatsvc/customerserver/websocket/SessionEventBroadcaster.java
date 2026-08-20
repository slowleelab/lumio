package com.lumio.chatsvc.customerserver.websocket;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.lumio.chatsvc.common.model.Session;
import com.lumio.chatsvc.common.model.SessionStatus;
import com.lumio.chatsvc.customerserver.dto.SessionInfo;
import com.lumio.chatsvc.customerserver.session.SessionLifecycleEvent;
import com.lumio.chatsvc.customerserver.session.SessionStateListener;
import com.lumio.chatsvc.customerserver.session.SessionStore;
import com.lumio.chatsvc.customerserver.session.SessionStateTransitionManager;
import jakarta.annotation.PostConstruct;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.stereotype.Component;
import org.springframework.web.servlet.mvc.method.annotation.SseEmitter;

import java.io.IOException;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;

/**
 * 会话变更实时推送 (SSE) — 坐席端客户列表不再靠轮询等客户进线。
 *
 * <p>监听 {@link SessionStateTransitionManager} 的会话生命周期事件 (创建/分配/激活/转接/关闭),
 * 把变更后的会话摘要实时推给已订阅的 SSE 客户端。客户端 (assist store) 据此即时增删 / 重排会话列表。
 *
 * <p>SSE 订阅建立时先推送当前非关闭会话的全量快照, 便于前端增量合并与断连重连后的对账,
 * 因此即使个别事件丢失, 下一次重连也能自动收敛。
 */
@Component
public class SessionEventBroadcaster implements SessionStateListener {
    private static final Logger LOGGER = LoggerFactory.getLogger(SessionEventBroadcaster.class);

    private final SessionStateTransitionManager transitionManager;
    private final SessionStore sessionStore;
    private final ObjectMapper objectMapper;

    private final List<SseEmitter> emitters = new CopyOnWriteArrayList<>();

    public SessionEventBroadcaster(SessionStateTransitionManager transitionManager,
                                   SessionStore sessionStore,
                                   ObjectMapper objectMapper) {
        this.transitionManager = transitionManager;
        this.sessionStore = sessionStore;
        this.objectMapper = objectMapper;
    }

    @PostConstruct
    public void init() {
        transitionManager.addListener(this);
    }

    /**
     * 注册一个 SSE 订阅, 连接即推送当前快照。
     *
     * @return 可直接返回给 Spring MVC 的 SseEmitter
     */
    public SseEmitter register() {
        SseEmitter emitter = new SseEmitter(0L); // 0 = 不主动超时, 靠客户端断开回收
        emitter.onCompletion(() -> emitters.remove(emitter));
        emitter.onTimeout(() -> emitters.remove(emitter));
        emitter.onError(e -> emitters.remove(emitter));
        emitters.add(emitter);

        // 连接即推送当前非关闭会话快照, 前端据此增量合并与断连重连对账
        sessionStore.findAll().stream()
            .filter(s -> s.getStatus() != SessionStatus.CLOSED)
            .map(this::toInfo)
            .forEach(info -> send(emitter, info));
        return emitter;
    }

    // ========== SessionStateListener ==========

    @Override
    public void onSessionCreated(SessionLifecycleEvent event) {
        broadcast(toInfo(event.getSession()));
    }

    @Override
    public void onSessionAssigned(SessionLifecycleEvent event) {
        broadcast(toInfo(event.getSession()));
    }

    @Override
    public void onSessionActivated(SessionLifecycleEvent event) {
        broadcast(toInfo(event.getSession()));
    }

    @Override
    public void onSessionTransferred(SessionLifecycleEvent event) {
        broadcast(toInfo(event.getSession()));
    }

    @Override
    public void onSessionClosed(SessionLifecycleEvent event) {
        // 关闭时强制标记 CLOSED, 前端据此把会话从列表移除
        SessionInfo info = toInfo(event.getSession());
        info.setStatus(SessionStatus.CLOSED.name());
        broadcast(info);
    }

    // ========== 私有方法 ==========

    private void broadcast(SessionInfo info) {
        for (SseEmitter emitter : emitters) {
            send(emitter, info);
        }
    }

    private void send(SseEmitter emitter, SessionInfo info) {
        try {
            emitter.send(SseEmitter.event().name("session").data(objectMapper.writeValueAsString(info)));
        } catch (IOException | IllegalStateException e) {
            // 连接已失效, 移除订阅
            emitters.remove(emitter);
        }
    }

    private SessionInfo toInfo(Session session) {
        SessionInfo info = new SessionInfo();
        info.setSessionId(session.getSessionId());
        info.setCustomerId(session.getCustomerId());
        info.setCustomerName(session.getCustomerName());
        info.setAgentId(session.getAgentId());
        info.setStatus(session.getStatus().name());
        info.setBackendId(session.getBackendId());
        info.setCreateTime(session.getCreateTime());
        info.setUpdateTime(session.getUpdateTime());
        return info;
    }
}