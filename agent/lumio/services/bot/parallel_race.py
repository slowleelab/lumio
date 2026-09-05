"""低置信并行竞速（目标架构 ⑤D）

置信度落在低置信带宽 (0.4~0.6) 时, FAQ 检索与 RAG 检索并行竞速,
归并取高分链路 —— 对冲意图分类的路由偏差, 替代"低置信直接拦回澄清"的
单一处置 (会话 9d64b59: 正经业务问题被噪声门误杀)。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class RaceOutcome:
    """竞速归并结果

    winner: "faq" (FAQ 命中直出) / "rag" (RAG 有据, 带上下文走知识生成) / "none" (双路皆空 → 澄清)
    """

    winner: str
    faq_answer: str | None = None
    faq_match_type: str | None = None
    rag_context: str = ""
    faq_error: bool = False
    rag_error: bool = False


async def race(
    faq_fn: Any,
    rag_fn: Any,
    *,
    faq_timeout: float = 5.0,
    rag_timeout: float = 10.0,
) -> RaceOutcome:
    """并行执行 FAQ 与 RAG 检索并归并

    Args:
        faq_fn: 返回 {"match_type": exact|semantic|miss, "results": [...]} 的协程
        rag_fn: 返回检索上下文字符串（空串=未命中）的协程
    """
    faq_res: dict | None = None
    rag_ctx: str = ""
    faq_err = rag_err = False

    async def _faq() -> None:
        nonlocal faq_res, faq_err
        try:
            faq_res = await asyncio.wait_for(faq_fn(), timeout=faq_timeout)
        except Exception as exc:
            logger.warning("竞速 FAQ 路失败(不阻断): %s", exc)
            faq_err = True

    async def _rag() -> None:
        nonlocal rag_ctx, rag_err
        try:
            rag_ctx = await asyncio.wait_for(rag_fn(), timeout=rag_timeout) or ""
        except Exception as exc:
            logger.warning("竞速 RAG 路失败(不阻断): %s", exc)
            rag_err = True

    await asyncio.gather(_faq(), _rag())

    # 归并: FAQ 命中 (标准答案, 零生成成本) 优先; 否则 RAG 有据走知识生成
    if faq_res and faq_res.get("match_type") in ("exact", "semantic") and faq_res.get("results"):
        top = faq_res["results"][0]
        answer = top.get("answer") or top.get("content")
        if answer:
            return RaceOutcome(winner="faq", faq_answer=answer, faq_match_type=faq_res["match_type"])
    if rag_ctx:
        return RaceOutcome(winner="rag", rag_context=rag_ctx, faq_error=faq_err)
    return RaceOutcome(winner="none", faq_error=faq_err, rag_error=rag_err)
