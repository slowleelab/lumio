"""指代消解 (anaphora_resolver) 单测: 规则层唯一性约束 + LLM 兜底(默认关)."""

import pytest

from lumio.services.bot.anaphora_resolver import AnaphoraResolver
from lumio.services.bot.entity_extractor import normalize_entity_type
from lumio.shared.models import Entity


def _h(*items: tuple[str, str]) -> list[Entity]:
    return [Entity(entity_type=t, value=v, confidence=1.0) for t, v in items]


class TestRuleLayer:
    async def test_unique_candidate_resolves(self) -> None:
        r = AnaphoraResolver(llm_client=None)
        hist = _h(("card_tail", "3344"), ("amount", "1200"))
        cur = _h(("CARD_NUMBER", "6222888866660000"))
        enriched, meta = await r.resolve("那张卡帮我查账单", hist, cur)
        assert ("card_tail", "3344") in [(e.entity_type, e.value) for e in enriched]
        assert meta["source"] == "rule"
        assert meta["kind"] == "mention"
        assert meta["resolved"]["value"] == "3344"

    async def test_multi_candidate_no_guess(self) -> None:
        # 宁缺毋滥: 两张不同卡 → 规则层不得乱猜 (两张卡的卡尾不同)
        r = AnaphoraResolver(llm_client=None)
        hist = _h(("card_tail", "3344"), ("card_tail", "5678"))
        enriched, meta = await r.resolve("那张卡帮我查", hist, [])
        assert enriched == []
        assert meta["candidates"] == 2
        assert meta["resolved"] is None

    async def test_no_candidate_no_resolve(self) -> None:
        r = AnaphoraResolver(llm_client=None)
        hist = _h(("amount", "1200"))
        enriched, meta = await r.resolve("那一期账单", hist, [])
        # "那一期" 指向 period, 历史无 period 候选 → 不解析
        assert enriched == []
        assert meta["resolved"] is None

    async def test_no_mention_not_triggered(self) -> None:
        r = AnaphoraResolver(llm_client=None)
        hist = _h(("card_tail", "3344"))
        enriched, meta = await r.resolve("我想查个账单", hist, [])
        assert enriched == []
        assert meta["triggered"] is False

    async def test_amount_mention(self) -> None:
        r = AnaphoraResolver(llm_client=None)
        hist = _h(("amount", "1200"))
        enriched, meta = await r.resolve("那笔消费多少钱", hist, [])
        assert ("amount", "1200") in [(e.entity_type, e.value) for e in enriched]

    async def test_llm_disabled_keeps_noop_on_ambiguous(self) -> None:
        # LLM 兜底默认关 (llm_client=None) → 多候选也不触发外部调用, 保持 no-op
        r = AnaphoraResolver(llm_client=None)
        hist = _h(("card_tail", "3344"), ("card_tail", "5678"))
        enriched, meta = await r.resolve("那张卡", hist, [])
        assert enriched == []
        assert meta["source"] == "none"

    async def test_rule_over_llm_flag_disabled(self) -> None:
        # anaphora_llm_fallback_enabled 默认 false: 即便给了 llm_client 也不该走兜底.
        # 这里用注入假 client 验证调用被开关挡住.
        from lumio.shared.config import get_settings

        if get_settings().classification.anaphora_llm_fallback_enabled:
            pytest.skip("灰度开关开启, 不测该默认关分支")

        class _FakeLLM:
            async def chat_json(self, messages, **kwargs):  # pragma: no cover
                raise AssertionError("LLM 兜底不应在开关关闭时被调用")

        r = AnaphoraResolver(llm_client=_FakeLLM())
        hist = _h(("card_tail", "3344"), ("card_tail", "5678"))
        enriched, _ = await r.resolve("那张卡", hist, [])
        assert enriched == []


