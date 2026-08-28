package com.lumio.chatsvc.customerserver.controller;

import com.lumio.chatsvc.common.model.ChatMessage;
import com.lumio.chatsvc.common.model.SenderType;
import com.lumio.chatsvc.common.model.Session;
import com.lumio.chatsvc.customerserver.client.LumioClient;
import com.lumio.chatsvc.customerserver.message.CustomerMessageStore;
import com.lumio.chatsvc.customerserver.security.JwtVerifier;
import com.lumio.chatsvc.customerserver.session.SessionManager;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.annotation.*;

import java.util.*;

@RestController
@RequestMapping("/api/sessions")
public class MessageController {
    private static final Logger log = LoggerFactory.getLogger(MessageController.class);
    private final CustomerMessageStore messageStore;
    private final LumioClient lumioClient;
    private final SessionManager sessionManager;
    private final JwtVerifier jwtVerifier;

    @Value("${websocket.agent.auth-enabled:true}")
    private boolean authEnabled;

    public MessageController(CustomerMessageStore messageStore, LumioClient lumioClient,
                             SessionManager sessionManager, JwtVerifier jwtVerifier) {
        this.messageStore = messageStore;
        this.lumioClient = lumioClient;
        this.sessionManager = sessionManager;
        this.jwtVerifier = jwtVerifier;
    }

    @PostMapping("/{sessionId}/messages")
    public ResponseEntity<Map<String, Object>> sendMessage(
            @PathVariable String sessionId,
            @RequestBody Map<String, String> body) {
        String sender = body.getOrDefault("sender", "customer");
        String content = body.getOrDefault("content", "");

        SenderType senderType = "agent".equals(sender) ? SenderType.AGENT : SenderType.CUSTOMER;
        ChatMessage msg = new ChatMessage(sessionId, senderType, sender, content);
        msg.setMessageId(UUID.randomUUID().toString());

        messageStore.addMessage(sessionId, msg);
        log.debug("Message stored: session={} sender={}", sessionId, sender);

        // 客户消息 → 路由到坐席 + 异步回调 Lumio 进行 AI 分析
        if ("customer".equals(sender)) {
            // 路由消息到坐席（如果会话已分配坐席）
            sessionManager.routeMessage(sessionId, msg);

            String customerId = body.getOrDefault("customer_id", null);
            lumioClient.analyzeMessage(sessionId, content, customerId);
        }

        Map<String, Object> resp = new LinkedHashMap<>();
        resp.put("accepted", true);
        resp.put("messageId", msg.getMessageId());
        resp.put("timestamp", msg.getTimestamp());
        return ResponseEntity.ok(resp);
    }

    /** 非阻塞读取所有消息（不删除，多端共享）。坐席侧读取需鉴权 + 归属校验。
     *  since 为 seq 游标：>0 时只返回 seq > since 的消息，用于历史/离线补发。 */
    @GetMapping("/{sessionId}/messages")
    public ResponseEntity<List<Map<String, Object>>> getMessages(
            @PathVariable String sessionId,
            @RequestParam(defaultValue = "0") long since,
            @RequestHeader(value = "Authorization", defaultValue = "") String authorization,
            @RequestParam(value = "agent_id", required = false) String agentIdParam) {
        if (!authorizeAgentHistoryRead(sessionId, authorization, agentIdParam)) {
            return ResponseEntity.status(403).build();
        }
        List<ChatMessage> pending;
        if (since > 0) {
            pending = messageStore.getMessagesSince(sessionId, since);
        } else {
            pending = messageStore.getPendingMessages(sessionId);
        }
        return ResponseEntity.ok(toResultList(pending));
    }

    /**
     * HTTP 长轮询：阻塞等待新消息，超时返回空列表。
     * 支持 since(seq) 游标参数 — 只返回 seq > since 的消息，多端独立轮询互不争抢。
     */
    @GetMapping("/{sessionId}/poll")
    public ResponseEntity<List<Map<String, Object>>> pollMessages(
            @PathVariable String sessionId,
            @RequestParam(defaultValue = "25000") long timeout,
            @RequestParam(defaultValue = "0") long since) {
        List<ChatMessage> messages = messageStore.pollMessages(sessionId, since, timeout);
        return ResponseEntity.ok(toResultList(messages));
    }

    /** 坐席历史读取鉴权：有效 token 且为 agent/admin 角色；可选 agent_id 参数做归属二次校验。 */
    private boolean authorizeAgentHistoryRead(String sessionId, String authorization,
                                               String agentIdParam) {
        if (!authEnabled) {
            return true; // 与坐席 WS 同一开关：演示环境可整体关闭鉴权
        }
        String token = authorization.startsWith("Bearer ") ? authorization.substring(7) : "";
        Map<String, Object> claims;
        try {
            claims = jwtVerifier.verify(token);
        } catch (Exception e) {
            return false;
        }
        String role = claims.get("role") == null ? "" : claims.get("role").toString();
        if (!"agent".equals(role) && !"admin".equals(role)) {
            return false;
        }
        // 归属二次校验：若显式带 agent_id 参数，则必须与会话所属坐席一致
        if (agentIdParam != null && !agentIdParam.isBlank()) {
            return sessionManager.getSession(sessionId)
                    .map(Session::getAgentId)
                    .map(agentIdParam::equals)
                    .orElse(false);
        }
        return true;
    }

    private List<Map<String, Object>> toResultList(List<ChatMessage> messages) {
        List<Map<String, Object>> result = new ArrayList<>();
        for (ChatMessage m : messages) {
            Map<String, Object> item = new LinkedHashMap<>();
            item.put("messageId", m.getMessageId());
            item.put("sessionId", m.getSessionId());
            item.put("sender", m.getSenderType() == SenderType.AGENT ? "agent" : "customer");
            item.put("content", m.getContent());
            item.put("timestamp", m.getTimestamp());
            item.put("seq", m.getSeq());
            result.add(item);
        }
        return result;
    }
}
