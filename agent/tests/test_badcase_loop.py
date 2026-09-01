"""闭环优化方案测试: Badcase 采集/归因闸门/金标扩充 (方案 v2.0 P0-P2 核心)"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from lumio.services.common.badcase_loop import (
    BadcaseJudge,
    dedup_key,
    filter_variants,
    fix_table_for_layer,
    rule_augment,
)
from lumio.services.common.badcase_store import capture_badcase, update_fix_status
from lumio.shared.orm_models import Badcase


def _sf():
    """内存级 fake session factory (与 test_console_admin 同款)"""

    store: dict = {"badcases": []}

    class FakeResult:
        def __init__(self, val):
            self._v = val

        def scalar_one_or_none(self):
            return self._v

        def scalar(self):
            return len(store["badcases"])

        def scalars(self):
            return self

        def all(self):
            return list(store["badcases"])

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def add(self, obj):
            store["badcases"].append(obj)

        async def commit(self):
            pass

        async def refresh(self, obj):
            pass

        async def get(self, model, pk):
            return store["badcases"][0] if store["badcases"] else None

        def execute(self, q):
            return FakeResult(None)

    class Factory:
        def __call__(self):
            return Session()

    return Factory(), store


@pytest.mark.asyncio
async def test_capture_persists_and_dedup_key() -> None:
    factory, store = _sf()
    bc = await capture_badcase(
        factory,
        trace_id="t1",
        session_id="s1",
        signal_source="transfer",
        user_input="我要挂失信用卡",
        signal_detail={"reason": "x"},
    )
    assert bc.signal_source == "transfer"
    assert bc.dedup_group_id == dedup_key("我要挂失信用卡")
    assert bc.fix_status == "pending"


    @pytest.mark.asyncio
    async def test_update_fix_status(self) -> None:
        """状态流转持久化"""
        from unittest.mock import AsyncMock, MagicMock

        from uuid_utils import uuid7


        bc = Badcase(
            id=uuid7(), trace_id="t", session_id="s", signal_source="transfer",
            user_input="x", fix_status="pending",
        )
        session = MagicMock()
        session.get = AsyncMock(return_value=bc)
        session.commit = AsyncMock()

        class F:
            def __call__(self):
                return session

        ok = await update_fix_status(F(), str(bc.id), fix_status="deployed", note="done")
        assert ok is True
        assert bc.fix_status == "deployed"


# ── 模块 A 归因闸门 ──


def _judge() -> BadcaseJudge:
    llm = MagicMock()
    llm.chat_json = AsyncMock()
    return BadcaseJudge(llm, model="test-judge", min_confidence=0.7, samples=3)


def _vote(layer="layer_5", cat="knowledge", conf=0.9):
    return {"root_cause_layer": layer, "root_cause_category": cat, "evidence": "e", "confidence": conf}


class TestAttribution:
    @pytest.mark.asyncio
    async def test_unanimous_auto_accept(self) -> None:
        j = _judge()
        j._llm.chat_json = AsyncMock(side_effect=[_vote(), _vote(), _vote()])
        ctx = {"trace_id": "t1", "user_input": "q", "intent": "faq", "confidence": 0.3}
        r = await j.attribute(ctx)
        assert r.root_cause_layer == "layer_5"
        assert r.fix_table == fix_table_for_layer("layer_5")
        assert r.needs_human_review is False
        assert r.majority_ratio == 1.0

    @pytest.mark.asyncio
    async def test_split_votes_go_human(self) -> None:
        j = _judge()
        j._llm.chat_json = AsyncMock(side_effect=[_vote("layer_5"), _vote("layer_3", "semantic"), _vote("layer_5")])
        ctx = {"trace_id": "t2", "user_input": "q"}
        r = await j.attribute(ctx)
        assert r.needs_human_review is True  # 2/3 多数票不齐 → 人工
        assert r.majority_ratio == pytest.approx(2 / 3)

    @pytest.mark.asyncio
    async def test_low_confidence_goes_human(self) -> None:
        j = _judge()
        j._llm.chat_json = AsyncMock(side_effect=[_vote(conf=0.4), _vote(conf=0.4), _vote(conf=0.4)])
        r = await j.attribute({"trace_id": "t"})
        assert r.needs_human_review is True

    @pytest.mark.asyncio
    async def test_uncertain_goes_human(self) -> None:
        j = _judge()
        j._llm.chat_json = AsyncMock(side_effect=[_vote("uncertain", "uncertain", 0.8) for _ in range(3)])
        r = await j.attribute({"trace_id": "t"})
        assert r.needs_human_review is True

    @pytest.mark.asyncio
    async def test_all_samples_fail_returns_none(self) -> None:
        j = _judge()
        j._llm.chat_json = AsyncMock(side_effect=RuntimeError("down"))
        r = await j.attribute({"trace_id": "t"})
        assert r is None


# ── 模块 B 规则模板 + 过滤 ──


class TestRuleAugment:
    def test_generates_variants(self) -> None:
        out = rule_augment("如何查询账单")
        assert len(out) >= 3
        assert all(v != "如何查询账单" for v in out)

    def test_synonym_replace(self) -> None:
        out = rule_augment("怎么查询账单")
        assert any("如何" in v or "怎样" in v for v in out)


class TestFilterVariants:
    def test_len_and_dedup(self) -> None:
        passed, rejected = filter_variants(
            ["崭新的问题", "太短", "崭新的问题"],
            existing=set(),
        )
        # 超长被拒, 与 existing/批内重复被拒
        assert passed == ["崭新的问题"] and len(rejected) == 2

    def test_compliance_blocked(self) -> None:
        passed, rejected = filter_variants(["套现教程"], compliance_words={"套现"})
        assert passed == [] and len(rejected) == 1
