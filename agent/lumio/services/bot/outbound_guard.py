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

# 索敏话术 (qa_scan 首轮复盘: knowledge 回落生成仍在索要"卡号后四位以验证身份",
# 违反 KNOWLEDGE prompt 第 6 条纪律 — LLM 不守纪律时由闸门兜底)。身份核验流程
# (verification 弹框) 走独立通道不经本闸门, 此处拦的是自由文本里的索敏。
# 只匹配"要求客户提供"的方向性话术 (提供/告知/请输入+敏感凭证), 不匹配知识文档
# 引用的流程描述 (如"在手机银行APP中输入卡号后四位"是描述银行流程, 不是索要)。
_SENSITIVE_SOLICITATION_RE = re.compile(
    r"(提供|告知|发送|报一下|说一下|告诉我)[^。！？!?]{0,12}(您的?|你)?(卡号|后四位|后4位|末四位|完整卡号|密码|验证码)"
    r"|请[^。！？!?]{0,12}(提供|告知|输入|发送)[^。！？!?]{0,12}(卡号|后四位|后4位|末四位|密码|验证码)"
)

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

    def __init__(self, safety_filter: Any, clarify_response: str, emergency_reply: str = "") -> None:
        self._safety = safety_filter
        self._clarify = clarify_response
        # 紧急场景 (挂失/盗刷) 索敏拦截后的替换话术 — 通用澄清 ("您的意思我还没
        # 太理解") 对紧急挂失客户是二次伤害, 换挂失引导 + 转人工邀约
        self._emergency_reply = emergency_reply

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

        # 2b. 索敏话术: 自由文本索要卡号/密码/验证码 (LLM 违反 prompt 纪律的兜底)。
        # 剥离策略: 索敏句往往出现在合规引导之后的尾部 — 按句切分剔除命中句,
        # 剩余合规部分 (挂失渠道/转人工邀约) 保留放行; 全被剔/剩太短才整体替换。
        if _SENSITIVE_SOLICITATION_RE.search(reply):
            kept = [seg for seg in re.split(r"(?<=[。！？!?\n])", reply) if not _SENSITIVE_SOLICITATION_RE.search(seg)]
            stripped = "".join(kept).strip()
            if len(stripped) >= 20:
                logger.warning("出站拦截-索敏话术(剥离索敏句保留合规部分): %r → %r", reply[:60], stripped[:60])
                return OutboundVerdict(passed=False, reply=stripped, reason="sensitive_solicitation_stripped")
            replacement = self._emergency_reply or self._clarify
            logger.warning("出站拦截-索敏话术(整体替换): %r → %r", reply[:60], replacement[:60])
            return OutboundVerdict(passed=False, reply=replacement, reason="sensitive_solicitation")

        # 3. 幻觉数字 v1: 有 grounding 源时, 回复中的数字 token 应能在源中找到
        if grounding_source:
            tokens = [t for t in _DIGIT_TOKEN_RE.findall(reply) if t not in _DIGIT_ALLOWLIST]
            if tokens and not any(t in grounding_source for t in tokens):
                logger.warning("出站拦截-无依据数字断言: %s (grounding %d 字)", tokens[:4], len(grounding_source))
                return OutboundVerdict(passed=False, reply=self._clarify, reason="ungrounded_numbers")

        return OutboundVerdict(passed=True, reply=reply)
