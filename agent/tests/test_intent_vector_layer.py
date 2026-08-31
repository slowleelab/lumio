"""L2 向量检索意图测试（目标架构 ③ 分层管道第二层）"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from lumio.services.common.intent_vector import VectorIntentMatch
from lumio.shared.models import IntentLabel, IntentResult


def _vm(matched=True, intent="query", score=0.85):
    return VectorIntentMatch(matched=matched, intent=intent, score=score, exemplar="我的额度是多少")


@pytest.fixture
def classifier_with_vector(monkeypatch):
    """构造带 L2 向量层的 IntentClassifier（规则/BERT/LLM 全 mock）"""
    from lumio.services.common.classifier import IntentClassifier

    rule = MagicMock()
    rule.classify.return_value = IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.3)
    llm = MagicMock()
    llm.classify = AsyncMock(
        return_value={"intent": "faq", "confidence": 0.5, "entities": [], "sentiment": "neutral"}
    )
    vec = MagicMock()
    vec.search = AsyncMock(return_value=_vm())

    settings_patch = {
        "classification.vector_intent_enabled": True,
        "classification.vector_intent_threshold": 0.55,
        "classification.intent_threshold": 0.6,
    }
    real_get_settings = None

    clf = IntentClassifier(rule_classifier=rule, llm_classifier=llm, intent_vector=vec)

    from lumio.shared.config import Settings

    settings = Settings(_env_file=())
    settings.classification.vector_intent_enabled = True
    settings.classification.vector_intent_threshold = 0.78
    monkeypatch.setattr("lumio.services.common.classifier.get_settings", lambda: settings)
    return clf, vec


@pytest.mark.asyncio
async def test_l2_vector_hit_between_rule_and_llm(classifier_with_vector, monkeypatch) -> None:
    """规则未命中 → L2 向量命中 → 直接返回, 不调 L3 LLM"""
    clf, vec = classifier_with_vector
    result, _, _, source = await clf.classify("我的额度是多少")
    vec.search.assert_awaited_once()
    # L2 判定五域 query → 域代表叶子 account_bill_query; 定义句式强制咨询域
    assert result.primary_intent in (IntentLabel.ACCOUNT_BILL_QUERY, IntentLabel.FAQ)
    assert result.primary_confidence == pytest.approx(0.85, abs=1e-3)
    assert source == "vector"


@pytest.mark.asyncio
async def test_l2_low_score_falls_to_llm(classifier_with_vector, monkeypatch) -> None:
    """L2 置信不足 (<阈值) → 落 L3 LLM"""
    clf, vec = classifier_with_vector
    vec.search = AsyncMock(return_value=_vm(score=0.4))
    result, _, _, source = await clf.classify("随便一句")
    vec.search.assert_awaited_once()
    assert source in ("llm", "fallback")  # 落 L3 (LLM faq 兜底归 fallback 源)


@pytest.mark.asyncio
async def test_l2_disabled_skips(classifier_with_vector, monkeypatch) -> None:
    """开关关闭 → 跳过 L2"""
    clf, vec = classifier_with_vector
    clf._intent_vector = None
    await clf.classify("我的额度是多少")
    vec.search.assert_not_awaited()


@pytest.mark.asyncio
async def test_l2_error_tolerated(classifier_with_vector) -> None:
    """L2 异常不阻断, 落 L3"""
    clf, vec = classifier_with_vector
    vec.search = AsyncMock(side_effect=RuntimeError("milvus down"))
    result, _, _, source = await clf.classify("我的额度是多少")
    assert source in ("llm", "fallback")
