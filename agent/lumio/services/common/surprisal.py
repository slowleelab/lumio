"""字符级惊讶度 OOD 信号 (P2) — 中英分模型 + 脚本切分的统计异常判定。

对键盘误触/乱码做"字符统计异常"检测: 正常中文句子的单字+双字分布平稳, 平均惊讶度低;
键盘乱敲(hjfw / asdkjhf)在字母级分布上是低概率组合, 惊讶度高。中/英脚本分开建模，
输入按 Unicode 切分中(单字)、英(字母)段, 各段用对应模型打分 —— 任一段"正常"即整体
放行, 避免误杀 Visa/refund/ATM 等英文词与 "refund 到没" 中英混写。

已知边界(不代表本模块单独裁决):
- 重复数字 "444444" 惊讶度过低会漏抓 → 由槽位/实体/内容噪声信号兜住;
- 带语义数字 "22元"/卡号不能当噪声 → 由槽位确认与实体信号兜住。
故本模块只输出"惊讶度异常"这一个信号, 交由 InputGate 多信号投票, 不作单独裁决。

模型的 n-gram 计数来自随服携带的小型种子语料(可经 P3 数据回流后重训覆盖)。
"""

from __future__ import annotations

import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# 中文单字段 (含扩展区); 英文按字母(拉丁字母段)分别建模
_CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf]")
_LATIN_RE = re.compile(r"[A-Za-z]")


@dataclass
class SegmentScore:
    """一段文本的惊讶度打分."""

    script: str  # "cjk" | "latin"
    text: str
    avg_surprisal: float  # 每字符平均惊讶度 (bits), 越低越正常
    char_count: int


@dataclass
class SurprisalVerdict:
    """整条输入的多段惊讶度结果."""

    segments: list[SegmentScore] = field(default_factory=list)
    any_normal: bool = False  # 任一段"正常"(低惊讶) → 整体放行(防误杀混写)

    @property
    def abnormal(self) -> bool:
        """全部段都异常(高惊讶) 才视为可疑; 无段(纯数字/符号/空白)不算异常."""
        return bool(self.segments) and not self.any_normal


class _CharNgramModel:
    """单字/字母 + 双字/双字母 n-gram 计数模型 (Laplace 平滑)."""

    def __init__(self, units: str) -> None:
        self._units = units  # "char" (中文单字) | "letter" (英文小写字母)
        self._uni: Counter[str] = Counter()
        self._bi: Counter[str] = Counter()
        self._vocab_total = 0
        self._bi_total = 0

    def fit(self, samples: list[str]) -> None:
        for s in samples:
            toks = self._tokenize(s)
            for i, t in enumerate(toks):
                self._uni[t] += 1
                if i > 0:
                    self._bi[f"{toks[i-1]}{t}"] += 1
        self._vocab_total = sum(self._uni.values())
        self._bi_total = sum(self._bi.values())

    def _tokenize(self, text: str) -> list[str]:
        if self._units == "letter":
            return list(text.lower())
        return list(text)

    def avg_surprisal(self, text: str) -> float:
        """每字符平均惊讶度 (Laplace 平滑, bigram 优先回退 unigram)."""
        toks = self._tokenize(text)
        if not toks:
            return 0.0
        # 平滑: 归一为概率质量上限(封顶), 避免 vocab 外 token 拖爆惊讶度
        norm = max(1, self._vocab_total + len(self._uni))
        surprise_sum = 0.0
        prev = ""
        for i, t in enumerate(toks):
            if prev and i > 0:
                big = f"{prev}{t}"
                # P(t | prev) = count(prev,t) / count(prev), 平滑 +1
                prev_count = self._uni[prev]
                p_cond = (self._bi[big] + 1) / (prev_count + norm)
            else:
                p_cond = (self._uni[t] + 1) / norm
            surprise_sum += -math.log2(p_cond)
            prev = t
        return surprise_sum / len(toks)


