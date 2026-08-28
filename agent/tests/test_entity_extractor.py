"""规则实体抽取器 + 类型归一 单测 (语气: 快路径实体管线)."""

from lumio.services.bot.entity_extractor import extract_entities, normalize_entity_type
from lumio.shared.entity_sandbox import filter_for_cross_session
from lumio.shared.models import Entity


class TestExtractEntities:
    def test_card_number_spaced(self) -> None:
        res = extract_entities("我卡号是 6222 8888 6666 0000")
        assert ("CARD_NUMBER", "6222888866660000") in [(e.entity_type, e.value) for e in res]

    def test_card_number_hyphenated(self) -> None:
        res = extract_entities("我的卡号6222-8888-6666-0000")
        assert any(e.entity_type == "CARD_NUMBER" and e.value == "6222888866660000" for e in res)

    def test_card_tail(self) -> None:
        assert any(e.entity_type == "card_tail" and e.value == "3344" for e in extract_entities("卡尾3344"))
        assert any(e.entity_type == "card_tail" and e.value == "3344" for e in extract_entities("后四位是 3344"))

    def test_phone(self) -> None:
        assert any(e.entity_type == "PHONE" and e.value == "13800138000" for e in extract_entities("手机 13800138000"))

    def test_amount_with_unit(self) -> None:
        assert ("amount", "1200") in [(e.entity_type, e.value) for e in extract_entities("手续费 1200 元")]
        assert ("amount", "3200") in [(e.entity_type, e.value) for e in extract_entities("还款一笔 3200块")]

    def test_bare_number_is_not_amount(self) -> None:
        # 纯数字串(无金额单位)不得判为金额, 防把卡号/手机号/身份证误抽
        assert not any(e.entity_type == "amount" for e in extract_entities("号码是 6222888866660000"))

    def test_period(self) -> None:
        assert any(e.entity_type == "period" and "3" in e.value for e in extract_entities("帮我查一下 3 月份的账单"))

    def test_card_type_no_subsumed_keyword(self) -> None:
        # "白金卡" 不得顺带抽出内嵌的 "金卡"
        types = [e.value for e in extract_entities("我的白金卡") if e.entity_type == "card_type"]
        assert types == ["白金卡"]

    def test_dedup(self) -> None:
        # 同句重复词只抽一次
        res = extract_entities("金卡 金卡")
        assert sum(1 for e in res if e.entity_type == "card_type") == 1


class TestNormalizeEntityType:
    def test_canonical_passthrough(self) -> None:
        assert normalize_entity_type("amount") == "amount"
        assert normalize_entity_type("PHONE") == "PHONE"

    def test_alias(self) -> None:
        assert normalize_entity_type("金额") == "amount"
        assert normalize_entity_type("card_number") == "CARD_NUMBER"
        assert normalize_entity_type("手机号") == "PHONE"

    def test_unknown_returns_none(self) -> None:
        assert normalize_entity_type("whatever_unknown_type") is None
        assert normalize_entity_type("") is None


class TestCrossSessionFilter:
    def test_denylist_card_number_dropped(self) -> None:
        # 合规: 卡号/手机号等敏感实体不得进入跨会话池
        out = filter_for_cross_session(
            [
                Entity(entity_type="CARD_NUMBER", value="6222888866660000"),
                Entity(entity_type="card_type", value="白金卡"),
            ]
        )
        types = {e.entity_type for e in out}
        assert "CARD_NUMBER" not in types
        assert "card_type" in types
