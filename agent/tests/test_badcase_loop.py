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

        async def execute(self, q):
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


def _sf_smart():
    """带查询参数解析的 fake factory: 支持 capture_badcase 的去重 SELECT 语义"""

    store: dict = {"badcases": []}

    class FakeResult:
        def __init__(self, val):
            self._v = val

        def scalar_one_or_none(self):
            return self._v

        def scalar(self):
            return 0

        def scalars(self):
            return self

        def all(self):
            return [self._v] if self._v else []

    def _dedup_match(stmt):
        try:
            params = stmt.compile().params
        except Exception:
            return None
        colvals = {}
        for k, v in params.items():
            base = k.rsplit("_", 1)[0]
            if base in ("dedup_group_id", "signal_source", "fix_status"):
                colvals[base] = v
        if not colvals:
            return None
        hits = [b for b in store["badcases"] if all(getattr(b, c, None) == v for c, v in colvals.items())]
        return hits[-1] if hits else None

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

        async def execute(self, q):
            return FakeResult(_dedup_match(q))

    class Factory:
        def __call__(self):
            return Session()

    return Factory(), store


@pytest.mark.asyncio
async def test_capture_dedup_aggregates_same_input() -> None:
    """同输入+同信号源+未处置 → 累加出现次数, 不插新行"""
    factory, store = _sf_smart()
    kw = dict(trace_id="t", session_id="s", signal_source="transfer", user_input="我的卡丢了, 要挂失")
    first = await capture_badcase(factory, **kw)
    assert len(store["badcases"]) == 1
    assert first.signal_detail["occurrences"] == 1

    second = await capture_badcase(factory, **kw)
    assert len(store["badcases"]) == 1, "重复信号应聚合到既有行"
    assert second is first
    assert second.signal_detail["occurrences"] == 2
    assert second.signal_detail["last_seen_at"] == "s"

    # 不同信号源 → 各自一行
    await capture_badcase(factory, trace_id="t", session_id="s", signal_source="negative_feedback", user_input="我的卡丢了, 要挂失")
    assert len(store["badcases"]) == 2


@pytest.mark.asyncio
async def test_capture_reopens_after_resolved() -> None:
    """处置过 (fix_status != pending) 的组重新开新行, 保留修复前后对照"""
    factory, store = _sf_smart()
    kw = dict(trace_id="t", session_id="s", signal_source="transfer", user_input="我的卡丢了, 要挂失")
    first = await capture_badcase(factory, **kw)
    first.fix_status = "deployed"  # 模拟已处置
    again = await capture_badcase(factory, **kw)
    assert len(store["badcases"]) == 2, "已处置后同输入应开新行"
    assert again is not first
    assert again.signal_detail["occurrences"] == 1


@pytest.mark.asyncio
async def test_capture_dedup_merges_snapshot() -> None:
    """聚合时快照合并 (后到的中间产物字段覆盖), bot_output 取最近"""
    factory, store = _sf_smart()
    kw = dict(trace_id="t", session_id="s1", signal_source="transfer", user_input="转人工")
    await capture_badcase(factory, bot_output="旧回复", snapshot={"intent": "transfer_agent"}, **kw)
    merged = await capture_badcase(
        factory, bot_output="新回复", snapshot={"confidence": 0.9, "intent": "transfer_agent"}, **kw
    )
    assert merged.snapshot == {"intent": "transfer_agent", "confidence": 0.9}
    assert merged.bot_output == "新回复"


# ── 跨家族远程裁判 (Anthropic 协议 + 本地兜底) ─────────────────────────


def _judge_env(monkeypatch, base="https://fake.example", key="k", model="GLM-5.3-Flash"):
    from lumio.shared.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s.llm, "judge_base_url", base)
    monkeypatch.setattr(s.llm, "judge_api_key", key)
    monkeypatch.setattr(s.llm, "judge_model", model)
    monkeypatch.setattr(s.llm, "judge_timeout", 10.0)


@pytest.mark.asyncio
async def test_remote_judge_parses_anthropic_response(monkeypatch) -> None:
    """thinking 块过滤 + text 块拼接 + chat_json 解析"""
    import lumio.services.common.judge_client as jc

    _judge_env(monkeypatch, base="https://fake.example")
    client = jc.RemoteJudgeClient(fallback_llm=None)

    async def fake_remote(messages, timeout):
        return '{"root_cause_layer": "layer_5", "note": "ok"}'

    monkeypatch.setattr(client, "_remote", fake_remote)
    out = await client.chat_json([{"role": "user", "content": "x"}])
    assert out["root_cause_layer"] == "layer_5"


