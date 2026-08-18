"""嵌入服务抽象层

支持 Ollama 和 TEI (Text Embeddings Inference) 两种嵌入后端，
通过 EmbeddingProvider Protocol 统一接口，EmbeddingCircuitBreaker 提供熔断保护。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import math
from typing import Protocol, runtime_checkable

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential

from lumio.shared.exceptions import EmbeddingServiceError, EmbeddingTimeoutError

logger = logging.getLogger(__name__)

BGE_QUERY_INSTRUCTION: str = "为这个句子生成表示以用于检索相关文章："

# mxbai-embed-large 上下文约 512 token；实测 480~640 中文字符之间即超限报 400。
# 超过该上限的输入在 provider 内切段均值池化，避免整条文本被 ollama 拒绝。
_EMBED_MAX_CHARS = 460


def _split_into_segments(text: str, max_chars: int = _EMBED_MAX_CHARS) -> list[str]:
    """将超长文本切成不超过 max_chars 的分段（优先在标点/空格处断开）"""
    if len(text) <= max_chars:
        return [text]
    segments: list[str] = []
    start = 0
    # 优先断点：标点、逗号、顿号、句点、空格、换行
    breaks = "。！？；，、， \n"
    while start < len(text):
        end = min(start + max_chars, len(text))
        if end < len(text):
            # 在分段末尾回退到最近的可断裂字符
            cut = end
            while cut > start and text[cut - 1] not in breaks:
                cut -= 1
            if cut - start < max_chars // 2:  # 回退过多则直接硬切
                cut = end
        else:
            cut = end
        segments.append(text[start:cut])
        start = cut
    return segments


def _mean_pool(vectors: list[list[float]]) -> list[float]:
    """对多条等长向量做均值池化（切段嵌入后合并）"""
    if not vectors:
        return []
    dim = len(vectors[0])
    pooled = [0.0] * dim
    for v in vectors:
        for d in range(dim):
            pooled[d] += v[d]
    n = len(vectors)
    return [c / n for c in pooled]


@runtime_checkable
class EmbeddingProvider(Protocol):
    """嵌入服务统一协议"""

    @property
    def dim(self) -> int:
        """嵌入向量维度"""
        ...

    @property
    def name(self) -> str:
        """模型名称"""
        ...

    @property
    def query_instruction(self) -> str:
        """查询嵌入指令前缀"""
        ...

    async def embed(self, texts: list[str], *, instruction: str = "") -> list[list[float]]:
        """将文本列表转换为嵌入向量列表

        Args:
            texts: 待嵌入文本列表
            instruction: 可选指令前缀，非空时添加到每个文本前面
        """
        ...

    async def embed_query(self, text: str) -> list[float]:
        """将单条查询文本转换为嵌入向量，自动添加查询指令"""
        ...

    async def health_check(self) -> bool:
        """检查嵌入服务是否可用"""
        ...


class OllamaEmbedding:
    """Ollama 嵌入服务实现

    通过 Ollama /api/embed 接口获取嵌入向量。
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        dim: int,
        timeout: float = 10.0,
        max_retries: int = 2,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._dim = dim
        self._timeout = timeout
        self._max_retries = max_retries

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def name(self) -> str:
        return self._model

    @property
    def query_instruction(self) -> str:
        return BGE_QUERY_INSTRUCTION

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=0.5, max=5),
        reraise=True,
    )
    async def embed(self, texts: list[str], *, instruction: str = "") -> list[list[float]]:
        """调用 Ollama /api/embed 获取嵌入向量

        超过模型上下文(实测 480~640 中文字符)的输入先切段分别嵌入再均值池化，
        避免 ollama 因单条输入超长直接返回 400。
        """
        if instruction:
            texts = [f"{instruction}{t}" for t in texts]

        # 建立 段 -> 原文本索引 的映射
        seg_plan: list[tuple[int, str]] = []
        for i, t in enumerate(texts):
            for s in _split_into_segments(t):
                seg_plan.append((i, s))

        seg_embeddings: list[list[float]] = []
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                # 分段批量请求，避免单次携带过多输入
                for k in range(0, len(seg_plan), 64):
                    batch = [s for _, s in seg_plan[k : k + 64]]
                    response = await client.post(
                        f"{self._base_url}/api/embed",
                        json={"model": self._model, "input": batch},
                    )
                    response.raise_for_status()
                    data = response.json()
                    batch_embeddings = data.get("embeddings", [])
                    if not batch_embeddings:
                        raise EmbeddingServiceError("嵌入服务返回空结果")
                    seg_embeddings.extend(batch_embeddings)
        except httpx.TimeoutException as exc:
            raise EmbeddingTimeoutError(str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            raise EmbeddingServiceError(str(exc)) from exc

        if len(seg_embeddings) != len(seg_plan):
            raise EmbeddingServiceError("段嵌入数量与输入不匹配")

        # 同一条输入的多段向量均值池化
        result: list[list[float]] = []
        for i in range(len(texts)):
            vecs = [v for (j, _), v in zip(seg_plan, seg_embeddings, strict=False) if j == i]
            result.append(_mean_pool(vecs))
        return result

    async def embed_query(self, text: str) -> list[float]:
        """嵌入查询文本，自动添加 BGE 查询指令"""
        results = await self.embed([text], instruction=self.query_instruction)
        return results[0]

    async def health_check(self) -> bool:
        """通过 GET /api/tags 检查 Ollama 服务可用性"""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(f"{self._base_url}/api/tags")
                return response.status_code == 200
        except Exception:
            return False


class FixedEmbedding:
    """确定性哈希嵌入 provider（开发/测试占位用，不依赖外部服务）

    向量维度与 Milvus schema 对齐（默认 1024），由文本哈希确定性生成。
    语义上不携带真实含义，仅用于在无嵌入模型/无网络环境下打通摄入、
    BM25 检索与 RAG 流程；真实模型（provider_type='ollama'）就绪后替换。
    """

    def __init__(self, dim: int = 1024, seed: int = 42) -> None:
        self._dim = dim
        self._seed = seed

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def name(self) -> str:
        return "fixed"

    @property
    def query_instruction(self) -> str:
        return ""

    async def embed(self, texts: list[str], *, instruction: str = "") -> list[list[float]]:
        import hashlib
        import random

        vectors: list[list[float]] = []
        for text in texts:
            payload = f"{instruction}{text}"
            digest = hashlib.sha256(payload.encode("utf-8")).digest()
            rng = random.Random(self._seed ^ int.from_bytes(digest[:8], "big", signed=False))
            vectors.append([rng.uniform(-1.0, 1.0) for _ in range(self._dim)])
        return vectors

    async def embed_query(self, text: str) -> list[float]:
        result = await self.embed([text])
        return result[0]

    async def health_check(self) -> bool:
        return True


class TEIEmbedding:
    """HuggingFace Text Embeddings Inference (TEI) 嵌入服务实现

    通过 TEI /embed 接口获取嵌入向量，支持批量处理。
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        dim: int,
        batch_size: int = 128,
        timeout: float = 10.0,
        max_retries: int = 2,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._dim = dim
        self._batch_size = batch_size
        self._timeout = timeout
        self._max_retries = max_retries

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def name(self) -> str:
        return self._model

    @property
    def query_instruction(self) -> str:
        return BGE_QUERY_INSTRUCTION

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(min=0.5, max=5),
        reraise=True,
    )
    async def embed(self, texts: list[str], *, instruction: str = "") -> list[list[float]]:
        """调用 TEI /embed 获取嵌入向量，按 batch_size 分批请求"""
        if instruction:
            texts = [f"{instruction}{t}" for t in texts]

        all_embeddings: list[list[float]] = []
        num_batches = math.ceil(len(texts) / self._batch_size)

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                for i in range(num_batches):
                    batch = texts[i * self._batch_size : (i + 1) * self._batch_size]
                    response = await client.post(
                        f"{self._base_url}/embed",
                        json={"inputs": batch},
                    )
                    response.raise_for_status()
                    batch_embeddings: list[list[float]] = response.json()
                    if not batch_embeddings:
                        raise EmbeddingServiceError("嵌入服务返回空结果")
                    all_embeddings.extend(batch_embeddings)
        except httpx.TimeoutException as exc:
            raise EmbeddingTimeoutError(str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            raise EmbeddingServiceError(str(exc)) from exc

        if not all_embeddings:
            raise EmbeddingServiceError("嵌入服务返回空结果")

        return all_embeddings

    async def embed_query(self, text: str) -> list[float]:
        """嵌入查询文本，自动添加 BGE 查询指令"""
        results = await self.embed([text], instruction=self.query_instruction)
        return results[0]

    async def health_check(self) -> bool:
        """通过 GET /health 检查 TEI 服务可用性"""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(f"{self._base_url}/health")
                return response.status_code == 200
        except Exception:
            return False


class EmbeddingCircuitBreaker:
    """嵌入服务熔断器

    周期性探测后端健康状态，连续失败达到阈值后打开熔断（标记不可用），
    连续成功达到阈值后关闭熔断（恢复可用）。
    """

    def __init__(
        self,
        provider: EmbeddingProvider,
        probe_interval: float = 30.0,
        failure_threshold: int = 3,
        recovery_threshold: int = 2,
    ) -> None:
        self._provider = provider
        self._probe_interval = probe_interval
        self._failure_threshold = failure_threshold
        self._recovery_threshold = recovery_threshold
        self._is_open = True  # 初始假定不可用，首次探测成功后关闭
        self._consecutive_failures = 0
        self._consecutive_successes = 0
        self._probe_task: asyncio.Task[None] | None = None

    @property
    def is_available(self) -> bool:
        """熔断器是否闭合（服务可用）"""
        return not self._is_open

    @property
    def provider(self) -> EmbeddingProvider:
        """底层嵌入提供者"""
        return self._provider

    async def start_probe(self) -> None:
        """启动周期性探测任务"""
        if self._probe_task is None or self._probe_task.done():
            self._probe_task = asyncio.create_task(self._probe_loop())

    async def stop_probe(self) -> None:
        """停止周期性探测任务"""
        if self._probe_task and not self._probe_task.done():
            self._probe_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._probe_task
            self._probe_task = None

    async def _probe_loop(self) -> None:
        """周期性执行健康探测"""
        while True:
            try:
                await self._probe_once()
            except Exception:
                logger.exception("嵌入服务探测异常")
            await asyncio.sleep(self._probe_interval)

    async def _probe_once(self) -> None:
        """执行一次健康探测并更新熔断状态"""
        healthy = await self._provider.health_check()

        if healthy:
            self._consecutive_successes += 1
            self._consecutive_failures = 0
        else:
            self._consecutive_failures += 1
            self._consecutive_successes = 0

        # 熔断打开条件：连续失败达到阈值
        if not self._is_open and self._consecutive_failures >= self._failure_threshold:
            self._is_open = True
            logger.warning("嵌入服务熔断器打开：连续 %d 次探测失败", self._consecutive_failures)

        # 熔断关闭条件：连续成功达到阈值
        if self._is_open and self._consecutive_successes >= self._recovery_threshold:
            self._is_open = False
            logger.info("嵌入服务熔断器关闭：连续 %d 次探测成功", self._consecutive_successes)


def create_embedding_provider(
    provider_type: str,
    ollama_base_url: str = "http://localhost:11434",
    ollama_model: str = "mxbai-embed-large",
    tei_base_url: str = "http://localhost:8080",
    tei_model: str = "BAAI/bge-M3",
    dim: int = 1024,
    batch_size: int = 128,
    timeout: float = 10.0,
    max_retries: int = 2,
) -> OllamaEmbedding | TEIEmbedding | FixedEmbedding:
    """嵌入服务工厂函数

    根据配置创建对应的嵌入服务实例。

    Args:
        provider_type: 提供者类型，"ollama" 或 "tei"
        ollama_base_url: Ollama 服务地址
        ollama_model: Ollama 模型名称
        tei_base_url: TEI 服务地址
        tei_model: TEI 模型名称
        dim: 嵌入向量维度
        batch_size: TEI 批量大小
        timeout: 请求超时（秒）
        max_retries: 最大重试次数
    """
    if provider_type == "ollama":
        return OllamaEmbedding(
            base_url=ollama_base_url,
            model=ollama_model,
            dim=dim,
            timeout=timeout,
            max_retries=max_retries,
        )
    if provider_type == "tei":
        return TEIEmbedding(
            base_url=tei_base_url,
            model=tei_model,
            dim=dim,
            batch_size=batch_size,
            timeout=timeout,
            max_retries=max_retries,
        )
    if provider_type == "fixed":
        # 确定性占位嵌入（开发/测试、无网络环境）
        return FixedEmbedding(dim=dim)
    raise EmbeddingServiceError(f"不支持的嵌入服务类型: {provider_type}")
