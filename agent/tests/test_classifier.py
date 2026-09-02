"""双通道意图分类器单元测试"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from lumio.services.common.classifier import (
    IntentClassifier,
    LLMClassifier,
    RuleClassifier,
    get_domain,
)
from lumio.shared.models import Entity, IntentLabel, IntentResult, SentimentLabel

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
    # 2026-08-25 标定: 置信度 = seed 实测命中率 (0.56, 原拍脑袋常数 0.9 虚高放行误判),
    # 低于快路径阈值 0.7 → 规则命中后改走 BERT/LLM, 不再直接放行
    assert result.primary_confidence == 0.56


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
    """LLM 调用失败应上抛 (由 IntentClassifier except 用 Fast Path 结果兜底)。

    2026-08-30 修复: 此前内部吞异常返回 faq@0.0, 外层 Fast Path 兜底永不触发,
    BERT 已认出的正确结果被覆写成 0.0 → 噪声门误杀 (会话 f1fec705/9d64b59)。
    """
    mock_llm = MagicMock()
    mock_llm.classify = AsyncMock(side_effect=Exception("LLM 不可用"))

    classifier = LLMClassifier(mock_llm)
    with pytest.raises(Exception, match="LLM 不可用"):
        await classifier.classify("随便什么")


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
    """知识/政策类意图应路由到 knowledge 域"""
    assert get_domain(IntentLabel.FAQ) == "knowledge"
    assert get_domain(IntentLabel.REPAY_METHOD_QUERY) == "knowledge"
    assert get_domain(IntentLabel.FEE_ANNUAL) == "knowledge"
    assert get_domain(IntentLabel.INST_PARAM_QUERY) == "knowledge"


def test_get_domain_business() -> None:
    """业务类意图应路由到 business 域 (draft-0.3 §4.2: 查询类已翻 business 主路径)"""
    assert get_domain(IntentLabel.BILL_QUERY) == "business"  # 旧 flat → account_bill_query
    assert get_domain(IntentLabel.ACCOUNT_BILL_QUERY) == "business"
    assert get_domain(IntentLabel.TXN_QUERY) == "business"
    assert get_domain(IntentLabel.LIMIT_QUERY) == "business"


def test_get_domain_fallback() -> None:
    """闲聊/未知意图应路由到 fallback 域"""
    assert get_domain(IntentLabel.CHITCHAT) == "fallback"  # 旧 flat → nb_chitchat
    assert get_domain(IntentLabel.NB_CHITCHAT) == "fallback"
    assert get_domain(IntentLabel.HANDOFF_END) == "fallback"


def test_get_domain_sensitive_and_handoff() -> None:
    """risk/complain/transfer 域映射 (派发层与 business 同走 _handle_business)"""
    assert get_domain(IntentLabel.CARD_LOSS) == "risk"  # 旧 flat → card_loss_report
    assert get_domain(IntentLabel.CARD_LOSS_REPORT) == "risk"
    assert get_domain(IntentLabel.COMPLAINT) == "complain"  # 旧 flat → dispute_submit
    assert get_domain(IntentLabel.DISPUTE_SUBMIT) == "complain"
    assert get_domain(IntentLabel.TRANSFER_AGENT) == "transfer"
    assert get_domain(IntentLabel.RISK_FRAUD_REPORT) == "risk"


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

    # 旧 flat 值 → 归一化主名 (draft-0.3 §3.1)
    assert normalize_intent("bill_query") == IntentLabel.ACCOUNT_BILL_QUERY
    assert normalize_intent("card_loss") == IntentLabel.CARD_LOSS_REPORT
    assert normalize_intent("complaint") == IntentLabel.DISPUTE_SUBMIT
    assert normalize_intent("chitchat") == IntentLabel.NB_CHITCHAT
    assert normalize_intent("faq") == IntentLabel.FAQ_PRODUCT
    # 主名/identity 值原样返回
    assert normalize_intent("limit_query") == IntentLabel.LIMIT_QUERY
    assert normalize_intent("transfer_agent") == IntentLabel.TRANSFER_AGENT
    assert normalize_intent("account_bill_query") == IntentLabel.ACCOUNT_BILL_QUERY
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


# ── 标签映射从训练产物读取 (防 _CLASSES/训练 IDX 手工同步漂移) ──


class _FakeModelConfig:
    """桩对象: 仅带 config.id2label, 模拟 transformers 保存的标签映射 (键为字符串)."""

    def __init__(self, id2label: dict | None) -> None:
        self.config = type("C", (), {"id2label": id2label})()


def test_labels_from_model_config_reads_training_order() -> None:
    from lumio.services.common.bert_classifier import _CLASSES, _labels_from_model_config

    # 训练侧 id2label 只含 10 个训练类 (枚举现为 149+10 全集, 训练集仍是 10 类)
    classes = [c.value for c in _CLASSES]
    labels = _labels_from_model_config(_FakeModelConfig({str(i): c for i, c in enumerate(classes)}))
    assert labels == [IntentLabel(c) for c in classes]


def test_labels_from_model_config_falls_back_when_missing() -> None:
    from lumio.services.common.bert_classifier import _CLASSES, _labels_from_model_config

    assert _labels_from_model_config(_FakeModelConfig(None)) == _CLASSES
    assert _labels_from_model_config(type("M", (), {"config": None})()) == _CLASSES


def test_labels_from_model_config_falls_back_on_bad_labels() -> None:
    from lumio.services.common.bert_classifier import _CLASSES, _labels_from_model_config

    # 未知标签 → 回退
    assert _labels_from_model_config(_FakeModelConfig({"0": "not_a_real_intent"})) == _CLASSES
    # 标签集不全 (缺类) → 回退, 不静默用错 IDX
    partial = {str(i): c for i, c in enumerate(IntentLabel)}
    del partial["9"]
    assert _labels_from_model_config(_FakeModelConfig(partial)) == _CLASSES


# ── 温度来源优先级: 显式入参 > 训练产物 config.temperature ──


def test_resolve_temperature_explicit_wins() -> None:
    from lumio.services.common.bert_classifier import _resolve_temperature

    assert _resolve_temperature(0.8, 1.5) == 0.8
    assert _resolve_temperature(1.0, 1.5) == 1.0  # 显式 1.0 表示"禁用缩放", 尊重调用方


def test_resolve_temperature_from_config() -> None:
    from lumio.services.common.bert_classifier import _resolve_temperature

    assert _resolve_temperature(None, 1.35) == 1.35
    assert _resolve_temperature(None, "1.35") == 1.35  # json 数字/字符串兼容


def test_resolve_temperature_invalid_falls_back_to_none() -> None:
    from lumio.services.common.bert_classifier import _resolve_temperature

    assert _resolve_temperature(None, None) is None
    assert _resolve_temperature(None, 0.0) is None  # 非法温度 → 不缩放
    assert _resolve_temperature(None, -1.0) is None
    assert _resolve_temperature(None, "not_a_number") is None


# ── 多轮输入格式契约 (训练/推理分布一致性) ──


def test_bert_build_dialog_input_format_contract() -> None:
    """冻结 _build_dialog_input 的输出格式: 老→新顺序、段间无分隔、换行接当前句。

    这是 spike 脚本 _HISTORY_TPL 多轮合成样本必须对齐的契约 — 若此快照被改,
    必须同步更新 scripts/intent_classifier_spike.py 的 gen_history_samples,
    否则训练/推理输入格式再次漂移 (历史侧分布不一致)。
    """
    from lumio.services.common.bert_classifier import BertIntentClassifier

    out = BertIntentClassifier._build_dialog_input(
        "分一万二",
        [
            {"speaker": "customer", "content": "我想分期"},
            {"speaker": "bot", "content": "请问您想分期的金额是多少?"},
        ],
    )
    assert out == "用户:我想分期客服:请问您想分期的金额是多少?\n分一万二"


def test_bert_build_dialog_input_no_history_is_plain_text() -> None:
    """无历史时输出即当前句本身 (单句训练样本与运行时单句路径一致)."""
    from lumio.services.common.bert_classifier import BertIntentClassifier

    assert BertIntentClassifier._build_dialog_input("查账单", None) == "查账单"
    assert BertIntentClassifier._build_dialog_input("查账单", []) == "查账单"


# ── #7 LLM 分类缓存 (慢路径降本第一段) ──


def _llm_settings(enabled: bool = True, ttl: int = 60, max_entries: int = 8):
    from types import SimpleNamespace

    return SimpleNamespace(
        llm=SimpleNamespace(
            classify_cache_enabled=enabled,
            classify_cache_ttl_seconds=ttl,
            classify_cache_max_entries=max_entries,
            classify_timeout=3.0,
        )
    )


@pytest.mark.asyncio
async def test_llm_classify_cache_hit_skips_second_call() -> None:
    """相同输入在 TTL 内复用分类结果, LLM 只被调用一次."""
    from unittest.mock import patch

    llm = MagicMock()
    llm.classify = AsyncMock(
        return_value={"intent": "bill_query", "confidence": 0.8, "entities": [], "sentiment": "neutral"}
    )
    clf = LLMClassifier(llm)
    with patch("lumio.services.common.classifier.get_settings", return_value=_llm_settings()):
        r1 = await clf.classify("帮我查账单")
        r2 = await clf.classify("帮我查账单")
    assert r1[0].primary_intent == r2[0].primary_intent == IntentLabel.BILL_QUERY
    llm.classify.assert_awaited_once()


@pytest.mark.asyncio
async def test_llm_classify_cache_returns_deep_copy() -> None:
    """缓存返回深拷贝: 调用方对结果的突变不得污染后续命中."""
    from unittest.mock import patch

    llm = MagicMock()
    llm.classify = AsyncMock(
        return_value={"intent": "bill_query", "confidence": 0.8, "entities": [], "sentiment": "neutral"}
    )
    clf = LLMClassifier(llm)
    with patch("lumio.services.common.classifier.get_settings", return_value=_llm_settings()):
        first = await clf.classify("查账单")
        first[0].energy = -99.0  # 模拟上层透传 mutation
        first[1].append(Entity(entity_type="period", value="本月"))
        second = await clf.classify("查账单")
    assert second[0].energy is None
    assert second[1] == []


@pytest.mark.asyncio
async def test_llm_classify_cache_different_text_and_ttl_expiry() -> None:
    """不同文本不命中; TTL 过期后重新调用 LLM."""
    from unittest.mock import patch

    llm = MagicMock()
    llm.classify = AsyncMock(return_value={"intent": "faq", "confidence": 0.6, "entities": [], "sentiment": "neutral"})
    clf = LLMClassifier(llm)
    with patch("lumio.services.common.classifier.get_settings", return_value=_llm_settings(ttl=0)):
        await clf.classify("问题一")
        await clf.classify("问题一")  # TTL=0 → 视为过期, 重新调用
    assert llm.classify.await_count == 2

    llm.classify.reset_mock()
    clf2 = LLMClassifier(llm)  # 新实例: 隔离上一 phase 的缓存, 只验不同文本不命中
    with patch("lumio.services.common.classifier.get_settings", return_value=_llm_settings()):
        await clf2.classify("问题一")
        await clf2.classify("问题二")
    assert llm.classify.await_count == 2


@pytest.mark.asyncio
async def test_llm_classify_cache_disabled_and_failure_not_cached() -> None:
    """开关关闭不走缓存; 失败兜底不缓存 (下次重试仍调 LLM)."""
    from unittest.mock import patch

    llm = MagicMock()
    clf = LLMClassifier(llm)
    with patch("lumio.services.common.classifier.get_settings", return_value=_llm_settings(enabled=False)):
        llm.classify = AsyncMock(
            return_value={"intent": "faq", "confidence": 0.6, "entities": [], "sentiment": "neutral"}
        )
        await clf.classify("同一句")
        await clf.classify("同一句")
        assert llm.classify.await_count == 2

    llm.classify = AsyncMock(side_effect=TimeoutError("boom"))
    clf2 = LLMClassifier(llm)
    with patch("lumio.services.common.classifier.get_settings", return_value=_llm_settings()):
        # 2026-08-30: 失败改为上抛 (外层用 Fast Path 兜底), 仍验证失败不缓存
        with pytest.raises(TimeoutError):
            await clf2.classify("会失败的句子")
        with pytest.raises(TimeoutError):
            await clf2.classify("会失败的句子")
    assert llm.classify.await_count == 2  # 失败不缓存


@pytest.mark.asyncio
async def test_llm_classify_cache_lru_eviction() -> None:
    """缓存超过上限时淘汰最旧条目."""
    from unittest.mock import patch

    llm = MagicMock()
    llm.classify = AsyncMock(return_value={"intent": "faq", "confidence": 0.6, "entities": [], "sentiment": "neutral"})
    clf = LLMClassifier(llm)
    with patch("lumio.services.common.classifier.get_settings", return_value=_llm_settings(max_entries=2)):
        await clf.classify("句一")
        await clf.classify("句二")
        await clf.classify("句三")  # 淘汰句一
        await clf.classify("句一")  # 未命中 → 重调
    assert llm.classify.await_count == 4
    assert len(clf._cache) == 2


@pytest.mark.asyncio
async def test_slow_path_faq_does_not_override_business_fast_path() -> None:
    """P0 (会话 0681c635): LLM 慢路径兜底 FAQ@0.0 不应覆盖快路径已识别的业务意图.

    BERT 识别 installment_inquiry@0.5 (落在 0.3~0.7 中间区间), 慢路径超时兜底
    FAQ@0.0 -- 此前 FAQ@0.0 覆盖快路径, 噪声门按 low_confidence 误杀明确业务诉求.
    """
    fake = _fake_bert(IntentLabel.INSTALLMENT_INQUIRY, 0.5)
    mock_llm_classifier = MagicMock()
    mock_llm_classifier.classify = AsyncMock(
        return_value=(IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.0), [], SentimentLabel.NEUTRAL)
    )
    classifier = IntentClassifier(
        rule_classifier=RuleClassifier(), llm_classifier=mock_llm_classifier, fast_threshold=0.7, bert_classifier=fake
    )
    intent, _entities, _sentiment, source = await classifier.classify("我要办分期")

    assert intent.primary_intent == IntentLabel.INSTALLMENT_INQUIRY  # 回退快路径, 而非 FAQ
    assert source == "fallback"


@pytest.mark.asyncio
async def test_slow_path_lower_confidence_falls_back_to_fast_path() -> None:
    """P0: 慢路径置信低于快路径时, 信任快路径 (即便慢路径是另一个业务意图)."""
    fake = _fake_bert(IntentLabel.BILL_QUERY, 0.6)
    mock_llm_classifier = MagicMock()
    mock_llm_classifier.classify = AsyncMock(
        return_value=(IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.4), [], SentimentLabel.NEUTRAL)
    )
    classifier = IntentClassifier(
        rule_classifier=RuleClassifier(), llm_classifier=mock_llm_classifier, fast_threshold=0.7, bert_classifier=fake
    )
    intent, _entities, _sentiment, source = await classifier.classify("查一下我的账单")

    assert intent.primary_intent == IntentLabel.BILL_QUERY
    assert source == "fallback"


def test_parse_intent_chinese_alias() -> None:
    """LLM 输出中文意图 (如"办理分期") 经别名映射兜底, 而非解析失败退化为 FAQ."""
    from lumio.services.common.classifier import _parse_intent

    assert _parse_intent("办理分期") == IntentLabel.INSTALLMENT_INQUIRY
    assert _parse_intent("分期咨询") == IntentLabel.INSTALLMENT_INQUIRY
    assert _parse_intent("账单查询") == IntentLabel.BILL_QUERY
    assert _parse_intent("转人工") == IntentLabel.TRANSFER_AGENT
    # 枚举值优先
    assert _parse_intent("installment_inquiry") == IntentLabel.INSTALLMENT_INQUIRY
    # 未知中文 -> FAQ
    assert _parse_intent("胡言乱语") == IntentLabel.FAQ


# ── 办理词规则覆盖 (会话 48882b05 同型消歧) ──


@pytest.mark.asyncio
async def test_bert_limit_query_overridden_by_apply_rule() -> None:
    """BERT 旧标签空间把"我要提额"判 limit_query → 规则层 limit_apply_increase@0.96 覆盖"""
    fake = _fake_bert(IntentLabel.LIMIT_QUERY, 0.81)
    classifier = IntentClassifier(rule_classifier=RuleClassifier(), llm_classifier=None, bert_classifier=fake)
    intent, _entities, _sentiment, source = await classifier.classify("我要提额")

    assert intent.primary_intent == IntentLabel.LIMIT_APPLY_INCREASE
    assert intent.primary_confidence == 0.96
    assert source == "rule"
    # 审计留痕: BERT 原始判定保留在 fast_intent/fast_conf
    assert intent.fast_intent == IntentLabel.LIMIT_QUERY
    assert intent.fast_conf == 0.81


@pytest.mark.asyncio
async def test_bert_limit_query_decrease_overridden() -> None:
    fake = _fake_bert(IntentLabel.LIMIT_QUERY, 0.8)
    classifier = IntentClassifier(rule_classifier=RuleClassifier(), llm_classifier=None, bert_classifier=fake)
    intent, _entities, _sentiment, source = await classifier.classify("我想降额")

    assert intent.primary_intent == IntentLabel.LIMIT_APPLY_DECREASE
    assert source == "rule"


@pytest.mark.asyncio
async def test_non_apply_bert_result_not_overridden() -> None:
    """非办理词不被覆盖: BERT 判 limit_query@0.95 (纯查询句) 保持原样"""
    fake = _fake_bert(IntentLabel.LIMIT_QUERY, 0.95)
    classifier = IntentClassifier(rule_classifier=RuleClassifier(), llm_classifier=None, bert_classifier=fake)
    intent, _entities, _sentiment, source = await classifier.classify("我的可用额度是多少")

    assert intent.primary_intent == IntentLabel.LIMIT_QUERY
    assert source == "bert"


# ── 查询类意图规则覆盖 (一小时模拟 badcase 根治: 账单查询被 BERT 判 faq) ──


@pytest.mark.asyncio
async def test_bert_faq_overridden_by_query_rule() -> None:
    """BERT 把账单查询判成 faq 高置信 → 查询规则覆盖, 走工具链而非知识链"""
    fake = _fake_bert(IntentLabel.FAQ, 0.9)
    classifier = IntentClassifier(rule_classifier=RuleClassifier(), llm_classifier=None, bert_classifier=fake)
    intent, _entities, _sentiment, source = await classifier.classify("帮我查一下信用卡账单")

    assert intent.primary_intent == IntentLabel.BILL_QUERY
    assert intent.primary_confidence == 0.84
    assert source == "rule:query"
    assert intent.fast_intent == IntentLabel.FAQ  # 审计留痕


@pytest.mark.asyncio
async def test_consultive_bill_text_not_overridden() -> None:
    """咨询句 (含手续费/怎么) 不被查询规则覆盖 —— "账单分期手续费怎么算"是咨询不是查询"""
    fake = _fake_bert(IntentLabel.FAQ, 0.9)
    classifier = IntentClassifier(rule_classifier=RuleClassifier(), llm_classifier=None, bert_classifier=fake)
    intent, _entities, _sentiment, source = await classifier.classify("账单分期手续费怎么算")

    assert intent.primary_intent == IntentLabel.FAQ
    assert source == "bert"


@pytest.mark.asyncio
async def test_balance_query_overridden() -> None:
    fake = _fake_bert(IntentLabel.FAQ, 0.75)
    classifier = IntentClassifier(rule_classifier=RuleClassifier(), llm_classifier=None, bert_classifier=fake)
    intent, _entities, _sentiment, source = await classifier.classify("我的信用卡可用额度还有多少")

    assert intent.primary_intent == IntentLabel.LIMIT_QUERY
    assert source == "rule:query"


# ── 预处理乱序纠错 (layer_1 坏例根治: "数人字民币") ──


def test_fix_adjacent_typos_swapped_word() -> None:
    from lumio.services.common.classifier import fix_adjacent_typos

    assert fix_adjacent_typos("怎么给数人字民币硬钱包充值呢") == "怎么给数字人民币硬钱包充值呢"
    assert fix_adjacent_typos("我要查我的账单账")  # 不在词表形态的不误改
    assert fix_adjacent_typos("信用卡挂失") == "信用卡挂失"  # 正常输入零改动
    assert fix_adjacent_typos("") == ""


def test_fix_adjacent_typos_normal_input_untouched() -> None:
    from lumio.services.common.classifier import fix_adjacent_typos

    for normal in ("帮我查一下信用卡账单", "数字人民币硬钱包怎么充值", "我的卡丢了要挂失"):
        assert fix_adjacent_typos(normal) == normal


def test_wallet_stolen_overridden_to_card_loss() -> None:
    """挂失补词覆盖: '钱包被偷'必须判挂失而非 faq (错过挂失黄金时间是 P0)"""
    fake = _fake_bert(IntentLabel.FAQ, 0.73)
    classifier = IntentClassifier(rule_classifier=RuleClassifier(), llm_classifier=None, bert_classifier=fake)
    intent, _e, _s, source = asyncio.run(classifier.classify("钱包被偷了, 卡也在里面"))
    assert intent.primary_intent == IntentLabel.CARD_LOSS
    assert source == "rule"


def test_colloquial_limit_query_variants() -> None:
    """口语变体额度查询 (长对话场景暴露): '还能刷多少'应判额度而非被拒"""
    fake = _fake_bert(IntentLabel.FAQ, 0.74)
    classifier = IntentClassifier(rule_classifier=RuleClassifier(), llm_classifier=None, bert_classifier=fake)
    intent, _e, _s, _source = asyncio.run(classifier.classify("请问一下 现在卡里还能刷多少"))
    assert intent.primary_intent == IntentLabel.LIMIT_QUERY
    assert intent.primary_confidence == 0.95


# ── 闲聊/噪声置信封顶 (会话 8700a2ea: "锄禾日当午"→chitchat@0.70 直通知识链) ──


@pytest.mark.asyncio
async def test_dual_path_chitchat_conf_capped() -> None:
    """LLM 慢路径自评 chitchat@0.70 → 出口封顶 0.29 (低于低置信地板)"""
    rule = RuleClassifier()
    mock_llm_classifier = MagicMock()
    mock_llm_classifier.classify = AsyncMock(
        return_value=(
            IntentResult(primary_intent=IntentLabel.NB_CHITCHAT, primary_confidence=0.70),
            [],
            SentimentLabel.NEUTRAL,
        )
    )

    classifier = IntentClassifier(rule_classifier=rule, llm_classifier=mock_llm_classifier, fast_threshold=0.7)
    intent, _entities, _sentiment, source = await classifier.classify("锄禾日当午")

    assert intent.primary_intent == IntentLabel.NB_CHITCHAT
    assert intent.primary_confidence <= 0.29
    # 快慢分歧证据保留原始值
    assert intent.fast_conf is None or intent.fast_conf >= 0.0
    assert source == "llm"


@pytest.mark.asyncio
async def test_fast_path_chitchat_conf_capped() -> None:
    """BERT 快路径 chitchat@0.75 直接放行时同样封顶"""
    fake_bert = MagicMock()
    fake_bert.classify = AsyncMock(
        return_value=IntentResult(primary_intent=IntentLabel.NB_CHITCHAT, primary_confidence=0.75)
    )

    classifier = IntentClassifier(
        rule_classifier=RuleClassifier(),
        llm_classifier=None,
        fast_threshold=0.7,
        bert_classifier=fake_bert,
    )
    intent, _entities, _sentiment, source = await classifier.classify("哈哈哈")

    assert intent.primary_confidence <= 0.29


@pytest.mark.asyncio
async def test_business_intent_conf_not_capped() -> None:
    """业务意图置信不受封顶影响"""
    rule = RuleClassifier()
    mock_llm_classifier = MagicMock()
    mock_llm_classifier.classify = AsyncMock(
        return_value=(
            IntentResult(primary_intent=IntentLabel.BILL_QUERY, primary_confidence=0.88),
            [],
            SentimentLabel.NEUTRAL,
        )
    )

    classifier = IntentClassifier(rule_classifier=rule, llm_classifier=mock_llm_classifier, fast_threshold=0.7)
    # 用不命中规则的输入, 让慢路径 LLM 结果 (bill_query@0.88) 成为最终结果
    intent, _entities, _sentiment, _source = await classifier.classify("这个怎么弄")

    assert intent.primary_intent == IntentLabel.BILL_QUERY
    assert intent.primary_confidence == 0.88


@pytest.mark.asyncio
async def test_chitchat_legacy_alias_conf_capped() -> None:
    """旧 flat 别名 IntentLabel.CHITCHAT ("chitchat") 同样封顶

    E2E 实测 (会话 8700a2ea 复盘轮): BERT/LLM 直接构造别名对象, 不经归一化,
    首版封顶集合只含 NB_CHITCHAT/NB_NOISE 时漏封 — poll 仍显示 chitchat@0.7。
    """
    rule = RuleClassifier()
    mock_llm_classifier = MagicMock()
    mock_llm_classifier.classify = AsyncMock(
        return_value=(
            IntentResult(primary_intent=IntentLabel.CHITCHAT, primary_confidence=0.70),
            [],
            SentimentLabel.NEUTRAL,
        )
    )

    classifier = IntentClassifier(rule_classifier=rule, llm_classifier=mock_llm_classifier, fast_threshold=0.7)
    intent, _entities, _sentiment, _source = await classifier.classify("锄禾日当午")

    assert intent.primary_confidence <= 0.29
