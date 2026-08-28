"""rerank_service.py (本地 cross-encoder 重排服务) 契约测试

通过 monkeypatch 假模型, 在不依赖 torch/sentence-transformers 的情况下验证 /
rerank 端点的排序、min-max 归一化、raw_scores 透传与 top_n 截断语义。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import httpx
import pytest

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "rerank_service.py"
_spec = importlib.util.spec_from_file_location("rerank_service", _SCRIPT)
assert _spec and _spec.loader
rerank_service = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rerank_service)


class _FakeModel:
    """按 (query, doc) 对返回可预测原始分的假 cross-encoder"""

    def predict(self, pairs):
        # score 与输入顺序一致: 后一篇更高 → 验证排序翻转
        return [float(len(p[1]) % 10) / 10 for p in pairs]


def _fake_load() -> tuple:
    return _FakeModel(), "BAAI/bge-reranker-v2-m3"


# ── _minmax ──


def test_minmax_normalizes():
    assert rerank_service._minmax([3.0, 1.0, 2.0]) == [1.0, 0.0, 0.5]


def test_minmax_all_equal():
    assert rerank_service._minmax([0.5, 0.5]) == [1.0, 1.0]


def test_minmax_single():
    assert rerank_service._minmax([0.7]) == [1.0]


# ── /rerank 端点 ──


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(rerank_service, "_load_model", _fake_load)
    transport = httpx.ASGITransport(app=rerank_service.app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_rerank_empty_texts(client):
    r = await client.post("/rerank", json={"query": "q", "texts": []})
    assert r.status_code == 200
    assert r.json() == []


async def test_rerank_orders_and_normalizes(client):
    r = await client.post("/rerank", json={"query": "q", "texts": ["aa", "bbbbbbbbb"]})  # 后文分数高, 应排第一
    assert r.status_code == 200
    data = r.json()
    assert data[0]["index"] == 1  # 高分排前
    assert data[1]["index"] == 0
    # min-max 归一化到 0~1, 最高者为 1.0
    assert all(0.0 <= item["score"] <= 1.0 for item in data)
    assert data[0]["score"] == 1.0
    assert data[0]["text"] == "bbbbbbbbb"


async def test_rerank_raw_scores_passthrough(client):
    r = await client.post("/rerank", json={"query": "q", "texts": ["aa", "bbbbbbbbb"], "raw_scores": True})
    data = r.json()
    # raw 模式直接返回原始分 (非 0~1 归一), 仍按分数降序
    assert data[0]["index"] == 1
    assert data[0]["score"] == 0.9


async def test_rerank_topn_truncates(client):
    r = await client.post("/rerank", json={"query": "q", "texts": ["a", "bb", "ccc", "dddd"], "top_n": 2})
    data = r.json()
    assert len(data) == 2
    # top_n 超出输入长度时钳制
    r2 = await client.post("/rerank", json={"query": "q", "texts": ["a"], "top_n": 10})
    assert len(r2.json()) == 1


async def test_rerank_response_shape(client):
    """回包形态: [{index, score, text}] 按 score 降序 (TEI 兼容契约)"""
    r = await client.post("/rerank", json={"query": "q", "texts": ["a", "bbbbbbbbbb"]})
    item = r.json()[0]
    assert set(item.keys()) == {"index", "score", "text"}
