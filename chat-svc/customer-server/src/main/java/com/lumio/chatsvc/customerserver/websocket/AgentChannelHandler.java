package com.lumio.chatsvc.customerserver.websocket;

import com.fasterxml.jackson.databind.ObjectMapper;
import com.lumio.chatsvc.common.model.ChatMessage;
import com.lumio.chatsvc.common.model.SenderType;
import com.lumio.chatsvc.common.model.Session;
import com.lumio.chatsvc.common.model.SessionStatus;
import com.lumio.chatsvc.common.model.SessionSubStatus;
import com.lumio.chatsvc.customerserver.client.LumioClient;
import com.lumio.chatsvc.customerserver.dto.SessionInfo;
import com.lumio.chatsvc.customerserver.message.CustomerMessageStore;
import com.lumio.chatsvc.customerserver.message.MessageListener;
import com.lumio.chatsvc.customerserver.session.SessionLifecycleEvent;
import com.lumio.chatsvc.customerserver.session.SessionManager;
import com.lumio.chatsvc.customerserver.session.SessionStateListener;
import com.lumio.chatsvc.customerserver.session.SessionStateTransitionManager;
import com.lumio.chatsvc.customerserver.session.SessionStore;
import jakarta.annotation.PostConstruct;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.context.annotation.Lazy;
import org.springframework.stereotype.Component;
import org.springframework.web.socket.CloseStatus;
import org.springframework.web.socket.TextMessage;
import org.springframework.web.socket.WebSocketSession;
import org.springframework.web.socket.handler.TextWebSocketHandler;

import java.io.IOException;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.UUID;
import java.util.concurrent.ConcurrentHashMap;

/**
 * 坐席实时通道 (WebSocket) — 坐席端聊天框所有数据交互的统一入口。
 *
 * <p>端点: {@code /ws/agent/{agentId}}。坐席前端通过它：
 * <ul>
 *   <li>接收会话 / 客户进线事件: {@code {type:"session", ... SessionInfo}}</li>
 *   <li>接收新消息: {@code {type:"message", session_id, sender, content, messageId, timestamp}}</li>
 *   <li>发送坐席消息: 上行 {@code {type:"agent_message", session_id, content}}</li>
 * </ul>
 * 连接建立时先下发当前非关闭会话快照, 供前端增量合并与断线重连对账。
 *
 * <p>同时实现了 {@link SessionStateListener}（会话生命周期）与 {@link MessageListener}
 * （新消息写入）两个订阅，统一向所有在线坐席广播。</p>
 */
