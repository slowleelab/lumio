"""问题治理测试 (进程内, 真库): 聚合/方案 upsert/批量流转/守门"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio

from lumio.services.common import pattern_store as ps
from lumio.services.common.badcase_store import capture_badcase, get_badcase
from lumio.services.common.pattern_router import (
    BatchBody,
    PlanPayload,
    batch_transition_endpoint,
    get_pattern_endpoint,
    list_patterns_endpoint,
    save_plan_endpoint,
)
from lumio.shared.auth import AuthUser
from lumio.shared.exceptions import LumioError

_ADMIN = AuthUser(user_id="unit-admin", role="admin")


def _fake_request(sf) -> SimpleNamespace:
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(db_session_factory=sf)))


@pytest_asyncio.fixture()
async def pattern_db(db_schema: None):
    from lumio.services.common.database import get_async_session_factory

    sf = get_async_session_factory()
    created: list[str] = []
    yield sf, created
    # 清理 (组为动态聚合, 清案例即清组; 方案表按 group_key 清)
    from sqlalchemy import delete

    from lumio.shared.orm_models import Badcase, FixPattern

    async with sf() as s:
        for cid in created:
            bc = await get_badcase(sf, cid)
            if bc:
                await s.delete(await s.get(Badcase, bc.id))
        await s.execute(delete(FixPattern).where(FixPattern.group_key.like("%pat_test_%")))
        await s.commit()


async def _mk_case(sf, created: list[str], sid: str, intent: str, layer: str, user_input: str) -> str:
    bc = await capture_badcase(
        sf,
        trace_id=sid,
        session_id=sid,
        customer_id=None,
        signal_source="qa_scan",
        intent_label=intent,
        user_input=user_input,
        bot_output="回复",
        signal_detail={"verdict": "fail"},
        snapshot=None,
    )
    created.append(str(bc.id))
    # 落根因 (capture 不带根因; 直接 update)
    async with sf() as s:
        from sqlalchemy import update

        from lumio.shared.orm_models import Badcase

        await s.execute(update(Badcase).where(Badcase.id == bc.id).values(root_cause_layer=layer))
        await s.commit()
    return str(bc.id)


async def test_list_aggregates_and_unattributed(pattern_db) -> None:
    sf, created = pattern_db
    await _mk_case(sf, created, "pat-t1", "pat_test_biz", "layer_3", "查下账单1")
    await _mk_case(sf, created, "pat-t2", "pat_test_biz", "layer_3", "查下账单2")
    await _mk_case(sf, created, "pat-t3", "pat_test_kb", "layer_5", "白金卡权益")
    await _mk_case(sf, created, "pat-t4", "pat_test_unc", "uncertain", "未归因案例")

    res = await list_patterns_endpoint(_ADMIN, _fake_request(sf))
    g1 = next(i for i in res["items"] if i["group_key"] == "pat_test_biz::layer_3")
    assert g1["case_count"] == 2 and g1["pending_count"] == 2
    assert g1["fix_table"] == "B_intent"  # layer_3 → B_intent (权威映射)
    g2 = next((i for i in res["items"] if i["group_key"] == "pat_test_kb::layer_5"), None)
    assert g2 is not None and g2["fix_table"] == "A_knowledge"
    keys = {i["group_key"] for i in res["items"]}
    assert "pat_test_unc::uncertain" not in keys  # uncertain 不入组


async def test_detail_and_plan_upsert(pattern_db) -> None:
    sf, created = pattern_db
    await _mk_case(sf, created, "pat-t5", "pat_test_plan", "layer_3", "意图误判现场")
    key = ps.group_key_for("pat_test_plan", "layer_3")
    d = await get_pattern_endpoint(key, _ADMIN, _fake_request(sf))
    assert d["case_count"] >= 1 and d["plan_text"] == ""

    payload = PlanPayload(
        intent_label="pat_test_plan",
        root_cause_layer="layer_3",
        fix_table="B_intent",
        plan_text="补种子语料",
        plan_owner="张三",
    )
    await save_plan_endpoint(key, payload, _ADMIN, _fake_request(sf))
    payload.plan_text = "补种子语料 v2"
    await save_plan_endpoint(key, payload, _ADMIN, _fake_request(sf))  # upsert 不重复

    d2 = await get_pattern_endpoint(key, _ADMIN, _fake_request(sf))
    assert d2["plan_text"] == "补种子语料 v2" and d2["plan_owner"] == "张三"
    assert "意图语料补充" in d2["plan_template"]  # 模板按分流表 (B_intent) 生成

    with pytest.raises(LumioError):
        await save_plan_endpoint(
            key,
            PlanPayload(intent_label="pat_test_plan", root_cause_layer="layer_3", plan_text="  "),
            _ADMIN,
            _fake_request(sf),
        )


async def test_plan_illegal_layer_table_rejected(pattern_db) -> None:
    """组层×表组合守门: layer_3 组不允许 A_knowledge 方案, layer_6 组允许 D/A 两表"""
    sf, created = pattern_db
    await _mk_case(sf, created, "pat-t7", "pat_test_gate", "layer_3", "守门现场")
    await _mk_case(sf, created, "pat-t8", "pat_test_gate6", "layer_6", "守门现场6")
    key3 = ps.group_key_for("pat_test_gate", "layer_3")
    key6 = ps.group_key_for("pat_test_gate6", "layer_6")

    with pytest.raises(LumioError) as ei:
        await save_plan_endpoint(
            key3,
            PlanPayload(
                intent_label="pat_test_gate",
                root_cause_layer="layer_3",
                fix_table="A_knowledge",
                plan_text="越界方案",
                plan_owner="x",
            ),
            _ADMIN,
            _fake_request(sf),
        )
    assert ei.value.code == 3001

    # 允许集内次优路径 (layer_6 → A_knowledge) 通过
    await save_plan_endpoint(
        key6,
        PlanPayload(
            intent_label="pat_test_gate6",
            root_cause_layer="layer_6",
            fix_table="A_knowledge",
            plan_text="补知识承托生成",
            plan_owner="x",
        ),
        _ADMIN,
        _fake_request(sf),
    )
    d = await get_pattern_endpoint(key6, _ADMIN, _fake_request(sf))
    assert d["fix_table"] == "A_knowledge"


async def test_batch_confirm_and_advance(pattern_db) -> None:
    sf, created = pattern_db
    cid = await _mk_case(sf, created, "pat-t6", "pat_test_loss", "layer_3", "挂失意图误判")
    key = ps.group_key_for("pat_test_loss", "layer_3")
    req = _fake_request(sf)

    res = await batch_transition_endpoint(key, BatchBody(fix_status="fixing"), _ADMIN, req)
    assert res["done"] >= 1
    bc = await get_badcase(sf, cid)
    assert bc.fix_status == "fixing"
    assert bc.human_confirmed_layer == "layer_3"  # 批量确认随方案落地
    assert bc.needs_human_review is False

    res2 = await batch_transition_endpoint(key, BatchBody(fix_status="canary"), _ADMIN, req)
    assert res2["done"] >= 1
    bc2 = await get_badcase(sf, cid)
    assert bc2.fix_status == "canary"

    # 非法跳态在目标过滤层即被挡 (fixing 批量仅针对 pending/reopened), 无案例被错误回退
    res3 = await batch_transition_endpoint(key, BatchBody(fix_status="fixing"), _ADMIN, req)
    assert res3["done"] == 0


async def test_group_not_found(pattern_db) -> None:
    sf, _ = pattern_db
    with pytest.raises(LumioError):
        await get_pattern_endpoint("nope::_nope", _ADMIN, _fake_request(sf))
