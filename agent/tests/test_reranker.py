"""重排序服务单元测试 (reranker.py)"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from lumio.services.common.reranker import (
    OllamaReranker,
    TEIReranker,
    create_reranker_provider,
)
from lumio.shared.models import RerankResult

# ── OllamaReranker ──


def test_ollama_name():
    """name 含模型名"""
    r = OllamaReranker(base_url="http://localhost:11434/", model="bge")
    assert r.base_url == "http://localhost:11434"  # 去尾部斜杠
    assert r.name == "ollama:bge"


def test_ollama_rerank_empty():
    """空文档 → 空结果"""
    assert OllamaReranker().rerank("q", []) == []


@patch("lumio.services.common.reranker.httpx.post")
def test_ollama_rerank_scores_and_sorts(mock_post):
    """批量 /api/rerank 返回 results, 按分数降序取 top_k"""

    def side_effect(url, json=None, timeout=None):
        # 断言走原生 /api/rerank, 传 query+documents 批量
        assert url == "http://localhost:11434/api/rerank"
        assert json["query"] == "查询"
        assert json["documents"] == ["文档A", "文档B"]
        resp = MagicMock()
        resp.json.return_value = {
            "results": [
                {"index": 0, "relevance_score": 0.9, "document": "文档A"},
                {"index": 1, "relevance_score": 0.5, "document": "文档B"},
            ]
        }
        return resp

    mock_post.side_effect = side_effect
    r = OllamaReranker()
    results = r.rerank("查询", ["文档A", "文档B"], top_k=2)
    assert len(results) == 2
    assert results[0].index == 0  # 文档A 得分高排前
    assert results[0].relevance_score == 0.9
    assert results[0].text == "文档A"
    assert isinstance(results[0], RerankResult)


@patch("lumio.services.common.reranker.httpx.post")
def test_ollama_rerank_topk_truncates(mock_post):
    """top_k 截断 (results 无序也需按分排序后截断)"""
    mock_post.return_value = MagicMock(
        json=lambda: {
            "results": [
                {"index": 2, "relevance_score": 0.3, "document": "d2"},
                {"index": 0, "relevance_score": 0.9, "document": "d0"},
                {"index": 1, "relevance_score": 0.6, "document": "d1"},
            ]
        }
    )
    r = OllamaReranker()
    results = r.rerank("q", ["d0", "d1", "d2"], top_k=2)
    assert len(results) == 2
    assert [x.index for x in results] == [0, 1]


@patch("lumio.services.common.reranker.httpx.post")
def test_ollama_rerank_error_returns_empty(mock_post):
    """rerank 调用异常/模型无 rerank 能力 (404) → 返回空, 由检索端回退 RRF"""
    mock_post.side_effect = RuntimeError("network down")
    r = OllamaReranker()
    assert r.rerank("q", ["d1"]) == []


@patch("lumio.services.common.reranker.httpx.post")
def test_ollama_rerank_http_404_returns_empty(mock_post):
    """HTTP 非 2xx (无 rerank 能力的模型/旧版服务) → 空结果快速回落"""
    resp = MagicMock()
    resp.raise_for_status.side_effect = RuntimeError("404 page not found")
    mock_post.return_value = resp
    assert OllamaReranker().rerank("q", ["d1"]) == []


@patch("lumio.services.common.reranker.httpx.get")
def test_ollama_health_check_ok(mock_get):
    """健康检查: 200 → True"""
    mock_get.return_value = MagicMock(status_code=200)
    assert OllamaReranker().health_check() is True


@patch("lumio.services.common.reranker.httpx.get")
def test_ollama_health_check_fail(mock_get):
    """健康检查: 异常 → False"""
    mock_get.side_effect = RuntimeError("down")
    assert OllamaReranker().health_check() is False


# ── TEIReranker ──


def test_tei_name():
    """name 含模型名"""
    assert TEIReranker(model="bge").name == "tei:bge"


def test_tei_rerank_empty():
    """空文档 → 空结果"""
    assert TEIReranker().rerank("q", []) == []


@patch("lumio.services.common.reranker.httpx.post")
def test_tei_rerank_ok(mock_post):
    """TEI 返回排序结果（字段为 score）"""
    mock_post.return_value = MagicMock(
        json=lambda: [
            {"index": 1, "score": 0.9},
            {"index": 0, "score": 0.8},
        ]
    )
    r = TEIReranker()
    results = r.rerank("q", ["d0", "d1"])
    assert len(results) == 2
    assert results[0].index == 1
    assert results[0].relevance_score == 0.9
    assert results[0].text == "d1"  # 按 index 取原文


@patch("lumio.services.common.reranker.httpx.post")
def test_tei_rerank_bare_array(mock_post):
    """兼容裸数组列表形态 [[index, score, text], ...]"""
    mock_post.return_value = MagicMock(json=lambda: [[1, 0.85, "d1"], [0, 0.2, "d0"]])
    r = TEIReranker()
    results = r.rerank("q", ["d0", "d1"])
    assert len(results) == 2
    assert results[0].index == 1
    assert results[0].relevance_score == 0.85


@patch("lumio.services.common.reranker.httpx.post")
def test_tei_rerank_index_out_of_range(mock_post):
    """index 越界 → 空文本"""
    mock_post.return_value = MagicMock(json=lambda: [{"index": 5, "score": 0.9}])
    r = TEIReranker()
    results = r.rerank("q", ["d0"])
    assert results[0].text == ""


@patch("lumio.services.common.reranker.httpx.post")
def test_tei_rerank_error_returns_empty(mock_post):
    """请求异常 → 空结果"""
    mock_post.side_effect = RuntimeError("down")
    assert TEIReranker().rerank("q", ["d0"]) == []


@patch("lumio.services.common.reranker.httpx.get")
def test_tei_health_check(mock_get):
    """健康检查"""
    mock_get.return_value = MagicMock(status_code=200)
    assert TEIReranker().health_check() is True
    mock_get.side_effect = RuntimeError("down")
    assert TEIReranker().health_check() is False


# ── 工厂 ──


def test_create_reranker_provider_ollama():
    """工厂: ollama"""
    p = create_reranker_provider(provider_type="ollama")
    assert isinstance(p, OllamaReranker)


def test_create_reranker_provider_tei():
    """工厂: tei"""
    p = create_reranker_provider(provider_type="tei")
    assert isinstance(p, TEIReranker)


def test_create_reranker_provider_invalid():
    """工厂: 未知类型抛 ValueError"""
    with pytest.raises(ValueError, match="不支持"):
        create_reranker_provider(provider_type="weaviate")
