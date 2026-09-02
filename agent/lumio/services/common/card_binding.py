"""客户绑定卡号解析（mock）

银行客服的完整卡号不来自对话（红线禁止在对话里索要完整卡号，只允许卡号后四位做身份提示），
而来自客户的实名绑定关系。工具编排在执行前按 ``customer_id`` 解析出绑定卡号注入
``arguments.card_no``，与 LLM 从对话里收集的信息解耦。

生产环境接实名/客户信息服务；开发期用 mock 映射，让工具编排跑通全链路。
"""

from __future__ import annotations

import re

# mock 映射：customer_id -> 绑定卡号（开发期演示用，不对接真实持卡人信息）
_MOCK_CARD_BY_CUSTOMER: dict[str, str] = {
    "cust-1": "6225880012346780",
    "cust-2": "6222021234567890",
}

# 未识别 customer_id 时回退的默认测试卡号
_DEFAULT_CARD = "6225880012346780"

_CARD_NO_RE = re.compile(r"\d{13,19}")


def resolve_card_no(customer_id: str | None) -> str:
    """按 customer_id 解析绑定卡号（mock）。

    未识别的 customer_id（含 None）返回默认测试卡号，保证开发期工具调用有值可用。
    """
    if customer_id and customer_id in _MOCK_CARD_BY_CUSTOMER:
        return _MOCK_CARD_BY_CUSTOMER[customer_id]
    return _DEFAULT_CARD


def is_full_card_no(value: object) -> bool:
    """是否为完整卡号（13-19 位数字）。用于判断注入是否必要：LLM 已给出完整卡号则不覆盖。"""
    return isinstance(value, str) and bool(_CARD_NO_RE.fullmatch(value.strip()))


def schema_declares_card_no(input_schema: dict) -> str | None:
    """工具入参 schema 声明的卡号参数名（仅对这类工具注入绑定卡号）。

    返回 schema 实际声明的键名（'card_no' / 'cardNo'），未声明返回 None —
    注入时用返回的键名, 避免 snake_case 注入 camelCase schema 导致缺参
    (v2 链 B 首跑实测: 注入 card_no 而 query_card_bill 要求 cardNo)。
    挂失/投诉等用其他参数名（如 card）的工具不注入, 避免污染入参。
    """
    properties = (input_schema or {}).get("properties") or {}
    for key in ("card_no", "cardNo"):
        if key in properties:
            return key
    return None
