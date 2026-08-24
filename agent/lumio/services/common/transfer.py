"""转人工判断逻辑

三级触发机制（优先级 L1 > L2 > L3）：
- L1 关键词触发：用户输入命中敏感词/转人工词
- L2 语义触发：情感负面且置信度高，或投诉意图
- L3 累计触发：连续低置信度轮次或多次兜底命中
"""

from __future__ import annotations

import logging
from pathlib import Path

from lumio.shared.config import get_settings
from lumio.shared.models import (
    SENSITIVE_INTENTS,
    TRANSFER_INTENTS,
    IntentResult,
    SentimentLabel,
    SessionState,
    TransferTriggerLevel,
)

logger = logging.getLogger(__name__)

# 语义级(L2)认定"转人工/敏感写"所需的最低置信。
# 乱码/连续低置信被判成 transfer_agent 或幻觉出敏感候补时把握通常远低于 0.5(已线上复现),
# 据此拉真人既误伤事实又给"灌乱码刷人工坐席"留口子。真·明确转人工靠 L1 关键词或高置信主意图。
# 此常量同时被 bot_agent 的 business 路径引用, 作为"低置信不即时派真人"的统一阈值。
TRANSFER_CONFIDENT_CONF = 0.5


# 默认转人工关键词
_DEFAULT_TRANSFER_KEYWORDS = [
    "人工",
    "转人工",
    "人工客服",
    "真人",
    "找经理",
    "投诉",
    "不满意",
    "我要投诉",
]


