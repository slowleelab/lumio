package com.lumio.chatsvc.customerserver.websocket;

import com.lumio.chatsvc.customerserver.security.JwtVerifier;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.http.server.ServerHttpRequest;
import org.springframework.http.server.ServerHttpResponse;
import org.springframework.web.socket.WebSocketHandler;
import org.springframework.web.socket.server.HandshakeInterceptor;

import java.util.Map;
import java.util.Set;

/**
 * 坐席 WebSocket 握手鉴权 + 坐席身份绑定。
 *
 * <p>浏览器原生 WebSocket 无法自定义 HTTP Header，鉴权经 query param {@code ?token=}
 * 携带 Lumio JWT。握手校验通过后把坐席 ID 写入 handshake attributes，
 * {@link AgentChannelHandler} 从 attributes 读取（而非信任 URL 路径）。
 *
 * <p>限制：当前以 {@code role ∈ {agent, admin}} 把关，坐席 ID 仍取 URL 路径未做
 * sub→agentId 强绑定（真实坐席 SSO 绑定为后续项，见架构评审）。关闭鉴权：
 * {@code websocket.agent.auth-enabled=false}（演示环境兜底，生产必须开启）。
 */
public class AgentAuthInterceptor implements HandshakeInterceptor {
    private static final Logger LOGGER = LoggerFactory.getLogger(AgentAuthInterceptor.class);

    public static final String ATTR_AGENT_ID = "agentId";

    private static final Set<String> ALLOWED_ROLES = Set.of("agent", "admin");

    private final JwtVerifier jwtVerifier;
    private final boolean authEnabled;

    public AgentAuthInterceptor(
            JwtVerifier jwtVerifier,
            @Value("${websocket.agent.auth-enabled:true}") boolean authEnabled) {
        this.jwtVerifier = jwtVerifier;
        this.authEnabled = authEnabled;
    }

    @Override
    public boolean beforeHandshake(ServerHttpRequest request, ServerHttpResponse response,
                                   WebSocketHandler wsHandler, Map<String, Object> attributes) {
        String agentId = extractAgentId(request.getURI().getPath());
        if (agentId == null || agentId.isEmpty()) {
            LOGGER.warn("坐席握手缺少 agentId: uri={}", request.getURI());
            return false;
        }

        if (authEnabled) {
            String token = "";
            if (request instanceof org.springframework.http.server.ServletServerHttpRequest ssr) {
                token = ssr.getServletRequest().getParameter("token");
            }
            if (token == null || token.isBlank()) {
                LOGGER.warn("坐席握手缺少 token: agentId={}", agentId);
                return false;
            }
            try {
                Map<String, Object> claims = jwtVerifier.verify(token);
                String role = claims.get("role") == null ? "" : claims.get("role").toString();
                if (!ALLOWED_ROLES.contains(role)) {
                    LOGGER.warn("坐席握手角色不允许: agentId={}, role={}", agentId, role);
                    return false;
                }
            } catch (Exception e) {
                LOGGER.warn("坐席握手鉴权失败: agentId={}, err={}", agentId, e.getMessage());
                return false;
            }
        }

        attributes.put(ATTR_AGENT_ID, agentId);
        return true;
    }

    @Override
    public void afterHandshake(ServerHttpRequest request, ServerHttpResponse response,
                               WebSocketHandler wsHandler, Exception exception) {
        // no-op
    }

    private static String extractAgentId(String path) {
        String trimmed = path.endsWith("/") ? path.substring(0, path.length() - 1) : path;
        String[] parts = trimmed.split("/");
        return parts.length > 0 ? parts[parts.length - 1] : null;
    }
}