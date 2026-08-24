"""双通道意图分类器

Fast Path: RuleClassifier（正则 + 关键词 + 模板匹配），覆盖高频意图
Slow Path: LLMClassifier（Qwen2.5-7B via Ollama，json_mode + few-shot），覆盖模糊/长尾意图

Fast Path 置信度 < 阈值时自动 fallthrough 到 Slow Path。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

from lumio.services.bot.entity_extractor import extract_entities, normalize_entity_type
from lumio.services.common.bert_classifier import BertIntentClassifier
from lumio.services.common.trap_collector import TrapCollector, TrapRecord
from lumio.shared.config import get_settings
from lumio.shared.models import Entity, IntentLabel, IntentResult, SentimentLabel

if TYPE_CHECKING:
    from lumio.services.common.llm import LLMClient

if TYPE_CHECKING:
    from lumio.services.common.llm import LLMClient

logger = logging.getLogger(__name__)

# 规则分类器阈值：Fast Path 置信度 >= 此值直接使用
_FAST_PATH_THRESHOLD = 0.7

# 低置信噪声闸下限: BERT 快路径置信度低于此值时视为"没认出来", 直接不花一次 LLM 慢路径分类.
# 实测 LLM 慢路径会把噪声置信抬到恰 ≥ 此下限 (如 0.219→0.30), 既浪费一次 6s+ 调用,
# 又掩盖噪声信号让下游 low_conf 噪声闸漏放. 仅 BERT 为快路径时激进兜底; 与
# bot_agent 的 CLARIFY_CONFIDENCE_FLOOR (0.3) 对齐, 低于它本来就该回确定性澄清.
_LOW_CONF_FLOOR = 0.3


def _collect_alternatives(
    hits: list[tuple[IntentLabel, float]], primary: IntentLabel, limit: int = 3
) -> list[IntentLabel]:
    """从规则命中列表收集次意图 (多意图).

    - 去重、剔除主意图
    - 按置信度降序
    - 低置信 (< 0.5) 的不算作有意义的次意图
    """
    seen: set[IntentLabel] = set()
    out: list[IntentLabel] = []
    for intent, conf in sorted(hits, key=lambda x: x[1], reverse=True):
        if intent == primary or intent in seen:
            continue
        if conf < 0.5:
            continue
        seen.add(intent)
        out.append(intent)
        if len(out) >= limit - 1:  # include primary, so return at most limit-1
            break
    return out


# 意图域映射：用于 supervisor 路由
INTENT_DOMAINS: dict[IntentLabel, str] = {
    IntentLabel.BILL_QUERY: "knowledge",
    IntentLabel.TRANSACTION_QUERY: "knowledge",
    IntentLabel.LIMIT_QUERY: "knowledge",
    IntentLabel.INSTALLMENT_INQUIRY: "knowledge",
    IntentLabel.REWARD_QUERY: "knowledge",
    IntentLabel.FAQ: "knowledge",
    IntentLabel.CARD_LOSS: "business",
    IntentLabel.COMPLAINT: "business",
    IntentLabel.TRANSFER_AGENT: "business",
    IntentLabel.CHITCHAT: "fallback",
}

# Fast Path 规则定义
# 每条规则包含: intent, patterns (正则), keywords (关键词), confidence
_RULES: list[dict[str, Any]] = [
    # 账单类
    {
        "intent": IntentLabel.BILL_QUERY,
        "patterns": [r"账单", r"消费记录", r"还款金额", r"本期账单", r"上个?月.?花了多少"],
        "keywords": ["账单", "消费", "还款", "欠款", "应还", "最低还款"],
        "confidence": 0.85,
    },
    # 交易查询
    {
        "intent": IntentLabel.TRANSACTION_QUERY,
        "patterns": [r"交易记录", r"明细", r"流水", r"扣款"],
        "keywords": ["交易", "明细", "流水", "扣款", "刷卡"],
        "confidence": 0.85,
    },
    # 额度类
    {
        "intent": IntentLabel.LIMIT_QUERY,
        "patterns": [r"额度", r"可用额度", r"信用额度", r"提额", r"降额"],
        "keywords": ["额度", "可用", "信用", "提额", "临时额度"],
        "confidence": 0.85,
    },
    # 分期类
    {
        "intent": IntentLabel.INSTALLMENT_INQUIRY,
        "patterns": [r"分期", r"期数", r"手续费率", r"账单分期", r"消费分期"],
        "keywords": ["分期", "期数", "手续费", "分期费率"],
        "confidence": 0.85,
    },
    # 积分类
    {
        "intent": IntentLabel.REWARD_QUERY,
        "patterns": [r"积分", r"积分兑换", r"积分过期", r"积分余额"],
        "keywords": ["积分", "兑换", "过期", "积分商城"],
        "confidence": 0.85,
    },
    # FAQ
    {
        "intent": IntentLabel.FAQ,
        "patterns": [r"什么是", r"怎么办理", r"如何操作", r"流程是什么"],
        "keywords": [],
        "confidence": 0.7,
    },
    # 挂失
    {
        "intent": IntentLabel.CARD_LOSS,
        "patterns": [r"挂失", r"补卡", r"换卡", r"卡片丢失"],
        "keywords": ["挂失", "丢失", "补卡", "换卡"],
        "confidence": 0.9,
    },
    # 投诉
    {
        "intent": IntentLabel.COMPLAINT,
        "patterns": [r"投诉", r"不满意", r"举报", r"投诉你们"],
        "keywords": ["投诉", "不满", "举报"],
        "confidence": 0.9,
    },
    # 转人工
    {
        "intent": IntentLabel.TRANSFER_AGENT,
        "patterns": [r"转人工", r"人工客服", r"找人工", r"我要找.*人"],
        "keywords": ["人工", "转人工", "真人"],
        "confidence": 0.95,
    },
    # 闲聊
    {
        "intent": IntentLabel.CHITCHAT,
        "patterns": [r"你好", r"嗨", r"在吗", r"你是谁", r"谢谢", r"再见"],
        "keywords": [],
        "confidence": 0.8,
    },
]

# LLM 分类 Prompt
_CLASSIFY_SYSTEM_PROMPT = """你是一个银行信用卡客服意图分类器。根据用户输入，输出 JSON 格式的分类结果。

