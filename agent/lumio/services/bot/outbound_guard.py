"""出站合规闸门（目标架构 ⑦）

统一出站检查：话术合规（敏感词）+ 幻觉检测 v1（无依据的具体数字断言）。
位于回复生成之后、返回用户之前；拦截时替换为澄清话术并写审计决策。

幻觉检测 v1 口径（保守，宁放勿拦）：
- 仅当本轮有 grounding 源（RAG 上下文/工具结果）时才启用数字比对；
- 回复中的多位数字 token 全部不在 grounding 源中出现 → 判为无依据断言；
- 无 grounding 源时只拦"编造办理结果"类话术（已为您办理/已成功执行）。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# 编造办理结果类话术 (无工具执行证据时出现即拦截)
_FABRICATED_EXECUTION_PATTERNS = ("已为您办理", "已成功办理", "已完成办理", "已为您完成", "已成功为您")

_DIGIT_TOKEN_RE = re.compile(r"\d{2,}")

# 常见无害数字 (热线/年份/通用时限), 不参与比对
_DIGIT_ALLOWLIST = {"4008895558", "12356", "4001619995", "2025", "2026"}


@dataclass
class OutboundVerdict:
    """出站检查结论

    passed=True → 原样放行; passed=False → reply 已替换为安全话术。
    """

    passed: bool
    reply: str
    reason: str = ""


class OutboundGuard:
    """出站闸门

    Args:
        safety_filter: 复用入站侧 SafetyFilter 的敏感词集 (check_input 语义对出站同样适用)
        clarify_response: 拦截后的替换话术
    """

    def __init__(self, safety_filter: Any, clarify_response: str) -> None:
        self._safety = safety_filter
        self._clarify = clarify_response

    def check(self, reply: str, grounding_source: str = "", tool_executed: bool = False) -> OutboundVerdict:
        """出站检查

        Args:
            reply: 待出站回复
            grounding_source: 本轮 grounding 源（RAG 检索上下文或工具结果原文）
            tool_executed: 本轮是否真实执行过工具（编造办理话术的豁免依据）
        """
        if not reply:
            return OutboundVerdict(passed=True, reply=reply)

        # 1. 话术合规: 敏感词命中 → 拦截 (check_input 返回 (is_safe, hit_words))
        is_safe, hit_words = self._safety.check_input(reply)
        if not is_safe:
            logger.warning("出站拦截-敏感词: %s", list(hit_words)[:5])
            return OutboundVerdict(passed=False, reply=self._clarify, reason="sensitive_words")

        # 2. 编造办理结果: 声称已办理但本轮无工具执行 → 拦截
        if not tool_executed and any(p in reply for p in _FABRICATED_EXECUTION_PATTERNS):
            logger.warning("出站拦截-编造办理话术(无工具执行): %r", reply[:60])
            return OutboundVerdict(passed=False, reply=self._clarify, reason="fabricated_execution")

        # 3. 幻觉数字 v1: 有 grounding 源时, 回复中的数字 token 应能在源中找到
        if grounding_source:
            tokens = [t for t in _DIGIT_TOKEN_RE.findall(reply) if t not in _DIGIT_ALLOWLIST]
            if tokens and not any(t in grounding_source for t in tokens):
                logger.warning("出站拦截-无依据数字断言: %s (grounding %d 字)", tokens[:4], len(grounding_source))
                return OutboundVerdict(passed=False, reply=self._clarify, reason="ungrounded_numbers")

        return OutboundVerdict(passed=True, reply=reply)
