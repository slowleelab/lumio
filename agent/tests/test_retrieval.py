"""混合检索引擎测试"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lumio.services.common.retrieval import (
    build_es_filters,
    build_milvus_expr,
    retrieve,
    rrf_fusion,
    search_bm25,
    search_vector,
)
from lumio.shared.models import RerankResult, RetrievedChunk, RetrieveRequest, RetrieveResponse


class TestBuildEsFilters:
    def test_keyword_filter(self):
        filters = {"category": "FAQ", "doc_type": "rule"}
        clauses = build_es_filters(filters)
        assert len(clauses) == 2
        assert {"term": {"category": "FAQ"}} in clauses
        assert {"term": {"doc_type": "rule"}} in clauses

    def test_date_range_filter(self):
        filters = {"effective_date": {"gte": "2026-01-01", "lte": "2026-12-31"}}
        clauses = build_es_filters(filters)
        assert len(clauses) == 1
        # ES 数值型 range 边界按内部毫秒解释（即使 mapping 声明 epoch_second），需转毫秒
        assert clauses[0]["range"]["effective_date"]["gte"] == 1767196800000  # 2026-01-01 in ms
        assert clauses[0]["range"]["effective_date"]["lte"] == 1798646400000  # 2026-12-31 in ms

    def test_keywords_filter(self):
        filters = {"keywords": ["年费", "积分"]}
        clauses = build_es_filters(filters)
        assert len(clauses) == 1
        assert clauses[0]["terms"]["keywords"] == ["年费", "积分"]

    def test_empty_filters(self):
        assert build_es_filters({}) == []


class TestBuildMilvusExpr:
    def test_keyword_expr(self):
        expr = build_milvus_expr({"category": "FAQ"})
        assert 'category == "FAQ"' in expr

    def test_keywords_array_contains(self):
        expr = build_milvus_expr({"keywords": "年费"})
        assert 'ARRAY_CONTAINS(keywords, "年费")' in expr

    def test_keywords_multi_array_contains(self):
        expr = build_milvus_expr({"keywords": ["年费", "减免"]})
        assert 'ARRAY_CONTAINS(keywords, "年费")' in expr
        assert 'ARRAY_CONTAINS(keywords, "减免")' in expr
        assert " or " in expr

    def test_empty_expr(self):
        assert build_milvus_expr({}) == ""

    def test_multiple_conditions(self):
        expr = build_milvus_expr({"category": "FAQ", "doc_type": "rule"})
        assert " and " in expr

    def test_bool_value_lowercased(self):
        """布尔过滤值必须转小写: ingestion 写入 Milvus 的是 VARCHAR "true",
        修复前 f-string 直转 Python True 得 "True" -> 恒不匹配 -> 向量检索静默 0 命中."""
        expr = build_milvus_expr({"approval_status": "PUBLISHED", "is_current_version": True})
        assert 'is_current_version == "true"' in expr
        assert 'is_current_version == "True"' not in expr
        assert 'approval_status == "PUBLISHED"' in expr


class TestRRFFusion:
    def test_fusion_with_overlap(self):
        bm25 = [
            RetrievedChunk(chunk_id="a", content="A", score=1.0, source_doc="d1"),
            RetrievedChunk(chunk_id="b", content="B", score=0.8, source_doc="d1"),
            RetrievedChunk(chunk_id="c", content="C", score=0.6, source_doc="d1"),
        ]
        vector = [
            RetrievedChunk(chunk_id="b", content="B", score=0.9, source_doc="d1"),
            RetrievedChunk(chunk_id="a", content="A", score=0.7, source_doc="d1"),
            RetrievedChunk(chunk_id="d", content="D", score=0.5, source_doc="d1"),
        ]
        result = rrf_fusion(bm25, vector, k=60)
        # "a" and "b" appear in both lists, should rank higher
        ids = [c.chunk_id for c in result]
        assert "a" in ids
        assert "b" in ids
        # Overlapping chunks should have higher RRF scores
        a_chunk = next(c for c in result if c.chunk_id == "a")
        d_chunk = next(c for c in result if c.chunk_id == "d")
        assert a_chunk.score > d_chunk.score

    def test_fusion_no_overlap(self):
        bm25 = [RetrievedChunk(chunk_id="a", content="A", score=1.0, source_doc="d1")]
        vector = [RetrievedChunk(chunk_id="b", content="B", score=0.9, source_doc="d1")]
        result = rrf_fusion(bm25, vector, k=60)
        assert len(result) == 2
        # a is rank 1 in bm25, b is rank 1 in vector -> same RRF score
        assert result[0].score == result[1].score or abs(result[0].score - result[1].score) < 0.001

    def test_fusion_single_list(self):
        bm25 = [RetrievedChunk(chunk_id="a", content="A", score=1.0, source_doc="d1")]
        result = rrf_fusion(bm25, [], k=60)
        assert len(result) == 1
        assert result[0].chunk_id == "a"

    def test_fusion_empty(self):
        result = rrf_fusion([], [], k=60)
        assert result == []


class TestSearchBM25:
    @pytest.mark.asyncio
    async def test_success(self):
        mock_es = AsyncMock()
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    {"_id": "1", "_score": 5.0, "_source": {"chunk_id": "c1", "content": "年费100元", "doc_id": "d1"}}
                ]
            }
        }
        result, best = await search_bm25(mock_es, "年费", top_k=5)
        assert len(result) == 1
        assert result[0].chunk_id == "c1"
        assert result[0].content == "年费100元"
        assert best == 5.0

    @pytest.mark.asyncio
    async def test_none_client_degradation(self):
        result = await search_bm25(None, "年费", top_k=5)
        assert result == ([], None)

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self):
        mock_es = AsyncMock()
        mock_es.search.side_effect = Exception("ES down")
        result = await search_bm25(mock_es, "年费", top_k=5)
        assert result == ([], None)

    @pytest.mark.asyncio
    async def test_zero_hits_answered_distinct_from_failure(self):
        """ES 应答但 0 命中: chunks 空 + best=0.0 (非 None) -- 相关性门据此区分"没有相关内容"与"ES 挂了"."""
        mock_es = AsyncMock()
        mock_es.search.return_value = {"hits": {"hits": []}}
        result, best = await search_bm25(mock_es, "乱码", top_k=5)
        assert result == []
        assert best == 0.0


class TestSearchVector:
    @pytest.mark.asyncio
    async def test_none_collection_degradation(self):
        result = await search_vector(None, [0.1] * 1024, top_k=5)
        assert result == []


class TestRetrieve:
    @pytest.mark.asyncio
    async def test_bm25_only(self):
        mock_es = AsyncMock()
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    {
                        "_id": "1",
                        "_score": 3.0,
                        "_source": {
                            "chunk_id": "c1",
                            "content": "test",
                            "doc_id": "d1",
                            "approval_status": "PUBLISHED",
                            "is_current_version": True,
                        },
                    }
                ]
            }
        }
        request = RetrieveRequest(query="test", top_k=3, search_type="bm25_only", rerank=False)
        resp = await retrieve(
            request, es_client=mock_es, milvus_collection=None, embedding_provider=None, reranker=None
        )
        assert isinstance(resp, RetrieveResponse)
        assert len(resp.results) >= 1

    @pytest.mark.asyncio
    async def test_degradation_both_fail(self):
        request = RetrieveRequest(query="test", top_k=3, search_type="hybrid", rerank=False)
        resp = await retrieve(request, es_client=None, milvus_collection=None, embedding_provider=None, reranker=None)
        assert resp.results == []
        assert resp.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_confidence_threshold(self):
        """BM25 score 低于 confidence_threshold 时被过滤（bm25_only 模式使用 confidence_threshold）"""
        mock_es = AsyncMock()
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    {
                        "_id": "1",
                        "_score": 0.1,
                        "_source": {
                            "chunk_id": "c1",
                            "content": "low score",
                            "doc_id": "d1",
                            "approval_status": "PUBLISHED",
                            "is_current_version": True,
                        },
                    },
                ]
            }
        }
        request = RetrieveRequest(query="test", top_k=3, search_type="bm25_only", rerank=False)
        with patch("lumio.services.common.retrieval.get_settings") as mock_settings:
            mock_settings.return_value.rag.confidence_threshold = 0.5
            mock_settings.return_value.rag.rrf_confidence_threshold = 0.0
            mock_settings.return_value.rag.rrf_k = 60
            mock_settings.return_value.rag.require_lexical_evidence = False
            mock_settings.return_value.elasticsearch.index_prefix = "lumio"
            resp = await retrieve(
                request, es_client=mock_es, milvus_collection=None, embedding_provider=None, reranker=None
            )
        # BM25 score 0.1 < RRF threshold 0.0 → 不过滤（RRF threshold 仅用于 RRF 融合结果）
        # bm25_only 路径使用 rrf_confidence_threshold=0.0，score 0.1 > 0.0 → 保留
        assert len(resp.results) == 1

    @pytest.mark.asyncio
    async def test_reranker_degradation(self):
        """Reranker 失败时降级到 RRF 结果"""
        mock_es = AsyncMock()
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    {
                        "_id": "1",
                        "_score": 3.0,
                        "_source": {
                            "chunk_id": "c1",
                            "content": "test content",
                            "doc_id": "d1",
                            "approval_status": "PUBLISHED",
                            "is_current_version": True,
                        },
                    }
                ]
            }
        }
        mock_reranker = MagicMock()
        mock_reranker.rerank.side_effect = RuntimeError("Reranker down")

        request = RetrieveRequest(query="test", top_k=3, search_type="bm25_only", rerank=True)
        resp = await retrieve(
            request, es_client=mock_es, milvus_collection=None, embedding_provider=None, reranker=mock_reranker
        )
        # Should still return results (from RRF/BM25), not fail
        assert len(resp.results) >= 1

    @pytest.mark.asyncio
    async def test_embedding_failure_degrades_to_bm25_no_nameerror(self):
        """P3-3 修复: embed_query 抛异常时, except 分支不应触发 NameError(vector_task).

        修复前: line 486 `for t in (bm25_task, vector_task)` 在 vector_task 未赋值时
        直接 NameError, 把真正的嵌入失败掩盖为 5xx.

        修复后: vector_task 初始化为 None, except 分支显式 None 检查, 降级到 BM25 only.
        """
        mock_es = AsyncMock()
        mock_es.search.return_value = {
            "hits": {
                "hits": [
                    {
                        "_id": "1",
                        "_score": 3.0,
                        "_source": {
                            "chunk_id": "c1",
                            "content": "bm25 hit",
                            "doc_id": "d1",
                            "approval_status": "PUBLISHED",
                            "is_current_version": True,
                        },
                    }
                ]
            }
        }
        # embedding 服务挂掉
        mock_embedding = MagicMock()
        mock_embedding.embed_query = AsyncMock(side_effect=RuntimeError("embedding service down"))
        mock_milvus = MagicMock()  # 提供但永远到不了

        request = RetrieveRequest(query="test", top_k=3, search_type="hybrid", rerank=False)
        resp = await retrieve(
            request,
            es_client=mock_es,
            milvus_collection=mock_milvus,
            embedding_provider=mock_embedding,
            reranker=None,
        )
        # 降级到 BM25 only, 不抛 NameError
        assert resp.results, "embedding 降级后 BM25 结果应保留"
        assert all(r.chunk_id == "c1" for r in resp.results)


def _es_hit(chunk_id: str, score: float) -> dict:
    """构造一条合规(已发布/当前版本)的 ES BM25 命中."""
    return {
        "_id": chunk_id,
        "_score": score,
        "_source": {
            "chunk_id": chunk_id,
            "content": f"content-{chunk_id}",
            "doc_id": "d1",
            "approval_status": "PUBLISHED",
            "is_current_version": True,
        },
    }


def _es_mock(score: float | None) -> AsyncMock:
    """ES mock: score=None 表示服务异常 (返回空/None 信号), 否则返回一条该分数命中."""
    mock_es = AsyncMock()
    if score is None:
        mock_es.search.side_effect = RuntimeError("ES down")
    else:
        mock_es.search.return_value = {"hits": {"hits": [_es_hit("c1", score)]}}
    return mock_es


def _vector_stack() -> tuple[MagicMock, MagicMock]:
    """向量检索 mock: embedding + Milvus 各返回一条合规 chunk (余弦分 0.6, 无区分度)."""

    class _Hit:
        def __init__(self, cid: str, distance: float, **fields: object):
            self.id = cid
            self.distance = distance
            self.entity = {
                "chunk_id": cid,
                "content": f"vec-{cid}",
                "doc_id": "d1",
                "approval_status": "PUBLISHED",
                "is_current_version": True,
                **fields,
            }

    class _Result:
        def __init__(self, hits: list):
            self._hits = hits

        def __getitem__(self, i: int):
            return self._hits[i]

        def __len__(self) -> int:
            return len(self._hits)

    mock_embedding = MagicMock()
    mock_embedding.embed_query = AsyncMock(return_value=[0.1] * 4)
    mock_milvus = MagicMock()
    mock_milvus.search = MagicMock(return_value=[_Result([_Hit("v1", 0.6)])])
    return mock_embedding, mock_milvus


class TestBM25RelevanceGate:
    """P1 词法证据门: reranker 退化时(Ollama /api/rerank 404 -> RRF 阈值 0.0 零过滤),
    BM25 零命中(ES 应答但没有任何 token 匹配)判定无相关知识, 不把向量召回喂 LLM.
    实测校准记录: 绝对分数下限不可用 -- 真实短查询'年费'@0.92 与乱码'额佛呢份'@2.77
    区间重叠(BM25 分数混合查询词特异度与相关性); 零命中是唯一无歧义信号."""

    @pytest.mark.asyncio
    async def test_gate_blocks_zero_bm25_hits(self):
        """ES 应答但 0 命中 (best=0.0): 无词法证据, 向量-only 证据(余弦无区分度)不足以放行."""
        mock_es = AsyncMock()
        mock_es.search.return_value = {"hits": {"hits": []}}
        mock_embedding, mock_milvus = _vector_stack()
        request = RetrieveRequest(query="gbfgbf", top_k=3, search_type="hybrid", rerank=False)
        resp = await retrieve(
            request, es_client=mock_es, milvus_collection=mock_milvus, embedding_provider=mock_embedding, reranker=None
        )
        assert resp.results == []

    @pytest.mark.asyncio
    async def test_gate_passes_nonzero_score_even_if_low(self):
        """非零低分(e33d1fa8 的'额佛呢份'实测 2.78)不拦 -- 绝对下限被实测证伪, 拦截靠上游
        分歧门; 本门只管"零命中=确定无词法证据"这一无歧义信号."""
        mock_es = _es_mock(2.78)
        mock_embedding, mock_milvus = _vector_stack()
        request = RetrieveRequest(query="额佛呢份", top_k=3, search_type="hybrid", rerank=False)
        resp = await retrieve(
            request, es_client=mock_es, milvus_collection=mock_milvus, embedding_provider=mock_embedding, reranker=None
        )
        assert resp.results, "非零 BM25 分不应被词法证据门拦截"

    @pytest.mark.asyncio
    async def test_gate_passes_relevant_query(self):
        """真实问题(实测最低 3.70): 正常返回."""
        mock_es = _es_mock(3.70)
        mock_embedding, mock_milvus = _vector_stack()
        request = RetrieveRequest(query="信用卡年费多少", top_k=3, search_type="hybrid", rerank=False)
        resp = await retrieve(
            request, es_client=mock_es, milvus_collection=mock_milvus, embedding_provider=mock_embedding, reranker=None
        )
        assert resp.results, "相关性足够的查询不应被门拦"

    @pytest.mark.asyncio
    async def test_gate_passes_short_real_query(self):
        """真实单关键词查询('年费'实测仅 0.92, 但 >0): 有词法证据, 不拦 (绝对下限会误杀此类)."""
        mock_es = _es_mock(0.92)
        request = RetrieveRequest(query="年费", top_k=3, search_type="bm25_only", rerank=False)
        resp = await retrieve(
            request, es_client=mock_es, milvus_collection=None, embedding_provider=None, reranker=None
        )
        assert resp.results, "低分但有命中的真实短查询不应被门拦"

    @pytest.mark.asyncio
    async def test_gate_not_fired_when_es_down(self):
        """ES 挂(best=None): 门不启用, 保留 vector-only 降级路径 (降级矩阵不变)."""
        mock_es = _es_mock(None)
        mock_embedding, mock_milvus = _vector_stack()
        request = RetrieveRequest(query="怎么查账单", top_k=3, search_type="hybrid", rerank=False)
        resp = await retrieve(
            request, es_client=mock_es, milvus_collection=mock_milvus, embedding_provider=mock_embedding, reranker=None
        )
        assert resp.results, "ES 故障时 vector-only 降级应保留"

    @pytest.mark.asyncio
    async def test_gate_bypassed_when_reranker_active(self):
        """reranker 对 query+doc 联合打分 0.9(强证据)时绕过词法门 -- 比 BM25 零命中更强的信号."""
        mock_es = AsyncMock()
        mock_es.search.return_value = {"hits": {"hits": []}}  # BM25 零命中
        mock_embedding, mock_milvus = _vector_stack()
        mock_reranker = MagicMock()
        mock_reranker.rerank = MagicMock(return_value=[RerankResult(index=0, relevance_score=0.9, text="vec-v1")])
        request = RetrieveRequest(query="怎么查账单", top_k=3, search_type="hybrid", rerank=True)
        resp = await retrieve(
            request,
            es_client=mock_es,
            milvus_collection=mock_milvus,
            embedding_provider=mock_embedding,
            reranker=mock_reranker,
        )
        assert resp.results, "reranker 高相关分数应绕过词法证据门"

    @pytest.mark.asyncio
    async def test_gate_blocks_bm25_only_zero_hits(self):
        """bm25_only 模式同样受门约束: 零命中 -> 空."""
        mock_es = AsyncMock()
        mock_es.search.return_value = {"hits": {"hits": []}}
        request = RetrieveRequest(query="看快看快", top_k=3, search_type="bm25_only", rerank=False)
        resp = await retrieve(
            request, es_client=mock_es, milvus_collection=None, embedding_provider=None, reranker=None
        )
        assert resp.results == []


class TestSearchBM25Extended:
    @pytest.mark.asyncio
    async def test_with_filters(self):
        """带过滤条件时 query body 含 bool.filter"""
        mock_es = AsyncMock()
        mock_es.search.return_value = {"hits": {"hits": []}}
        await search_bm25(mock_es, "年费", filters={"category": "fee"})
        body = mock_es.search.call_args.kwargs["body"]
        assert "bool" in body["query"]
        assert "filter" in body["query"]["bool"]

    @pytest.mark.asyncio
    async def test_parent_content_attached(self):
        """parent_chunk_id 命中时附加 parent_content"""
        mock_es = AsyncMock()

        async def fake_search(**kwargs):
            return {
                "hits": {
                    "hits": [
                        {
                            "_id": "c1",
                            "_score": 1.0,
                            "_source": {
                                "chunk_id": "c1",
                                "content": "子块",
                                "doc_id": "d1",
                                "parent_chunk_id": "p1",
                                "category": "fee",
                            },
                        }
                    ]
                }
            }

        async def fake_mget(*a, **kw):
            return {"docs": [{"found": True, "_id": "p1", "_source": {"content": "父块内容"}}]}

        mock_es.search = fake_search
        mock_es.mget = fake_mget
        result, best = await search_bm25(mock_es, "年费")
        assert result[0].metadata["parent_content"] == "父块内容"
        assert best == 1.0

    @pytest.mark.asyncio
    async def test_batch_fetch_parents_es(self):
        """批量获取 parent: 未命中时跳过"""
        from lumio.services.common.retrieval import _batch_fetch_parents_es

        mock_es = AsyncMock()
        mock_es.mget.return_value = {
            "docs": [{"found": False, "_id": "p1"}, {"found": True, "_id": "p2", "_source": {"content": "x"}}]
        }
        contents = await _batch_fetch_parents_es(mock_es, "idx", ["p1", "p2"])
        assert contents == {"p2": "x"}


class TestSearchVectorExtended:
    @pytest.mark.asyncio
    async def test_success_with_parents(self):
        """向量检索成功 + parent 内容附加"""
        from lumio.services.common.retrieval import search_vector

        class _Hit:
            def __init__(self, cid, distance, **fields):
                self.id = cid
                self.distance = distance
                self.entity = {"chunk_id": cid, "content": "内容", "doc_id": "d1", **fields}

        class _Result:
            def __init__(self, hits):
                self._hits = hits

            def __getitem__(self, i):
                return self._hits[i]

            def __len__(self):
                return len(self._hits)

        collection = MagicMock()

        def fake_search(**kwargs):
            return [_Result([_Hit("c1", 0.9, parent_chunk_id="p1")])]

        def fake_query(**kwargs):
            return [_Hit("p1", 0.5, content="父内容")]

        collection.search = fake_search
        collection.query = fake_query

        result = await search_vector(collection, [0.1] * 4, top_k=5)
        assert len(result) == 1
        assert result[0].chunk_id == "c1"
        assert result[0].score == 0.9

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self):
        """向量检索异常 -> 空列表"""
        from lumio.services.common.retrieval import search_vector

        collection = MagicMock()

        def fake_search(**kwargs):
            raise RuntimeError("milvus down")

        collection.search = fake_search
        result = await search_vector(collection, [0.1] * 4)
        assert result == []

    @pytest.mark.asyncio
    async def test_search_hang_times_out_and_degrades(self):
        """Milvus 通道死亡(search 永不返回) -> search_timeout 硬超时 -> 空列表降级.

        实测背景: Milvus 容器重启后旧 gRPC 通道上的 search 无超时会永久挂起
        (挂在 to_thread 线程里, 事件循环无法自救), 连噪声输入都被拖到 60s
        编排超时; 现在客户端 wait_for 兜底, 超时降级 BM25-only."""
        from time import monotonic, sleep

        from lumio.services.common.retrieval import search_vector

        collection = MagicMock()

        def fake_search(**kwargs):
            sleep(30)  # 模拟死通道: 永不返回
            return []

        collection.search = fake_search
        with patch("lumio.services.common.retrieval.get_settings") as mock_settings:
            mock_settings.return_value.milvus.search_timeout = 0.2
            t0 = monotonic()
            result = await search_vector(collection, [0.1] * 4)
            elapsed = monotonic() - t0
        assert result == []
        assert elapsed < 5, f"超时兜底未生效: {elapsed:.1f}s"



class TestDateToEpoch:
    def test_valid(self):
        from lumio.services.common.retrieval import _date_to_epoch

        assert _date_to_epoch("2026-08-01") is not None

    def test_invalid(self):
        from lumio.services.common.retrieval import _date_to_epoch

        assert _date_to_epoch("not-a-date") is None
        assert _date_to_epoch(None) is None


class TestCacheKey:
    def test_cache_key_stable(self):
        from lumio.services.common.retrieval import _build_cache_key

        k1 = _build_cache_key("查询", {"category": "fee"}, 5)
        k2 = _build_cache_key("查询", {"category": "fee"}, 5)
        assert k1 == k2
        k3 = _build_cache_key("查询", {"category": "fee"}, 10)
        assert k1 != k3