## 输出格式
```json
{
  "intent": "意图标签",
  "confidence": 0.0-1.0的置信度,
  "entities": [{"entity_type": "类型", "value": "值"}],
  "sentiment": "positive/neutral/negative/angry"
}
```

## 可选意图标签
- bill_query: 账单查询
- transaction_query: 交易记录查询
- limit_query: 额度查询
- installment_inquiry: 分期咨询
- reward_query: 积分查询
- faq: 常见问题
- card_loss: 挂失/补卡
- complaint: 投诉
- transfer_agent: 转人工
- chitchat: 闲聊

## 示例
用户: 我上个月花了多少钱
输出: {"intent": "bill_query", "confidence": 0.9, "entities": [{"entity_type": "time_range", "value": "上个月"}], "sentiment": "neutral"}

用户: 额度太低了能不能提一下
输出: {"intent": "limit_query", "confidence": 0.85, "entities": [{"entity_type": "action", "value": "提额"}], "sentiment": "neutral"}

用户: 你们的年费怎么这么贵，我要投诉
输出: {"intent": "complaint", "confidence": 0.95, "entities": [{"entity_type": "topic", "value": "年费"}], "sentiment": "angry"}

用户: 你好呀
输出: {"intent": "chitchat", "confidence": 0.9, "entities": [], "sentiment": "positive"}

