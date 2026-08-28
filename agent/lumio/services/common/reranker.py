"""重排序服务

提供基于 Ollama 和 TEI (Text Embeddings Inference) 的重排序能力，
用于对 RAG 检索结果进行二次排序以提升相关性。
"""

from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

import httpx

from lumio.shared.metrics import RERANK_DEGRADATION
from lumio.shared.models import RerankResult

logger = logging.getLogger(__name__)


# ── Protocol ──


@runtime_checkable
class RerankerProvider(Protocol):
    """重排序提供者协议"""

    @property
    def name(self) -> str:
        """提供者名称"""
        ...

    def rerank(self, query: str, documents: list[str], top_k: int = 5) -> list[RerankResult]:
        """对文档列表相对于查询进行重排序，返回 top_k 结果"""
        ...

    def health_check(self) -> bool:
        """检查服务是否可用"""
        ...


# ── Ollama 实现 ──


class OllamaReranker:
    """基于 Ollama 原生 /api/rerank 的重排序实现

    使用 Ollama 的 rerank 接口对整批文档一次性打分 (路由端批量),
    按得分降序排列后返回 top_k 结果。

    bge-reranker 一类 BERT 重排模型不支持生成, 旧实现误用 /api/generate
    逐文档让模型"吐出分数", 恒返回全 0 (退化回落 RRF), 却仍付出了并发生成调用
    的耗时与 GPU 争用。现改用 /api/rerank (Ollama>=0.5.4); 若该模型/服务无
    rerank 能力则尽快返回空列表, 由检索端回退 RRF, 不再空耗。
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "bge-reranker-v2-m3",
        timeout: float = 30.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    @property
    def name(self) -> str:
        return f"ollama:{self.model}"

    def rerank(self, query: str, documents: list[str], top_k: int = 5) -> list[RerankResult]:
        """调用 Ollama /api/rerank 一次性批量打分并排序."""
        if not documents:
            return []
        try:
            resp = httpx.post(
                f"{self.base_url}/api/rerank",
                json={
                    "model": self.model,
                    "query": query,
                    "documents": documents,
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception:
            # 模型/服务无 rerank 能力 (404) 或调用失败 → 返回空, 检索端回退 RRF.
            # 不再走逐文档 /api/generate (BERT 重排模型无法生成, 白耗 GPU).
            RERANK_DEGRADATION.labels(reason="unavailable").inc()
            logger.warning("Ollama rerank 不可用 (%s), 回退 RRF: %s", self.model, self.base_url)
            return []

        results = payload.get("results") or []
        ranked: list[RerankResult] = []
        for item in sorted(results, key=lambda r: r.get("relevance_score", 0.0), reverse=True):
            idx = int(item.get("index", -1))
            score = float(item.get("relevance_score", 0.0))
            text = item.get("document", "") or (documents[idx] if 0 <= idx < len(documents) else "")
            ranked.append(RerankResult(index=idx, relevance_score=score, text=text))
        return ranked[:top_k]

    def health_check(self) -> bool:
        """检查 Ollama 服务是否可用"""
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=self.timeout)
            return resp.status_code == 200
        except Exception:
            logger.exception("Ollama 健康检查失败")
            return False


# ── TEI 实现 ──


class TEIReranker:
    """基于 HuggingFace TEI /rerank 的重排序实现

    TEI 提供 /rerank 端点，直接返回排序后的结果及分数。
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8080",
        model: str = "BAAI/bge-reranker-v2-m3",
        timeout: float = 10.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout

    @property
    def name(self) -> str:
        return f"tei:{self.model}"

    def rerank(self, query: str, documents: list[str], top_k: int = 5) -> list[RerankResult]:
        """调用 TEI /rerank 端点进行重排序

        请求体包含 query、texts 和 top_n，响应为已排序的索引和分数列表。
        兼容 TEI / rerank_service 的两种响应形态：
        - 字典列表：[{"index": 0, "score": 0.9, "text": "..."}]（字段为 score）
        - 裸数组列表：[[index, score, text], ...]
        """
        if not documents:
            return []

        try:
            resp = httpx.post(
                f"{self.base_url}/rerank",
                json={"query": query, "texts": documents, "top_n": top_k},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data: list = resp.json()
            results: list[RerankResult] = []
            for item in data:
                if isinstance(item, dict):
                    index = item["index"]
                    score = item.get("score", item.get("relevance_score", 0.0))
                else:
                    index, score = item[0], item[1]
                text = documents[index] if 0 <= index < len(documents) else ""
                results.append(RerankResult(index=index, relevance_score=float(score), text=text))
            return results
        except Exception:
            RERANK_DEGRADATION.labels(reason="error").inc()
            logger.exception("TEI 重排序请求失败")
            return []

    def health_check(self) -> bool:
        """检查 TEI 服务是否可用"""
        try:
            resp = httpx.get(f"{self.base_url}/health", timeout=self.timeout)
            return resp.status_code == 200
        except Exception:
            logger.exception("TEI 健康检查失败")
            return False


# ── 工厂函数 ──


def create_reranker_provider(
    provider_type: str = "ollama",
    ollama_base_url: str = "http://localhost:11434",
    ollama_model: str = "bge-reranker-v2-m3",
    tei_base_url: str = "http://localhost:8080",
    tei_model: str = "BAAI/bge-reranker-v2-m3",
    timeout: float = 30.0,
) -> OllamaReranker | TEIReranker:
    """创建重排序提供者实例

    Args:
        provider_type: 提供者类型，"ollama" 或 "tei"
        ollama_base_url: Ollama 服务地址
        ollama_model: Ollama 模型名称
        tei_base_url: TEI 服务地址
        tei_model: TEI 模型名称
        timeout: 请求超时时间（秒）

    Returns:
        对应类型的重排序提供者实例

    Raises:
        ValueError: 不支持的 provider_type
    """
    if provider_type == "ollama":
        return OllamaReranker(base_url=ollama_base_url, model=ollama_model, timeout=timeout)
    if provider_type == "tei":
        return TEIReranker(base_url=tei_base_url, model=tei_model, timeout=timeout)
    raise ValueError(f"不支持的重排序提供者类型: {provider_type}")