def split_segments(text: str) -> list[tuple[str, str]]:
    """把文本切分为连续的 [cjk 单字串] 与 [latin 字母串] 段 (忽略数字/标点/空白).

    中英混写会被切成多个段; 每段交给对应脚本模型打分。
    """
    segments: list[tuple[str, str]] = []
    buf: list[str] = []
    cur: str | None = None
    for ch in text:
        kind = "cjk" if _CJK_RE.match(ch) else "latin" if _LATIN_RE.match(ch) else None
        if kind is None:
            # 非中/英字符(数字/标点/空白): 结束当前段, 不参与计数但断开连接
            if cur is not None and buf:
                segments.append((cur, "".join(buf)))
                buf = []
                cur = None
            continue
        if cur is not None and kind != cur:
            if buf:
                segments.append((cur, "".join(buf)))
            buf = []
        cur = kind
        buf.append(ch)
    if cur is not None and buf:
        segments.append((cur, "".join(buf)))
    return segments


# ── 随服携带的种子语料: 支撑中/英惊讶度模型的初始频率分布 ──
# 仅作默认基线; P3 数据回流后可用真实语料重建模型覆盖.
_DEFAULT_CJK_SAMPLES = [
    "你好请问有什么可以帮您",
    "我想查一下信用卡账单",
    "本期账单应还金额是多少",
    "怎么申请提高临时额度",
    "我的信用卡可以分期吗",
    "积分能兑换什么礼品",
    "这张卡的刷卡手续费怎么收",
    "还款日是什么时候",
    "我上次消费的明细帮我看看",
    "客服你好我想咨询年费",
    "最低还款额是怎么计算的",
    "账单日和还款日分别是哪天",
    "我的卡丢失了需要挂失",
    "我要投诉客服服务态度",
    "请帮我转接人工客服",
    "谢谢没有其他问题了",
    "请问你们客服热线是多少",
    "我想了解积分兑换规则",
    "信用卡到期了怎么换新卡",
    "这次的退款什么时候到账",
]

_DEFAULT_LATIN_SAMPLES = [
    "hello hi yes no thanks bill refund transfer visa atm credit card bank account balance",
    "limit reward points score query charge fee monthly annual installment payment due date",
    "loss report complaint service agent human please help how what when why amount",
]


class SurprisalScorer:
    """中/英双分子级惊讶度评分器(懒加载种子模型)."""

    def __init__(
        self,
        cjk_samples: list[str] | None = None,
        latin_samples: list[str] | None = None,
        *,
        normal_threshold: float = 7.0,
    ) -> None:
        self._cjk = _CharNgramModel("char")
        self._latin = _CharNgramModel("letter")
        self._cjk.fit(cjk_samples or _DEFAULT_CJK_SAMPLES)
        self._latin.fit(latin_samples or _DEFAULT_LATIN_SAMPLES)
        # 单段视为"正常"的惊讶度上限(按每字符平均 bits). 需部署时验证集标定.
        self._normal_threshold = normal_threshold

    def evaluate(self, text: str) -> SurprisalVerdict:
        """对输入做多段惊讶度评估. 自身异常/无中英段 → 返回空判定, 由调用方兜底."""
        if not text or not text.strip():
            return SurprisalVerdict()
        v = SurprisalVerdict()
        try:
            for script, seg in split_segments(text):
                model = self._cjk if script == "cjk" else self._latin
                score = model.avg_surprisal(seg)
                v.segments.append(
                    SegmentScore(script=script, text=seg, avg_surprisal=round(score, 4), char_count=len(seg))
                )
                if score <= self._normal_threshold:
                    v.any_normal = True
        except Exception:
            logger.warning("惊讶度评估异常, 返回空判定(不阻断): %r", text[:40], exc_info=True)
            return SurprisalVerdict()
        return v


# 模块级单例: 供依赖注入/热路径复用; 测试用独立实例
_default_scorer: SurprisalScorer | None = None


def default_surprisal_scorer() -> SurprisalScorer:
    global _default_scorer
    if _default_scorer is None:
        _default_scorer = SurprisalScorer()
    return _default_scorer


def reset_default_surprisal() -> None:
    global _default_scorer
    _default_scorer = None
