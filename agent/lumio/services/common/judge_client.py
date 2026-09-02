"""跨家族远程裁判客户端 (Anthropic Messages 协议)

闭环方案 §9.3: 归因裁判应与生成模型跨家族, 避免"学生给自己批卷"的自我偏好
偏差。本地单卡只有 qwen 一个生成模型, 裁判走远程 GLM coding plan
(open.bigmodel.cn/api/anthropic, Anthropic 协议) 即可实现跨家族。

接口与 BadcaseJudge 的调用约定对齐: chat(messages) -> str / chat_json(messages) -> dict。
- 响应 content 是块数组 (GLM 思考模型带 thinking 块), 只拼接 type=text 的块。
- 远程调用失败 (网络/限流/鉴权) 自动回退到本地 LLMClient, 闭环不断粮。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from lumio.shared.config import get_settings
from lumio.shared.logger import get_logger

logger = get_logger(__name__)


class RemoteJudgeClient:
    """Anthropic Messages 协议裁判客户端 (本地 LLMClient 兜底)"""

    def __init__(self, fallback_llm: Any = None) -> None:
        settings = get_settings().llm
        self._base = settings.judge_base_url.rstrip("/")
        self._key = settings.judge_api_key
        self._model = settings.judge_model
        self._timeout = settings.judge_timeout
        self._fallback = fallback_llm  # 本地 LLMClient (OpenAI 兼容), 失败兜底
        self._degraded = False  # 连续失败后进入降级, 周期性重试远程
        self._used_local = False  # 本轮归因是否发生过本地回退 (审计标记)
        self._strict = settings.judge_strict  # 严格模式: 失败不回退, 全程纯远程
        self._retry_delays: tuple[float, ...] = (0.0, 2.0, 4.0)  # 首次 + 2 次退避重试

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def effective_model(self) -> str:
        """审计用: 实际参与判定的模型 (发生过本地回退则如实标记)"""
        return f"{self._model}+本地回退" if self._used_local else self._model

    async def chat(self, messages: list[dict[str, str]], *, timeout: float | None = None, **_: Any) -> str:
        if not (self._base and self._key):
            return await self._local(messages, timeout)
        if self._degraded and not self._strict:
            return await self._local(messages, timeout)
        try:
            # 瞬时限流/网络抖动先重试 (2s/4s 退避), 仍失败按模式处置
            return await self._remote_with_retry(messages, timeout or self._timeout)
        except Exception as exc:
            if self._strict:
                logger.warning("远程裁判失败 (严格模式, 不回退): %s", exc)
                raise
            logger.warning("远程裁判调用失败, 本轮回退本地 judge: %s", exc)
            self._degraded = True
            _schedule_remote_recovery(self)
            return await self._local(messages, timeout)

    async def _remote_with_retry(self, messages: list[dict[str, str]], timeout: float) -> str:
        import asyncio

        last: Exception | None = None
        for delay in self._retry_delays:
            if delay:
                await asyncio.sleep(delay)
            try:
                return await self._remote(messages, timeout)
            except Exception as exc:
                last = exc
        raise last or RuntimeError("远程裁判重试耗尽")

    async def chat_json(self, messages: list[dict[str, str]], *, timeout: float | None = None, **_: Any) -> dict:
        text = await self.chat(messages, timeout=timeout)
        parsed = _parse_json(text)
        if parsed is None:
            raise ValueError(f"裁判输出非 JSON: {text[:80]}")
        return parsed

    # ── 内部 ──

    async def _remote(self, messages: list[dict[str, str]], timeout: float) -> str:
        headers = {"x-api-key": self._key, "anthropic-version": "2023-06-01"}
        payload = {
            "model": self._model,
            "max_tokens": 1024,
            # 裁判是判定锚点驱动的结构化任务, 关闭 thinking: 实测 12.3s→3.3s (3.7x)
            # 且证据更聚焦 (深度推理对锚点判定无增益)
            "thinking": {"type": "disabled"},
            "messages": [m for m in messages if m.get("role") != "system"],
        }
        system = "\n".join(m["content"] for m in messages if m.get("role") == "system")
        if system:
            payload["system"] = system
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
            resp = await client.post(f"{self._base}/v1/messages", headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        blocks = data.get("content") or []
        text = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        if not text.strip():
            raise ValueError(f"远程裁判空回复 (blocks={len(blocks)}, stop={data.get('stop_reason')})")
        return text

    async def _local(self, messages: list[dict[str, str]], timeout: float | None) -> str:
        if self._fallback is None:
            raise RuntimeError("远程裁判失败且无本地兜底")
        self._used_local = True
        return await self._fallback.chat(messages, timeout=timeout)


def _parse_json(raw: str) -> dict | None:
    """容错解析: 剥 ```json 围栏 / 截取首尾大括号"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        parsed = json.loads(text[start:end])
        return parsed if isinstance(parsed, dict) else None
    except (ValueError, json.JSONDecodeError):
        return None


def _schedule_remote_recovery(client: RemoteJudgeClient, *, seconds: float = 60.0) -> None:
    """降级后 1 分钟自动恢复尝试远程 (限流多为瞬时, 5 分钟过于保守)"""
    import asyncio

    async def _recover() -> None:
        await asyncio.sleep(seconds)
        client._degraded = False
        logger.info("远程裁判降级恢复: 下一轮归因重新尝试远程模型")

    try:
        task = asyncio.get_running_loop().create_task(_recover())
        _RECOVERY_TASKS.add(task)
        task.add_done_callback(_RECOVERY_TASKS.discard)
    except RuntimeError:
        client._degraded = False


_RECOVERY_TASKS: set = set()
