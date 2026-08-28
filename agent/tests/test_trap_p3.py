"""闭环 P3 单元测试: eval_gates / model_registry / sample_backflow

全部纯逻辑 + 临时文件, 无 DB / 无 torch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lumio.services.common.eval_gates import GOLDEN_CASES, EvalGates
from lumio.services.common.model_registry import ModelRegistry
from lumio.services.common.sample_backflow import (
    BackflowCandidate,
    finalize_confirmed,
    select_candidates,
    write_staging,
)
from lumio.services.common.trap_eval import AttribSample, AttributeEngine, EvalLayer, VerdictType

# ── 四类评估门 ──────────────────────────────────────────────────────────────


class TestEvalGates:
    def _good(self, text: str) -> tuple[str, float]:
        """按黄金集/敏感集正确分类的恒真模型 (用于 golden/sensitive 全过).

        golden 真值直接来自 GOLDEN_CASES (内置用例 + seed 易混淆对), 真值集扩容时
        本桩自动跟随, 不会因新增门用例而失效。
        """
        map_: dict[str, str] = {
            **dict(GOLDEN_CASES),
            "我的信用卡被盗刷了，马上冻结": "card_loss",
            "我要投诉你们银行乱扣费": "complaint",
            "帮我转接人工客服": "transfer_agent",
        }
        return map_.get(text, "faq"), 0.95

    def test_good_model_passes_all_offline(self) -> None:
        results = EvalGates().run(self._good)
        assert len(results) == 4
        assert all(r.passed for r in results)

    def test_safety_gate_detects_crisis_positives(self) -> None:
        results = EvalGates().run(self._good)
        safety = next(r for r in results if r.name == "safety")
        assert safety.passed is True  # 正样本命中且负样本无误触

    def test_wrong_intent_fails_golden(self) -> None:
        def bad(text: str) -> tuple[str, float]:
            return "faq", 0.95  # 全答成 faq → golden 必挂

        results = EvalGates().run(bad)
        golden = next(r for r in results if r.name == "golden")
        assert golden.passed is False
        assert golden.failures  # 有失败明细

    def test_low_confidence_sensitive_fails(self) -> None:
        def low(text: str) -> tuple[str, float]:
            return "card_loss", 0.1  # 命中但置信度 < 底线

        results = EvalGates().run(low)
        sensitive = next(r for r in results if r.name == "sensitive")
        assert sensitive.passed is False

    def test_arun_matches_run(self) -> None:
        async def model(text: str) -> tuple[str, float]:
            return self._good(text)

        import asyncio

        syn = EvalGates().run(self._good)
        asyn = asyncio.run(EvalGates().arun(model))
        assert [r.passed for r in syn] == [r.passed for r in asyn]


# ── 模型注册表 / canary ────────────────────────────────────────────────────


class TestModelRegistry:
    def test_register_canary_promote_rollback(self, tmp_path: Path) -> None:
        reg = ModelRegistry(state_path=str(tmp_path / "registry.json"))

        # v1 先作为基线 promote 到 active, 回滚才有目标
        def gates_ok():
            return [{"name": "golden", "passed": True}]

        reg.register("v1", "data/intent_classification/base", notes="基线")
        reg.set_canary("v1", traffic=1.0)
        assert reg.promote(gate_runner=gates_ok)[0] is True
        assert reg._active == "v1"
        assert reg.compose_classifier_path("fallback") != "fallback"  # v1 生效

        reg.register("v2", "data/intent_classification/v2", notes="回流后")
        reg.set_canary("v2", traffic=0.5)
        assert reg._canary == "v2" and reg._canary_traffic == 0.5

        # 门全 PASS → promote
        ok, report = reg.promote(gate_runner=gates_ok)
        assert ok is True
        assert reg._active == "v2"
        assert reg._canary is None
        assert reg._canary_traffic == 0.0
        # v2 路径生效
        assert reg.compose_classifier_path("fallback").endswith("v2")

        # 回滚
        assert reg.rollback() == "v1"
        assert reg._active == "v1"

    def test_promote_rejected_when_gate_fails(self, tmp_path: Path) -> None:
        reg = ModelRegistry(state_path=str(tmp_path / "r.json"))
        reg.register("v1", "base")
        reg.set_canary("v1")
        ok, report = reg.promote(gate_runner=lambda: [{"name": "golden", "passed": False}])
        assert ok is False
        assert reg._active is None  # 未设 active

    def test_promote_requires_gates_by_default(self, tmp_path: Path) -> None:
        reg = ModelRegistry(state_path=str(tmp_path / "r.json"), allow_ungated=False)
        reg.register("v1", "base")
        reg.set_canary("v1")
        ok, _ = reg.promote(gate_runner=None)
        assert ok is False

    def test_persistence_roundtrip(self, tmp_path: Path) -> None:
        state = str(tmp_path / "r.json")
        reg = ModelRegistry(state_path=state)
        reg.register("v1", "base")
        reg.set_canary("v1", traffic=0.3)
        reg.save()
        reg2 = ModelRegistry(state_path=state)
        assert reg2._canary == "v1"
        assert reg2._canary_traffic == 0.3

    def test_duplicate_register_raises(self, tmp_path: Path) -> None:
        reg = ModelRegistry(state_path=str(tmp_path / "r.json"))
        reg.register("v1", "base")
        with pytest.raises(ValueError):
            reg.register("v1", "other")


# ── 样本回流 ────────────────────────────────────────────────────────────────


def _clf_sample(final_intent: str, verdict: object, **kw) -> tuple[AttribSample, object]:
    s = AttribSample(
        sample_id=kw.get("sample_id", f"id-{final_intent}"),
        text=kw.get("text", f"如何{final_intent}"),
        final_source=kw.get("final_source", "llm"),
        final_intent=final_intent,
        final_confidence=kw.get("final_confidence", 0.2),
        margin=kw.get("margin", 0.05),
        divergence=kw.get("divergence", True),
        fast_source=kw.get("fast_source", "bert"),
        fast_intent=kw.get("fast_intent", "faq"),
        fast_confidence=0.2,
    )
    return s, verdict


class TestSampleBackflow:
    def _classification_failure(self, text: str, sample_id: str) -> tuple[AttribSample, object]:
        s = AttribSample(
            sample_id=sample_id,
            text=text,
            final_source="llm",
            final_intent="faq",
            final_confidence=0.2,
            margin=0.05,
            divergence=True,
            fast_source="bert",
            fast_intent="bill_query",
            fast_confidence=0.2,
        )
        v = AttributeEngine().attribute(s)
        return s, v

    def test_select_only_classification_failures(self) -> None:
        s, v = self._classification_failure("怎么申请分期", "s1")
        assert v.layer == EvalLayer.CLASSIFICATION
        assert v.verdict == VerdictType.FAILURE
        cands = select_candidates([(s, v)], max_n=10)
        assert cands
        assert cands[0].layer == "classification"
        assert cands[0].verdict == "failure"

    def test_skips_healthy_layer(self) -> None:
        s = AttribSample(
            sample_id="h", text="正常", final_source="fast", final_intent="faq", final_confidence=0.95, margin=0.4
        )
        v = AttributeEngine().attribute(s)  # healthy / unassigned
        cands = select_candidates([(s, v)], max_n=10)
        assert cands == []

    def test_dedup_by_text_intent(self) -> None:
        s1, v1 = self._classification_failure("重复问法", "a")
        s2, v2 = self._classification_failure("重复问法", "b")
        cands = select_candidates([(s1, v1), (s2, v2)], max_n=10)
        assert len(cands) == 1
        assert cands[0].sample_id == "a"

    def test_staging_and_finalize(self, tmp_path: Path) -> None:
        review = str(tmp_path / "review.jsonl")
        seed_path = tmp_path / "seed.json"
        # 真实 seed schema: 计数在 meta.counts.examples, 不在 meta.examples (回流若写错键,
        # 官方计数口径会过期 — 见 sample_backflow.finalize_confirmed 注释)
        seed_path.write_text(
            json.dumps(
                {"meta": {"version": "0.2.0", "counts": {"examples": 0, "confusable_pairs": 15}}, "examples": []},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        cands = [BackflowCandidate("a", "这是样例", "faq", "classification", "failure", 0.3, 0.1, "reason")]
        write_staging(cands, review)
        # 未批准 → 不并入
        assert finalize_confirmed(review, str(seed_path))[0] == 0
        # 批准后 → 并入
        import json as _j

        lines = []
        for line in Path(review).read_text(encoding="utf-8").splitlines():
            row = _j.loads(line)
            row["approved"] = True
            lines.append(_j.dumps(row, ensure_ascii=False))
        Path(review).write_text("\n".join(lines), encoding="utf-8")
        added, version = finalize_confirmed(review, str(seed_path))
        assert added == 1
        assert version.startswith("0.2.")
        merged = _j.loads(seed_path.read_text(encoding="utf-8"))
        assert merged["meta"]["counts"]["examples"] == 1
        assert "examples" not in merged["meta"]  # 不允许写错键

    def test_finalize_skips_invalid_intent(self, tmp_path: Path) -> None:
        review = str(tmp_path / "r.jsonl")
        row = {
            "sample_id": "b",
            "text": "某文本",
            "intent": "not_a_real_intent",
            "layer": "classification",
            "verdict": "failure",
            "confidence": 0.3,
            "margin": 0.1,
            "reason": "r",
            "approved": True,
        }
        Path(review).write_text(json.dumps(row, ensure_ascii=False), encoding="utf-8")
        seed_path = tmp_path / "seed.json"
        seed_path.write_text(
            json.dumps({"meta": {"version": "0.1.0", "examples": 0}, "examples": []}), encoding="utf-8"
        )
        assert finalize_confirmed(review, str(seed_path))[0] == 0
