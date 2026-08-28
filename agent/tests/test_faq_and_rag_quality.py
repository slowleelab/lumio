"""FAQ 命中与 RAG 检索质量案例测试

用一组有代表性的业务查询案例验证:
1. FAQ 检索 (search_faq) 三路命中: 精确匹配 / 语义匹配 / 未命中, 以及权限/卡种过滤。
2. RAG 检索 (retrieve) 质量: 相关性排序 / 置信度过滤 / 词法证据门。

全部 mock 依赖 (Redis/Milvus/ES/embedding), 无真实中间件, CI 可跑。
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lumio.services.common.faq_service import search_faq
from lumio.services.common.retrieval import retrieve
from lumio.shared.models import RetrieveRequest


def _faq_hit(chunk_id: str, content: str, category: str, score: float, card_types: str = "") -> MagicMock:
    """构造 Milvus 语义检索命中的一个 FAQ hit"""
    hit = MagicMock()
    hit.entity = {
        "chunk_id": chunk_id,
        "content": content,
        "category": category,
        "card_types": card_types,
        "keywords": [],
    }
    hit.score = score
    return hit


def _exact_faq_payload(**overrides) -> str:
    """构造 Redis 精确匹配缓存里的 FAQ JSON"""
    payload = {
        "id": "faq-1",
        "question": "如何办理账单分期",
        "answer": "办理分期请通过手机银行 App，进入信用卡-账单分期。",
        "allowed_roles": None,
        "card_types": None,
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


# ── FAQ 命中 ──


class TestFaqHit:
    @pytest.mark.asyncio
    async def test_exact_match_hit(self) -> None:
        """精确匹配: 归一化后的问法命中 Redis 缓存, 直接返回 exact, 不走语义"""
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=_exact_faq_payload())

        result = await search_faq("如何办理账单分期", redis_client=redis)

        assert result["match_type"] == "exact"
        assert result["results"][0]["question"] == "如何办理账单分期"

    @pytest.mark.asyncio
    async def test_exact_match_permission_filter_falls_to_semantic(self) -> None:
        """精确命中但角色无权限: 降级到语义匹配 (不直接返回)"""
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=_exact_faq_payload(allowed_roles=["vip"]))

        # 普通角色 + 无语义检索能力 -> 未命中
        result = await search_faq("如何办理账单分期", redis_client=redis, user_role="normal")

        assert result["match_type"] == "miss"

    @pytest.mark.asyncio
    async def test_semantic_match_hit(self) -> None:
        """语义匹配: 变体问法经 embedding 命中 Milvus faq_qa chunk"""
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        embedding = MagicMock()
        embedding.embed_query = AsyncMock(return_value=[0.1, 0.2, 0.3])
        milvus = MagicMock()
        milvus.search = MagicMock(return_value=[[_faq_hit("faq-1", "如何办理账单分期？", "分期", 0.92)]])

        result = await search_faq(
            "分期怎么弄", redis_client=redis, embedding_provider=embedding, milvus_collection=milvus
        )

        assert result["match_type"] == "semantic"
        assert result["results"][0]["faq_id"] == "faq-1"
        assert result["results"][0]["score"] == 0.92

    @pytest.mark.asyncio
    async def test_semantic_card_type_filter(self) -> None:
        """语义命中但卡种不适用: 跳过该 FAQ, 结果为空"""
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        embedding = MagicMock()
        embedding.embed_query = AsyncMock(return_value=[0.1])
        milvus = MagicMock()
        milvus.search = MagicMock(
            return_value=[[_faq_hit("faq-1", "白金卡专享分期", "分期", 0.90, card_types="白金卡")]]
        )

        result = await search_faq(
            "分期怎么弄", redis_client=redis, embedding_provider=embedding, milvus_collection=milvus, card_type="普卡"
        )

        assert result["match_type"] == "miss"

    @pytest.mark.asyncio
    async def test_miss_when_no_cache_and_no_semantic(self) -> None:
        """无精确命中 + 无语义能力 -> miss, 由调用方降级通用 RAG"""
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)

        result = await search_faq("今天天气怎么样", redis_client=redis)

        assert result["match_type"] == "miss"
        assert result["results"] == []


# ── RAG 检索质量 ──


def _es_hit(doc_id: str, content: str, score: float) -> dict:
    return {
        "_id": doc_id,
        "_score": score,
        "_source": {
            "chunk_id": doc_id,
            "content": content,
            "doc_id": f"d-{doc_id}",
            "approval_status": "PUBLISHED",
            "is_current_version": True,
        },
    }


def _patch_rag_settings(**overrides) -> Any:
    """patch retrieve 的 get_settings, 返回可控的 RAG 配置"""
    settings = MagicMock()
    settings.rag.rrf_k = 60
    settings.rag.confidence_threshold = 0.5
    settings.rag.rrf_confidence_threshold = 0.0
    settings.rag.require_lexical_evidence = False
    settings.elasticsearch.index_prefix = "lumio"
    for k, v in overrides.items():
        setattr(settings.rag, k, v)
    return settings


class TestRagQuality:
    @pytest.mark.asyncio
    async def test_relevant_chunks_rank_first(self) -> None:
        """相关性排序: 高分相关文档排在低分无关文档之前 (bm25_only 保持 BM25 分数降序)"""
        mock_es = AsyncMock()
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    _es_hit("c-relevant", "信用卡年费减免：刷卡满 6 次免次年年费", 4.5),
                    _es_hit("c-partial", "信用卡年费常见问题汇总", 2.1),
                    _es_hit("c-noise", "信用卡积分兑换规则说明", 0.3),
                ]
            }
        }
        request = RetrieveRequest(query="年费怎么减免", top_k=3, search_type="bm25_only", rerank=False)

        with patch("lumio.services.common.retrieval.get_settings", return_value=_patch_rag_settings()):
            resp = await retrieve(
                request, es_client=mock_es, milvus_collection=None, embedding_provider=None, reranker=None
            )

        ids = [c.chunk_id for c in resp.results]
        assert ids[0] == "c-relevant"  # 最相关的排第一
        assert "c-relevant" in ids
        assert resp.results[0].score >= resp.results[-1].score  # 分数降序

    @pytest.mark.asyncio
    async def test_low_confidence_filtered_by_threshold(self) -> None:
        """置信度阈值: 低于 rrf_confidence_threshold 的候选被过滤"""
        mock_es = AsyncMock()
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    _es_hit("c-good", "信用卡年费减免政策", 3.0),
                    _es_hit("c-weak", "无关内容", 0.05),
                ]
            }
        }
        request = RetrieveRequest(query="年费", top_k=3, search_type="bm25_only", rerank=False)

        with patch(
            "lumio.services.common.retrieval.get_settings",
            return_value=_patch_rag_settings(rrf_confidence_threshold=0.5),
        ):
            resp = await retrieve(
                request, es_client=mock_es, milvus_collection=None, embedding_provider=None, reranker=None
            )

        assert all(c.score >= 0.5 for c in resp.results)
        assert any(c.chunk_id == "c-good" for c in resp.results)

    @pytest.mark.asyncio
    async def test_lexical_gate_blocks_zero_bm25_hits(self) -> None:
        """词法证据门: BM25 零命中(无任何 token 匹配)时返回空, 不把无关知识喂给 LLM"""
        mock_es = AsyncMock()
        # ES 应答了, 但 best score 为 0 —— 没有任何词法证据
        mock_es.search.return_value = {"hits": {"hits": []}}

        request = RetrieveRequest(query="额佛呢份", top_k=3, search_type="bm25_only", rerank=False)

        with patch(
            "lumio.services.common.retrieval.get_settings",
            return_value=_patch_rag_settings(require_lexical_evidence=True),
        ):
            resp = await retrieve(
                request, es_client=mock_es, milvus_collection=None, embedding_provider=None, reranker=None
            )

        assert resp.results == []

    @pytest.mark.asyncio
    async def test_degradation_to_empty_when_both_down(self) -> None:
        """降级矩阵: ES + Milvus 都不可用时返回空, 不抛异常"""
        request = RetrieveRequest(query="年费", top_k=3, search_type="hybrid", rerank=False)
        resp = await retrieve(request, es_client=None, milvus_collection=None, embedding_provider=None, reranker=None)
        assert resp.results == []
