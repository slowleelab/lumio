"""闭环 P1 感知缝 (TrapCollector)

在意图分类器上注入"单点接缝": 每次 classify 完成时, 由采样器判断本次结果是否
值得落库为感知样本 (failure/uncertainty 样本). 业务代码 (bot_agent) 完全不感知
采样逻辑 —— 它以 contextvar 提供会话/客户归属, 其余全部由本模块被动完成.

采样触发条件 (or):
- final_source in (llm, fallback): 走了慢路径/兜底 → 高不确定性样本
- abs(final_confidence - threshold) < band: 贴近决策边界 (margin/bound sampling)
- rule vs BERT 分歧 (divergence): 规则与 BERT 给出了不同主意图
- ambient: 以固定小概率随机采样任意结果, 打破"只采样异常"的选择偏差 (漂移可检测)

生产约束:
- PII 打码: 文本入库前 mask (手机/身份证/卡号保留后四位), 防明文落库
- GDPR: customer_id 保留原始值, 支持 gdpr_delete 客户级批量删除
- 有界留存: purge_older_than() 按 retention 清档; aggregate() 提供漂移聚合
- 后台落库: asyncio.create_task 持有引用防 GC; 写失败仅告警不打断主链路
"""
from __future__ import annotations

import asyncio
import contextvars
import random
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from lumio.shared.logger import get_logger

logger = get_logger(__name__)

# ── PII 打码 ────────────────────────────────────────────────────────────────
# 单次匹配最大数字段 (手机11 / 身份证15·18 / 卡号13-19), 再按类型识别.
# 用单一 pass 而非链式 sub, 避免"手机号模式在长身份证串内误匹配/身份证在卡号开头漏首字".
_DIGIT_RUN = re.compile(r"(?<!\d)([Xx\d]{11,19})(?![\dXx])")


def _mask_run(m: re.Match[str]) -> str:
    raw = m.group(1)
    # 手机号: 11 位且 1[3-9] 开头
    if re.fullmatch(r"1[3-9]\d{9}", raw):
        return "****" + raw[-4:]
    # 身份证: 15 位 或 18 位 (17 数字 + 校验码 [0-9Xx])
    if re.fullmatch(r"\d{15}", raw) or re.fullmatch(r"\d{17}[\dXx]", raw):
        return "****" + raw[-4:]
    # 银行卡/信用卡: 13~19 位数字串
    if re.fullmatch(r"\d{13,19}", raw):
        return "****" + raw[-4:]
    return raw


def mask_pii(text: str) -> str:
    """对文本内 PII (手机号/身份证/卡号) 打码, 仅保留后四位. 其余原地保留."""
    if not text:
        return text
    return _DIGIT_RUN.sub(_mask_run, text)


# ── 归属上下文 (业务代码通过 contextvar 提供, 采样器被动读取) ───────────────
_TrapCtx = tuple[str | None, str | None]  # (session_id, customer_id)
_trap_context: contextvars.ContextVar[_TrapCtx] = contextvars.ContextVar(
    "trap_context", default=(None, None)
)


@dataclass
class TrapContextToken:
    """上下文 set/reset 令牌, 便于业务代码 with 语句对齐释放. 此处取名为 token 以贴合
    contextvars.Token 语义, 但不依赖其类型. """

    token: contextvars.Token[_TrapCtx]


def set_trap_context(session_id: str | None, customer_id: str | None) -> TrapContextToken:
    return TrapContextToken(_trap_context.set((session_id, customer_id)))


def reset_trap_context(context_token: TrapContextToken) -> None:
    _trap_context.reset(context_token.token)


# ── 采样记录 ─────────────────────────────────────────────────────────────────
@dataclass
class TrapRecord:
    """一次分类结果的感知快照 (构建于 classify 内部, 同步完成, 含全部落库字段)."""

    text: str
    fast_source: str  # bert | rule
    fast_intent: str
    fast_confidence: float
    rule_intent: str | None  # 仅 BERT 快路径时填充 (分歧检测)
    final_source: str  # fast | llm | fallback
    final_intent: str
    final_confidence: float
    margin: float  # abs(final_confidence - threshold)
    divergence: bool = False
    reasons: list[str] = field(default_factory=list)
    session_id: str | None = None
    customer_id: str | None = None

    @property
    def masked_text(self) -> str:
        return mask_pii(self.text)

    def to_row(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "customer_id": self.customer_id,
            "text": self.masked_text,
            "fast_source": self.fast_source,
            "fast_intent": self.fast_intent,
            "fast_confidence": self.fast_confidence,
            "rule_intent": self.rule_intent,
            "final_source": self.final_source,
            "final_intent": self.final_intent,
            "final_confidence": self.final_confidence,
            "margin": self.margin,
            "reasons": self.reasons,
            "divergence": self.divergence,
        }


