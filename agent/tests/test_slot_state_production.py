"""槽位状态生产级升级测试

覆盖: 历史反填 / 跨意图继承 / 并发 CAS union / card_number 自动落槽+尾号派生 /
COMPLAINT issue_detail 原文回填 / 值归一+长度上限 / 单一真相源(不再写 lumio:slot key)。
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from lumio.services.bot.bot_agent import LumioAgent
from lumio.services.common.session import SessionManager
from lumio.shared.models import Entity, IntentLabel, SessionPhase, SessionState, SessionSubPhase


def _state(
    slot_values: dict | None = None,
    last_entities: list | None = None,
    entity_pool: list | None = None,
    session_id: str = "s1",
) -> SessionState:
    return SessionState(
        session_id=session_id,
        customer_id="c1",
        current_phase=SessionPhase.BOT,
        sub_phase=SessionSubPhase.BOT_ACTIVE,
        slot_values=slot_values or {},
        last_entities=last_entities or [],
        entity_pool=entity_pool or [],
        created_at=datetime.now(UTC),
        last_active_at=datetime.now(UTC),
    )


def _agent(session_manager: MagicMock | None = None) -> LumioAgent:
    sm = session_manager or MagicMock()
    return LumioAgent(
        classifier=MagicMock(),
        degradation_mgr=MagicMock(),
        transfer_checker=MagicMock(),
        session_manager=sm,
    )


def _ent(etype: str, value: str) -> Entity:
    return Entity(entity_type=etype, value=value)


class TestHistoryBackfill:
    """历史 last_entities/entity_pool → 槽位反填"""

    @pytest.mark.asyncio
    async def test_fills_from_last_entities_when_current_empty(self) -> None:
        """本轮无实体, 但历史 last_entities 有 card_tail → 反填为槽值"""
        sm = MagicMock()
        sm.get_session = AsyncMock(return_value=_state(last_entities=[_ent("card_tail", "1234")]))
        sm.patch_state = AsyncMock(return_value={"ok": True, "new_version": 2})
        agent = _agent(sm)

        prompt = await agent._load_slot_prompt("s1", IntentLabel.CARD_LOSS, [])

        assert prompt != ""
        assert "1234" in prompt  # 卡尾已收集, 不再问
        patch = sm.patch_state.await_args.args[2]["slot_values"]
        assert patch["card_tail"]["value"] == "1234"

    @pytest.mark.asyncio
    async def test_fills_from_entity_pool(self) -> None:
        """entity_pool 历史实体也参与补槽 (跨轮累积)"""
        sm = MagicMock()
        sm.get_session = AsyncMock(return_value=_state(entity_pool=[_ent("amount", "5000")]))
        sm.patch_state = AsyncMock(return_value={"ok": True})
        agent = _agent(sm)

        prompt = await agent._load_slot_prompt("s1", IntentLabel.INSTALLMENT_INQUIRY, [])
        assert prompt != ""
        assert "5000" in prompt


class TestCrossIntentInheritance:
    """意图切换不再清除已填槽 (挂失→补卡 复用卡尾)"""

    @pytest.mark.asyncio
    async def test_slot_retained_across_intent_switch(self) -> None:
        sm = MagicMock()
        sm.get_session = AsyncMock(
            return_value=_state(slot_values={"card_tail": {"name": "card_tail", "value": "4321", "source": "entity"}})
        )
        sm.patch_state = AsyncMock(return_value={"ok": True})
        agent = _agent(sm)

        # 新意图 CARD_LOSS: 已填 card_tail 直接复用, 不覆盖、不追问
        prompt = await agent._load_slot_prompt("s1", IntentLabel.CARD_LOSS, [])
        assert prompt != ""
        assert "4321" in prompt
        assert "待收集" not in prompt  # 唯一必填槽已填

    @pytest.mark.asyncio
    async def test_missing_required_reflects_fills(self) -> None:
        sm = MagicMock()
        sm.get_session = AsyncMock(
            return_value=_state(slot_values={"card_tail": {"name": "card_tail", "value": "4321", "source": "entity"}})
        )
        agent = _agent(sm)
        assert await agent._missing_required_slots("s1", IntentLabel.CARD_LOSS) == []

        # 未填时返回必填缺槽
        sm.get_session = AsyncMock(return_value=_state())
        missing = await agent._missing_required_slots("s1", IntentLabel.CARD_LOSS)
        assert any(name == "card_tail" for name, _ in missing)


class TestCardNumberAndDerived:
    """满卡号自动落槽 + card_tail 派生"""

    @pytest.mark.asyncio
    async def test_full_card_number_fills_and_derives_tail(self) -> None:
        sm = MagicMock()
        sm.get_session = AsyncMock(return_value=_state())
        sm.patch_state = AsyncMock(return_value={"ok": True})
        agent = _agent(sm)

        prompt = await agent._load_slot_prompt(
            "s1", IntentLabel.CARD_LOSS, [_ent("CARD_NUMBER", "6222 0000 0000 1234")]
        )
        patch = sm.patch_state.await_args.args[2]["slot_values"]
        # 值归一: 去空格
        assert patch["card_number"]["value"] == "6222000000001234"
        # 派生尾号
        assert patch["card_tail"]["value"] == "1234"
        assert patch["card_tail"]["source"] == "derived"
        assert "1234" in prompt


class TestComplaintIssueDetail:
    """COMPLAINT 必填食位 issue_detail 原文回填, 不互锁噪声门"""

    @pytest.mark.asyncio
    async def test_issue_detail_backfilled_from_message(self) -> None:
        sm = MagicMock()
        sm.get_session = AsyncMock(return_value=_state())
        sm.patch_state = AsyncMock(return_value={"ok": True})
        agent = _agent(sm)

        text = "我的账单有重复扣款麻烦帮我处理一下"
        prompt = await agent._load_slot_prompt("s1", IntentLabel.COMPLAINT, [], user_input=text)
        assert prompt != ""
        patch = sm.patch_state.await_args.args[2]["slot_values"]
        assert patch["issue_detail"]["source"] == "message"
        assert patch["issue_detail"]["value"] == text

    @pytest.mark.asyncio
    async def test_too_short_message_does_not_fill(self) -> None:
        sm = MagicMock()
        sm.get_session = AsyncMock(return_value=_state())
        sm.patch_state = AsyncMock(return_value={"ok": True})
        agent = _agent(sm)
        await agent._load_slot_prompt("s1", IntentLabel.COMPLAINT, [], user_input="啊")
        patch = sm.patch_state.await_args.args[2]["slot_values"] if sm.patch_state.await_args else {}
        assert "issue_detail" not in patch


class TestNormalization:
    """值归一 (去分隔) + 长度上限"""

    @pytest.mark.asyncio
    async def test_phone_number_squashed(self) -> None:
        sm = MagicMock()
        sm.get_session = AsyncMock(return_value=_state())
        sm.patch_state = AsyncMock(return_value={"ok": True})
        agent = _agent(sm)
        await agent._load_slot_prompt("s1", IntentLabel.CARD_LOSS, [_ent("PHONE", "139 1234 5678")])
        patch = sm.patch_state.await_args.args[2]["slot_values"]
        assert patch["phone_number"]["value"] == "13912345678"

    @pytest.mark.asyncio
    async def test_value_length_capped(self) -> None:
        sm = MagicMock()
        sm.get_session = AsyncMock(return_value=_state())
        sm.patch_state = AsyncMock(return_value={"ok": True})
        agent = _agent(sm)
        long_val = "x" * 200
        await agent._load_slot_prompt("s1", IntentLabel.INSTALLMENT_INQUIRY, [_ent("amount", long_val)])
        patch = sm.patch_state.await_args.args[2]["slot_values"]
        assert len(patch["amount"]["value"]) == 64


class TestSingleSourceOfTruth:
    """不再写独立 lumio:slot key, 槽位随会话 meta (patch_state) 持久化"""

    @pytest.mark.asyncio
    async def test_no_separate_slot_key_write(self) -> None:
        sm = MagicMock()
        sm.get_session = AsyncMock(return_value=_state())
        sm.patch_state = AsyncMock(return_value={"ok": True})
        agent = _agent(sm)
        await agent._load_slot_prompt("s1", IntentLabel.INSTALLMENT_INQUIRY, [_ent("amount", "1000")])
        # 持久化改走 patch_state; 不再触碰独立 key
        sm.patch_state.assert_awaited_once()
        assert not hasattr(sm, "_redis") or sm._redis.setex.call_count == 0

    @pytest.mark.asyncio
    async def test_faq_returns_empty_prompt(self) -> None:
        sm = MagicMock()
        sm.get_session = AsyncMock(return_value=_state())
        sm.patch_state = AsyncMock(return_value={"ok": True})
        agent = _agent(sm)
        assert await agent._load_slot_prompt("s1", IntentLabel.FAQ, []) == ""


class TestConcurrentSlotUnion:
    """patch_state 槽名级 last-write-wins union: 并发两轮填不同槽互不覆盖"""

    def test_apply_merge_rules_unions_slots(self) -> None:
        mgr = SessionManager.__new__(SessionManager)  # 纯方法测试, 无需 Redis
        current = {
            "slot_values": {"amount": {"name": "amount", "value": "1000", "source": "entity", "updated_at": "t1"}}
        }
        patched = {"slot_values": {"period": {"name": "period", "value": "12", "source": "entity", "updated_at": "t2"}}}
        transformed = mgr._apply_merge_rules(current, patched)
        slots = transformed["slot_values"]
        assert slots["amount"]["value"] == "1000"  # 并发写者已填的保留
        assert slots["period"]["value"] == "12"  # 本写者新增的落库

    def test_apply_merge_rules_same_slot_last_wins(self) -> None:
        mgr = SessionManager.__new__(SessionManager)
        current = {
            "slot_values": {"amount": {"name": "amount", "value": "old", "source": "entity", "updated_at": "t1"}}
        }
        patched = {
            "slot_values": {"amount": {"name": "amount", "value": "new", "source": "entity", "updated_at": "t2"}}
        }
        slots = mgr._apply_merge_rules(current, patched)["slot_values"]
        assert slots["amount"]["value"] == "new"
