"""多轮指代消解（回指 → 落实体）。

背景: 此前多轮"可用"全靠三条轻机制(BERT 上下文拼接 / SlotTracker 槽状态 / 回话识别豁免),
没有真正的回指消解 —— "上文提过两张卡, 这轮说'那张卡'"解不了到底指哪张。

本模块分两层:
- 规则预扫层 (0ms, 无 LLM), 按优先级处理三路回指:
  1. 显式有头词回指: 命中指示/限定词(这/那/该/此 + 张卡/卡号/手机号/笔/期/个月等)时,
     从历史实体池按类型偏好取唯一候选;
  2. 零主回指: 上文在等必填槽(missing_slots)且本句未填时, 从历史取该缺槽类型的唯一候选补齐;
  3. 纯代词回指: "它/那个/这张"等无确定头词, 当历史实体池全局恰好一个候选时解析.
  **唯一性约束是本层的安全核心**: 匹配候选恰好一个才解析; 0 个或多于 1 个 → 宁可放过
  (宁缺毋滥), 交 LLM 兜底或放弃, 规则层绝不乱猜.
- LLM 兜底层 (默认关): 仅当规则层判定"存在回指、但候选不唯一/无法确定"时按开关触发,
  让 LLM 从候选池里挑具体所指. 默认由 closed_loop.json 灰度开关控制.

输入的历史实体池来自 `SessionState.last_entities` (跨轮累积的实体池). 该池此前因
_build_result 不传 entities 而恒空, 由 A 阶段实体管线修复后真正累积。
"""

from __future__ import annotations

import logging
import re
from typing import Any

from lumio.services.bot.entity_extractor import normalize_entity_type
from lumio.shared.models import Entity

logger = logging.getLogger(__name__)

# 普通（非回指）提到的限定，为避免误抽："这种" 属于指代但往往指类别而非实体，
# 保守起见不参与指代消解。

# ── 回指触发规则: (正则, 优先考虑的历史实体类型, 有序=偏好序) ──
# "card" 语义桶的偏好序: 卡尾(最可识别) > 卡号 > 卡种. 目标类型若在历史里出现多个不同值
# (如两个卡尾) → 判定为"多候选", 规则层放弃.
_ANAPHORA_RULES: list[tuple[re.Pattern[str], list[str]]] = [
    (
        re.compile(r"(这|那|该|此)张([信用卡]{0,3}卡|白金卡|金卡|普卡|钻石卡|visa|master)?"),
        ["card_tail", "CARD_NUMBER", "card_type"],
    ),
    (
        re.compile(r"(这|那|该|此)个?(卡号|手机号|预留手机号|电话号码|手机)"),
        ["PHONE", "CARD_NUMBER"],
    ),
    (
        re.compile(r"(这|那|该|此)(一笔|笔|次)(消费|交易|金额|花费|钱|支出)?"),
        ["amount"],
    ),
    (
        # 直接带金额/账单类头词的指示代词 (如 "那个金额" / "这次还款")
        re.compile(r"(这|那|该|此)个?(金额|消费|费用|还款|花费|支出|账单)"),
        ["amount", "period"],
    ),
    (
        re.compile(r"(这|那|该|此)(期|个月|月的账单|个账单|账单|期数)"),
        ["period"],
    ),
]

# 规则层解析置信度 (命中唯一候选时给的高置信, 但 < 1 以保留余地)
_RULE_CONFIDENCE = 0.8

# 纯代词回指: 无确定头词(带头的已由 _ANAPHORA_RULES 先处理), 仅当历史实体池全局恰好
# 一个候选时解析 (高度保守). 用 search 而非锚定, 容忍 "就那个吧" 之类带语气前缀.
_RE_PRONOUN = re.compile(r"那个|这个|这种|那张|那份|它")

# 缺槽名 → 历史实体类型 (零主回指按上轮在等的槽, 从历史补该槽的唯一候选值)
_SLOT_TO_ENTITY_TYPE: dict[str, str] = {
    "amount": "amount",
    "period": "period",
    "card_tail": "card_tail",
    "card_number": "CARD_NUMBER",
    "phone_number": "PHONE",
    "card_type": "card_type",
}

# 零主回指的安全闸(避免"正常的下一句新问题"误把历史值硬塞进缺槽):
# 当用户上轮在等的缺槽, 本句既无新实体、无同样代词、无显式头词, 则只有短促的
# "确认/支应"或"点名(提到该缺槽)"才视为对缺槽内容的省略回指.
_CONFIRM_TOKENS: tuple[str, ...] = ("确认", "是的", "对的", "没错", "嗯", "对", "行", "好的", "可以", "就那个")

# 实体类型 → (点名该缺槽时出现的锚词). 用于"点名缺槽但没带值"的省略回指.
_SLOT_ANCHOR: dict[str, tuple[str, ...]] = {
    "card_tail": ("卡尾", "后四位", "尾号"),
    "CARD_NUMBER": ("卡号",),
    "PHONE": ("手机号", "电话"),
    "amount": ("金额", "那笔", "消费", "费用"),
    "period": ("期数", "账单", "月份", "一期"),
    "card_type": ("卡种", "那种卡"),
}

