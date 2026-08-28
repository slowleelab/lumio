"""闭环 P2 评估/归因 (AttributeEngine) 单元测试

覆盖: 分类层归因(分歧/慢路径/贴边)、retrieval/generation 候选、
结果弱标签叠加、汇总聚合、会话弱标签富化.
纯逻辑单测, 无 DB / 无 Redis.
"""

from __future__ import annotations

from lumio.services.common.trap_eval import (
    AttribSample,
    AttributeEngine,
    EvalLayer,
    VerdictType,
)


def _sample(**kw) -> AttribSample:
    base = dict(
        sample_id="s1",
        text="我的额度怎么查",
        fast_source="rule",
        fast_intent="limit_query",
        fast_confidence=0.9,
        rule_intent=None,
        final_source="fast",
        final_intent="limit_query",
        final_confidence=0.9,
        margin=0.3,
        divergence=False,
        reasons=[],
    )
    base.update(kw)
    return AttribSample(**base)  # type: ignore[arg-type]


class TestClassificationLayer:
    def test_divergence_root_cause(self) -> None:
        v = AttributeEngine().attribute(
            _sample(fast_source="bert", fast_intent="bill_query", rule_intent="limit_query", divergence=True)
        )
        assert v.layer == EvalLayer.CLASSIFICATION
        assert v.verdict == VerdictType.FAILURE
        assert any("分歧" in e for e in v.evidences)
        assert v.re_rank >= 10

    def test_slow_path_root_cause(self) -> None:
        v = AttributeEngine().attribute(_sample(final_source="llm", fast_confidence=0.3))
        assert v.layer == EvalLayer.CLASSIFICATION
        assert any("慢路径" in e for e in v.evidences)

    def test_fallback_root_cause(self) -> None:
        v = AttributeEngine().attribute(_sample(final_source="fallback", fast_confidence=0.2))
        assert v.layer == EvalLayer.CLASSIFICATION

    def test_near_threshold_evidences_but_confident_fast(self) -> None:
        # 快路径自信但贴边 + 无结果问题 → 分类层 UCERTAIN/FAILURE 由 verdict 受 outcome 影响
        v = AttributeEngine(margin_band=0.15).attribute(
            _sample(final_source="fast", final_confidence=0.68, margin=0.08)
        )
        # 无 outcome 负信号 → 走 UNASSIGNED/UNCERTAIN
        assert v.layer == EvalLayer.UNASSIGNED
        assert v.verdict == VerdictType.UNCERTAIN


class TestOutcomeDrivenLayers:
    def test_confident_knowledge_unsatisfied_is_retrieval_candidate(self) -> None:
        v = AttributeEngine().attribute(
            _sample(final_source="fast", final_intent="faq", final_confidence=0.95, margin=0.35, transferred=True)
        )
        assert v.layer == EvalLayer.RETRIEVAL
        assert v.verdict == VerdictType.PENDING
        assert v.re_rank == 6

    def test_confident_business_unsatisfied_is_generation_candidate(self) -> None:
        v = AttributeEngine().attribute(
            _sample(
                final_source="fast",
                final_intent="card_loss",
                final_confidence=0.95,
                margin=0.35,
                human_request_score=2,
            )
        )
        assert v.layer == EvalLayer.GENERATION
        assert v.verdict == VerdictType.PENDING

    def test_outcome_negative_escalates_classification(self) -> None:
        v = AttributeEngine().attribute(
            _sample(final_source="llm", fast_confidence=0.2, transferred=True, human_request_score=1)
        )
        assert v.layer == EvalLayer.CLASSIFICATION
        # 分类断层 + 结果未解决 → 更高重排权
        assert v.re_rank >= 15


class TestHealthy:
    def test_high_confidence_no_issue_healthy(self) -> None:
        v = AttributeEngine().attribute(_sample(final_source="fast", final_confidence=0.95, margin=0.4))
        assert v.layer == EvalLayer.UNASSIGNED
        assert v.verdict == VerdictType.HEALTHY
        assert v.re_rank == 0


class TestSummary:
    def test_root_cause_summary_aggregates(self) -> None:
        engine = AttributeEngine()
        verdicts = [
            engine.attribute(_sample(final_source="llm", sample_id="a", fast_confidence=0.2)),
            engine.attribute(
                _sample(
                    final_source="fast",
                    final_intent="faq",
                    final_confidence=0.95,
                    margin=0.4,
                    transferred=True,
                    sample_id="b",
                )
            ),
            engine.attribute(_sample(sample_id="c", final_confidence=0.95, margin=0.4)),
        ]
        summary = engine.root_cause_summary(verdicts)
        assert summary["total"] == 3
        # a → classification failure; b → retrieval pending; c → healthy
        assert summary["by_layer"].get("classification") == 1
        assert summary["by_layer"].get("retrieval") == 1
        assert summary["by_verdict"].get("healthy") == 1
        # actionable 不含 healthy
        assert {x["sample_id"] for x in summary["actionable"]} == {"a", "b"}

    def test_actionable_sorted_by_rerank_desc(self) -> None:
        engine = AttributeEngine()
        low = engine.attribute(_sample(final_source="llm", sample_id="low", fast_confidence=0.2))
        high = engine.attribute(_sample(final_source="llm", sample_id="high", fast_confidence=0.2, transferred=True))
        summary = engine.root_cause_summary([low, high])
        assert summary["actionable"][0]["sample_id"] == "high"
