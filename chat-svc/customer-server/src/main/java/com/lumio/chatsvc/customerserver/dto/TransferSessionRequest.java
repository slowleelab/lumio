package com.lumio.chatsvc.customerserver.dto;

import com.fasterxml.jackson.annotation.JsonProperty;

import java.util.List;
import java.util.Map;

public class TransferSessionRequest {
    /** 字段名对齐 bot (Python) 与前端发送的 snake_case JSON, 否则 Jackson 无法绑定到 camelCase 属性 */
    @JsonProperty("session_id")
    private String sessionId;
    @JsonProperty("customer_id")
    private String customerId;
    @JsonProperty("customer_name")
    private String customerName;
    @JsonProperty("transfer_reason")
    private String transferReason;
    @JsonProperty("transfer_summary")
    private String transferSummary;
    private List<Map<String, String>> history;
    private String intent;
    private String sentiment;
    @JsonProperty("vip_level")
    private String vipLevel;

    public String getSessionId() { return sessionId; }
    public void setSessionId(String s) { this.sessionId = s; }
    public String getCustomerId() { return customerId; }
    public void setCustomerId(String s) { this.customerId = s; }
    public String getCustomerName() { return customerName; }
    public void setCustomerName(String s) { this.customerName = s; }
    public String getTransferReason() { return transferReason; }
    public void setTransferReason(String s) { this.transferReason = s; }
    public String getTransferSummary() { return transferSummary; }
    public void setTransferSummary(String s) { this.transferSummary = s; }
    public List<Map<String, String>> getHistory() { return history; }
    public void setHistory(List<Map<String, String>> h) { this.history = h; }
    public String getIntent() { return intent; }
    public void setIntent(String s) { this.intent = s; }
    public String getSentiment() { return sentiment; }
    public void setSentiment(String s) { this.sentiment = s; }
    public String getVipLevel() { return vipLevel; }
    public void setVipLevel(String s) { this.vipLevel = s; }
}
