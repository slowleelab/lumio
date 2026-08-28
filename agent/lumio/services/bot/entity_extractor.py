"""确定性规则实体抽取器（快路径实体来源，0 LLM）。

现状痛点: 实体抽取此前只在 LLM 慢路径做, 快路径(规则/BERT)一律返回空实体列表,
导致默认 fast path 命中时没有任何实体, 历史实体池长期为空. 本模块补上规则层抽取,
让实体在快路径下也能稳定产出, 并与 SlotTracker 的实体词汇表严格对齐。

词汇约定: 本模块产出的 `Entity.entity_type` 使用与 `slot_tracker._ENTITY_TO_SLOT`
完全一致的规范 key (`amount`/`CARD_NUMBER`/`card_tail`/`PHONE`/`period`/`card_type`),
保证 slot 填充与指代消解直接生效。`normalize_entity_type` 则负责把 LLM 慢路径
自由生成的类型名也映射回这套规范 key。
"""

from __future__ import annotations

import re

from lumio.shared.models import Entity

# ── 实体类型规范化 (自由类型名/大小写/别名 → 规范 key) ──

# 注意: 规范 key 刻意与 slot_tracker._ENTITY_TO_SLOT 保持一致 (含大小写).
_ENTITY_TYPE_ALIASES: dict[str, str] = {
    "amount": "amount",
    "金额": "amount",
    "money": "amount",
    "card_number": "CARD_NUMBER",
    "cardnumber": "CARD_NUMBER",
    "卡号": "CARD_NUMBER",
    "full_card_number": "CARD_NUMBER",
    "card_tail": "card_tail",
    "卡尾": "card_tail",
    "后四位": "card_tail",
    "card_tail4": "card_tail",
    "card_tail_four": "card_tail",
    "phone": "PHONE",
    "PHONE": "PHONE",
    "phone_number": "PHONE",
    "手机号": "PHONE",
    "mobile": "PHONE",
    "tel": "PHONE",
    "period": "period",
    "账单周期": "period",
    "期数": "period",
    "instalment_period": "period",
    "date": "period",
    "DATE": "period",
    "时间": "period",
    "月份": "period",
    "month": "period",
    "time_range": "period",  # LLM 慢路径示例常用 loose 类型, 归一为 period
    "time": "period",
    "card_type": "card_type",
    "卡种": "card_type",
    "卡类型": "card_type",
    "card_brand": "card_type",
}


def normalize_entity_type(raw: str) -> str | None:
    """把任意实体类型名(LLM 自由生成/大小写变体/中文别名)映射到规范 key。

    Returns:
        规范 key (与 slot_tracker._ENTITY_TO_SLOT 对齐); 未知类型返回 None(丢弃)。
    """
    if not raw:
        return None
    canon = _ENTITY_TYPE_ALIASES.get(raw)
    if canon is not None:
        return canon
    # 大小写不敏感兜底
    canon = _ENTITY_TYPE_ALIASES.get(raw.lower())
    if canon is not None:
        return canon
    if raw.upper() in _ENTITY_TYPE_ALIASES:
        return _ENTITY_TYPE_ALIASES[raw.upper()]
    return None


# ── 规则抽取 ──

# 卡号: 16 位, 分组间允许连字符/空格(常见打卡展示如 "6222 8888 6666 0000")
# 前后留边界避免把 18 位身份证/金额/手机截进卡号
_RE_CARD_NUMBER = re.compile(r"(?<![0-9])[0-9]{4}[ -]?[0-9]{4}[ -]?[0-9]{4}[ -]?[0-9]{4}(?![0-9])")
# 卡号后四位: 词边界内的 4 位数字, 且前面紧邻"尾/后四位/卡尾"等提示 (避免把任意 4 位当卡尾)
_RE_CARD_TAIL = re.compile(r"(?:卡尾|后.{0,2}四位?|尾号?)是?[为:：]?\s*(\d{4})")
_RE_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
# 金额: 必须与金额单位(元/块¥￥$)紧邻 —— 纯数字串(卡号/手机号/身份证)不算金额.
# 情形1 数字在后 单位在前: "手续费是 1200 元 / £1200"; 情形2 符号在前: "¥1200".
_RE_AMOUNT = re.compile(
    r"(?<!\d)([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)(?:\.\d{1,2})?(?![0-9])\s*(?:元|块|块钱|圆|人民币)"
    r"|[$￥¥]\s*(?<!\d)([0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)(?:\.\d{1,2})?(?![0-9])"
)
# 期数/月份: "3月"/"三月"/"第3期"/"3期"/"当月"/"上期"/"本月"
_RE_PERIOD = re.compile(
    r"(第\s*\d\s*期|(?:\d|一|二|三|四|五|六|七|八|九|十(?:一|二)?)\s*[月]份?|当[月期]|上[月期]|本[月期]|这[月期]|下[个]?[月期])"
)

