"""PII 脱敏单元测试"""

from __future__ import annotations

from lumio.shared.pii import (
    mask_bank_card,
    mask_email,
    mask_id_card,
    mask_phone,
    mask_pii,
    mask_sensitive_fields,
)


def test_mask_phone():
    """手机号脱敏: 13812345678 → 138****5678"""
    assert mask_phone("我的手机是13812345678") == "我的手机是138****5678"
    assert mask_phone("call 15900001111 now") == "call 159****1111 now"
    # 非手机号不误判（不以 1[3-9] 开头）
    assert mask_phone("12345678901") == "12345678901"


def test_mask_id_card():
    """身份证号脱敏"""
    masked = mask_id_card("身份证110101199001011234")
    assert "110101" in masked
    assert "1234" in masked
    assert "19900101" not in masked


def test_mask_bank_card():
    """银行卡号脱敏: 6222021234567890 → 6222****7890"""
    masked = mask_bank_card("卡号6222021234567890")
    assert "6222" in masked
    assert "7890" in masked
    assert "2123456" not in masked


def test_mask_email():
    """邮箱脱敏: zhangsan@example.com → z***@example.com"""
    assert mask_email("邮箱zhangsan@example.com") == "邮箱z***@example.com"
    assert mask_email("a@b.cn") == "a***@b.cn"


def test_mask_sensitive_fields():
    """JSON 敏感字段值脱敏"""
    json_str = '{"password": "mysecret123", "token": "abc-xyz"}'
    masked = mask_sensitive_fields(json_str)
    assert "mysecret123" not in masked
    assert "abc-xyz" not in masked
    assert "******" in masked


def test_mask_pii_combined():
    """混合 PII 一键脱敏"""
    text = '手机13812345678，邮箱zhangsan@test.com，{"password":"secret123"}'
    masked = mask_pii(text)
    assert "13812345678" not in masked
    assert "zhangsan@test.com" not in masked
    assert "secret123" not in masked
    assert "138****5678" in masked
    assert "z***@test.com" in masked


def test_mask_pii_empty():
    """空字符串安全处理"""
    assert mask_pii("") == ""
    assert mask_pii(None) is None  # type: ignore[arg-type]


def test_mask_pii_no_pii():
    """无 PII 文本不变"""
    text = "信用卡年费减免政策"
    assert mask_pii(text) == text


def test_pan_to_tail():
    """P1a: 完整卡号折叠尾四位 (会话 1fb54681: 明文 PAN 落库合规隐患)"""
    from lumio.shared.pii import pan_to_tail

    assert pan_to_tail("1234567890000000") == "****0000"
    assert pan_to_tail("6222 8800 6666 0000") == "****0000"  # 去分隔符
    assert pan_to_tail("6222-8800-6666-0000") == "****0000"
    # 短值/非 PAN 原样返回 (卡尾/金额不受影响)
    assert pan_to_tail("0000") == "0000"
    assert pan_to_tail("3000") == "3000"
    assert pan_to_tail("") == ""
    # 已掩码值幂等
    assert pan_to_tail("****0000") == "****0000"
