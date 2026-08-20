package com.lumio.chatsvc.customerserver.websocket;

import com.lumio.chatsvc.customerserver.config.WebSocketProperties;
import com.lumio.chatsvc.customerserver.security.JwtVerifier;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.autoconfigure.condition.ConditionalOnProperty;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.socket.config.annotation.EnableWebSocket;
import org.springframework.web.socket.config.annotation.WebSocketConfigurer;
import org.springframework.web.socket.config.annotation.WebSocketHandlerRegistry;
import org.springframework.web.socket.server.standard.ServletServerContainerFactoryBean;

import java.time.Duration;

/**
 * 客户 WebSocket 配置
 */
@Configuration
@EnableWebSocket
@ConditionalOnProperty(prefix = "websocket.customer", name = "enabled", havingValue = "true", matchIfMissing = false)
public class WebSocketConfig implements WebSocketConfigurer {
    private static final Logger LOGGER = LoggerFactory.getLogger(WebSocketConfig.class);

    private final CustomerWebSocketHandler customerWebSocketHandler;
    private final AgentChannelHandler agentChannelHandler;
    private final WebSocketProperties webSocketProperties;
    private final JwtVerifier jwtVerifier;

    @Autowired
    public WebSocketConfig(CustomerWebSocketHandler customerWebSocketHandler,
                           AgentChannelHandler agentChannelHandler,
                           WebSocketProperties webSocketProperties,
                           JwtVerifier jwtVerifier) {
        this.customerWebSocketHandler = customerWebSocketHandler;
        this.agentChannelHandler = agentChannelHandler;
        this.webSocketProperties = webSocketProperties;
        this.jwtVerifier = jwtVerifier;
    }

    @Override
    public void registerWebSocketHandlers(WebSocketHandlerRegistry registry) {
        String path = webSocketProperties.getCustomer().getPath();
        LOGGER.info("注册客户 WebSocket 处理器: {}/*", path);
        // 支持 /ws/customer/{sessionId} 格式的路径
        registry.addHandler(customerWebSocketHandler, path + "/*")
                .setAllowedOrigins("*");

        // 坐席实时通道: /ws/agent/{agentId}
        // 同时注册 /api/ws/agent/{agentId}: 前端经 Vite 代理走 /api/chat-svc -> /api 重写后为 /api/ws/...
        // (Spring 的 HTTP 端点统一带 /api 前缀, 而 WS 处理器原本注册在裸 /ws 下, 故二者都挂上)
        LOGGER.info("注册坐席 WebSocket 处理器: /ws/agent/* 与 /api/ws/agent/*");
        registry.addHandler(agentChannelHandler, "/ws/agent/*", "/api/ws/agent/*")
                .addInterceptors(new AgentAuthInterceptor(jwtVerifier,
                        webSocketProperties.getAgent().isAuthEnabled()))
                .setAllowedOrigins("*");
    }

    @Bean
    public ServletServerContainerFactoryBean createWebSocketContainer() {
        ServletServerContainerFactoryBean container = new ServletServerContainerFactoryBean();
        container.setMaxTextMessageBufferSize(8192);
        container.setMaxBinaryMessageBufferSize(8192);
        container.setMaxSessionIdleTimeout(Duration.ofMinutes(30).toMillis());
        return container;
    }
}