@pytest.mark.asyncio
async def test_remote_judge_falls_back_to_local(monkeypatch) -> None:
    """远程异常 → 本地 LLMClient 兜底, 且进入降级期 (后续直接走本地)"""
    import lumio.services.common.judge_client as jc

    _judge_env(monkeypatch)
    from lumio.shared.config import get_settings

    monkeypatch.setattr(get_settings().llm, "judge_strict", False)  # 免受部署 env 污染
    calls = {"local": 0}
    local = MagicMock()
    local.chat = AsyncMock(side_effect=lambda m, timeout=None: (calls.__setitem__("local", calls["local"] + 1) or "本地回复"))
    client = jc.RemoteJudgeClient(fallback_llm=local)

    async def boom(messages, timeout):
        raise RuntimeError("network down")

    monkeypatch.setattr(client, "_remote", boom)
    out = await client.chat([{"role": "user", "content": "x"}])
    assert out == "本地回复"
    assert calls["local"] == 1
    # 降级后不再打远程
    out2 = await client.chat([{"role": "user", "content": "y"}])
    assert out2 == "本地回复" and calls["local"] == 2


def test_remote_judge_json_fencing() -> None:
    from lumio.services.common.judge_client import _parse_json

    assert _parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _parse_json('前置说明 {"b": 2} 尾部') == {"b": 2}
    assert _parse_json("完全不是json") is None


# ── 严格模式: 全程纯 GLM (失败重试耗尽后判失败, 不回退本地) ──


@pytest.mark.asyncio
async def test_strict_mode_never_falls_back(monkeypatch) -> None:
    import lumio.services.common.judge_client as jc
    from lumio.shared.config import get_settings

    _judge_env(monkeypatch)
    get_settings().llm.judge_strict = True
    try:
        local = MagicMock()
        local.chat = AsyncMock(return_value="本地回复")
        client = jc.RemoteJudgeClient(fallback_llm=local)

        async def boom(messages, timeout):
            raise RuntimeError("429 rate limit")

        monkeypatch.setattr(client, "_remote", boom)
        client._retry_delays = (0.0, 0.0)
        with pytest.raises(RuntimeError, match="429"):
            await client.chat([{"role": "user", "content": "x"}])
        local.chat.assert_not_awaited()  # 严格模式绝不碰本地
    finally:
        get_settings().llm.judge_strict = False


@pytest.mark.asyncio
async def test_remote_retry_recovers_transient(monkeypatch) -> None:
    """瞬时限流重试后成功, 不降级不回退"""
    import lumio.services.common.judge_client as jc

    _judge_env(monkeypatch)
    client = jc.RemoteJudgeClient(fallback_llm=None)
    calls = {"n": 0}

    async def flaky(messages, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("429 transient")
        return "重试后成功"

    monkeypatch.setattr(client, "_remote", flaky)
    client._retry_delays = (0.0, 0.0)  # 测试跳过退避等待
    out = await client.chat([{"role": "user", "content": "x"}])
    assert out == "重试后成功"
    assert calls["n"] == 2


class TestOutboundSensitiveSolicitation:
    """出站索敏检测 (qa_scan 复盘: knowledge 生成索要卡号后四位)"""

    def _guard(self):
        from lumio.services.bot.outbound_guard import OutboundGuard

        class _FakeSafety:
            def check_input(self, text):
                return True, []

        return OutboundGuard(_FakeSafety(), "安全话术")

    def test_blocks_solicitation(self) -> None:
        g = self._guard()
        for reply in [
            "请提供您信用卡的后四位以便验证身份。",
            "请告诉我您的卡号后四位，我帮您查询。",
            "麻烦提供一下验证码。",
            "为了确保安全, 请发送您的完整卡号。",
        ]:
            v = g.check(reply, grounding_source="挂失流程文档")
            assert v.passed is False, reply
            assert v.reason == "sensitive_solicitation"

    def test_allows_process_description(self) -> None:
        g = self._guard()
        # 流程描述 (转述银行官方渠道的操作步骤) 不算索敏
        v = g.check("您可以在手机银行APP挂失, 或拨打客服热线400-888-8888办理。")
        assert v.passed is True
        v = g.check("挂失补卡需在网点办理, 携带本人身份证即可。")
        assert v.passed is True
