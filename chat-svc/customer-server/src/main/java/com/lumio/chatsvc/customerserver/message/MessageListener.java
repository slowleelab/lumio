package com.lumio.chatsvc.customerserver.message;

import com.lumio.chatsvc.common.model.ChatMessage;

/**
 * 消息监听器 — 在 {@link CustomerMessageStore#addMessage} 写入消息后回调。
 *
 * <p>作为消息写入的单一漏斗拦截点（MessageController、pushToCustomer 等入口都汇入
 * {@code addMessage}），供坐席实时推送通道等订阅新消息，避免各自维护广播逻辑。</p>
 */
public interface MessageListener {

    /**
     * 某条消息被写入某个会话。
     *
     * @param sessionId 会话 ID
     * @param message   已写入的消息
     */
    void onMessage(String sessionId, ChatMessage message);
}