class TransferChecker:
    """转人工判断器

    三级触发，优先级 L1 > L2 > L3，命中高级别直接触发。
    """

    def __init__(
        self,
        transfer_keywords: list[str] | None = None,
        sensitive_keywords: list[str] | None = None,
    ) -> None:
        settings = get_settings()

        # 加载转人工关键词
        if transfer_keywords:
            self._transfer_keywords = set(transfer_keywords)
        else:
            self._transfer_keywords = set(_DEFAULT_TRANSFER_KEYWORDS)
        # 从配置文件加载
        # P3-8 整改: 改用相对源文件路径, 而非 cwd 相对 (生产 systemd/Docker 用任意 cwd 静默 fail)
        # __file__ = agent/lumio/services/common/transfer.py → parents[3] = agent/
        config_path = Path(__file__).resolve().parents[3] / "config" / "transfer_keywords.txt"
        if config_path.exists():
            file_keywords = _load_keywords_from_file(config_path)
            self._transfer_keywords.update(file_keywords)

        # 加载敏感词
        if sensitive_keywords:
            self._sensitive_keywords = set(sensitive_keywords)
        else:
            self._sensitive_keywords = set()
            sensitive_path = Path(settings.safety.sensitive_words_path)
            if sensitive_path.exists():
                self._sensitive_keywords = set(_load_keywords_from_file(sensitive_path))

        # L3 参数
        self._low_confidence_threshold = settings.session.low_confidence_threshold

    def check(
        self,
        text: str,
        intent: IntentResult,
        sentiment: SentimentLabel = SentimentLabel.NEUTRAL,
        session: SessionState | None = None,
    ) -> tuple[bool, TransferTriggerLevel | None, str]:
        """检查是否应触发转人工

        Args:
            text: 用户输入文本
            intent: 本轮意图分类结果
            sentiment: 本轮情感标签
            session: 当前会话状态（用于 L3 累计判断）

        Returns:
            (是否转人工, 触发级别, 原因描述)
        """
        # L1: 关键词触发（最高优先级）
        triggered, reason = self._check_l1(text)
        if triggered:
            return True, TransferTriggerLevel.L1, reason

        # L2: 语义触发
        triggered, reason = self._check_l2(text, intent, sentiment)
        if triggered:
            return True, TransferTriggerLevel.L2, reason

        # L3: 累计触发
        if session:
            triggered, reason = self._check_l3(session, intent)
            if triggered:
                return True, TransferTriggerLevel.L3, reason

        return False, None, ""

    def _check_l1(self, text: str) -> tuple[bool, str]:
        """L1 关键词触发

        检查用户输入是否命中转人工关键词或敏感词。
        """
        for keyword in self._transfer_keywords:
            if keyword in text:
                return True, f"L1_KEYWORD_HIT: 命中转人工关键词「{keyword}」"

        for keyword in self._sensitive_keywords:
            if keyword in text:
                return True, f"L1_SENSITIVE_HIT: 命中敏感词「{keyword}」"

        return False, ""

    def _check_l2(
        self,
        text: str,
        intent: IntentResult,
        sentiment: SentimentLabel,
    ) -> tuple[bool, str]:
        """L2 语义触发

        条件（已加置信度门槛，堵低置信/幻觉候补拉真人的口子）：
        - 主意图为 transfer_agent **且置信 ≥ TRANSFER_CONFIDENT_CONF** → 客户主动要求人工
          (低置信的 transfer_agent 多为乱码/连续低置信误判，靠 L1 关键词或下一轮高置信即可转)
        - 主意图为敏感写(挂失/投诉) → **不受置信门槛**，合规底线即时转
        - 敏感写仅出现在候补意图时，需整体置信 ≥ TRANSFER_CONFIDENT_CONF 才立即转
          (如"卡丢了顺便查最后一笔"高置信多意图；乱码幻觉出的敏感候补低置信则不得拉真人)
        - 情感 negative/angry 且置信 > 0.8
        """
        conf = intent.primary_confidence
        if intent.primary_intent in TRANSFER_INTENTS and conf >= TRANSFER_CONFIDENT_CONF:
            return True, "L2_INTENT_TRANSFER: 用户主动要求转人工"

        if intent.primary_intent in SENSITIVE_INTENTS:
            return True, f"L2_INTENT_{intent.primary_intent.name}: 敏感写意图={intent.primary_intent.value}"

        # 次要意图里也含敏感写的场景 (如"卡丢了,顺便查最后一笔消费"挂失主+交易次),
        # 集合判断避免拆细后精确比较漏判敏感意图。
        # 仅当整体置信不低时才据此即时转 —— 乱码被幻觉出 complaint 候补(已复现)必须被拦下。
        alt_labels = set(intent.alternatives or [])
        if alt_labels & SENSITIVE_INTENTS and conf >= TRANSFER_CONFIDENT_CONF:
            sensitive = next(iter(alt_labels & SENSITIVE_INTENTS))
            return True, f"L2_INTENT_SENSITIVE: 命中敏感意图={sensitive.value}"

        if sentiment in (SentimentLabel.NEGATIVE, SentimentLabel.ANGRY) and conf > 0.8:
            return True, f"L2_NEGATIVE_SENTIMENT: 情感={sentiment.value}, 置信度={conf:.2f}"

        return False, ""

    def _check_l3(
        self,
        session: SessionState,
        intent: IntentResult,
    ) -> tuple[bool, str]:
        """L3 累计触发

        条件：
        - 连续 N 轮低置信度（默认 3）
        - 或同一会话内 fallback 命中过多
        """
        if session.low_confidence_streak >= self._low_confidence_threshold:
            return True, f"L3_LOW_CONFIDENCE_STREAK: 连续 {session.low_confidence_streak} 轮低置信度"

        # 检查置信度历史中低置信度比例
        if len(session.confidence_history) >= 5:
            recent = session.confidence_history[-5:]
            low_count = sum(1 for c in recent if c < 0.3)
            if low_count >= 3:
                return True, f"L3_REPEATED_FALLBACK: 最近5轮中{low_count}轮为兜底回复"

        return False, ""


def _load_keywords_from_file(path: Path) -> list[str]:
    """从文件加载关键词列表（每行一个）"""
    try:
        content = path.read_text(encoding="utf-8")
        return [line.strip() for line in content.splitlines() if line.strip() and not line.startswith("#")]
    except Exception:
        logger.warning("加载关键词文件失败: %s", path)
        return []
