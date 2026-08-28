package com.lumio.chatsvc.customerserver.security;

import com.fasterxml.jackson.databind.ObjectMapper;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.stereotype.Component;

import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.Base64;
import java.util.Map;

/**
 * 轻量 JWT(HS256) 校验器 — 与 Lumio(agent/lumio/shared/auth.py) 同一签发密钥与声明约定。
 *
 * <p>使用 JDK 自带 {@code javax.crypto.Mac} 实现 HMAC-SHA256，不引入额外依赖。
 * 校验签名、过期时间、iss(issuer=lumio)/aud(audience=lumio-api)；角色取自 {@code role} 声明。
 *
 * <p>密钥与 Python 侧 {@code LUMIO_JWT_SECRET} 对齐（默认即占位密钥，生产必须覆盖）。
 */
@Component
public class JwtVerifier {
    private static final Logger LOGGER = LoggerFactory.getLogger(JwtVerifier.class);

    private static final String HMAC_ALG = "HmacSHA256";
    private static final String ISSUER = "lumio";
    private static final String AUDIENCE = "lumio-api";

    private final String secret;
    private final ObjectMapper objectMapper;

    public JwtVerifier(
            @Value("${lumio.jwt.secret:lumio-dev-secret-change-in-production}") String secret,
            ObjectMapper objectMapper) {
        this.secret = secret;
        this.objectMapper = objectMapper;
    }

    /**
     * 校验 JWT 并返回 payload 声明。无效/过期/签发不符时抛 {@link InvalidJwtException}。
     */
    @SuppressWarnings("unchecked")
    public Map<String, Object> verify(String token) throws InvalidJwtException {
        String[] parts = token.split("\\.");
        if (parts.length != 3) {
            throw new InvalidJwtException("JWT 结构非法");
        }
        String signingInput = parts[0] + "." + parts[1];

        try {
            // 1. 签名校验（HS256）
            Mac mac = Mac.getInstance(HMAC_ALG);
            mac.init(new SecretKeySpec(secret.getBytes(StandardCharsets.UTF_8), HMAC_ALG));
            byte[] expected = mac.doFinal(signingInput.getBytes(StandardCharsets.UTF_8));
            byte[] actual = Base64.getUrlDecoder().decode(parts[2]);
            if (!MessageDigest.isEqual(expected, actual)) {
                throw new InvalidJwtException("JWT 签名校验失败");
            }

            // 2. 解析声明
            byte[] claimsBytes = Base64.getUrlDecoder().decode(parts[1]);
            String claimsJson = new String(claimsBytes, StandardCharsets.UTF_8);
            Map<String, Object> claims = objectMapper.readValue(claimsJson, Map.class);

            // 3. iss / aud / exp
            if (!ISSUER.equals(claims.get("iss"))) {
                throw new InvalidJwtException("iss 不符");
            }
            if (!AUDIENCE.equals(claims.get("aud"))) {
                throw new InvalidJwtException("aud 不符");
            }
            Object exp = claims.get("exp");
            if (exp instanceof Number n) {
                long nowSeconds = System.currentTimeMillis() / 1000L;
                if (n.longValue() < nowSeconds) {
                    throw new InvalidJwtException("JWT 已过期");
                }
            } else {
                throw new InvalidJwtException("JWT 缺少 exp");
            }
            return claims;
        } catch (InvalidJwtException e) {
            throw e;
        } catch (Exception e) {
            LOGGER.debug("JWT 校验失败", e);
            throw new InvalidJwtException("JWT 解析失败: " + e.getMessage());
        }
    }

    /** JWT 校验失败异常 */
    public static class InvalidJwtException extends Exception {
        public InvalidJwtException(String message) {
            super(message);
        }
    }
}