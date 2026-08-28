"""客户绑定卡号解析单元测试 (会话 1efbd1ad 排查: 查询类工具 card_no 缺口)"""

from __future__ import annotations

from lumio.services.common.card_binding import is_full_card_no, resolve_card_no


def test_resolve_card_no_known_customer() -> None:
    """已知 customer_id 返回对应绑定卡号"""
    assert resolve_card_no("cust-1") == "6225880012346780"
    assert resolve_card_no("cust-2") == "6222021234567890"


def test_resolve_card_no_unknown_falls_back_to_default() -> None:
    """未知/空 customer_id 返回默认测试卡号"""
    assert resolve_card_no(None) == "6225880012346780"
    assert resolve_card_no("unknown") == "6225880012346780"


def test_is_full_card_no() -> None:
    """完整卡号判定: 13-19 位数字"""
    assert is_full_card_no("6225880012346780") is True
    assert is_full_card_no("4879") is False  # 后四位不是完整卡号
    assert is_full_card_no("") is False
    assert is_full_card_no(None) is False
    assert is_full_card_no("abc") is False
    assert is_full_card_no("6225 8800 1234 6780") is False  # 含空格不判完整
