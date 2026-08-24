"""统一多信号噪声闸 (P2) — 惊讶度 + 上下文意图 + energy 认知 + 状态的多信号投票。

仿 input_guard 对外形态: `evaluate(...) -> GateVerdict`。设计要点:

- **惊讶度只是一个信号, 不是独立裁决**: 只有惊讶度"异常" 且被另一个"不认"信号
  (置信过低 / energy-unknown / 内容噪声形状) 佐证时才硬拦; 单独惊讶度异常不拦。
- **回话/实体/槽位是硬否决 (veto)**: 本句在回上文话、或抽到填缺槽的实体 → 放行,
  即便惊讶度再高, 防"上轮问卡号, 本句答 4444"被误杀。2 计划里"带语义数字/回话
  由槽位与实体兜住"正是这一层。
- **保守优先**: 容错地"宁可放过" — 组件自身任何异常 → 统一放行并记 WARN, 不让闸门
  故障断业务(横切关注点"容错")。
- **默认关 / 观测**: 惊讶度种子模型随服携带但需 P3 数据回流标定, 故细粒度阈值走配置
  (默认关), 上线先"记不改判"。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from lumio.services.common.surprisal import SurprisalScorer, default_surprisal_scorer

logger = logging.getLogger(__name__)


@dataclass
class GateVerdict:
    """一次多信号闸判定. decision 仅 "block" | "pass" (无 review, 保持结论收敛).

    decision="block" 时 reason 为拦截依据; "pass" 时 signals 携带各信号供审计。
    """

    decision: str  # "block" | "pass"
    reason: str | None = None
    signals: dict[str, Any] = field(default_factory=dict)


class InputGate:
    """多信号噪声闸: 惊讶度(中/英) + energy 认知 + 置信 + 内容噪声, 回话/实体硬否决."""

    def __init__(self, scorer: SurprisalScorer | None = None) -> None:
        self._scorer = scorer or default_surprisal_scorer()

    def evaluate(
        self,
        *,
        text: str,
        is_replying: bool = False,
        has_entity_slot: bool = False,
        low_conf: bool = False,
        noise_shape: bool = False,
        energy_verdict: str | None = None,
    ) -> GateVerdict:
        """多信号投票.

        Args:
            text: 用户原文
            is_replying: 本句在回上文缺的必填槽/确认词 (P0 判定结果)
            has_entity_slot: 抽到实体命中缺的必填槽
            low_conf: 意图置信低于下限(分类器"不认")
            noise_shape: 内容级噪声(纯数字/零元音拉丁乱码)
            energy_verdict: P1 energy-OOD 三态 (known/ambiguous/unknown/None)
        """
        # 组件自身异常 → 放行 + WARN (容错, 不阻断)
        try:
            surprisal = self._scorer.evaluate(text)
        except Exception:
            logger.warning("InputGate 惊讶度信号异常, 放行: %r", (text or "")[:40], exc_info=True)
            return GateVerdict(decision="pass", signals={"surprisal": "error"})

        surprisal_abnormal = surprisal.abnormal
        # 佐证信号: 任一"不认/吃不准"都算, 让惊讶度异常能与 energy 模糊带等协同判拦
        corroborate = low_conf or noise_shape or energy_verdict in ("unknown", "ambiguous")
        signals = {
            "surprisal_abnormal": surprisal_abnormal,
            "surprisal_segments": [s.__dict__ for s in surprisal.segments],
            "low_conf": low_conf,
            "noise_shape": noise_shape,
            "energy_verdict": energy_verdict,
            "corroborate": corroborate,
        }

        # 硬否决: 真实回话/实体填槽 → 永不因惊讶度拦截
        if is_replying or has_entity_slot:
            signals["veto"] = "reply_or_slot"
            return GateVerdict(decision="pass", signals=signals)

        # 硬拦: 惊讶度异常 且 被"不认"信号佐证 (多信号投票, 避免单独惊讶度误杀)
        if surprisal_abnormal and corroborate:
            signals["block_reason"] = "surprisal_corroborated"
            return GateVerdict(decision="block", reason="surprisal_corroborated", signals=signals)

        return GateVerdict(decision="pass", signals=signals)
