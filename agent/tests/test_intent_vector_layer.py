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
    llm.classify = AsyncMock(return_value={"intent": "faq", "confidence": 0.5, "entities": [], "sentiment": "neutral"})
    vec = MagicMock()
    vec.search = AsyncMock(return_value=_vm())

    clf = IntentClassifier(rule_classifier=rule, llm_classifier=llm, intent_vector=vec)

    from lumio.shared.config import Settings

    settings = Settings(_env_file=())
    settings.classification.vector_intent_enabled = True
    settings.classification.vector_intent_threshold = 0.78
    monkeypatch.setattr("lumio.services.common.classifier.get_settings", lambda: settings)
    return clf, vec


@pytest.mark.asyncio
async def test_l2_vector_hit_between_rule_and_llm(classifier_with_vector, monkeypatch) -> None:
    """规则未命中 → L2 向量命中且与快路径同域 (精确叶子) → 直接返回, 不调 L3 LLM"""
    clf, vec = classifier_with_vector
    # 同域场景: 快路径 FAQ(knowledge 域), 向量也判 knowledge → 取快路径叶子
    vec.search = AsyncMock(return_value=_vm(intent="consulting", score=0.85))
    result, _, _, source = await clf.classify("白金卡有什么权益")
    vec.search.assert_awaited_once()
    assert result.primary_intent == IntentLabel.FAQ
    assert result.primary_confidence == pytest.approx(0.85, abs=1e-3)
    assert source == "vector"


@pytest.mark.asyncio
async def test_l2_domain_representative_caps_to_llm(classifier_with_vector) -> None:
    """域代表兜底不算强识别 (会话 8d988206 复盘): 快路径业务意图与向量域不一致
    (limit_query@query域 vs 向量强制咨询域) → 取域代表时置信封顶到采纳阈下,
    强制落 L3 终判 — 余弦相似度不冒充分类置信豁免 LLM"""
    clf, vec = classifier_with_vector
    # 复现生产形态: 快路径 limit_query@0.2 (query 域), 向量命中被"怎么办"句式强制到
    # consulting 域 → 异域 → 域代表分支 (曾经的假高置信直返豁免 L3)

    clf._rule.classify.return_value = IntentResult(primary_intent=IntentLabel.LIMIT_QUERY, primary_confidence=0.2)
    vec.search = AsyncMock(return_value=_vm(intent="query", score=0.85))
    result, _, _, source = await clf.classify("我想转账但限额怎么办")
    vec.search.assert_awaited_once()
    clf._llm.classify.assert_awaited_once()  # L3 被触发
    assert result.primary_confidence < 0.85  # 高相似度不再透传为高置信


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