class TestZeroAnaphora:
    async def test_zero_anaphora_fills_slot(self) -> None:
        # 上轮在等"卡尾", 本句是确认/点名型省略回指且未填新实体 → 补历史唯一候选(卡尾)
        r = AnaphoraResolver(llm_client=None)
        hist = _h(("card_tail", "3344"), ("category", "x"))
        enriched, meta = await r.resolve("确认", hist, [], missing_slots=[("card_tail", "卡号后四位")])
        assert ("card_tail", "3344") in [(e.entity_type, e.value) for e in enriched]
        assert meta["kind"] == "zero"
        assert meta["source"] == "rule"

    async def test_zero_anaphora_anchor_by_name(self) -> None:
        # 点名缺槽(提到"尾号")但没带值 → 补历史唯一候选
        r = AnaphoraResolver(llm_client=None)
        hist = _h(("card_tail", "3344"))
        enriched, meta = await r.resolve("就那个尾号", hist, [], missing_slots=[("card_tail", "卡号后四位")])
        assert ("card_tail", "3344") in [(e.entity_type, e.value) for e in enriched]
        assert meta["kind"] == "zero"

    async def test_zero_anaphora_multi_candidate_no_guess(self) -> None:
        # 历史里两个不同卡尾 → 缺槽补值因子不唯一, 不猜
        r = AnaphoraResolver(llm_client=None)
        hist = _h(("card_tail", "3344"), ("card_tail", "5678"))
        enriched, meta = await r.resolve("确认", hist, [], missing_slots=[("card_tail", "卡号后四位")])
        assert enriched == []
        assert meta["kind"] == "zero"
        assert meta["resolved"] is None

    async def test_zero_anaphora_skips_when_current_fills(self) -> None:
        # 本句已填缺槽 → 不重复补
        r = AnaphoraResolver(llm_client=None)
        hist = _h(("card_tail", "3344"))
        cur = _h(("card_tail", "5678"))
        enriched, meta = await r.resolve("刚补的尾号是5678", hist, cur, missing_slots=[("card_tail", "卡号后四位")])
        assert ("card_tail", "5678") in [(e.entity_type, e.value) for e in enriched]
        assert ("card_tail", "3344") not in [(e.entity_type, e.value) for e in enriched]
        assert meta["resolved"] is None

    async def test_zero_anaphora_no_missing_slots_noop(self) -> None:
        # 无缺槽 → 不触发零主回指
        r = AnaphoraResolver(llm_client=None)
        hist = _h(("card_tail", "3344"))
        enriched, meta = await r.resolve("确认", hist, [])
        assert enriched == []
        assert meta["triggered"] is False

    async def test_zero_anaphora_question_never_fills(self) -> None:
        # 本句是新问题(带提问标记) → 绝不把历史值硬塞进缺槽
        r = AnaphoraResolver(llm_client=None)
        hist = _h(("card_tail", "3344"))
        enriched, meta = await r.resolve("那手续费是多少", hist, [], missing_slots=[("card_tail", "卡号后四位")])
        assert enriched == []
        assert meta["triggered"] is False


class TestPronounAnaphora:
    async def test_mention_with_amount_head(self) -> None:
        # "那个金额" 带确定头词"金额" → 走带类型的 mention 路, 解析历史唯一金额
        r = AnaphoraResolver(llm_client=None)
        hist = _h(("amount", "1200"))
        enriched, meta = await r.resolve("就是那个金额", hist, [])
        assert ("amount", "1200") in [(e.entity_type, e.value) for e in enriched]
        assert meta["kind"] == "mention"
        assert meta["source"] == "rule"

    async def test_pronoun_resolves_global_unique(self) -> None:
        # 裸代词"那个": 历史全局唯一 (amount=1200) → pronoun 路解析
        r = AnaphoraResolver(llm_client=None)
        hist = _h(("amount", "1200"))
        enriched, meta = await r.resolve("就那个吧", hist, [])
        assert ("amount", "1200") in [(e.entity_type, e.value) for e in enriched]
        assert meta["kind"] == "pronoun"
        assert meta["source"] == "rule"

    async def test_pronoun_multi_candidate_no_guess(self) -> None:
        # 历史有多类/多值实体 → 全局候选不唯一, 纯代词不猜
        r = AnaphoraResolver(llm_client=None)
        hist = _h(("amount", "1200"), ("card_tail", "3344"))
        enriched, meta = await r.resolve("就那个吧", hist, [])
        assert enriched == []
        assert meta["kind"] == "pronoun"
        assert meta["resolved"] is None

    async def test_pronoun_no_history_noop(self) -> None:
        r = AnaphoraResolver(llm_client=None)
        enriched, meta = await r.resolve("就那个吧", [], [])
        assert enriched == []
        assert meta["resolved"] is None
        assert meta["source"] == "none"


class TestNormalize:
    def test_llm_entity_normalized(self) -> None:
        # LLM 慢路径输出的自由类型经 normalize 映射回规范 key, 才能喂 slot/消解
        assert normalize_entity_type("手机号") == "PHONE"
        assert normalize_entity_type("amount") == "amount"


class TestResolveMisc:
    async def test_empty_history(self) -> None:
        r = AnaphoraResolver(llm_client=None)
        enriched, meta = await r.resolve("那张卡", [], [])
        assert enriched == []
        assert meta["triggered"] is True  # 有指示词但无历史候选

    async def test_dict_history_normalized(self) -> None:
        # 历史实体也可能以 dict 形式传入, 应被归一识别
        r = AnaphoraResolver(llm_client=None)
        hist = [{"entity_type": "卡号", "value": "6222888866660000"}, {"entity_type": "card_type", "value": "白金卡"}]
        enriched, meta = await r.resolve("那张卡", hist, [])
        # "那张卡" 偏好序先试 card_tail(无)再 CARD_NUMBER → dict 里被归一为 CARD_NUMBER
        assert ("CARD_NUMBER", "6222888866660000") in [(e.entity_type, e.value) for e in enriched]
        assert meta["source"] == "rule"