# ── 采样器 ───────────────────────────────────────────────────────────────────
class TrapCollector:
    """分类样本采集器. 注入 IntentClassifier, 被动捕获不确定/分歧样本并异步落库."""

    def __init__(
        self,
        session_factory: Any | None = None,
        *,
        threshold: float = 0.6,
        band: float = 0.15,
        ambient_rate: float = 0.02,
        enabled: bool = True,
    ) -> None:
        self._session_factory = session_factory
        self._threshold = threshold
        self._band = band
        self._ambient_rate = ambient_rate
        self._enabled = enabled
        # 持有后台 task 引用, 防 asyncio GC 在 await 期间回收 task (项目规范)
        self._pending_tasks: set[asyncio.Task[Any]] = set()

    # 供测试注入
    def bind_session_factory(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    def should_capture(self, rec: TrapRecord) -> bool:
        """纯判定: 该样本是否应当被感知采样捕获 (无副作用, 便于单测)."""
        if not self._enabled:
            return False
        rec.reasons = []
        if rec.final_source in ("llm", "fallback"):
            rec.reasons.append("slow_path")
        if rec.margin < self._band:
            rec.reasons.append("near_threshold")
        if rec.divergence:
            rec.reasons.append("rule_bert_divergence")
        if self._ambient_hit():
            rec.reasons.append("ambient")
        return bool(rec.reasons)

    def _ambient_hit(self) -> bool:
        return random.random() < self._ambient_rate

    async def capture(self, rec: TrapRecord) -> bool:
        """判定 + 后台异步落库. 返回是否被采集 (同步分支决定, 便于测试断言).

        采样判定在同步阶段完成 (读取上下文填入归属), 落库放后台 task.
        """
        if not self.should_capture(rec):
            return False
        self._apply_context(rec)
        task = asyncio.create_task(self._persist(rec))
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
        return True

    def _apply_context(self, rec: TrapRecord) -> None:
        session_id, customer_id = _trap_context.get()
        if session_id:
            rec.session_id = session_id
        if customer_id:
            rec.customer_id = customer_id

    async def _persist(self, rec: TrapRecord) -> None:
        if self._session_factory is None:
            # 未绑定 DB → 仅告警 (测试/无 DB 环境静默)
            logger.warning("TrapCollector 未绑定 session_factory, 样本丢弃 intent=%s", rec.fast_intent)
            return
        from lumio.shared.orm_models import ClassifierSample

        try:
            async with self._session_factory() as session:
                row = ClassifierSample(**rec.to_row())
                session.add(row)
                await session.commit()
        except Exception:  # 采样不得打挂主链路
            logger.warning("分类样本落库失败: %r", rec.masked_text[:50], exc_info=True)

    async def purge_older_than(self, days: int) -> int:
        """有界留存: 删除超过 days 天的采样, 返回删除行数."""
        if self._session_factory is None:
            return 0
        from sqlalchemy import delete

        from lumio.shared.orm_models import ClassifierSample

        cutoff = datetime.now(UTC) - timedelta(days=days)
        async with self._session_factory() as session:
            stmt = delete(ClassifierSample).where(ClassifierSample.created_at < cutoff)
            result = await session.execute(stmt)
            await session.commit()
            return int(result.rowcount or 0)

    async def aggregate(
        self, *, window_days: int = 7, min_samples: int = 10
    ) -> list[dict[str, Any]]:
        """漂移/分布聚合: 按 final_intent 统计窗口内样本数 + 平均置信度.

        供漂移监控侧消费 (dev 可 crontab 周期调用; 后续接 Grafana/告警).
        """
        if self._session_factory is None:
            return []
        from sqlalchemy import func, select

        from lumio.shared.orm_models import ClassifierSample

        cutoff = datetime.now(UTC) - timedelta(days=window_days)
        stmt = (
            select(
                ClassifierSample.final_intent.label("intent"),
                func.count(ClassifierSample.id).label("count"),
                func.avg(ClassifierSample.final_confidence).label("avg_confidence"),
            )
            .where(ClassifierSample.created_at >= cutoff)
            .group_by(ClassifierSample.final_intent)
            .order_by(func.count(ClassifierSample.id).desc())
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            rows = []
            for row in result.all():
                if int(row.count) >= min_samples:
                    rows.append(
                        {
                            "intent": row.intent,
                            "count": int(row.count),
                            "avg_confidence": float(row.avg_confidence),
                            "window_days": window_days,
                        }
                    )
        return rows


# 模块级单例 (供依赖注入装配时复用; 测试注入独立实例)
_default_trap: TrapCollector | None = None


def default_trap_collector() -> TrapCollector:
    global _default_trap
    if _default_trap is None:
        _default_trap = TrapCollector()
    return _default_trap


def reset_default_trap() -> None:
    """测试 teardown: 重置单例避免跨用例状态泄漏."""
    global _default_trap
    _default_trap = None
