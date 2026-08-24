"""闭环 P1 感知缝 (TrapCollector) 单元测试

覆盖: PII 打码、采样触发条件、上下文归属、后台落库、分类器接缝注入.
不加载真实 torch / 不依赖 PG (用 fake session factory 捕获落库行).
"""

from __future__ import annotations

import asyncio

from lumio.services.common.classifier import IntentClassifier
from lumio.services.common.trap_collector import (
    TrapCollector,
    TrapRecord,
    mask_pii,
    reset_trap_context,
    set_trap_context,
)
from lumio.shared.models import IntentLabel, IntentResult


class _FakeSession:
    """捕获 add 进来的 ORM 对象, 不真正落库."""

    def __init__(self, sink: list) -> None:
        self._sink = sink

    def add(self, row) -> None:
        self._sink.append(row)

    async def commit(self) -> None:
        return None


class _FakeFactory:
    def __init__(self, sink: list) -> None:
        self._sink = sink

    def __call__(self):
        return self

    async def __aenter__(self) -> _FakeSession:
        return _FakeSession(self._sink)

    async def __aexit__(self, *exc) -> None:
        return None


class _FakeRule:
    def __init__(self, intent: str, conf: float = 0.9) -> None:
        self._intent = IntentLabel(intent)
        self._conf = conf

    def classify(self, text: str) -> IntentResult:
        return IntentResult(primary_intent=self._intent, primary_confidence=self._conf)


# ── PII 打码 ────────────────────────────────────────────────────────────────


class TestMaskPii:
    def test_masks_phone_keeps_last4(self) -> None:
        assert mask_pii("我的手机号是 13812345678") == "我的手机号是 ****5678"

    def test_masks_id_keeps_last4(self) -> None:
        assert mask_pii("身份证 110101199003070000") == "身份证 ****0000"

    def test_masks_card_keeps_last4(self) -> None:
        assert mask_pii("卡号 6222021234567890") == "卡号 ****7890"

    def test_leaves_plain_text(self) -> None:
        assert mask_pii("怎么查信用卡账单") == "怎么查信用卡账单"

    def test_empty(self) -> None:
        assert mask_pii("") == ""


def _rec(**kw) -> TrapRecord:
    base = dict(
        text="用户想问额度",
        fast_source="rule",
        fast_intent="limit_query",
        fast_confidence=0.3,
        rule_intent=None,
        final_source="fallback",
        final_intent="faq",
        final_confidence=0.3,
        margin=0.1,
    )
    base.update(kw)
    return TrapRecord(**base)  # type: ignore[arg-type]


# ── 采样触发条件 ─────────────────────────────────────────────────────────────


class TestShouldCapture:
    def setup_method(self) -> None:
        # 固定随机 → ambient 命中与否可预测: 用 rate=0 关闭环境采样
        self.collector = TrapCollector(enabled=True, threshold=0.6, band=0.15, ambient_rate=0.0)

    def test_slow_path_captured(self) -> None:
        assert self.collector.should_capture(_rec(final_source="llm", margin=0.3))

    def test_near_threshold_captured(self) -> None:
        assert self.collector.should_capture(_rec(final_source="fast", final_confidence=0.65, margin=0.05))

    def test_divergence_captured(self) -> None:
        assert self.collector.should_capture(_rec(divergence=True, margin=0.3))

    def test_clear_normal_not_captured(self) -> None:
        # 高置信 + 无分歧 + 无慢路径 → 不采 (除非 ambient)
        assert not self.collector.should_capture(_rec(final_source="fast", final_confidence=0.9, margin=0.3))

    def test_disabled_never_captures(self) -> None:
        c = TrapCollector(enabled=False, ambient_rate=0.0)
        assert not c.should_capture(_rec(final_source="llm", margin=0.1))

    def test_reasons_ordered(self) -> None:
        rec = _rec(final_source="llm", margin=0.05, divergence=True)
        self.collector.should_capture(rec)
        assert rec.reasons == ["slow_path", "near_threshold", "rule_bert_divergence"]