## 要求
- 只输出 JSON，不要其他文字
- 置信度 0-1 之间，不确定时给低分
- 模糊输入给 intent="faq"，confidence < 0.5
"""


class RuleClassifier:
    """规则分类器（Fast Path）

    正则匹配 + 关键词匹配，返回最高置信度的意图。
    """

    def __init__(self, rules: list[dict[str, Any]] | None = None) -> None:
        self._rules = rules or _RULES
        # 预编译正则
        self._compiled: list[dict[str, Any]] = []
        for rule in self._rules:
            compiled_patterns = [re.compile(p) for p in rule.get("patterns", [])]
            self._compiled.append(
                {
                    "intent": rule["intent"],
                    "patterns": compiled_patterns,
                    "keywords": rule.get("keywords", []),
                    "confidence": rule.get("confidence", 0.7),
                }
            )

    def classify(self, text: str) -> IntentResult:
        """对用户输入进行规则分类

        匹配逻辑：最长匹配优先，多意图时取置信度最高的。
        关键词匹配置信度略低于正则匹配。

        Args:
            text: 用户输入文本

        Returns:
            IntentResult（primary_confidence 低于阈值时触发 Slow Path）
        """
        # 多意图: 收集所有命中的规则, 置信度最高的为主, 其余去重后进 alternatives.
        hits: list[tuple[IntentLabel, float]] = []
        best_intent = IntentLabel.FAQ
        best_confidence = 0.0

        for rule in self._compiled:
            matched = False
            rule_confidence = rule["confidence"]

            # 正则匹配
            for pattern in rule["patterns"]:
                match = pattern.search(text)
                if match:
                    matched = True
                    # 正则匹配使用规则设定的置信度
                    break

            # 关键词匹配（置信度降 0.1）
            if not matched:
                for keyword in rule["keywords"]:
                    if keyword in text:
                        matched = True
                        rule_confidence -= 0.1
                        break

            if matched:
                hits.append((rule["intent"], rule_confidence))
                if rule_confidence > best_confidence:
                    best_intent = rule["intent"]
                    best_confidence = rule_confidence

        return IntentResult(
            primary_intent=best_intent,
            primary_confidence=best_confidence,
            alternatives=_collect_alternatives(hits, best_intent),
        )


class LLMClassifier:
    """LLM 分类器（Slow Path）

    通过 Qwen2.5-7B 的 json_mode 输出结构化分类结果。
    同时提取实体和情感分析。
    """

    def __init__(self, llm_client: LLMClient) -> None:
        self._llm = llm_client

    async def classify(self, text: str) -> tuple[IntentResult, list[Entity], SentimentLabel]:
        """LLM 意图分类

        Args:
            text: 用户输入文本

        Returns:
            (IntentResult, 实体列表, 情感标签)
        """
        try:
            # 强制总时长上限: 此前 timeout 只透传给 OpenAI SDK 作 per-read 超时, 对
            # 流式输出(小 token 持续到达)永不触发 → LLM 分类可跑 8s+ (拖垮整轮应答).
            # asyncio.wait_for 给硬总 deadline; 超时走下方兜底 FAQ@0.0 → 下游 low_conf
            # 噪声闸拦回确定性澄清, 不再叠加第二次 LLM 生成.
            timeout = get_settings().llm.classify_timeout
            result = await asyncio.wait_for(
                self._llm.classify(
                    system_prompt=_CLASSIFY_SYSTEM_PROMPT,
                    user_input=text,
                    timeout=timeout,
                ),
                timeout=timeout,
            )
        except Exception:
            logger.warning("LLM 分类调用失败/超时(≤%.1fs)，返回兜底结果", get_settings().llm.classify_timeout)
            return (
                IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.0),
                [],
                SentimentLabel.NEUTRAL,
            )

        intent_label = _parse_intent(result.get("intent", ""))
        confidence = result.get("confidence", 0.0)
        entities = _parse_entities(result.get("entities", []))
        sentiment = _parse_sentiment(result.get("sentiment", ""))

        return (
            IntentResult(primary_intent=intent_label, primary_confidence=confidence),
            entities,
            sentiment,
        )

    async def arbitrate(self, text: str, history: list[dict[str, str]] | None = None) -> dict[str, Any]:
        """P1 模糊带宽仲裁: 让 LLM 判 {business|chitchat|noise} 作为弱信号。

        只作为"决策仲裁"(非分类慢路径): 语义落在模糊带宽(energy/BERT 中段、无铁证)时,
        把原文(可拼 history)交 LLM 判定它到底是业务、闲聊还是噪声, 与证据链投票。
        LLM 结果不单独作通过依据 — 调用方需结合置信/energy/缺槽; 自评置信视为弱信号。

        Returns:
            {"domain": "business"|"chitchat"|"noise"|"unknown",
             "confidence": float, "structured": bool}
            调用失败/LLM 不可用时返回 {"domain": "unknown", "confidence": 0.0, "structured": False}.
        """
        try:
            history_txt = ""
            if history:
                turns = [f"{t.get('speaker')}:{t.get('content')}" for t in history if t.get("content")]
                history_txt = ("\n".join(turns[-6:]) + "\n") if turns else ""
            timeout = get_settings().llm.classify_timeout
            result = await asyncio.wait_for(
                self._llm.classify(
                    system_prompt=_ARBITRATE_SYSTEM_PROMPT,
                    user_input=f"{history_txt}用户: {text}",
                    timeout=timeout,
                ),
                timeout=timeout,
            )
        except Exception:
            logger.warning("LLM 仲裁调用失败，返回 unknown 弱信号")
            return {"domain": "unknown", "confidence": 0.0, "structured": False}

        raw = (result.get("domain") or "").strip().lower()
        if raw not in ("business", "chitchat", "noise"):
            return {"domain": "unknown", "confidence": 0.0, "structured": False}
        conf = float(result.get("confidence", 0.0))
        return {"domain": raw, "confidence": conf, "structured": True}


# P1 LLM 仲裁 prompt: 只判"这到底属于哪一类", 用于模糊带的保守兜底裁决.
_ARBITRATE_SYSTEM_PROMPT = """你是银行信用卡客服的"输入仲裁"。判断一句话到底属于哪类，输出 JSON。
只允许三种类别（严格小写）：
- business: 与银行业务相关的真实诉求（查账/分期/挂失/投诉/额度/积分/转人工等）
- chitchat: 闲聊/寒暄/玩笑，不涉及具体业务
- noise: 乱码/按键误触/无意义输入/与对话无关的噪音