# 疑问/新话题标记: 命中即为"提问/另起话题", 不是对缺槽的省略回指, 不得补值
_QUESTION_RE = re.compile(r"[?？]|多少|怎么|如何|是什么|在吗|呢$|请问")


def _is_zero_anaphora_reference(text: str, missing_slots: list[tuple[str, str]]) -> bool:
    """是否构成对缺槽内容的(确认/点名型)省略回指。

    保守判定: 非提问、非新话题、简短, 且或为确认词、或点名了某个缺槽类型.
    仍需外层"历史该缺槽候选唯一"约束才会真正补值, 双层保险。
    """
    t = text.strip()
    if not t or len(t) > 18 or _QUESTION_RE.search(t):
        return False
    if any(tok in t for tok in _CONFIRM_TOKENS):
        return True
    for name, _ in missing_slots:
        etype = _SLOT_TO_ENTITY_TYPE.get(name)
        if etype and any(anchor in t for anchor in _SLOT_ANCHOR.get(etype, ())):
            return True
    return False


class AnaphoraResolver:
    """多轮指代消解器。

    Args:
        llm_client: 可选的 LLM 客户端, 用于 LLM 兜底层; 默认 None ⇒ 只走规则层。
    """

    def __init__(self, llm_client: Any | None = None) -> None:
        self._llm = llm_client

    async def resolve(
        self,
        text: str,
        history_entities: list[Entity],
        current_entities: list[Entity],
        missing_slots: list[tuple[str, str]] | None = None,
    ) -> tuple[list[Entity], dict[str, Any]]:
        """消解当前句把历史实体池中的具体所指, 合并进当前实体。

        Args:
            text: 当前用户输入。
            history_entities: 历史(跨轮)实体池, 通常来自 SessionState.last_entities。
            current_entities: 本轮已抽取的实体。
            missing_slots: 上轮仍在等待的必填槽 [(name, label), ...], 用于零主回指补槽。

        Returns:
            (enriched_entities, meta)。meta 含 {triggered, kind, source, candidates, resolved} 供观测。
        """
        hist: list[Entity] = []
        for e in history_entities or []:
            if isinstance(e, dict):
                try:
                    norm_t = normalize_entity_type(e.get("entity_type", ""))
                    if norm_t is None:
                        continue
                    hist.append(Entity(entity_type=norm_t, value=e.get("value", ""), confidence=1.0))
                except Exception:
                    continue
            elif isinstance(e, Entity):
                hist.append(e)

        if not text:
            return current_entities, {
                "triggered": False,
                "kind": "none",
                "source": "none",
                "candidates": 0,
                "resolved": None,
            }

        # 1) 显式有头词回指 (优先级最高)
        mention_types = _match_mention(text)
        if mention_types is not None:
            resolved = self._gather_rule_candidate(hist, mention_types)
            if resolved is not None:
                return _resolved_return(current_entities, resolved, "mention", "rule")
            # 存在回指但历史无法唯一确定 → 交 LLM(默认关); 仍不明则放弃
            resolved = await self._llm_try(text, hist)
            if resolved is not None:
                return _resolved_return(current_entities, resolved, "mention", "llm")
            return current_entities, {
                "triggered": True,
                "kind": "mention",
                "source": "none",
                "candidates": len(hist),
                "resolved": None,
            }

        # 2) 零主回指: 上文在等必填槽、本句未填 → 从历史补该缺槽的唯一候选值.
        # 仅当"确认/点名型省略回指"且历史该缺槽候选唯一才补, 双层保险不误塞.
        if missing_slots:
            missing_types = [_SLOT_TO_ENTITY_TYPE[n] for n, _ in missing_slots if _SLOT_TO_ENTITY_TYPE.get(n)]
            if missing_types:
                already_filled = any(e.entity_type in set(missing_types) for e in current_entities)
                if not already_filled and _is_zero_anaphora_reference(text, missing_slots):
                    resolved = self._gather_zero_candidate(hist, missing_types)
                    if resolved is not None:
                        return _resolved_return(current_entities, resolved, "zero", "rule")
                    return current_entities, {
                        "triggered": True,
                        "kind": "zero",
                        "source": "none",
                        "candidates": len(hist),
                        "resolved": None,
                    }

        # 3) 纯代词回指: 历史实体池全局恰好一个候选时解析
        if _match_pronoun(text):
            resolved = self._gather_global_unique(hist)
            if resolved is not None:
                return _resolved_return(current_entities, resolved, "pronoun", "rule")
            resolved = await self._llm_try(text, hist)
            if resolved is not None:
                return _resolved_return(current_entities, resolved, "pronoun", "llm")
            return current_entities, {
                "triggered": True,
                "kind": "pronoun",
                "source": "none",
                "candidates": len(hist),
                "resolved": None,
            }

        return current_entities, {
            "triggered": False,
            "kind": "none",
            "source": "none",
            "candidates": 0,
            "resolved": None,
        }

    async def _llm_try(self, text: str, hist: list[Entity]) -> Entity | None:
        """按灰度开关尝试 LLM 兜底; 开关关/无客户端/失败 → None(不阻断)。"""
        from lumio.shared.config import get_settings

        if not hist or self._llm is None or not get_settings().classification.anaphora_llm_fallback_enabled:
            return None
        try:
            return await self._llm_resolve(text, hist)
        except Exception as exc:
            logger.warning("指代消解 LLM 兜底失败(不阻断): %s", exc)
            return None

    @staticmethod
    def _gather_rule_candidate(
        hist: list[Entity],
        types: list[str],
    ) -> Entity | None:
        """在历史实体池中, 按偏好序找"恰一个唯一值"的候选。

        Returns:
            Entity 表示解析结果; 无法唯一确定(0 个候选, 或多个候选)返回 None。
        """
        for t in types:
            distinct = sorted({e.value for e in hist if e.entity_type == t and e.value})
            if len(distinct) == 1:
                return Entity(entity_type=t, value=distinct[0], confidence=_RULE_CONFIDENCE)
            # 多个不同值 → 该类型存在多候选, 规则层不确定, 放弃
            if len(distinct) > 1:
                return None
        return None  # 该所指类别在历史中无候选

    @staticmethod
    def _gather_zero_candidate(hist: list[Entity], types: list[str]) -> Entity | None:
        """零主回指: 缺槽类型在历史中恰好一条唯一候选(不同 type+value 组合恰一)时补齐。

        Returns:
            Entity 表示补齐结果; 0 个候选或多种候选返回 None。
        """
        type_set = set(types)
        pairs = {(e.entity_type, e.value) for e in hist if e.entity_type in type_set and e.value}
        if len(pairs) == 1:
            entity_type, value = next(iter(pairs))
            return Entity(entity_type=entity_type, value=value, confidence=_RULE_CONFIDENCE)
        return None

    @staticmethod
    def _gather_global_unique(hist: list[Entity]) -> Entity | None:
        """纯代词回指: 历史实体池全局恰好一条候选(不同 type+value 组合恰一)时解析。"""
        pairs = {(e.entity_type, e.value) for e in hist if e.value}
        if len(pairs) == 1:
            entity_type, value = next(iter(pairs))
            return Entity(entity_type=entity_type, value=value, confidence=_RULE_CONFIDENCE)
        return None

    async def _llm_resolve(self, text: str, hist: list[Entity]) -> Entity | None:
        """LLM 兜底: 从候选实体池中挑出当前句回指的具体所指。"""
        if not hist:
            return None
        candidate_lines = "; ".join(f"{e.entity_type}={e.value}" for e in hist[:12])
        system_prompt = (
            "你是一个银行信用卡客服的指代消解器。客户上一条消息可能用指示词(这张/那笔/这期等)回指"
            "之前提到的某条具体信息。请你从给出的候选实体中, 选出他确切指代的那一条。\n"
            '只输出 JSON, 不要多余文字, 格式: {"entity_type": "...", "value": "...", "confidence": 0.0~1.0}\n'
            "要求: entity_type 必须是以下之一(严格): amount, CARD_NUMBER, card_tail, PHONE, period, card_type。\n"
            '若无法确定, 输出 {"entity_type": "", "value": "", "confidence": 0}。'
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"候选实体: {candidate_lines}\n客户当前消息: {text}"},
        ]
        result = await self._llm.chat_json(messages, temperature=0.1, max_tokens=64, timeout=15.0)
        if not isinstance(result, dict):
            return None
        raw_type = result.get("entity_type", "") or ""
        raw_value = result.get("value", "") or ""
        norm_t = normalize_entity_type(raw_type)
        if norm_t is None or not raw_value or float(result.get("confidence", 0) or 0) < 0.5:
            logger.info("指代消解 LLM 未给出可靠结果: type=%r value=%r", raw_type, raw_value)
            return None
        return Entity(entity_type=norm_t, value=raw_value, confidence=min(float(result["confidence"]), 1.0))


def _match_mention(text: str) -> list[str] | None:
    """命中显式有头词回指规则 → 返回该所指类型的偏好序; 否则 None。"""
    for pattern, types in _ANAPHORA_RULES:
        if pattern.search(text):
            return types
    return None


def _match_pronoun(text: str) -> bool:
    """是否纯代词回指(无确定头词, 需靠全局唯一候选)。

    带头的指代(那个金额等)已由 _ANAPHORA_RULES(mention 路)先截获, 到此处只剩裸代词。
    """
    return _RE_PRONOUN.search(text) is not None


def _resolved_return(
    entities: list[Entity], resolved: Entity, kind: str, source: str
) -> tuple[list[Entity], dict[str, Any]]:
    merged = _dedup_append(entities, resolved)
    return (
        merged,
        {"triggered": True, "kind": kind, "source": source, "candidates": 1, "resolved": resolved.model_dump()},
    )


def _dedup_append(entities: list[Entity], resolved: Entity) -> list[Entity]:
    """把解析出的实体并入, 同类同值去重保序。"""
    out = list(entities)
    for e in out:
        if e.entity_type == resolved.entity_type and e.value == resolved.value:
            return out
    out.append(resolved)
    return out
