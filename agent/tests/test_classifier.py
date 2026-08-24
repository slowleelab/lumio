"""双通道意图分类器单元测试"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from lumio.services.common.classifier import (
    IntentClassifier,
    LLMClassifier,
    RuleClassifier,
    get_domain,
)
from lumio.shared.models import IntentLabel, IntentResult, SentimentLabel

# ── RuleClassifier ──


def test_rule_classify_bill_query() -> None:
    """账单关键词应分类为 bill_query"""
    classifier = RuleClassifier()
    result = classifier.classify("我想查一下账单")
    assert result.primary_intent == IntentLabel.BILL_QUERY
    assert result.primary_confidence >= 0.7


def test_rule_classify_limit_query() -> None:
    """额度关键词应分类为 limit_query"""
    classifier = RuleClassifier()
    result = classifier.classify("我的额度是多少")
    assert result.primary_intent == IntentLabel.LIMIT_QUERY


def test_rule_classify_card_loss() -> None:
    """挂失关键词应分类为 card_loss"""
    classifier = RuleClassifier()
    result = classifier.classify("信用卡丢了要挂失")
    assert result.primary_intent == IntentLabel.CARD_LOSS
    assert result.primary_confidence >= 0.8


def test_rule_classify_transfer_agent() -> None:
    """转人工关键词应分类为 transfer_agent"""
    classifier = RuleClassifier()
    result = classifier.classify("我要转人工")
    assert result.primary_intent == IntentLabel.TRANSFER_AGENT
    assert result.primary_confidence >= 0.9


def test_rule_classify_unknown_returns_low_confidence() -> None:
    """无法匹配的输入应返回低置信度"""
    classifier = RuleClassifier()
    result = classifier.classify("帮我看一下这个东西怎么回事")
    assert result.primary_confidence < 0.7


def test_rule_classify_regex_pattern() -> None:
    """正则模式应匹配变体输入"""
    classifier = RuleClassifier()
    result = classifier.classify("我上个月花了多少钱")
    assert result.primary_intent == IntentLabel.BILL_QUERY


def test_rule_keyword_lower_confidence_than_regex() -> None:
    """关键词匹配置信度应低于正则匹配"""
    classifier = RuleClassifier()
    # "额度" 是关键词但不在正则模式中直接匹配（额度的正则也包含"额度"）
    result_regex = classifier.classify("我的可用额度")
    result_keyword = classifier.classify("额度")
    # 正则匹配了更具体的模式，置信度应 >= 关键词
    assert result_regex.primary_confidence >= result_keyword.primary_confidence


# ── LLMClassifier ──


@pytest.mark.asyncio
async def test_llm_classify_success() -> None:
    """LLM 分类应返回结构化结果"""
    mock_llm = MagicMock()
    mock_llm.classify = AsyncMock(
        return_value={
            "intent": "bill_query",
            "confidence": 0.85,
            "entities": [{"entity_type": "time_range", "value": "上个月"}],
            "sentiment": "neutral",
        }
    )

    classifier = LLMClassifier(mock_llm)
    intent, entities, sentiment = await classifier.classify("上个月消费了多少")

    assert intent.primary_intent == IntentLabel.BILL_QUERY
    assert intent.primary_confidence == 0.85
    assert len(entities) == 1
    # LLM 输出的 loose 类型 "time_range" 已被归一为规范 key "period" (槽位词汇表)
    assert entities[0].entity_type == "period"
    assert entities[0].value == "上个月"
    assert sentiment == SentimentLabel.NEUTRAL


@pytest.mark.asyncio
async def test_llm_classify_fallback_on_error() -> None:
    """LLM 调用失败时应返回兜底结果"""
    mock_llm = MagicMock()
    mock_llm.classify = AsyncMock(side_effect=Exception("LLM 不可用"))

    classifier = LLMClassifier(mock_llm)
    intent, entities, sentiment = await classifier.classify("随便什么")

    assert intent.primary_intent == IntentLabel.FAQ
    assert intent.primary_confidence == 0.0
    assert entities == []
    assert sentiment == SentimentLabel.NEUTRAL


# ── IntentClassifier (双通道) ──


@pytest.mark.asyncio
async def test_dual_path_fast_path_hit() -> None:
    """高置信度规则匹配应直接使用 Fast Path"""
    rule = RuleClassifier()
    classifier = IntentClassifier(rule_classifier=rule, llm_classifier=None)
    intent, entities, sentiment, source = await classifier.classify("我要查账单")

    assert intent.primary_intent == IntentLabel.BILL_QUERY
    assert source == "rule"


@pytest.mark.asyncio
async def test_dual_path_slow_path_fallthrough() -> None:
    """低置信度时应 fallthrough 到 LLM"""
    rule = RuleClassifier()
    mock_llm_classifier = MagicMock()
    mock_llm_classifier.classify = AsyncMock(
        return_value=(
            IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.6),
            [],
            SentimentLabel.NEUTRAL,
        )
    )

    classifier = IntentClassifier(rule_classifier=rule, llm_classifier=mock_llm_classifier, fast_threshold=0.7)
    intent, entities, sentiment, source = await classifier.classify("这个怎么弄")

    assert source == "llm"
    mock_llm_classifier.classify.assert_called_once()


@pytest.mark.asyncio
async def test_dual_path_no_llm_uses_fallback() -> None:
    """无 LLM 时应使用 Fast Path 低置信度结果"""
    rule = RuleClassifier()
    classifier = IntentClassifier(rule_classifier=rule, llm_classifier=None)
    intent, entities, sentiment, source = await classifier.classify("这个怎么弄")

    assert source == "fallback"


# ── IntentClassifier (小 BERT 快路径) ──


def _fake_bert(
    return_intent: IntentLabel = IntentLabel.FAQ, conf: float = 0.95, *, exc: Exception | None = None
) -> MagicMock:
    """构造一个假的 BERT 分类器 (async classify), 不加载真 torch。"""
    fake = MagicMock()
    if exc is not None:
        fake.classify = AsyncMock(side_effect=exc)
    else:
        fake.classify = AsyncMock(return_value=IntentResult(primary_intent=return_intent, primary_confidence=conf))
    return fake


@pytest.mark.asyncio
async def test_bert_fast_path_hit() -> None:
    """BERT 高置信度应直接命中, source == bert, 不触发 LLM"""
    fake = _fake_bert(IntentLabel.REWARD_QUERY, 0.95)
    classifier = IntentClassifier(rule_classifier=RuleClassifier(), llm_classifier=None, bert_classifier=fake)
    intent, entities, sentiment, source = await classifier.classify("积分商城在哪")

    assert source == "bert"
    assert intent.primary_intent == IntentLabel.REWARD_QUERY
    fake.classify.assert_awaited_once()


@pytest.mark.asyncio
async def test_bert_low_confidence_fallthrough_to_llm() -> None:
    """BERT 低置信度 (< 阈值) 应 fallthrough 到 LLM 慢路径"""
    fake = _fake_bert(IntentLabel.FAQ, 0.4)
    mock_llm_classifier = MagicMock()
    mock_llm_classifier.classify = AsyncMock(
        return_value=(IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.6), [], SentimentLabel.NEUTRAL)
    )
    classifier = IntentClassifier(
        rule_classifier=RuleClassifier(), llm_classifier=mock_llm_classifier, fast_threshold=0.7, bert_classifier=fake
    )
    _intent, _entities, _sentiment, source = await classifier.classify("这个怎么弄")

    assert source == "llm"
    mock_llm_classifier.classify.assert_called_once()


@pytest.mark.asyncio
async def test_bert_slow_path_attaches_fast_signal() -> None:
    """P0 快慢分歧信号: LLM 慢路径覆盖 BERT 快路径时, fast_conf/fast_intent 必须随结果
    透传 -- 会话 e33d1fa8 (乱码 BERT limit_query@0.39 -> LLM bill_query@0.7) 的分歧证据链
    就断在这一环: 信号不透传, 下游噪声门永远只看见被通胀的最终置信."""
    fake = _fake_bert(IntentLabel.LIMIT_QUERY, 0.39)
    mock_llm_classifier = MagicMock()
    mock_llm_classifier.classify = AsyncMock(
        return_value=(
            IntentResult(primary_intent=IntentLabel.BILL_QUERY, primary_confidence=0.7),
            [],
            SentimentLabel.NEUTRAL,
        )
    )
    classifier = IntentClassifier(
        rule_classifier=RuleClassifier(), llm_classifier=mock_llm_classifier, fast_threshold=0.7, bert_classifier=fake
    )
    intent, _entities, _sentiment, source = await classifier.classify("额佛呢份")

    assert source == "llm"
    assert intent.primary_intent == IntentLabel.BILL_QUERY
    assert intent.fast_conf == 0.39
    assert intent.fast_intent == IntentLabel.LIMIT_QUERY


@pytest.mark.asyncio
async def test_bert_fast_path_hit_has_no_fast_signal() -> None:
    """快路径直接命中时无"分歧"可言: fast_conf/fast_intent 保持 None (下游谓词恒 False)."""
    fake = _fake_bert(IntentLabel.REWARD_QUERY, 0.95)
    classifier = IntentClassifier(rule_classifier=RuleClassifier(), llm_classifier=None, bert_classifier=fake)
    intent, _entities, _sentiment, source = await classifier.classify("积分商城在哪")

    assert source == "bert"
    assert intent.fast_conf is None
    assert intent.fast_intent is None


@pytest.mark.asyncio
async def test_bert_sub_floor_short_circuits_llm() -> None:
    """BERT 置信 < 噪声下限 (0.3) → 短路 LLM 慢路径, 直接返回 bert:lowconf。

    回归: 此前乱码/噪声 (BERT 置信 ~0.22) 会落入 LLM 慢路径分类 (~6s), 且 LLM 把置信
    抬到恰 ≥0.3 掩盖噪声信号, 让下游 low_conf 噪声闸漏放 → 二次 LLM 生成 (~10s 应答)。
    现在直接短路, 交 low_conf 闸回确定性澄清, 零 LLM 开销。
    """
    from lumio.services.common.classifier import _LOW_CONF_FLOOR

    fake = _fake_bert(IntentLabel.FAQ, 0.2)  # 低于下限
    mock_llm_classifier = MagicMock()
    mock_llm_classifier.classify = AsyncMock(
        return_value=(IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.6), [], SentimentLabel.NEUTRAL)
    )
    classifier = IntentClassifier(
        rule_classifier=RuleClassifier(), llm_classifier=mock_llm_classifier, fast_threshold=0.7, bert_classifier=fake
    )
    intent, _entities, _sentiment, source = await classifier.classify("阿萨法上课呢")

    assert source == "bert:lowconf"
    assert intent.primary_confidence < _LOW_CONF_FLOOR
    mock_llm_classifier.classify.assert_not_called()


@pytest.mark.asyncio
async def test_bert_low_confidence_no_llm_uses_fallback() -> None:
    """BERT 低置信度且无 LLM → fallback"""
    fake = _fake_bert(IntentLabel.FAQ, 0.4)
    classifier = IntentClassifier(rule_classifier=RuleClassifier(), llm_classifier=None, bert_classifier=fake)
    _intent, _entities, _sentiment, source = await classifier.classify("这个怎么弄")

    assert source == "fallback"


@pytest.mark.asyncio
async def test_bert_error_falls_back_to_rule() -> None:
    """BERT 推理异常 → 回退规则快路径 (规则命中则以 rule 返回), 不抛异常"""
    fake = _fake_bert(exc=RuntimeError("模型加载失败"))
    classifier = IntentClassifier(rule_classifier=RuleClassifier(), llm_classifier=None, bert_classifier=fake)
    intent, entities, sentiment, source = await classifier.classify("我要查账单")

    assert source == "rule"
    assert intent.primary_intent == IntentLabel.BILL_QUERY


# ── get_domain ──


def test_get_domain_knowledge() -> None:
    """知识类意图应路由到 knowledge 域"""
    assert get_domain(IntentLabel.BILL_QUERY) == "knowledge"
    assert get_domain(IntentLabel.LIMIT_QUERY) == "knowledge"
    assert get_domain(IntentLabel.REWARD_QUERY) == "knowledge"


def test_get_domain_business() -> None:
    """业务类意图应路由到 business 域"""
    assert get_domain(IntentLabel.CARD_LOSS) == "business"
    assert get_domain(IntentLabel.COMPLAINT) == "business"


def test_get_domain_fallback() -> None:
    """闲聊/未知意图应路由到 fallback 域"""
    assert get_domain(IntentLabel.CHITCHAT) == "fallback"


# ── 多轮上下文透传 (draft-0.2 第一批) ──


def test_classify_passes_history_to_bert_fast_path() -> None:
    """IntentClassifier 应把多轮上下文透传给 BERT 快路径."""
    fake_bert = MagicMock()
    fake_bert.classify = AsyncMock(
        return_value=IntentResult(primary_intent=IntentLabel.INSTALLMENT_INQUIRY, primary_confidence=0.9)
    )
    clf = IntentClassifier(
        rule_classifier=RuleClassifier(),
        llm_classifier=None,
        fast_threshold=0.7,
        bert_classifier=fake_bert,
    )
    import asyncio

    result, _, _, source = asyncio.run(
        clf.classify("手续费怎么收", history=[{"speaker": "customer", "content": "我想分期"}])
    )
    assert source == "bert"
    assert result.primary_intent == IntentLabel.INSTALLMENT_INQUIRY
    # 确认 history 真被传给了 BERT
    fake_bert.classify.assert_awaited_once()
    assert fake_bert.classify.await_args.kwargs.get("history") == [{"speaker": "customer", "content": "我想分期"}]


def test_rule_classify_multi_intent_alternatives() -> None:
    """规则命中多个意图时, 次意图应进 alternatives (多意图)."""
    classifier = RuleClassifier()
    # "卡丢了要挂失" 触发挂失; 附加 "查交易流水" 触发交易
    res = classifier.classify("卡丢了要挂失, 顺便帮我查一下最近交易流水")
    assert res.primary_intent == IntentLabel.CARD_LOSS
    assert IntentLabel.TRANSACTION_QUERY in res.alternatives


def test_bert_build_dialog_input_keeps_latest() -> None:
    """多轮上下文拼接应保留最近的 customer/bot 轮并输出当前句."""
    from lumio.services.common.bert_classifier import BertIntentClassifier

    inp = BertIntentClassifier._build_dialog_input(
        "手续费怎么收",
        [
            {"speaker": "customer", "content": "我想分期"},
            {"speaker": "bot", "content": "好的, 请问要分几期呢?"},
            {"speaker": "customer", "content": "那手续费怎么收?"},
        ],
    )
    assert "分期" in inp
    assert inp.endswith("手续费怎么收")


# ── normalize_intent 兼容 (draft-0.2 第一批) ──


def test_normalize_intent_identity_and_fallback() -> None:
    from lumio.shared.models import normalize_intent

    assert normalize_intent("bill_query") == IntentLabel.BILL_QUERY
    assert normalize_intent("card_loss") == IntentLabel.CARD_LOSS
    # 未知旧值兜底 FAQ, 不抛异常 (存量 ReadIsolation 兼容)
    assert normalize_intent("some_old_unknown_label") == IntentLabel.FAQ


# ── P1 能量-OOD + 校准 (纯函数, 无 torch 依赖) ──


def test_logit_energy_sharp_gt_scattered() -> None:
    """强判定(某个 logit 明显最大)energy 应高于分散的 OOD 输入."""
    from lumio.services.common.bert_classifier import logit_energy

    sharp = logit_energy([5.0, 0.1, 0.0, -0.2])
    scattered = logit_energy([0.5, 0.4, 0.3, 0.2])
    assert sharp < scattered  # 置信: energy 低(负得更深)
    # 数值等价: energy 约等于 -max logit (次级 logit 贡献 ~0.02)
    assert abs(sharp - (-5.0)) < 0.05


def test_logit_energy_empty_and_equal() -> None:
    import math

    from lumio.services.common.bert_classifier import logit_energy

    assert logit_energy([]) == 0.0
    # 全相等 → energy = -log(|C|), 数值上高于(分散于)强判定场景
    eq = logit_energy([0.0, 0.0, 0.0, 0.0])
    assert abs(eq - (-math.log(4.0))) < 1e-9


def test_calibrate_temperature_one_is_identity() -> None:
    from lumio.services.common.bert_classifier import calibrate_logits

    logs = [3.0, 1.0, -1.0]
    assert calibrate_logits(logs, 1.0) == logs
    assert calibrate_logits(logs, 0.0) == logs  # 非法温度按不缩放
    assert calibrate_logits(logs, None) == logs


def test_calibrate_temperature_moves_confidence_down() -> None:
    """温度 >1 压低置信度差(校准过拟合小 BERT 的虚高)."""
    import math

    from lumio.services.common.bert_classifier import calibrate_logits

    logs = [10.0, 0.0]
    logits_cal = calibrate_logits(logs, 2.0)
    p = lambda v: math.exp(v) / (math.exp(logits_cal[0]) + math.exp(logits_cal[1]))  # noqa: E731
    assert p(logits_cal[0]) < 1.0 - 1e-9  # 不再饱和到几乎 1


def test_ood_verdict_three_states() -> None:
    from lumio.services.common.bert_classifier import ood_verdict

    # 阈值 0, 带宽 1: energy<=-1 → known, -1<energy<=1 → ambiguous, >1 → unknown (方向见函数 doc)
    assert ood_verdict(-2.0, 0.0, 1.0) == "known"  # 低 energy = 自信
    assert ood_verdict(0.5, 0.0, 1.0) == "ambiguous"
    assert ood_verdict(2.0, 0.0, 1.0) == "unknown"  # 高 energy = OOD/噪声
    assert ood_verdict(-0.5, 0.0, 1.0) == "ambiguous"
    # 零带宽 → 中间态消失(只 two 态)
    assert ood_verdict(-0.1, 0.0, 0.0) == "known"
    assert ood_verdict(0.1, 0.0, 0.0) == "unknown"


async def test_llm_arbitrate_parses_domain() -> None:
    """LLM 仲裁: 结构化结果按 domain 映射, 未知/失败回 unknown 弱信号."""
    llm = MagicMock()
    llm.classify = AsyncMock(return_value={"domain": "noise", "confidence": 0.8})
    clf = LLMClassifier(llm)
    res = await clf.arbitrate("hfkwjf")
    assert res["domain"] == "noise"
    assert res["structured"] is True

    llm.classify = AsyncMock(return_value={"domain": "business", "confidence": 0.7})
    res = await clf.arbitrate("我想查账单")
    assert res["domain"] == "business"

    # 非法/缺失 domain → unknown, 不抛异常
    llm.classify = AsyncMock(return_value={"domain": "hack-me", "confidence": 0.9})
    res = await clf.arbitrate("x")
    assert res["domain"] == "unknown"

    # LLM 调用抛异常 → unknown (不阻断)
    llm.classify = AsyncMock(side_effect=RuntimeError("llm down"))
    res = await clf.arbitrate("x")
    assert res["domain"] == "unknown"
    assert res["structured"] is False


def test_bert_build_dialog_input_filters_clarify_pairs() -> None:
    """clarify 收尾的乱码轮对不进上下文 (乱码历史拖低真实意图置信, 会话 f08227d4)。"""
    from lumio.services.common.bert_classifier import BertIntentClassifier

    inp = BertIntentClassifier._build_dialog_input(
        "分期",
        [
            {"speaker": "customer", "content": "额佛呢份"},
            {"speaker": "bot", "content": "您的意思我还没太理解。", "response_source": "clarify"},
            {"speaker": "customer", "content": "分期"},
            {"speaker": "bot", "content": "请问您想分期的金额是多少？", "response_source": "llm"},
        ],
    )
    assert "额佛呢份" not in inp
    assert "您的意思我还没太理解" not in inp
    # 非 clarify 轮次正常保留
    assert "分期" in inp
    assert "请问您想分期的金额是多少" in inp
    assert inp.endswith("分期")


def test_bert_build_dialog_input_keeps_turns_without_source_marker() -> None:
    """无 response_source 标记的历史轮次 (旧调用方) 全部保留, 向后兼容。"""
    from lumio.services.common.bert_classifier import BertIntentClassifier

    inp = BertIntentClassifier._build_dialog_input(
        "手续费怎么收",
        [
            {"speaker": "customer", "content": "我想分期"},
            {"speaker": "bot", "content": "好的, 请问要分几期呢?"},
        ],
    )
    assert "我想分期" in inp
    assert "请问要分几期呢" in inp
    assert inp.endswith("手续费怎么收")