# 卡种关键词 (含规格/品牌)
_CARD_TYPE_KEYWORDS: list[str] = [
    "白金卡",
    "金卡",
    "普卡",
    "钻石卡",
    "无限卡",
    "钛金卡",
    "青年卡",
    "visa",
    "mastercard",
    "万事达",
    "银联",
    "运通",
    "amex",
    "jcb",
]


def _dedup(entities: list[Entity]) -> list[Entity]:
    """同类同值去重, 保留先出现的一条。"""
    seen: set[tuple[str, str]] = set()
    out: list[Entity] = []
    for e in entities:
        key = (e.entity_type, e.value)
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _extract_amount(text: str) -> list[Entity]:
    out: list[Entity] = []
    for m in _RE_AMOUNT.finditer(text):
        # 取命中的那一组 (情形1=group1, 情形2=group2)
        value = m.group(1) or m.group(2)
        if not value:
            continue
        try:
            if "," in value:
                value = value.replace(",", "")
            out.append(Entity(entity_type="amount", value=value, start=m.start(), end=m.end(), confidence=0.9))
        except Exception:
            continue
    return out


def extract_entities(text: str) -> list[Entity]:
    """从单句文本抽取实体 (快路径规则层)。

    覆盖 6 类与 slot 词汇表对齐: CARD_NUMBER / card_tail / PHONE / amount / period / card_type。
    顺序刻意: 先精准类(卡号/手机/卡尾), 后宽泛类(金额/期数/卡种), 减少误报。
    """
    out: list[Entity] = []
    # 优先级高、边界硬
    for m in _RE_CARD_NUMBER.finditer(text):
        value = re.sub(r"[^0-9]", "", m.group(0))
        out.append(Entity(entity_type="CARD_NUMBER", value=value, start=m.start(), end=m.end(), confidence=1.0))
    for m in _RE_CARD_TAIL.finditer(text):
        out.append(Entity(entity_type="card_tail", value=m.group(1), start=m.start(), end=m.end(), confidence=0.95))
    for m in _RE_PHONE.finditer(text):
        out.append(Entity(entity_type="PHONE", value=m.group(0), start=m.start(), end=m.end(), confidence=1.0))
    # 金额: 排除已被卡号/卡尾占用的位置
    card_spans = {(e.start, e.end) for e in out if e.start is not None}
    for e in _extract_amount(text):
        if (e.start, e.end) in card_spans:
            continue
        out.append(e)
    for m in _RE_PERIOD.finditer(text):
        raw = m.group(0)
        # 期数归一: "第 3 期" → "3期"; 中文明教/数字统一
        cleaned = re.sub(r"\s+", "", raw)
        out.append(Entity(entity_type="period", value=cleaned, start=m.start(), end=m.end(), confidence=0.85))
    # 卡种: 按长度降序优先长词, 并跳过已命中重叠位置, 避免 "白金卡" 同时抽出内嵌的 "金卡"
    covered: list[bool] = [False] * len(text)
    for kw in sorted(_CARD_TYPE_KEYWORDS, key=len, reverse=True):
        idx = text.lower().find(kw.lower())
        if idx == -1:
            continue
        if any(covered[idx : idx + len(kw)]):
            continue
        covered[idx : idx + len(kw)] = [True] * len(kw)
        out.append(
            Entity(
                entity_type="card_type",
                value=kw,
                start=idx,
                end=idx + len(kw),
                confidence=0.9,
            )
        )
    return _dedup(out)