规则：
- 拿不准、或像是在接上文的数字/金额/卡号回话但语义不明 → 归 {business}（保守，宁放过不误拦）
- 明确的乱码（如 hjfw、纯键盘乱敲）→ noise
- 输出格式：{"domain": "business|chitchat|noise", "confidence": 0-1}
只输出 JSON，不要其他文字。
"""


class IntentClassifier:
    """双通道意图分类编排器

    Fast Path（规则｜小 BERT） → 置信度 >= 阈值 → 直接使用
                              → 置信度 < 阈值 → Slow Path（LLM）
    """

    def __init__(
        self,
        rule_classifier: RuleClassifier | None = None,
        llm_classifier: LLMClassifier | None = None,
        fast_threshold: float = _FAST_PATH_THRESHOLD,
        bert_classifier: BertIntentClassifier | None = None,
        trap: TrapCollector | None = None,
    ) -> None:
        self._rule = rule_classifier or RuleClassifier()
        self._llm = llm_classifier
        self._threshold = fast_threshold
        self._bert = bert_classifier
        self._trap = trap  # P1 感知缝: 被动采样失败/不确定/分歧样本
        # P1: 最近一次 BERT 快路径的 energy-OOD 分 (无 BERT/未启用时为 None).
        # 供上层噪声闸读取, 与 classify 主结果解耦, 不改变返回签名。
        self._last_energy: float | None = None

    # P2-17: 规则路径情绪关键词 (愤怒/负面), 支撑情绪转人工
    _ANGRY_KEYWORDS: frozenset[str] = frozenset(
        {"太生气了", "气死", "愤怒", "忍无可忍", "差劲", "什么态度", "投诉你们", "垃圾", "恼火", "火大"}
    )
    _NEGATIVE_KEYWORDS: frozenset[str] = frozenset(
        {"不满", "失望", "很烦", "烦死了", "郁闷", "难受", "心累", "无语", "焦虑", "担心", "害怕"}
    )

    @staticmethod
    def _rule_sentiment(text: str) -> SentimentLabel:
        """规则情绪检测: 关键词命中 → angry/negative, 否则 neutral."""
        if not text:
            return SentimentLabel.NEUTRAL
        for kw in IntentClassifier._ANGRY_KEYWORDS:
            if kw in text:
                return SentimentLabel.ANGRY
        for kw in IntentClassifier._NEGATIVE_KEYWORDS:
            if kw in text:
                return SentimentLabel.NEGATIVE
        return SentimentLabel.NEUTRAL

    async def classify(
        self,
        text: str,
        history: list[dict[str, str]] | None = None,
    ) -> tuple[IntentResult, list[Entity], SentimentLabel, str]:
        """执行双通道分类

        Args:
            text: 用户输入文本
            history: 可选多轮上下文 (speaker/content), 透传给 BERT 快路径做对话级意图判定

        Returns:
            (IntentResult, 实体列表, 情感标签, 分类来源 "bert"|"rule"|"llm"|"fallback"|"bert:lowconf")
        """
        # Fast Path: 优先小 BERT, 否则规则; BERT 异常时回退规则 (打不挂线上)
        fast_source = "rule"
        if self._bert is not None:
            try:
                fast_result = await self._bert.classify(text, history=history)
                fast_source = "bert"
                # P1: 同次前向顺带算 energy-OOD 分, 供上层噪声闸作"认不认"信号.
                # 开启时丢给线程池再跑一次前向; 失败仅清空, 不阻断 (energy 是辅助信号).
                if get_settings().classification.ood_enabled:
                    try:
                        self._last_energy = await self._bert.ood_score(text, history=history)
                        # 随本次分类结果对象透传 (而非共享属性), 避免并发会话读时串线.
                        fast_result.energy = self._last_energy
                    except Exception:
                        self._last_energy = None
            except Exception:
                logger.warning("BERT 分类失败, 回退规则快路径")
                fast_result = self._rule.classify(text)
                self._last_energy = None
        else:
            fast_result = self._rule.classify(text)
            self._last_energy = None

        if fast_result.primary_confidence >= self._threshold:
            logger.debug(
                "Fast Path 命中: intent=%s, confidence=%.2f, source=%s",
                fast_result.primary_intent.value,
                fast_result.primary_confidence,
                fast_source,
            )
            # P2-17: Fast Path 情绪检测 — 此前规则通道恒 NEUTRAL, 情绪只在 LLM 慢路径
            # 生效 (覆盖面窄). 规则命中时用关键词快速判定愤怒/负面, 支撑情绪转人工.
            await self._emit_sample(text, fast_source, fast_result, fast_result, fast_source)
            return fast_result, extract_entities(text), self._rule_sentiment(text), fast_source

        # P-低置信短路: BERT 快路径强"不认"(置信 < 噪声下限)时, 不跑 LLM 慢路径分类.
        # 慢路径不仅贵 (一次 LLM 分类 ~6s), 还把置信抬到恰 ≥ 下限掩盖噪声, 让下游
        # low_conf 噪声闸失效 → 噪声被漏放到第二次 LLM 生成. 直接返回 fast_result,
        # 交给 _evaluate_noise_gate 的 low_conf 拦回确定性澄清, 零 LLM 开销.
        # 仅在 BERT 快路径命中 (<0.7 才会走到这) 且 BERT 置信 < 下限时触发; 真实业务
        # 样本实测均 ≥0.35, 不受影响; is_replying 豁免由下游闸先行断言。
        if fast_source == "bert" and fast_result.primary_confidence < _LOW_CONF_FLOOR:
            logger.debug(
                "BERT 低置信短路慢路径 (conf=%.3f < %s): skip LLM classify, intent=%s",
                fast_result.primary_confidence,
                _LOW_CONF_FLOOR,
                fast_result.primary_intent.value,
            )
            await self._emit_sample(text, fast_source, fast_result, fast_result, "bert:lowconf")
            return fast_result, extract_entities(text), self._rule_sentiment(text), "bert:lowconf"

        # Slow Path
        if self._llm is None:
            logger.debug("Slow Path 不可用，使用 Fast Path 低置信度结果")
            await self._emit_sample(text, fast_source, fast_result, fast_result, "fallback")
            return fast_result, extract_entities(text), SentimentLabel.NEUTRAL, "fallback"

        # 熔断器打开 → 跳过 LLM
        if not self._llm._llm._breaker.is_available:
            logger.debug("LLM 熔断器打开，跳过 Slow Path")
            await self._emit_sample(text, fast_source, fast_result, fast_result, "fallback")
            return fast_result, extract_entities(text), SentimentLabel.NEUTRAL, "fallback"

        logger.debug(
            "Fast Path 置信度不足 (%.2f < %.2f)，进入 Slow Path",
            fast_result.primary_confidence,
            self._threshold,
        )

        try:
            llm_result, entities, sentiment = await self._llm.classify(text)
        except Exception:
            logger.warning("LLM 分类调用失败，使用 Fast Path 结果兜底")
            await self._emit_sample(text, fast_source, fast_result, fast_result, "fallback")
            return fast_result, extract_entities(text), SentimentLabel.NEUTRAL, "fallback"

        # LLM 结果置信度也很低时，标记来源为 fallback
        source = "llm" if llm_result.primary_confidence >= 0.3 else "fallback"
        await self._emit_sample(text, fast_source, fast_result, llm_result, source)
        llm_result.energy = self._last_energy  # 快路径 energy 随慢路径结果一起透传
        # P0 快慢分歧信号: 慢路径覆盖快路径时保留快路径意图/置信, 供下游噪声闸与
        # 转人工派发门判"两路分歧" -- 慢路径对乱码的自评置信会稳定通胀(会话 e33d1fa8:
        # BERT limit_query@0.39 -> LLM bill_query@0.7), 最终置信单项不可信.
        llm_result.fast_conf = fast_result.primary_confidence
        llm_result.fast_intent = fast_result.primary_intent
        # 快照实体亦并入规则层抽取结果 (规则层在 LLM 抽漏时兜底), 只去重不覆盖
        return llm_result, _merge_entities(entities, extract_entities(text)), sentiment, source

    async def _emit_sample(
        self,
        text: str,
        fast_source: str,
        fast_result: IntentResult,
        final_result: IntentResult,
        final_source: str,
    ) -> None:
        """P1 感知缝: 把一次分类结果快照交给 TrapCollector 判定是否采样.

        规则通道是廉价正则 (<1ms), 仅在 BERT 为快路径时顺带跑一次用于分歧检测,
        避免重复跑昂贵的 BERT. 采样判定在同步阶段完成, 落库由后台 task 承担.
        """
        trap = self._trap
        if trap is None:
            return
        rule_intent: str | None = None
        divergence = False
        if fast_source == "bert":
            rule_intent = self._rule.classify(text).primary_intent.value
            divergence = rule_intent != fast_result.primary_intent.value
        rec = TrapRecord(
            text=text,
            fast_source=fast_source,
            fast_intent=fast_result.primary_intent.value,
            fast_confidence=fast_result.primary_confidence,
            rule_intent=rule_intent,
            final_source=final_source,
            final_intent=final_result.primary_intent.value,
            final_confidence=final_result.primary_confidence,
            margin=abs(final_result.primary_confidence - self._threshold),
            divergence=divergence,
        )
        await trap.capture(rec)


def get_domain(intent: IntentLabel) -> str:
    """获取意图所属域（用于 supervisor 路由）"""
    return INTENT_DOMAINS.get(intent, "fallback")


# ── 解析辅助函数 ──


def _parse_intent(raw: str) -> IntentLabel:
    """将 LLM 输出的意图字符串转为 IntentLabel 枚举"""
    try:
        return IntentLabel(raw)
    except ValueError:
        logger.debug("LLM 输出未知意图: %s", raw)
        return IntentLabel.FAQ


def _parse_entities(raw_entities: list[dict[str, Any]]) -> list[Entity]:
    """将 LLM 输出的实体列表转为 Entity 模型。

    实体类型经 normalize_entity_type 映射到规范 key (与 slot_tracker 词汇表对齐),
    未知/乱造的类型名被丢弃, 避免污染 slot 填充与指代消解候选。
    """
    entities: list[Entity] = []
    for e in raw_entities:
        try:
            etype = normalize_entity_type(e.get("entity_type", ""))
            if etype is None:
                continue
            entities.append(
                Entity(
                    entity_type=etype,
                    value=e.get("value", ""),
                    confidence=e.get("confidence", 0.7),
                )
            )
        except Exception:
            logger.debug("实体解析失败: %s", e)
    return entities


def _merge_entities(primary: list[Entity], secondary: list[Entity]) -> list[Entity]:
    """合并实体列表, 保序去重 (同类同值). primary 优先, secondary 兜底补漏。"""
    if not secondary:
        return primary
    if not primary:
        return secondary
    seen = {(e.entity_type, e.value) for e in primary}
    out = list(primary)
    for e in secondary:
        key = (e.entity_type, e.value)
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


def _parse_sentiment(raw: str) -> SentimentLabel:
    """将 LLM 输出的情感字符串转为 SentimentLabel 枚举"""
    try:
        return SentimentLabel(raw)
    except ValueError:
        return SentimentLabel.NEUTRAL
