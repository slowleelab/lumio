"""E2: 可解释性 / 决策可追溯.

监管要求: "客户问什么, AI 怎么答, 依据是什么"

实现:
- 每步决策记录到 decision_log 表
- 客户可查询: "我的对话 AI 是怎么回答的"
- 监管可审计: 批量导出决策日志

字段:
- session_id / turn_id / agent_name / action / reasoning / evidence / latency_ms
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from lumio.shared.logger import get_logger

logger = get_logger(__name__)

# ── 轮次上下文 (P1-3: turn_id 按轮贯穿) ─────────────────────────────
# 会话回放的决策链按"轮"分组展示: 同一轮 (客户一条消息 → bot 一次回复) 的全部
# 决策共用一个 turn_id。处理入口 (router._run_agent) 绑定后, 链路上所有
# log_decision 调用 (含嵌套 handler) 自动继承, 无需逐处透传。
# 此前每条决策独立 uuid4 — 字段名叫 turn_id 却标识不了轮次, 多轮会话的
# 决策混在一起无法归组 (会话 smoke-qa-1788567861 复盘)。
_current_turn_id: ContextVar[str] = ContextVar("lumio_decision_turn_id", default="")


def bind_turn_context(turn_id: str) -> None:
    """绑定当前轮次 ID — 之后本轮内的 log_decision 默认继承该值."""
    _current_turn_id.set(turn_id)


def current_turn_id() -> str:
    """读取当前轮次 ID (未绑定为空串)."""
    return _current_turn_id.get()


class DecisionAction(str, Enum):
    """决策动作."""

    INTENT_CLASSIFY = "intent_classify"
    TOOL_CALL = "tool_call"
    RAG_RETRIEVE = "rag_retrieve"
    LLM_GENERATE = "llm_generate"
    TRANSFER_AGENT = "transfer_agent"
    INJECTION_BLOCKED = "injection_blocked"
    GUARD_DENIED = "guard_denied"
    COMPRESSION = "compression"
    REFLECTION = "reflection"
    CACHE_HIT = "cache_hit"
    USER_CONFIRM = "user_confirm"
    MIS_KILL_CANDIDATE = "mis_kill_candidate"  # P3 疑似误杀(放行后又澄清/重问)回流探针
    # 多轮噪声/闲聊治理 (P0): 噪声被拦截 / 上下文回应放行
    NOISE_BLOCKED = "noise_blocked"
    # 诉求跟踪 (多轮会话管理): 诉求状态流转 / 高紧急回访
    TOPIC_TRACK = "topic_track"
    CONTEXT_REPLY_PASS = "context_reply_pass"
    # 全链路监控 (输入→输出关键步骤): 消息出队 / FAQ 直出 / 出站闸门 / 链路完成(含总耗时)
    TURN_START = "turn_start"
    FAQ_DIRECT = "faq_direct"
    OUTBOUND_GUARD = "outbound_guard"
    CHAIN_COMPLETE = "chain_complete"


@dataclass
class DecisionRecord:
    """单条决策记录."""

    decision_id: str
    session_id: str
    turn_id: str
    agent_name: str
    action: DecisionAction
    reasoning: str
    evidence: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    customer_id: str | None = None
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "agent_name": self.agent_name,
            "action": self.action.value,
            "reasoning": self.reasoning,
            "evidence": self.evidence,
            "latency_ms": self.latency_ms,
            "customer_id": self.customer_id,
            "created_at": self.created_at,
        }


class DecisionLogger:
    """决策日志记录器 (单例)."""

    def __init__(self) -> None:
        self._redis: Any = None
        self._db_session_factory: Any = None
        # 同步路径 fallback (无 event loop 时累积, 后续 flush 到 Redis/PG)
        self._sync_fallback: list[DecisionRecord] = []
        # 后台 task 引用, 防 GC
        self._pending_tasks: set[asyncio.Task[None]] = set()

    def record(
        self,
        session_id: str,
        turn_id: str,
        agent_name: str,
        action: DecisionAction,
        reasoning: str,
        evidence: dict[str, Any] | None = None,
        latency_ms: float = 0.0,
        customer_id: str | None = None,
    ) -> str:
        """记录单条决策 (fire-and-forget, 不阻塞主链路).

        Returns:
            decision_id
        """
        decision_id = str(uuid.uuid4())
        record = DecisionRecord(
            decision_id=decision_id,
            session_id=session_id,
            turn_id=turn_id,
            agent_name=agent_name,
            action=action,
            reasoning=reasoning[:500],  # 截断防爆
            evidence=evidence or {},
            latency_ms=latency_ms,
            customer_id=customer_id,
        )

        # 1. 异步写 Redis (实时查询用)
        try:
            import asyncio

            loop = asyncio.get_running_loop()  # 替代已弃用的 get_event_loop
            task = loop.create_task(self._write_redis(record))
            # 持有引用, 防 GC
            self._pending_tasks.add(task)
            task.add_done_callback(self._pending_tasks.discard)

            def _on_write_done(t: asyncio.Task[None]) -> None:
                if exc := t.exception():
                    logger.error("decision_log 写 Redis 失败: session=%s, err=%s", session_id, exc)

            task.add_done_callback(_on_write_done)

            # 1b. 异步落 PG decision_log 表 (持久化, 用于监管审计 + GDPR 删除)
            # 失败不影响主流程 (Redis 已写)
            pg_task = loop.create_task(self._write_pg(record))
            self._pending_tasks.add(pg_task)
            pg_task.add_done_callback(self._pending_tasks.discard)

            def _on_pg_done(t: asyncio.Task[None]) -> None:
                if exc := t.exception():
                    logger.debug("decision_log 写 PG 失败: session=%s, err=%s", session_id, exc)

            pg_task.add_done_callback(_on_pg_done)
        except RuntimeError:
            # 无运行 loop, 落入 fallback buffer (进程级 deque)
            try:
                self._sync_fallback.append(record)
                if len(self._sync_fallback) > 1000:
                    self._sync_fallback = self._sync_fallback[-1000:]  # 防内存爆炸
            except Exception as exc:
                logger.error("decision_log sync fallback 也失败: %s", exc)

        # 2. 同步写 stdout (生产应改为 Kafka/DB)
        logger.info(
            "DECISION[%s] %s/%s/%s: %s | %s",
            record.action.value,
            record.agent_name,
            record.session_id[:8],
            record.turn_id[:8] if record.turn_id else "n/a",
            record.reasoning[:200],
            f"evidence={list((record.evidence or {}).keys())[:5]}",
        )

        return decision_id

    async def _write_redis(self, record: DecisionRecord) -> None:
        """写 Redis (最近 100 条决策, 用于实时查询)."""
        redis = await self._get_redis()
        if not redis:
            return
        try:
            import json

            key = f"lumio:decision:session:{record.session_id}"
            await redis.lpush(key, json.dumps(record.to_dict(), ensure_ascii=False))
            await redis.ltrim(key, 0, 99)  # 只保留最近 100 条
            await redis.expire(key, 7 * 86400)  # 7 天
        except Exception as exc:
            logger.debug("Redis 决策日志写入失败: %s", exc)

    async def _write_pg(self, record: DecisionRecord) -> None:
        """落 PG decision_log 表 (E2 可解释性 / D2 GDPR 链路)

        失败不阻断主流程 (决策已落 Redis), 但显式 warn 并做一次简单重试
        (瞬断/连接抖动场景下避免审计记录丢失)。
        """
        factory = await self._get_db_session_factory()
        if not factory:
            return
        for attempt in range(2):
            try:
                import uuid_utils

                from lumio.shared.orm_models import DecisionLog

                async with factory() as session:  # type: ignore[no-untyped-call]
                    row = DecisionLog(
                        id=uuid_utils.uuid7(),
                        decision_id=record.decision_id,
                        session_id=record.session_id,
                        turn_id=record.turn_id,
                        customer_id=record.customer_id,
                        agent_name=record.agent_name,
                        action=record.action.value,
                        reasoning=record.reasoning,
                        evidence_json=record.evidence or None,
                        latency_ms=record.latency_ms,
                    )
                    session.add(row)
                    await session.commit()
                return
            except Exception as exc:
                if attempt == 0:
                    logger.warning("PG 决策日志写入失败, 重试一次: session=%s, err=%s", record.session_id, exc)
                    await asyncio.sleep(0.1)
                else:
                    logger.warning("PG 决策日志写入失败(重试后): session=%s, err=%s", record.session_id, exc)

    async def _get_db_session_factory(self) -> Any:
        if self._db_session_factory is None:
            try:
                from lumio.services.common.database import get_async_session_factory

                self._db_session_factory = get_async_session_factory()
            except Exception as exc:
                logger.debug("DB session factory 初始化失败: %s", exc)
                self._db_session_factory = False
        return self._db_session_factory if self._db_session_factory else None

    async def query_session(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """查询某会话的所有决策 (客户/坐席/监管可调用)."""
        redis = await self._get_redis()
        if not redis:
            return []
        try:
            import json

            items = await redis.lrange(f"lumio:decision:session:{session_id}", 0, limit - 1)
            return [json.loads(item) for item in items]
        except Exception as exc:
            logger.warning("决策查询失败: %s", exc)
            return []

    async def query_session_pg(self, session_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """从 PG decision_log 表查询会话决策 (持久化审计源, 监管可用).

        PG 不可用时回退 Redis(最近 100 条)。按 created_at 降序返回。
        """
        factory = await self._get_db_session_factory()
        if factory:
            try:
                from lumio.shared.orm_models import DecisionLog

                async with factory() as session:  # type: ignore[no-untyped-call]
                    from sqlalchemy import select

                    # 只选业务列, 跳过 `id` UUID 主键列: 该列在 asyncpg 下由
                    # Uuid(native_uuid=False) 反解 pgproto.UUID 时抛
                    # 'UUID' object has no attribute 'replace' (ORM 潜在缺陷,
                    # 查询侧规避).
                    stmt = (
                        select(
                            DecisionLog.decision_id,
                            DecisionLog.session_id,
                            DecisionLog.turn_id,
                            DecisionLog.customer_id,
                            DecisionLog.agent_name,
                            DecisionLog.action,
                            DecisionLog.reasoning,
                            DecisionLog.evidence_json,
                            DecisionLog.latency_ms,
                            DecisionLog.created_at,
                        )
                        .where(DecisionLog.session_id == session_id)
                        .order_by(DecisionLog.created_at.desc())
                        .limit(limit)
                    )
                    result = await session.execute(stmt)
                    rows = result.all()
                    return [
                        {
                            "decision_id": r.decision_id,
                            "session_id": r.session_id,
                            "turn_id": r.turn_id,
                            "customer_id": r.customer_id,
                            "agent_name": r.agent_name,
                            "action": r.action,
                            "reasoning": r.reasoning,
                            "evidence": r.evidence_json or {},
                            "latency_ms": r.latency_ms,
                            "created_at": r.created_at.isoformat() if r.created_at else None,
                        }
                        for r in rows
                    ]
            except Exception as exc:
                logger.warning("PG 决策查询失败, 回退 Redis: session=%s, err=%s", session_id, exc)
        return await self.query_session(session_id, limit=limit)

    async def _get_redis(self) -> Any:
        if self._redis is None:
            try:
                from lumio.services.common.redis_client import get_redis_client

                self._redis = get_redis_client()
            except Exception as exc:
                logger.debug("Redis 客户端初始化失败: %s", exc)
                self._redis = False
        return self._redis if self._redis else None

    # ── P3 定期评审: 噪声闸动作分组统计 (误杀率趋势) ──
    async def query_noise_gate_stats(
        self, *, window_days: int = 7, actions: tuple[str, ...] | None = None
    ) -> dict[str, int]:
        """按决策动作统计窗口内噪声闸事件次数.

        用于评审: 把 NOISE_BLOCKED / CONTEXT_REPLY_PASS / MIS_KILL_CANDIDATE 分组出报表,
        观察"回话放行占比"与"疑似误杀"趋势。PG 不可用时返回空 dict(不阻断)。
        """
        actions = actions or ("noise_blocked", "context_reply_pass", "mis_kill_candidate")
        factory = await self._get_db_session_factory()
        out: dict[str, int] = {}
        if not factory:
            return out
        try:
            from sqlalchemy import func, select

            from lumio.shared.orm_models import DecisionLog

            cutoff = datetime.now(UTC) - timedelta(days=window_days)
            stmt = (
                select(DecisionLog.action, func.count(DecisionLog.id).label("cnt"))
                .where(
                    DecisionLog.action.in_(actions),
                    DecisionLog.created_at >= cutoff,
                )
                .group_by(DecisionLog.action)
            )
            async with factory() as session:  # type: ignore[no-untyped-call]
                result = await session.execute(stmt)
                out = {str(row.action): int(row.cnt) for row in result.all()}
        except Exception as exc:
            logger.warning("噪声闸统计失败: %s", exc)
        return out


# 全局单例
_logger: DecisionLogger | None = None


def get_decision_logger() -> DecisionLogger:
    global _logger
    if _logger is None:
        _logger = DecisionLogger()
    return _logger


def log_decision(
    session_id: str,
    agent_name: str,
    action: DecisionAction,
    reasoning: str,
    evidence: dict[str, Any] | None = None,
    turn_id: str = "",
    latency_ms: float = 0.0,
    customer_id: str | None = None,
) -> str:
    """便捷函数: 同步记录决策.

    turn_id 留空时自动继承当前轮上下文 (bind_turn_context), 无上下文再独立生成。
    """
    if not turn_id:
        turn_id = _current_turn_id.get() or uuid.uuid4().hex[:16]
    return get_decision_logger().record(
        session_id=session_id,
        turn_id=turn_id,
        agent_name=agent_name,
        action=action,
        reasoning=reasoning,
        evidence=evidence,
        latency_ms=latency_ms,
        customer_id=customer_id,
    )
