"""闭环 P2 评估/归因 (AttributionEngine)

对 P1 感知缝采样的 classifier_sample 进行第二步加工:
1. 多头投票一致性 — 规则 vs BERT vs 最终(LLM) 是否一致; 若不 一致 → 分类层嫌疑.
2. 结果弱标签(Outcome) — 从会话状态读取"是否转人工/主动求助/同题重复/低置信连击",
   判断该样本最终是否得到解决 (用户是否仍不满).
3. 四层根因定位 — 把每个失败样本归因到 classification / retrieval / generation /
   guardrail 之一, 带判定证据链 + 置信度.

设计约束:
- attribute() 是纯函数 (无 DB / 无 Redis), 输入 AttribSample → AttributionVerdict,
  便于单测; 分派层归属逻辑全部收敛在此.
- 诚实边界: P1 只捕获了"分类层"信号. 因此本引擎能**精确**归因分类层 (分歧/贴边/慢路径);
  对"分类置信但用户仍未解决"的样本, 只能判定为 retrieval/generation 的**候选**
  (knowledge 意图→retrieval, 其余→generation), 并在 summary 里标注需 P3 补捕获
  RAG/生成下游信号才能定罪. 不伪造断层置信.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from lumio.services.common.classifier import get_domain
from lumio.shared.logger import get_logger
from lumio.shared.models import IntentLabel

logger = get_logger(__name__)

# 知识域意图 → RAG 检索支撑; 业务域 → 工具/生成支撑
_KNOWLEDGE_DOMAIN = "knowledge"


class EvalLayer(StrEnum):
    """四层根因. UNASSIGNED = 当前信号不足以归因 (需 P3 捕获下游信号)."""

    CLASSIFICATION = "classification"  # 意图分类层 (规则/BERT/LLM 分歧、贴边、兜底)
    RETRIEVAL = "retrieval"  # 知识检索层 (分类自信但知识没召回/答非所问)
    GENERATION = "generation"  # 生成层 (组织话术不佳)
    GUARDRAIL = "guardrail"  # 输入护栏/合规拦截层
    UNASSIGNED = "unassigned"


class VerdictType(StrEnum):
    HEALTHY = "healthy"  # 无异常 (采样由 ambient 触发)
    UNCERTAIN = "uncertain"  # 分类不确定 (建议监控, 未必是失败)
    FAILURE = "failure"  # 已判定失败 (有明确的分类断层 或 分类自信但用户未解决)
    PENDING = "pending"  # 存疑, 需人工/下游信号复核


def _safe_domain(intent_str: str) -> str:
    """把意图字符串安全转为 IntentLabel 后取所属域; 未知 → fallback."""
    try:
        return get_domain(IntentLabel(intent_str))
    except ValueError:
        return "fallback"


@dataclass
class AttribSample:
    """一次感知样本 + 结果弱标签. 字段对齐 classifier_sample 行 + 会话推导."""

    sample_id: str = ""
    text: str = ""
    session_id: str | None = None
    customer_id: str | None = None
    fast_source: str = "rule"  # bert | rule
    fast_intent: str = ""
    fast_confidence: float = 0.0
    rule_intent: str | None = None
    final_source: str = "fast"  # fast | llm | fallback
    final_intent: str = ""
    final_confidence: float = 0.0
    margin: float = 0.0
    divergence: bool = False
    reasons: list[str] = field(default_factory=list)
    created_at: datetime | None = None

    # ── 结果弱标签 (来自会话状态, 由 enrichment 填充) ──
    transferred: bool = False  # 会话最终转人工
    human_request_score: int = 0  # 用户主动求助次数
    repeated: bool = False  # 同题/同意图重复出现
    low_confidence_streak: int = 0  # 连续低置信轮数


@dataclass
class AttributionVerdict:
    sample_id: str
    layer: EvalLayer
    verdict: VerdictType
    intent: str
    evidences: list[str] = field(default_factory=list)
    re_rank: int = 0  # 负向权: 越大越值得优先回流/人工复核

    def to_dict(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "layer": self.layer.value,
            "verdict": self.verdict.value,
            "intent": self.intent,
            "evidences": self.evidences,
            "re_rank": self.re_rank,
        }


class AttributeEngine:
    """归因引擎. attribute() 为纯判定, 供单测直接调用; 汇总/落库在 admin 侧."""

    def __init__(self, margin_band: float = 0.15) -> None:
        self._margin_band = margin_band

    def attribute(self, s: AttribSample) -> AttributionVerdict:
        """对单个样本做四层归因. 纯逻辑, 无 IO."""
        evidences: list[str] = []

        # ── 守卫: 护栏层 ──
        # P1 采样不携带护栏命中位; 若未来补上 crisis/guard 字段再启用.
        # 预留: 不从分类样本伪造置信.

        # ── 分类层信号 (精确归因) ──
        classification_hit = False
        if s.divergence:
            classification_hit = True
            evidences.append(f"规则({s.rule_intent}) ≠ 快路径({s.fast_intent}) 分歧")
        if s.final_source in ("llm", "fallback"):
            classification_hit = True
            evidences.append(f"走慢路径/兜底 (source={s.final_source})")
        if s.margin < self._margin_band:
            evidences.append(f"贴近决策边界 (margin={s.margin:.2f})")

        # ── 结果弱标签 ──
        outcome_negative = s.transferred or s.human_request_score > 0 or s.repeated
        if classification_hit:
            # 分类断层是根因 (无论结果) → 归分类层
            verdict = VerdictType.FAILURE
            layer = EvalLayer.CLASSIFICATION
            rank = 10
            if outcome_negative:
                rank += 5
                evidences.append("结果未解决 (转人工/求助/重复)")
            return AttributionVerdict(s.sample_id, layer, verdict, s.final_intent, evidences, rank)

        if s.final_source == "fast" and outcome_negative:
            # 分类自信 → 结果未解决 → retrieval/generation 候选
            layer, msg = (
                (EvalLayer.RETRIEVAL, "检索召回不足 (待 P3 补 RAG 信号定罪)")
                if _safe_domain(s.final_intent) == _KNOWLEDGE_DOMAIN
                else (EvalLayer.GENERATION, "生成/工具编排不佳 (待 P3 补下游信号定罪)")
            )
            evidences.append(f"分类自信({s.final_confidence:.2f})但结果未解决")
            evidences.append(f"候选: {msg}")
            return AttributionVerdict(s.sample_id, layer, VerdictType.PENDING, s.final_intent, evidences, 6)

        # 分类自信且结果解决: 贴近边界/中低置信 → 存疑; 高置信无信号 → 正常
        if s.margin < self._margin_band or s.final_confidence < 0.9 or s.reasons:
            verdict = VerdictType.UNCERTAIN
            rank = 1
        else:
            verdict = VerdictType.HEALTHY
            rank = 0
        return AttributionVerdict(s.sample_id, EvalLayer.UNASSIGNED, verdict, s.final_intent, evidences, rank)

    def root_cause_summary(self, verdicts: list[AttributionVerdict]) -> dict[str, Any]:
        """聚合: 分层失败数 + 需人工复核的 top 样本 (按 re_rank 降序)."""
        by_layer: dict[str, int] = {}
        verdict_counts: dict[str, int] = {}
        for v in verdicts:
            by_layer[v.layer.value] = by_layer.get(v.layer.value, 0) + 1
            verdict_counts[v.verdict.value] = verdict_counts.get(v.verdict.value, 0) + 1
        top = sorted(
            [v for v in verdicts if v.verdict != VerdictType.HEALTHY],
            key=lambda v: (-v.re_rank, v.sample_id),
        )
        return {
            "total": len(verdicts),
            "by_layer": by_layer,
            "by_verdict": verdict_counts,
            "actionable": [v.to_dict() for v in top[:20]],
        }


async def enrich_sessions(redis: Any, sample_ids: list[str], session_ids: list[str]) -> dict[str, dict[str, Any]]:
    """从 Redis 加载会话, 推导结果弱标签, 按 session_id 返回.

    best-effort: 会话已过期/Redis 不可用 → 返回空标签集 (归因退化为纯分类层判定).
    """
    from lumio.services.common.session import SessionManager

    out: dict[str, dict[str, Any]] = {}
    if redis is None:
        return out
    sm = SessionManager(redis)
    for sid in dict.fromkeys(session_ids):
        try:
            st = await sm.get_session(sid)
        except Exception:
            continue
        if st is None:
            continue
        flag = {
            "transferred": bool(st.transfer_reason)
            or (st.current_phase is not None and st.current_phase.value == "assist"),
            "human_request_score": int(st.human_request_score or 0),
            "repeated": _has_repeated_intent(st),
            "low_confidence_streak": int(getattr(st, "low_confidence_streak", 0) or 0),
        }
        out[sid] = flag
    return out


def _has_repeated_intent(st: Any) -> bool:
    """同意图在 intent_stack 中重复出现 (≥2) → 用户绕圈提问."""
    stack = getattr(st, "intent_stack", []) or []
    from collections import Counter

    return any(c >= 2 for c in Counter(v.value if hasattr(v, "value") else str(v) for v in stack).values())


async def attribute_recent(
    *,
    db_session_factory: Any,
    redis: Any,
    window_days: int = 7,
    limit: int = 200,
    margin_band: float = 0.15,
) -> dict[str, Any]:
    """查询最近采样并归因: 结果柱状 + 可操作的失败样本 top."""
    from sqlalchemy import select

    from lumio.shared.orm_models import ClassifierSample

    engine = AttributeEngine(margin_band=margin_band)
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    async with db_session_factory() as session:
        stmt = (
            select(ClassifierSample)
            .where(ClassifierSample.created_at >= cutoff)
            .order_by(ClassifierSample.created_at.desc())
            .limit(limit)
        )
        rows = (await session.execute(stmt)).scalars().all()

    samples: list[AttribSample] = []
    session_ids = [r.session_id for r in rows if r.session_id]
    outcome_map = await enrich_sessions(redis, [], session_ids)
    for r in rows:
        s = AttribSample(
            sample_id=str(r.id),
            text=r.text,
            session_id=r.session_id,
            customer_id=r.customer_id,
            fast_source=r.fast_source,
            fast_intent=r.fast_intent,
            fast_confidence=r.fast_confidence,
            rule_intent=r.rule_intent,
            final_source=r.final_source,
            final_intent=r.final_intent,
            final_confidence=r.final_confidence,
            margin=r.margin,
            divergence=r.divergence,
            reasons=list(r.reasons or []),
            created_at=r.created_at,
        )
        flag = outcome_map.get(r.session_id, {}) if r.session_id else {}
        s.transferred = bool(flag.get("transferred", False))
        s.human_request_score = int(flag.get("human_request_score", 0))
        s.repeated = bool(flag.get("repeated", False))
        s.low_confidence_streak = int(flag.get("low_confidence_streak", 0))
        samples.append(s)

    verdicts = [engine.attribute(s) for s in samples]
    return engine.root_cause_summary(verdicts)
