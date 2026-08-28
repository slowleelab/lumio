"""P2 中英多信号噪声闸测试: 惊讶度机制 + InputGate 多信号投票.

惊讶度机制: 用足够大的注入语料验证"OOV/乱码高惊讶、正常词低惊讶、中英混写任一段正常即放行"
这三个不变量 — 机制本身是可靠的。默认种子语料是小样本占位, 需 P3 数据回流标定(业务侧),
故这里的机制测试显式注入语料, 不断言默认种子。
InputGate: 用受控 FakeScorer 精确断言投票逻辑(硬否决/佐证/容错), 与惊讶度模型质量解耦。
"""

from __future__ import annotations

import pytest

from lumio.services.bot.input_gate import InputGate
from lumio.services.common.surprisal import SegmentScore, SurprisalScorer, SurprisalVerdict

# ── 惊讶度机制 (注入足够大语料, 验证不变量) ──


@pytest.fixture(scope="module")
def fitted() -> SurprisalScorer:
    latin = ["hello bill refund visa credit bank account balance limit thanks please transfer service card"]
    cjk = ["你好信用卡账单积分兑换额度还款利息费用多少"]
    return SurprisalScorer(
        cjk_samples=cjk * 200,
        latin_samples=latin * 200,
        normal_threshold=10.0,
    )


def test_surprisal_normal_not_abnormal(fitted: SurprisalScorer) -> None:
    """正常业务词/句 → 不高惊讶 (any_normal → abnormal=False)."""
    for t in ["bill", "refund", "hello", "信用卡", "积分兑换", "你好账单"]:
        assert fitted.evaluate(t).abnormal is False


def test_surprisal_noise_abnormal(fitted: SurprisalScorer) -> None:
    """OOV 乱码/纯键盘误触 → 高惊讶, abnormal=True."""
    assert fitted.evaluate("hjfw").abnormal is True
    assert fitted.evaluate("zzzz").abnormal is True
    assert fitted.evaluate("臼杷扡尢").abnormal is True


def test_surprisal_mixed_any_normal_passes(fitted: SurprisalScorer) -> None:
    """中英混写任一段正常 → 整体不高惊讶 (不误杀 refund到没)."""
    assert fitted.evaluate("refund到没").abnormal is False


def test_surprisal_abnormal_all_segments() -> None:
    """全部段都异常才算 abnormal; 多个异常段分别记账."""
    scorer = SurprisalScorer(
        cjk_samples=["你知道信用卡"] * 100,
        latin_samples=["hello bill"] * 100,
        normal_threshold=5.0,
    )
    v = scorer.evaluate("hjfw 臼杷")
    assert v.abnormal is True
    assert len(v.segments) >= 1


def test_split_segments_ignores_digits_and_punct() -> None:
    from lumio.services.common.surprisal import split_segments

    # 纯数字/标点 → 无中英段
    assert split_segments("8888 22，###") == []
    # 中文+英文+数字被切成两个段
    segs = split_segments("refund 到没 8888")
    scripts = [s for s, _ in segs]
    assert scripts == ["latin", "cjk"]


# ── InputGate 多信号投票 (受控 scorer, 断言决策逻辑) ──


class _FakeScorer:
    """受控惊讶度 scorer: 由测试指定 abnormal, 隔离模型质量."""

    def __init__(self, abnormal: bool) -> None:
        self._abnormal = abnormal

    def evaluate(self, text: str) -> SurprisalVerdict:
        seg = SegmentScore(script="latin", text=text or "x", avg_surprisal=9.0, char_count=len(text))
        return SurprisalVerdict(segments=[seg], any_normal=not self._abnormal)


def _gate(abnormal: bool) -> InputGate:
    return InputGate(scorer=_FakeScorer(abnormal))


def test_gate_reply_vetoes_even_when_surprisal_abnormal() -> None:
    """硬否决: 真实回话 → 即便惊讶度异常也放行 (防"上轮问卡号, 本句答 4444")."""
    g = _gate(abnormal=True)
    v = g.evaluate(text="嗯对", is_replying=True, low_conf=False, noise_shape=False)
    assert v.decision == "pass"
    assert v.signals.get("veto") == "reply_or_slot"


def test_gate_entity_slot_vetoes() -> None:
    """实体填缺槽 → 放行 (防带语义数字/实体被惊讶度误杀)."""
    g = _gate(abnormal=True)
    v = g.evaluate(text="22元", has_entity_slot=True, low_conf=False, noise_shape=False)
    assert v.decision == "pass"


def test_gate_surprisal_alone_never_blocks() -> None:
    """单独惊讶度异常 → 不拦 (多信号投票, 保守)."""
    g = _gate(abnormal=True)
    v = g.evaluate(text="臼杷扡尢", is_replying=False, low_conf=False, noise_shape=False)
    assert v.decision == "pass"


def test_gate_blocks_when_corroborated() -> None:
    """惊讶度异常 + 低置信 → 拦; +energy ambiguous → 拦."""
    g = _gate(abnormal=True)
    assert g.evaluate(text="臼杷", low_conf=True).decision == "block"
    assert g.evaluate(text="臼杷", energy_verdict="unknown").decision == "block"
    assert g.evaluate(text="臼杷", energy_verdict="ambiguous").decision == "block"


def test_gate_pass_when_not_abnormal_even_if_low_conf() -> None:
    """非惊讶度异常: 其它信号不叠加, 走既有路径(InputGate 不强加拦截)."""
    g = _gate(abnormal=False)
    assert g.evaluate(text="我想查账单", low_conf=True, noise_shape=False).decision == "pass"


def test_gate_pass_noise_shape_but_not_abnormal() -> None:
    """内容噪声形状由 P0 既有闸处理; 惊讶度非异常时 InputGate 不重复拦."""
    g = _gate(abnormal=False)
    assert g.evaluate(text="4444", noise_shape=True).decision == "pass"


def test_gate_scorer_error_passes_fault_tolerant() -> None:
    """scorer 抛异常 → 放行 (容错, 不让闸门故障断业务)."""

    class BoomScorer:
        def evaluate(self, text: str) -> SurprisalVerdict:
            raise RuntimeError("boom")

    g = InputGate(scorer=BoomScorer())
    v = g.evaluate(text="你好", low_conf=True)
    assert v.decision == "pass"
    assert v.signals.get("surprisal") == "error"


# ── bot_agent 噪声门接线 (P2 InputGate 挂载) ──


def test_gate_default_off_has_no_effect() -> None:
    """noise_gate_enabled 默认关 → 既有无闸逻辑不受影响."""
    # 直接构造真实 InputGate, 验证默认构造可用 + evaluate 不抛
    g = InputGate()
    v = g.evaluate(text="你好")
    assert v.decision == "pass"
