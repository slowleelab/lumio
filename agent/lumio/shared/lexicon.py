"""策略词表统一加载器 (闭环 v2 防线②)

十几轮闭环沉淀的词表曾散落在 bot_agent / classifier / retrieval / faq_service
四模块, 彼此不可见 — 每个 badcase 往其中一张表加一行, 表间交互不可控。
现收口为单一版本化文件 data/policy/lexicon.json, 本模块提供带缓存的
读取入口; 各业务模块从这里取词, 不再各自硬编码。

纪律: 改词表必须过金标回归集 (tests/test_golden_regression.py)。
文件缺失/字段缺失时回退到内置默认 (与服务不可因词表文件损坏而崩),
并打 WARNING 提示词表未生效。
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_LEXICON_PATH = Path(__file__).resolve().parents[2] / "data" / "policy" / "lexicon.json"

# 内置兜底 (与 lexicon.json v1 同值) — 文件损坏时保底, 正常情况不会用到
_FALLBACK: dict = {
    "emergency_markers": ["挂失", "被盗", "被偷", "盗刷", "停卡", "冻结", "卡丢", "丢了卡", "钱包被", "找不到了", "不找了"],
    "consultative_loss_markers": ["怎么办", "怎么挂失", "如何挂失", "挂失流程", "怎么处理", "如何处理"],
    "faq_generic_grams": ["信用卡", "信用", "用卡", "的", "怎么", "如何", "什么", "可以", "吗", "怎么办", "一下", "麻烦", "请问"],
    "personal_query_markers": ["我的", "帮我查", "查一下", "查下", "还剩", "余额是", "是多少", "多少钱"],
    "retrieval_generic_grams": [
        "查看", "登陆", "登录", "办理", "操作", "咨询", "帮忙", "一下", "请问", "麻烦",
        "告诉", "了解", "相关", "问题", "业务", "银行", "网上", "客服", "怎么", "如何",
        "什么", "可以", "能为", "需要", "我想", "还是", "没有", "不是",
    ],
    "business_noun_grams": [
        "账单", "额度", "积分", "挂失", "还款", "分期", "年费", "逾期", "密码", "激活",
        "销户", "销卡", "发票", "利息", "手续费", "账单日", "还款日", "信用", "授信",
        "取现", "现金", "透支", "滞纳", "违约", "征信", "卡片", "补卡", "换卡", "盗刷",
        "钱包", "数币", "人民币", "转账", "消费", "交易", "明细", "流水", "最低还款",
        "临时额度", "还款额", "免息", "宽限", "积分兑换", "里程", "话费", "权益",
        "冻结", "解冻", "限额", "申请",
    ],
    "intent_retrieval_terms": {
        "CARD_LOSS": "信用卡挂失 挂失流程 补卡",
        "CARD_LOSS_REPORT": "信用卡挂失 挂失流程 补卡",
        "LIMIT_QUERY": "信用卡额度 可用额度 额度调整",
        "BILL_QUERY": "信用卡账单 账单查询 还款日",
        "ACCOUNT_BILL_QUERY": "信用卡账单 账单查询 还款日",
        "TRANSACTION_QUERY": "信用卡交易明细 交易记录 消费记录",
        "TXN_QUERY": "信用卡交易明细 交易记录 消费记录",
    },
}


@lru_cache(maxsize=1)
def _load() -> dict:
    try:
        data = json.loads(_LEXICON_PATH.read_text(encoding="utf-8"))
        tables = data.get("tables") or {}
        merged = dict(_FALLBACK)
        for key, spec in tables.items():
            if "values" in spec:
                merged[key] = list(spec["values"])
            elif "map" in spec:
                merged[key] = dict(spec["map"])
        return merged
    except Exception as exc:
        logger.warning("策略词表加载失败, 使用内置兜底: %s (%s)", _LEXICON_PATH, exc)
        return dict(_FALLBACK)


def lexicon_values(name: str) -> tuple[str, ...]:
    """取词表 (元组, 不可变防误改)"""
    v = _load().get(name)
    return tuple(v) if isinstance(v, list) else ()


def lexicon_map(name: str) -> dict[str, str]:
    """取映射型词表"""
    v = _load().get(name)
    return dict(v) if isinstance(v, dict) else {}