@Component
public class AgentChannelHandler extends TextWebSocketHandler
        implements SessionStateListener, MessageListener {
    private static final Logger LOGGER = LoggerFactory.getLogger(AgentChannelHandler.class);

    private final SessionStateTransitionManager transitionManager;
    private final SessionStore sessionStore;
    private final SessionManager sessionManager;
    private final CustomerMessageStore messageStore;
    private final CustomerWebSocketHandler customerWebSocketHandler;
    private final LumioClient lumioClient;
    private final ObjectMapper objectMapper;

    // agentId -> WebSocketSession
    private final Map<String, WebSocketSession> agentSessions = new ConcurrentHashMap<>();
    // WebSocket session id -> agentId
    private final Map<String, String> webSocketToAgent = new ConcurrentHashMap<>();

    public AgentChannelHandler(SessionStateTransitionManager transitionManager,
                               SessionStore sessionStore,
                               SessionManager sessionManager,
                               CustomerMessageStore messageStore,
                               @Lazy CustomerWebSocketHandler customerWebSocketHandler,
                               LumioClient lumioClient,
                               ObjectMapper objectMapper) {
        this.transitionManager = transitionManager;
        this.sessionStore = sessionStore;
        this.sessionManager = sessionManager;
        this.messageStore = messageStore;
        this.customerWebSocketHandler = customerWebSocketHandler;
        this.lumioClient = lumioClient;
        this.objectMapper = objectMapper;
    }

    @PostConstruct
    public void init() {
        transitionManager.addListener(this);
        messageStore.addMessageListener(this);
    }

    @Override
    public void afterConnectionEstablished(WebSocketSession session) throws Exception {
        // 坐席身份优先取自握手鉴权写入的 attributes, 兜底回退到路径提取
        Object attrAgentId = session.getAttributes().get(AgentAuthInterceptor.ATTR_AGENT_ID);
        String agentId = attrAgentId != null ? attrAgentId.toString() : extractAgentId(session);
        if (agentId == null || agentId.isEmpty()) {
            LOGGER.warn("坐席 WebSocket 连接缺少 agentId: wsId={}, uri={}",
                    session.getId(), session.getUri());
            session.close(CloseStatus.BAD_DATA);
            return;
        }

        agentSessions.put(agentId, session);
        webSocketToAgent.put(session.getId(), agentId);
        LOGGER.info("坐席 WebSocket 连接建立: wsId={}, agentId={}", session.getId(), agentId);

        // 连接即推送当前归属本坐席的非关闭会话快照（不再全量广播）
        sessionStore.findByAgentId(agentId).stream()
                .filter(s -> s.getStatus() != SessionStatus.CLOSED)
                .map(this::toInfo)
                .forEach(info -> send(session, sessionFrame(info)));
    }

    @Override
    protected void handleTextMessage(WebSocketSession session, TextMessage message) throws Exception {
        Map<String, Object> frame;
        try {
            frame = objectMapper.readValue(message.getPayload(), Map.class);
        } catch (Exception e) {
            sendError(session, "消息格式错误");
            return;
        }

        String type = (String) frame.get("type");
        if (type == null) {
            sendError(session, "缺少 type 字段");
            return;
        }

        // 心跳
        if ("PING".equals(type)) {
            Map<String, Object> pong = new LinkedHashMap<>();
            pong.put("type", "PONG");
            pong.put("timestamp", System.currentTimeMillis());
            send(session, pong);
            return;
        }

        // 坐席发送消息
        if ("agent_message".equals(type)) {
            String sessionId = (String) frame.get("session_id");
            String content = (String) frame.get("content");
            if (sessionId == null || sessionId.isEmpty() || content == null || content.isEmpty()) {
                sendError(session, "agent_message 缺少 session_id 或 content");
                return;
            }
            handleAgentSend(session, sessionId, content);
            return;
        }

        // 会话控制帧: 挂起/恢复/话后小结/结束 (均由会话归属坐席驱动, 走同一所有权校验)
        switch (type) {
            case "hold" -> handleControl(session, frame, SessionSubStatus.ON_HOLD);
            case "resume" -> handleControl(session, frame, SessionSubStatus.IN_CALL);
            case "review_submit" -> handleControl(session, frame, SessionSubStatus.REVIEWING);
            case "close_session" -> handleCloseSession(session, frame);
            default -> sendError(session, "未知 type: " + type);
        }
    }

    /**
     * 处理会话控制帧(hold/resume/review_submit)。校验所有权与状态转换合法性后,
     * 更新 session.subStatus 并同步 Lumio 阶段。
     */
    private void handleControl(WebSocketSession ws, Map<String, Object> frame, SessionSubStatus target) {
        String sessionId = (String) frame.get("session_id");
        String agentId = webSocketToAgent.get(ws.getId());
        Session session = sessionStore.findById(sessionId).orElse(null);

        if (session == null || !agentId.equals(session.getAgentId())) {
            sendError(ws, "无权操作该会话（会话不属于当前坐席）");
            return;
        }
        if (session.getStatus() != SessionStatus.ACTIVE) {
            sendError(ws, "仅通话中的会话可执行该操作");
            return;
        }

        SessionSubStatus from = session.getSubStatus();
        // 状态转换合法性: on_hold 只能来自 in_call; resume 只能来自 on_hold;
        // review_submit 只能来自 in_call (Lumio VALID_TRANSITIONS: active→on_hold/reviewing, on_hold→active)
        boolean valid;
        switch (target) {
            case ON_HOLD -> valid = from == null || from == SessionSubStatus.IN_CALL;
            case IN_CALL -> valid = from == SessionSubStatus.ON_HOLD;
            case REVIEWING -> valid = from == SessionSubStatus.IN_CALL;
            default -> valid = false;
        }
        if (!valid) {
            sendError(ws, "当前会话子状态不允许该操作: " + (from == null ? "(无)" : from.getValue()));
            return;
        }

        session.setSubStatus(target);
        sessionStore.save(session);
        lumioClient.notifySessionUpdate(sessionId, "agent", target.toLumioSubPhase(), agentId);

        Map<String, Object> ack = new LinkedHashMap<>();
        ack.put("type", "status_accepted");
        ack.put("session_id", sessionId);
        ack.put("sub_status", target.getValue());
        send(ws, ack);
        LOGGER.info("会话子状态变更: sessionId={}, target={}, agentId={}", sessionId, target.getValue(), agentId);
    }

    /** 结束会话: 归属校验后走现有 closeSession 流程, 由 LumioSessionListener 通知 Lumio ended。 */
    private void handleCloseSession(WebSocketSession ws, Map<String, Object> frame) {
        String sessionId = (String) frame.get("session_id");
        String agentId = webSocketToAgent.get(ws.getId());
        Session session = sessionStore.findById(sessionId).orElse(null);
        if (session == null || !agentId.equals(session.getAgentId())) {
            sendError(ws, "无权操作该会话（会话不属于当前坐席）");
            return;
        }
        sessionManager.closeSession(sessionId);
        // 同步 Lumio: 话后关闭 → ended, 闭合 queued→…→reviewing→ended 末段
        String endReason = (String) frame.get("end_reason");
        lumioClient.notifySessionUpdate(sessionId, "ended", null, agentId, endReason);
        Map<String, Object> ack = new LinkedHashMap<>();
        ack.put("type", "session_closed");
        ack.put("session_id", sessionId);
        send(ws, ack);
    }

    private void handleAgentSend(WebSocketSession ws, String sessionId, String content) {
        String agentId = webSocketToAgent.get(ws.getId());
        // 上行防越权: 会话必须归属当前坐席
        Session session = sessionStore.findById(sessionId).orElse(null);
        if (session == null || !agentId.equals(session.getAgentId())) {
            LOGGER.warn("坐席越权发送被拒绝: agentId={}, sessionId={} (非本坐席会话)",
                    agentId, sessionId);
            Map<String, Object> err = new LinkedHashMap<>();
            err.put("type", "error");
            err.put("error", "FORBIDDEN");
            err.put("message", "无权向该会话发送消息（会话不属于当前坐席）");
            send(ws, err);
            return;
        }

        ChatMessage msg = new ChatMessage(sessionId, SenderType.AGENT, "agent", content);
        msg.setMessageId(UUID.randomUUID().toString());

        // 落库 + 实时推给客户 (客户在线走 WS, 否则等待其长轮询)
        customerWebSocketHandler.pushToCustomer(sessionId, msg);

        Map<String, Object> ack = new LinkedHashMap<>();
        ack.put("type", "message_accepted");
        ack.put("session_id", sessionId);
        ack.put("messageId", msg.getMessageId());
        ack.put("timestamp", msg.getTimestamp());
        send(ws, ack);
        LOGGER.info("坐席已发送消息: agentId↔message, sessionId={}, messageId={}",
                sessionId, msg.getMessageId());
    }

    @Override
    public void afterConnectionClosed(WebSocketSession session, CloseStatus status) throws Exception {
        String agentId = webSocketToAgent.remove(session.getId());
        if (agentId != null) {
            // 仅当当前注册的还是这条连接时才移除, 避免覆盖更晚建立的连接
            agentSessions.remove(agentId, session);
        }
        LOGGER.info("坐席 WebSocket 连接关闭: wsId={}, agentId={}, status={}",
                session.getId(), agentId, status);
    }

    @Override
    public void handleTransportError(WebSocketSession session, Throwable exception) throws Exception {
        LOGGER.error("坐席 WebSocket 传输错误: wsId={}", session.getId(), exception);
    }

    // ========== SessionStateListener: 会话/客户进线实时推送 ==========

    @Override
    public void onSessionCreated(SessionLifecycleEvent event) {
        sendToAgent(event.getSession().getAgentId(), sessionFrame(toInfo(event.getSession())));
    }

    @Override
    public void onSessionAssigned(SessionLifecycleEvent event) {
        sendToAgent(event.getSession().getAgentId(), sessionFrame(toInfo(event.getSession())));
    }

    @Override
    public void onSessionActivated(SessionLifecycleEvent event) {
        sendToAgent(event.getSession().getAgentId(), sessionFrame(toInfo(event.getSession())));
    }

    @Override
    public void onSessionTransferred(SessionLifecycleEvent event) {
        sendToAgent(event.getSession().getAgentId(), sessionFrame(toInfo(event.getSession())));
    }

    @Override
    public void onSessionClosed(SessionLifecycleEvent event) {
        SessionInfo info = toInfo(event.getSession());
        info.setStatus(SessionStatus.CLOSED.name());
        sendToAgent(event.getSession().getAgentId(), sessionFrame(info));
    }

    // ========== MessageListener: 新消息实时推送 ==========

    @Override
    public void onMessage(String sessionId, ChatMessage message) {
        // 只推客户消息; 坐席自己的发送由前端乐观追加, 避免回显重复
        if (message.getSenderType() != SenderType.CUSTOMER) {
            return;
        }
        Map<String, Object> frame = new LinkedHashMap<>();
        frame.put("type", "message");
        frame.put("session_id", message.getSessionId());
        frame.put("sender", "customer");
        frame.put("sender_name", message.getSenderName());
        frame.put("content", message.getContent());
        frame.put("messageId", message.getMessageId());
        frame.put("timestamp", message.getTimestamp());
        frame.put("seq", message.getSeq());
        // 定向推给该会话所属坐席 (而非全量广播)
        String owner = sessionStore.findById(sessionId)
                .map(Session::getAgentId)
                .orElse(null);
        sendToAgent(owner, frame);
    }

    // ========== 私有方法 ==========

    /** 向指定坐席的连接推送帧; owner 为空/无在线连接时丢弃 */
    private void sendToAgent(String agentId, Map<String, Object> frame) {
        if (agentId == null || agentId.isEmpty()) {
            return;
        }
        send(agentSessions.get(agentId), frame);
    }

    private void send(WebSocketSession ws, Object message) {
        if (ws == null || !ws.isOpen()) {
            return;
        }
        try {
            String json = objectMapper.writeValueAsString(message);
            ws.sendMessage(new TextMessage(json));
        } catch (IOException e) {
            LOGGER.error("坐席 WS 发送失败: wsId={}", ws.getId(), e);
        }
    }

    private void sendError(WebSocketSession ws, String error) {
        Map<String, Object> frame = new LinkedHashMap<>();
        frame.put("type", "error");
        frame.put("message", error);
        send(ws, frame);
    }

    private Map<String, Object> sessionFrame(SessionInfo info) {
        Map<String, Object> frame = new LinkedHashMap<>();
        frame.put("type", "session");
        frame.put("sessionId", info.getSessionId());
        frame.put("customerId", info.getCustomerId());
        frame.put("customerName", info.getCustomerName());
        frame.put("agentId", info.getAgentId());
        frame.put("status", info.getStatus());
        frame.put("backendId", info.getBackendId());
        frame.put("createTime", info.getCreateTime());
        frame.put("updateTime", info.getUpdateTime());
        return frame;
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
        info.setLastSeq(messageStore.getLastSeq(session.getSessionId()));
        return info;
    }

    private String extractAgentId(WebSocketSession session) {
        String path = session.getUri().getPath();
        // 兼容 /ws/agent/{agentId} 与 /ws/agent/{agentId}/
        String trimmed = path.endsWith("/") ? path.substring(0, path.length() - 1) : path;
        String[] parts = trimmed.split("/");
        return parts.length > 0 ? parts[parts.length - 1] : null;
    }
}