# ── 上下文归属 + 落库 ────────────────────────────────────────────────────────


class TestCapture:
    def test_context_fills_session_and_customer(self) -> None:
        sink: list = []
        collector = TrapCollector(session_factory=_FakeFactory(sink), threshold=0.6, band=0.15, ambient_rate=0.0)
        tok = set_trap_context("sess-1", "cust-9")
        try:
            capture = _rec(final_source="llm", text="查 13812345678 的问题")
            collector.should_capture(capture)
            collector._apply_context(capture)
            assert capture.session_id == "sess-1"
            assert capture.customer_id == "cust-9"
        finally:
            reset_trap_context(tok)

    async def test_capture_persists_masked_row(self) -> None:
        from lumio.shared.orm_models import ClassifierSample

        sink: list = []
        collector = TrapCollector(session_factory=_FakeFactory(sink), threshold=0.6, band=0.15, ambient_rate=0.0)
        tok = set_trap_context("sess-1", "cust-9")
        try:
            captured = await collector.capture(_rec(final_source="llm", text="联系 13812345678 吗"))
            assert captured is True
            # 等后台 task 完成
            while collector._pending_tasks:
                await asyncio.sleep(0.01)
            assert len(sink) == 1
            row: ClassifierSample = sink[0]
            assert row.session_id == "sess-1"
            assert row.customer_id == "cust-9"
            assert "13812345678" not in row.text  # 已打码
            assert row.text == "联系 ****5678 吗"
        finally:
            reset_trap_context(tok)

    async def test_capture_skips_when_not_selected(self) -> None:
        sink: list = []
        collector = TrapCollector(session_factory=_FakeFactory(sink), threshold=0.6, band=0.15, ambient_rate=0.0)
        captured = await collector.capture(_rec(final_source="fast", final_confidence=0.9, margin=0.4))
        assert captured is False
        assert sink == []


# ── 分类器接缝 (无 BERT / 无 LLM → 慢路径兜底被捕获) ─────────────────────────


class TestClassifierSeam:
    async def test_low_confidence_fast_captured_via_seam(self) -> None:
        sink: list = []
        collector = TrapCollector(session_factory=_FakeFactory(sink), threshold=0.6, band=0.15, ambient_rate=0.0)
        clf = IntentClassifier(
            rule_classifier=_FakeRule("faq", conf=0.3),  # 低置信 → 走慢路径
            llm_classifier=None,  # 无 LLM → fallback
            fast_threshold=0.5,
            trap=collector,
        )
        tok = set_trap_context("sess-x", "cust-x")
        try:
            intent, _, _, source = await clf.classify("我的额度怎么查")
            assert source == "fallback"
            while collector._pending_tasks:
                await asyncio.sleep(0.01)
            assert len(sink) == 1
            assert sink[0].final_source == "fallback"
            assert sink[0].reasons == ["slow_path"]
        finally:
            reset_trap_context(tok)

    async def test_high_confidence_clear_not_captured(self) -> None:
        sink: list = []
        collector = TrapCollector(session_factory=_FakeFactory(sink), threshold=0.6, band=0.15, ambient_rate=0.0)
        clf = IntentClassifier(
            rule_classifier=_FakeRule("limit_query", conf=0.95),
            llm_classifier=None,
            fast_threshold=0.5,
            trap=collector,
        )
        intent, _, _, source = await clf.classify("我的额度怎么查")
        assert source == "rule"
        while collector._pending_tasks:
            await asyncio.sleep(0.01)
        assert sink == []

    async def test_no_trap_is_noop(self) -> None:
        clf = IntentClassifier(
            rule_classifier=_FakeRule("faq", conf=0.3),
            llm_classifier=None,
            fast_threshold=0.5,
        )
        intent, _, _, source = await clf.classify("任何问题")
        assert source == "fallback"
