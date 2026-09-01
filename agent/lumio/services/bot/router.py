"""机器人服务 HTTP API 路由

消息通道: Redis Streams (Consumer Group + PEL) → at-least-once 保证
结果通知: Redis Pub/Sub 即时唤醒 + Redis response key 作为结果载体
消费模式: XREADGROUP 10 条一批 → create_task 异步并行, per-session Lock 保序
过载保护: Semaphore(10) → 满荷走固定话术快速兜底, 不拒客
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import time
import uuid as uuid_module
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, File, Form, Query, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from lumio.services.common.audit import update_chat_message, write_chat_message
from lumio.services.common.deps import (
    DbSession,
    EmbeddingBreakerDep,
    ESBreakerDep,
    ESClientDep,
    MilvusBreakerDep,
    MilvusCollectionDep,
    MinioClientDep,
    RerankerProviderDep,
)
from lumio.services.common.retrieval import retrieve
from lumio.services.common.rule_loader import RuleLoader
from lumio.shared.auth import CurrentUser
from lumio.shared.config import get_settings
from lumio.shared.exceptions import DocumentFormatError, SessionNotFoundError
from lumio.shared.logger import setup_logger
from lumio.shared.metrics import (
    BOT_ACTIVE_WORKERS,
    BOT_AGENT_RESPONSES,
    BOT_AGENT_TIMEOUTS,
    BOT_ANSWER_LATENCY,
    BOT_FAST_REPLY,
    BOT_SEMAPHORE_UTILIZATION,
    BOT_STREAM_LENGTH,
    BOT_STREAM_PENDING,
    DEAD_LETTER_WRITES,
)
from lumio.shared.models import (
    ChatSendRequest,
    ChatSendResponse,
    RetrieveRequest,
    RetrieveResponse,
    SessionPhase,
    SessionSubPhase,
    VerificationResult,
)
from lumio.shared.orm_models import ChatMessageStatus, KbDocStatus, KbDocument, KbSourceType
from lumio.shared.rate_limit import get_limiter

if TYPE_CHECKING:
    pass

logger = setup_logger(__name__)

router = APIRouter(tags=["bot"])

# ── Redis 键 ────────────────────────────────────────────────────
CHAT_STREAM_KEY = "lumio:chat:stream"
DEAD_LETTER_KEY = "lumio:chat:dead_letter"
CONSUMER_GROUP = "bot-group"
RESPONSE_KEY_PREFIX = "lumio:response"
NOTIFY_CHANNEL_PREFIX = "lumio:notify"
RESPONSE_TTL = 120
STREAM_MAXLEN = 10000  # Stream 最大保留条数（近似裁剪，防止无限增长）
DEAD_LETTER_MAXLEN = 5000  # 死信队列最大保留条数
MAX_RETRY_COUNT = 3  # 消息最大重试次数，超过后转入死信队列

# ── 并发控制 ────────────────────────────────────────────────────
_agent_semaphore: asyncio.Semaphore | None = None

# per-session Queue + Worker: 每会话一个独享协程, 无锁串行消费, 可跳过过期消息
_session_queues: dict[str, asyncio.Queue] = {}
_session_active: dict[str, bool] = {}

# 数据库会话工厂（在 start_bot_worker 中注入，供审计落库使用）
_db_session_factory = None
# 规则加载器（Phase 3: DB+Redis 热加载，替代硬编码 _FAST_INTENT_PATTERNS）
_rule_loader: RuleLoader = RuleLoader()

# ── 快速兜底话术 (Semaphore 满荷时使用) ──────────────────────────
# 银行不能拒客, 满荷走快速通道: regex 意图匹配 <5ms → 固定话术 → source=fast_reply
_FAST_REPLIES: dict[str, str] = {
    "lost_card": "挂失为紧急业务，正在为您优先处理，请稍候。如超过 10 秒未回复，请直接输入'转人工'。",
    "complaint": "您的投诉已记录，正在转接人工处理。",
    "bill_query": "当前咨询量较大，账单查询结果稍后返回，也可输入'转人工'联系客服。",
    "limit_query": "您的问题正在处理中，预计 30 秒内回复。",
    "default": "当前咨询量较大，请稍候或输入'转人工'。",
}

# 快速意图匹配 regex (仅用于满荷兜底, 不做精确分类)
_FAST_INTENT_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("lost_card", re.compile(r"挂失|丢卡|卡丢了|卡不见了")),
    ("complaint", re.compile(r"投诉")),
    ("bill_query", re.compile(r"账单|消费|扣款|还款|欠款")),
    ("limit_query", re.compile(r"额度|提额|降额|信用额度")),
]

# P2-18 修复: RuleLoader 返回的是 domain (card/bill/limit/...), 与 _FAST_REPLIES
# 键名 (lost_card/bill_query/...) 不一致 → 挂失/账单满荷时全掉 default 话术.
# 加 domain → 键别名映射对齐.
_DOMAIN_TO_FAST_KEY: dict[str, str] = {
    "card": "lost_card",
    "complaint": "complaint",
    "bill": "bill_query",
    "limit": "limit_query",
    "installment": "default",
    "reward": "default",
    "transfer": "default",
    "chitchat": "default",
    "default": "default",
}


def _quick_intent_match(message: str) -> str:
    """快速意图匹配 (Phase 3: RuleLoader DB+Redis 热加载, <5ms)

    P2-18: 结果经 domain 别名映射到 _FAST_REPLIES 键, 保证挂失/账单走专属话术.
    """
    intent, _ = _rule_loader.match(message)
    return _DOMAIN_TO_FAST_KEY.get(intent, "default")


# 支持的文件扩展名 → KbSourceType 映射
_EXT_TO_SOURCE: dict[str, str] = {
    ".pdf": "PDF",
    ".docx": "DOCX",
    ".html": "HTML",
    ".md": "MARKDOWN",
    ".txt": "TXT",
    ".xlsx": "XLSX",
}
_ALLOWED_EXTENSIONS = set(_EXT_TO_SOURCE.keys())


def _ensure_session_owned(user: CurrentUser, session_id: str) -> None:
    """P0-1 第三轮修复: JWT 声明 session_id 存在时校验与请求一致 (越权防护).

    银行客服场景: 客户端首次 chat/send 时服务端生成 session_id 返回,
    客户端随后用该 session_id poll/end/transfer/feedback。
    若 JWT 中带 session_id (坐席/管理端场景), 必须与请求一致, 否则 403。
    普通客户 token 不带 session_id 声明时放行 (session_id 本身是随机会话 ID)。
    """
    from lumio.shared.auth import AuthorizationError

    if user.session_id and user.session_id != session_id:
        raise AuthorizationError("无权访问该会话")


# ── Helpers ─────────────────────────────────────────────────────


def _build_poll_json(
    *,
    status: str,
    reply: str | None = None,
    intent: str | None = None,
    confidence: float = 0.0,
    source: str | None = None,
    position: int | None = None,
    est_wait: str | None = None,
    suggestion: str | None = None,
) -> dict:
    has_message = status == "done" and reply is not None and len(reply) > 0
    data: dict = {"status": status, "has_message": has_message}
    if reply is not None:
        data["reply"] = reply
    if intent is not None:
        data["intent"] = intent
    if confidence:
        data["confidence"] = confidence
    if source:
        data["source"] = source
    if position is not None:
        data["position"] = position
    if est_wait:
        data["est_wait"] = est_wait
    if suggestion:
        data["suggestion"] = suggestion
    return data


async def _finish_message(
    redis_client,
    session_id: str,
    reply: str,
    intent: str | None = None,
    confidence: float = 0.0,
    source: str = "fallback",
    extra: dict | None = None,
) -> None:
    """写入 response key + 发布 Pub/Sub 通知"""
    # 安全过滤：对 Bot 回复进行敏感词过滤
    from lumio.shared.safety import safety_filter

    reply = safety_filter.filter_output(reply)

    payload = _build_poll_json(
        status="done",
        reply=reply,
        intent=intent,
        confidence=confidence,
        source=source,
    )
    # 扩展字段 (如 is_transfer/transfer_sid) 必须与回复在同一 key 一次写入,
    # 否则 Publish 唤醒轮询读删 key 后, 后补写会因 key 已删而丢失 (竞态).
    if extra:
        payload.update(extra)
    response_key = f"{RESPONSE_KEY_PREFIX}:{session_id}"
    notify_channel = f"{NOTIFY_CHANNEL_PREFIX}:{session_id}"

    await redis_client.setex(response_key, RESPONSE_TTL, json.dumps(payload, ensure_ascii=False))
    await redis_client.publish(notify_channel, "ready")


# ── P1-2 第三轮修复: message 级幂等标记 ──
# 旧实现按 session 级 response_key 判断幂等: 消息 A 处理中用户连发 B,
# A 写完 response key 后 B 被误判"已处理"而 XACK 丢弃 (消息永久丢失, at-least-once 被破坏).
# 现改为 message_id 维度: 每条消息处理完成后标记, XAUTOCLAIM 重投递时跳过已处理的.
_PROCESSED_PREFIX = "lumio:processed"
_PROCESSED_TTL = 300  # 5 分钟, 覆盖 XAUTOCLAIM 60s 重投窗口


async def _mark_processed(redis_client, client_message_id: str) -> None:
    """标记消息已处理 (幂等键, 防 at-least-once 重投递重复执行)."""
    if not client_message_id:
        return
    with contextlib.suppress(Exception):
        await redis_client.setex(f"{_PROCESSED_PREFIX}:{client_message_id}", _PROCESSED_TTL, "1")


# ── Consumer group 初始化 ───────────────────────────────────────


async def _init_stream_group(redis_client) -> None:
    """确保 stream 和 consumer group 已创建"""
    try:
        await redis_client.xgroup_create(CHAT_STREAM_KEY, CONSUMER_GROUP, id="0", mkstream=True)
        logger.info("Stream consumer group 已创建: %s / %s", CHAT_STREAM_KEY, CONSUMER_GROUP)
    except Exception:
        # XGROUP CREATE 在 group 已存在时抛错, 忽略
        logger.debug("Stream consumer group 已存在: %s / %s", CHAT_STREAM_KEY, CONSUMER_GROUP)


# ── XAUTOCLAIM 兜底 ─────────────────────────────────────────────


async def _claim_stale(redis_client, agent) -> None:
    """后台协程: 定期认领超时未 XACK 的挂死消息

    带重试上限: 同一消息被认领超过 MAX_RETRY_COUNT 次后转入死信队列，
    防止异常消息无限循环占用资源。
    """
    consumer_name = f"bot-claim-{os.getpid()}"
    retry_counter_key = "lumio:chat:retry_count"

    while True:
        try:
            await asyncio.sleep(30)
            claimed = await redis_client.xautoclaim(
                CHAT_STREAM_KEY,
                CONSUMER_GROUP,
                consumer_name,
                min_idle_time=60000,  # 60s 超时
                count=10,
            )
            if claimed and claimed[1]:
                for msg_id, fields in claimed[1]:
                    # 检查重试次数
                    retry_count = await redis_client.hincrby(retry_counter_key, str(msg_id), 1)

                    if retry_count > MAX_RETRY_COUNT:
                        logger.error(
                            "消息超过最大重试次数 %d，转入死信队列: msg_id=%s",
                            MAX_RETRY_COUNT,
                            msg_id,
                        )
                        # 写入死信队列
                        await redis_client.xadd(
                            DEAD_LETTER_KEY,
                            {
                                "original_msg_id": str(msg_id),
                                "session_id": str(fields.get("session_id", "")),
                                "message": str(fields.get("message", "")),
                                "error": f"超过最大重试次数 {MAX_RETRY_COUNT}",
                                "retry_count": str(retry_count),
                                "timestamp": str(asyncio.get_event_loop().time()),
                            },
                            maxlen=DEAD_LETTER_MAXLEN,
                            approximate=True,
                        )
                        # ACK 丢弃消息 + 清理重试计数
                        await redis_client.xack(CHAT_STREAM_KEY, CONSUMER_GROUP, msg_id)
                        await redis_client.hdel(retry_counter_key, str(msg_id))
                        # P0-5: 死信指标 + 告警日志
                        DEAD_LETTER_WRITES.labels(reason="retry_exhausted").inc()
                        logger.error(
                            "DEAD_LETTER: 消息重试超限进入死信 session=%s msg_id=%s — 需人工处理",
                            fields.get("session_id", ""),
                            msg_id,
                        )
                        continue

                    logger.warning(
                        "XAUTOCLAIM 认领挂死消息 (retry %d/%d): msg_id=%s",
                        retry_count,
                        MAX_RETRY_COUNT,
                        msg_id,
                    )
                    fields["_enqueue_time"] = asyncio.get_event_loop().time()
                    asyncio.create_task(_dispatch_message(redis_client, agent, msg_id, fields))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("XAUTOCLAIM 异常")


# ── 消息处理核心 ────────────────────────────────────────────────


async def _dispatch_message(redis_client, agent, msg_id: str, fields: dict) -> None:
    """消息分发: 按 session_id 路由到 per-session Queue, 首次触发 Worker"""
    session_id = fields.get("session_id", "")

    if not session_id:
        logger.warning("消息字段不完整: msg_id=%s", msg_id)
        await redis_client.xack(CHAT_STREAM_KEY, CONSUMER_GROUP, msg_id)
        return

    q = _session_queues.setdefault(session_id, asyncio.Queue(get_settings().bot.max_session_queue))

    # Worker 注册必须在 await 之前完成，防止竞态创建多个 Worker
    if session_id not in _session_active:
        _session_active[session_id] = True
        asyncio.create_task(_session_worker(session_id, q, redis_client, agent))

    # 有界准入: 队列满则本消息留在 Stream(PEL), 不进入内存队列, 避免无界积压.
    # 不 XACK → 由 XAUTOCLAIM 重投或 enqueue 超过 message_ttl 后超时兜底.
    try:
        q.put_nowait((msg_id, fields))
    except asyncio.QueueFull:
        logger.warning(
            "会话消息队列已满, 消息留在 stream: session=%s depth=%d",
            session_id,
            q.qsize(),
        )
        return


async def _session_worker(
    session_id: str,
    q: asyncio.Queue,
    redis_client,
    agent,
) -> None:
    """per-session Worker: 独享协程, 串行消费, 无锁

    消费完所有消息后自动退出 (队列为空且等待超时), 避免泄漏。
    """
    from lumio.shared.config import get_settings

    settings = get_settings()
    message_ttl = settings.bot.message_ttl_seconds

    try:
        while True:
            # 阻塞等待消息，同时设置空闲超时 (300s 无消息则退出 Worker)
            try:
                msg_id, fields = await asyncio.wait_for(q.get(), timeout=300)
            except TimeoutError:
                logger.debug("session worker 空闲退出: session=%s", session_id)
                break

            message = fields.get("message", "")
            enqueue_time = fields.get("_enqueue_time", 0.0)
            client_message_id = fields.get("message_id", "")
            customer_id = fields.get("customer_id", "")
            customer_name = fields.get("customer_name", "")
            channel = fields.get("channel", "web")
            trace_raw = fields.get("_trace_context", "")
            trace_id = trace_raw.split(":")[0] if trace_raw else None

            # ── 身份核验结果回传 (前端弹框完成) —— 专用路径, 不进 agent.run 完整流程 ──
            # 核验结果是结构化回传, 不是客户自然语言: 不做意图分类/噪声门/工具编排,
            # 也不作为对话轮次落历史; 直接推进 pending 核验状态机。
            verification_raw = fields.get("verification_result", "")
            if verification_raw:
                await _run_verification(
                    redis_client,
                    agent,
                    session_id,
                    verification_raw,
                    msg_id,
                    client_message_id,
                )
                continue

            # ── 审计落库：记录消息到达 ──
            if _db_session_factory and client_message_id:
                quick_intent = _quick_intent_match(message)
                await write_chat_message(
                    _db_session_factory,
                    session_id=session_id,
                    message_id=client_message_id,
                    content=message,
                    customer_id=customer_id,
                    channel=channel,
                    quick_intent=quick_intent,
                    trace_id=trace_id,
                )

            processing_start = asyncio.get_event_loop().time()

            # ── 从 Stream 消息中恢复 trace context, 链接 Worker Span 到 HTTP Span ──
            otel_token = None
            if trace_raw:
                try:
                    parts = trace_raw.split(":")
                    if len(parts) == 3:
                        from opentelemetry import context
                        from opentelemetry import trace as otel_trace
                        from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

                        tid = int(parts[0], 16)
                        sid = int(parts[1], 16)
                        flags = TraceFlags(int(parts[2], 16))
                        sc = SpanContext(trace_id=tid, span_id=sid, is_remote=True, trace_flags=flags)
                        parent_ctx = otel_trace.set_span_in_context(NonRecordingSpan(sc))
                        otel_token = context.attach(parent_ctx)
                except Exception:
                    pass

            try:
                # 1. 跳过过期消息
                now = asyncio.get_event_loop().time()
                if enqueue_time and (now - enqueue_time > message_ttl):
                    logger.debug("消息过期跳过: session=%s msg_id=%s", session_id, msg_id)
                    _metrics["to"] += 1
                    BOT_AGENT_TIMEOUTS.labels(source="queue_backlog").inc()
                    await _finish_message(
                        redis_client,
                        session_id,
                        "当前咨询量较大，回复超时，请重新发送或输入'转人工'。",
                        source="timeout",
                    )
                    await redis_client.xack(CHAT_STREAM_KEY, CONSUMER_GROUP, msg_id)
                    if _db_session_factory and client_message_id:
                        await update_chat_message(
                            _db_session_factory,
                            client_message_id,
                            processing_status=ChatMessageStatus.SKIPPED,
                            source="timeout",
                            processing_duration_ms=int((now - enqueue_time) * 1000),
                        )
                    await _mark_processed(redis_client, client_message_id)  # P1-2
                    continue

                # 2. 幂等检查 (P1-2: message 级维度, 防 XAUTOCLAIM 重投递重复执行)
                # 旧实现按 session 级 response_key 判断: 用户连发消息时新消息被误杀,
                # 且 return 直接杀死整个 per-session worker (队列残留消息滞留 60s).
                if client_message_id:
                    processed_key = f"{_PROCESSED_PREFIX}:{client_message_id}"
                    if await redis_client.get(processed_key):
                        logger.debug(
                            "消息幂等跳过(已处理): session=%s msg=%s",
                            session_id,
                            client_message_id,
                        )
                        await redis_client.xack(CHAT_STREAM_KEY, CONSUMER_GROUP, msg_id)
                        continue

                # 3. Semaphore 满荷 → 快速兜底 (P2-18: 接线 fast_reply_cooldown —
                # 冷却期内同一会话不做二次快速兜底, 避免连续给客户模板话术)
                cooldown_ok = True
                try:
                    cooldown_ts = await redis_client.get(f"lumio:fast_reply:{session_id}")
                    if cooldown_ts:
                        cooldown = get_settings().bot.fast_reply_cooldown
                        if time.time() - float(cooldown_ts) < cooldown:
                            cooldown_ok = False
                except Exception:
                    pass
                if _agent_semaphore and _agent_semaphore.locked() and cooldown_ok:
                    intent_hint = _quick_intent_match(message)
                    reply = _FAST_REPLIES.get(intent_hint, _FAST_REPLIES["default"])
                    await _finish_message(redis_client, session_id, reply, intent=intent_hint, source="fast_reply")
                    await redis_client.xack(CHAT_STREAM_KEY, CONSUMER_GROUP, msg_id)
                    _metrics["fr"] += 1
                    BOT_FAST_REPLY.inc()
                    # 记录冷却时间戳
                    with contextlib.suppress(Exception):
                        await redis_client.set(f"lumio:fast_reply:{session_id}", str(time.time()), ex=60)
                    if _db_session_factory and client_message_id:
                        duration = int((asyncio.get_event_loop().time() - processing_start) * 1000)
                        await update_chat_message(
                            _db_session_factory,
                            client_message_id,
                            processing_status=ChatMessageStatus.DONE,
                            intent=intent_hint,
                            source="fast_reply",
                            processing_duration_ms=duration,
                        )
                    logger.info("快速兜底: session=%s intent=%s", session_id, intent_hint)
                    await _mark_processed(redis_client, client_message_id)  # P1-2
                    return

                # 4. 标准 Agent 处理路径
                # 4a. 调用前队列检查: peek 队列并 drain 排队消息, 合并后一次 LLM 调用
                # P2 第三轮修复: 合并上限 (≤5 条 / ≤4000 字符) — 旧代码无界合并,
                # 攻击者短时灌入大量消息 → 数千字符进 LLM prompt
                max_merge_count = 5
                max_merge_chars = 4000
                merged_message_ids: list[str] = []
                merged_contents: list[str] = []

                if not q.empty():
                    while True:
                        try:
                            pending_msg_id, pending_fields = q.get_nowait()
                        except asyncio.QueueEmpty:
                            break

                        # 超限: 该条放回队列, 留给下一轮处理 (不丢失)
                        if len(merged_message_ids) >= max_merge_count or (
                            sum(len(m) for m in merged_contents) + len(pending_fields.get("message", ""))
                            > max_merge_chars
                        ):
                            q.put_nowait((pending_msg_id, pending_fields))
                            break

                        pending_msg = pending_fields.get("message", "")
                        pending_enqueue = pending_fields.get("_enqueue_time", 0.0)
                        pending_client_id = pending_fields.get("message_id", "")

                        # 跳过过期排队消息
                        if pending_enqueue and (now - pending_enqueue > message_ttl):
                            await redis_client.xack(CHAT_STREAM_KEY, CONSUMER_GROUP, pending_msg_id)
                            _metrics["to"] += 1
                            if _db_session_factory and pending_client_id:
                                await update_chat_message(
                                    _db_session_factory,
                                    pending_client_id,
                                    processing_status=ChatMessageStatus.SKIPPED,
                                    source="timeout",
                                )
                            continue

                        # 审计落库: 被合并的消息
                        if _db_session_factory and pending_client_id:
                            await write_chat_message(
                                _db_session_factory,
                                session_id=session_id,
                                message_id=pending_client_id,
                                content=pending_msg,
                                customer_id=pending_fields.get("customer_id", ""),
                                channel=pending_fields.get("channel", "web"),
                                quick_intent=_quick_intent_match(pending_msg),
                                trace_id=(
                                    pending_fields.get("_trace_context", "").split(":")[0]
                                    if pending_fields.get("_trace_context")
                                    else None
                                ),
                            )

                        merged_message_ids.append(pending_client_id)
                        merged_contents.append(pending_msg)
                        await redis_client.xack(CHAT_STREAM_KEY, CONSUMER_GROUP, pending_msg_id)

                if merged_contents:
                    # FIX-5: 语义标记 — LLM 明确感知这是用户连续发送的多条消息,
                    # 避免把合并文本当成一句混乱的话 (旧实现直接 \n 拼接无说明)
                    merged_message = (
                        f"（用户连续发送了 {len(merged_contents) + 1} 条消息，请一次性综合回复）\n"
                        + message
                        + "\n"
                        + "\n".join(merged_contents)
                    )
                    _metrics["mg"] += len(merged_contents)
                    logger.info(
                        "队列合并: session=%s merged=%d total_len=%d",
                        session_id,
                        len(merged_contents),
                        len(merged_message),
                    )
                else:
                    merged_message = message

                # 4b. 标准 Agent (Semaphore.locked() 检查存在微竞态, 但 acquire 会挂起等待, 不丢数据)
                async with _agent_semaphore:
                    try:
                        await _run_agent(
                            redis_client,
                            agent,
                            session_id,
                            merged_message,
                            msg_id,
                            fields.get("message_id", ""),
                            customer_id=customer_id,
                            customer_name=customer_name,
                            merged_message_ids=merged_message_ids,
                        )
                        # 处理耗时（含 RAG/LLM/工具/超时降级）分布
                        BOT_ANSWER_LATENCY.observe(asyncio.get_event_loop().time() - processing_start)
                    except Exception:
                        logger.exception("Agent 异常: session=%s msg_id=%s", session_id, msg_id)
                        await _finish_message(
                            redis_client,
                            session_id,
                            "系统处理您的请求时出现错误，请稍后再试。",
                            source="error_fallback",
                        )
                        if _db_session_factory and client_message_id:
                            duration = int((asyncio.get_event_loop().time() - processing_start) * 1000)
                            await update_chat_message(
                                _db_session_factory,
                                client_message_id,
                                processing_status=ChatMessageStatus.ERROR,
                                source="error_fallback",
                                processing_duration_ms=duration,
                                error_message="Agent 处理异常",
                            )
                        # 写入死信队列（便于后续排查和重试）
                        with contextlib.suppress(Exception):
                            await redis_client.xadd(
                                DEAD_LETTER_KEY,
                                {
                                    "original_msg_id": str(msg_id),
                                    "session_id": str(fields.get("session_id", "")),
                                    "message": str(fields.get("message", "")),
                                    "error": "Agent 处理异常",
                                    "timestamp": str(asyncio.get_event_loop().time()),
                                },
                                maxlen=DEAD_LETTER_MAXLEN,
                                approximate=True,
                            )
                        # P0-5: 死信指标 + 告警日志 (Grafana alert 依赖)
                        DEAD_LETTER_WRITES.labels(reason="agent_error").inc()
                        logger.error(
                            "DEAD_LETTER: 消息进入死信队列 session=%s msg_id=%s — 需人工处理",
                            fields.get("session_id", ""),
                            msg_id,
                        )
                    await redis_client.xack(CHAT_STREAM_KEY, CONSUMER_GROUP, msg_id)

            finally:
                # 释放 trace context
                if otel_token is not None:
                    from opentelemetry import context

                    context.detach(otel_token)

    except asyncio.CancelledError:
        logger.debug("session worker 取消: session=%s", session_id)
        raise
    finally:
        _session_active.pop(session_id, None)
        _session_queues.pop(session_id, None)


async def _run_verification(
    redis_client,
    agent,
    session_id: str,
    verification_raw: str,
    msg_id: str,
    client_message_id: str,
) -> None:
    """身份核验结果回传专用处理路径 (会话 564db34d 复盘).

    核验结果是结构化回传, 不跑意图分类/噪声门/工具编排, 也不落对话历史;
    直接推进 pending 的核验状态机, 结果经 _finish_message 写回轮询。
    """
    try:
        result_obj = VerificationResult.model_validate_json(verification_raw)
    except Exception as exc:
        logger.warning("核验结果解析失败: session=%s err=%s", session_id, exc)
        await _finish_message(redis_client, session_id, "核验回传格式错误，请重新发起办理。", source="error_fallback")
        await redis_client.xack(CHAT_STREAM_KEY, CONSUMER_GROUP, msg_id)
        await _mark_processed(redis_client, client_message_id)
        return

    try:
        result = await agent.handle_verification_result(session_id, result_obj)
    except Exception:
        logger.exception("核验结果处理异常: session=%s", session_id)
        await _finish_message(
            redis_client, session_id, "系统处理您的请求时出现错误，请稍后再试。", source="error_fallback"
        )
        await redis_client.xack(CHAT_STREAM_KEY, CONSUMER_GROUP, msg_id)
        await _mark_processed(redis_client, client_message_id)
        return

    reply = result.get("response", "身份核验已处理。")
    source = result.get("response_source", "template")
    verification = result.get("verification")
    extra = {"verification": verification} if verification else None
    await _finish_message(
        redis_client,
        session_id,
        reply,
        source=source,
        extra=extra,
    )
    await redis_client.xack(CHAT_STREAM_KEY, CONSUMER_GROUP, msg_id)
    await _mark_processed(redis_client, client_message_id)


async def _run_agent(
    redis_client,
    agent,
    session_id: str,
    message: str,
    msg_id: str,
    orig_message_id: str,
    customer_id: str = "",  # P1-6 第三轮修复: 透传 customer_id (画像学习依赖)
    customer_name: str = "",  # 透传客户名称, 供转人工后坐席端展示
    merged_message_ids: list[str] | None = None,
) -> None:
    """标准 Agent 处理路径 (Semaphore 内)

    Args:
        merged_message_ids: 被合并的消息 ID 列表（调用前队列检查合并的消息）
    """
    session_manager = agent._session_manager

    # Session 获取或创建
    state = await session_manager.get_or_create(session_id)

    # ENDED 会话复活: 客户超时/主动结束后再次发消息 → 回 BOT_ACTIVE 续聊
    # (保留历史/画像等 Redis 数据, 守卫由 transition_phase 重新启动)
    if state.current_phase.value == "ended":
        logger.info("会话已 ENDED, 复活为 BOT_ACTIVE: session=%s reason=%s", session_id, state.end_reason)
        try:
            await session_manager.transition_phase(
                session_id,
                SessionPhase.BOT,
                new_sub_phase=SessionSubPhase.BOT_ACTIVE,
                reason="customer_returned",
            )
        except Exception as exc:
            logger.warning("会话复活失败, 降级按新会话处理: session=%s err=%s", session_id, exc)

    # 如果已进入 AGENT 阶段，跳过 bot 处理
    if state.current_phase.value == "agent":
        # P2 第三轮修复: 不再静默 XACK 丢弃 — 客户排队期间的消息需要可见反馈
        logger.info("会话已进入 AGENT 阶段, 跳过 bot: session=%s", session_id)
        # FIX-4: 消息真实写入 Redis 历史 (此前"已记录"话术与事实不符),
        # 供坐席摘要 / 转回 Bot 时保留上下文
        try:
            from lumio.shared.models import DialogueTurn

            turn = DialogueTurn(
                turn_id=uuid_module.uuid4().hex,
                session_id=session_id,
                speaker="customer",
                content=message,
                intent=None,
                confidence=0.0,
                entities=[],
            )
            await session_manager.add_turn(session_id, turn)
        except Exception as exc:
            logger.warning("AGENT 阶段消息写入历史失败: session=%s err=%s", session_id, exc)
        await _finish_message(
            redis_client,
            session_id,
            "您已转接人工客服, 排队期间的消息已为您记录, 客服将尽快回复。",
            source="transfer_queued",
        )
        await redis_client.xack(CHAT_STREAM_KEY, CONSUMER_GROUP, msg_id)
        return

    # Agent 编排 (P1-3 第三轮修复: 端到端 deadline, 防止 5 轮工具循环 × 60s 撑爆 semaphore)
    try:
        result = await asyncio.wait_for(
            agent.run(session_id, message, customer_id=customer_id or None),
            timeout=get_settings().orchestration.global_timeout_ms / 1000,
        )
    except TimeoutError:
        logger.warning("Agent 编排超时: session=%s (>%dms)", session_id, get_settings().orchestration.global_timeout_ms)
        BOT_AGENT_TIMEOUTS.labels(source="orchestration").inc()
        # 编排预算耗尽（单个上游慢）: 用独立 source 与文案, 不再误导归因为"咨询量较大"
        await _finish_message(
            redis_client,
            session_id,
            "回复超时，请重新发送或输入'转人工'。",
            source="llm_timeout",
        )
        await redis_client.xack(CHAT_STREAM_KEY, CONSUMER_GROUP, msg_id)
        await _mark_processed(redis_client, orig_message_id)  # P1-2
        return

    intent = result.get("intent")
    primary_intent = intent.primary_intent if intent and hasattr(intent, "primary_intent") else None
    primary_confidence = intent.primary_confidence if intent and hasattr(intent, "primary_confidence") else 0.0
    is_transfer = result.get("should_transfer", False)
    transfer_reason = result.get("transfer_reason", "")
    reply = result.get("response", "抱歉，我暂时无法处理您的请求。")
    source = result.get("response_source", "fallback")
    entities = result.get("entities", [])
    verification = result.get("verification")  # 身份核验弹框信号 (会话 564db34d)
    BOT_AGENT_RESPONSES.labels(source=source).inc()

    # ── 保存对话历史（客户消息 + Bot 回复）──
    from uuid import uuid4 as _uuid4

    from lumio.shared.models import DialogueTurn

    # 数据标注修正 (审计复查 2026-08-31): 客户行此前复制了 bot 应答的意图/置信,
    # "我要挂失信用卡"被标成 transfer_agent@0.77 —— 意图是应答侧的分类结果,
    # 客户消息本身未被分类, 审计上属于错标, 置空。分类失败兜底 (conf=0.0 的
    # faq) 是无意义标注, bot 行同样置空表示"未识别", 防审计被 52% 的
    # faq@0.0 污染 (实测 1538 行中 800 行)。
    classify_failed = not primary_confidence
    customer_turn = DialogueTurn(
        turn_id=_uuid4().hex,
        session_id=session_id,
        speaker="customer",
        content=message,
        intent=None,
        confidence=None,
        entities=entities if isinstance(entities, list) else [],
    )
    bot_turn = DialogueTurn(
        turn_id=_uuid4().hex,
        session_id=session_id,
        speaker="bot",
        content=reply,
        intent=None if classify_failed else primary_intent,
        confidence=None if classify_failed else primary_confidence,
        response_source=source,
        retrieval_context=result.get("retrieval_context", ""),
    )
    # 对话记录落库(Redis 历史 + PG 合规审计). 失败绝不可静默 — 银行审计数据不得无声丢失.
    # 历史坑: 此处曾用 contextlib.suppress(Exception) 整体吞掉异常, 一旦会话状态取不到
    # (SessionNotFoundError), 整轮 Redis 历史 + dialogue_log 同时丢失且无任何日志.
    try:
        await session_manager.add_turn(session_id, customer_turn, intent=intent)
        # P0 修复: bot 轮不传 intent -- 此前两轮都带同一 intent, add_turn 会把
        # low_confidence_streak 与 confidence_history 各记两遍 (一轮对话翻倍,
        # 会话 178351b41: 15 轮对话 streak=30). 计数只按客户轮算, 每次交换 +1.
        await session_manager.add_turn(session_id, bot_turn)
    except SessionNotFoundError:
        logger.warning("会话状态未取到, 本轮对话历史未落库(Redis 历史+PG 审计丢失): session=%s", session_id)
    except Exception as exc:
        logger.warning("对话历史落库失败: session=%s err=%s", session_id, exc)

    # 转人工处理
    if is_transfer:
        transfer_url = ""
        transfer_sid = ""
        # 修复: 之前 agent._chat_client 在 init_agent 未赋值, 转人工桥接是死代码.
        # _run_agent 无 request 变量, 故读 agent._chat_client; 由 init_agent 注入 app.state.chat_svc_client.
        chat_client = getattr(agent, "_chat_client", None)
        if chat_client:
            try:
                # 重新加载会话状态（add_turn 可能已更新 version）
                state = await session_manager.get_session(session_id)

                history = []
                try:
                    turns = await session_manager.get_history(session_id, limit=20)
                    history = [{"speaker": t.speaker, "content": t.content} for t in turns]
                except Exception:
                    logger.debug("加载对话历史失败: session=%s", session_id)

                # 转接摘要 = 对话摘要(如有) + 最后回复
                conversation_summary = state.conversation_summary if state else ""
                transfer_summary = f"{conversation_summary}\n\n[最近回复] {reply}" if conversation_summary else reply

                # 已知实体随转接传递 (P2 第三轮修复: 旧代码计算后未赋值, 实体列表被丢弃)
                known_entities: list[dict[str, str]] = []
                if state and state.last_entities:
                    known_entities = [{"type": e.entity_type, "value": e.value} for e in state.last_entities]

                transfer_req = chat_client.build_transfer_request(
                    session_id=session_id,
                    customer_id=customer_id or "",
                    customer_name=customer_name or "",
                    transfer_reason=transfer_reason,
                    transfer_summary=transfer_summary,
                    history=history,
                    intent=str(primary_intent.value) if primary_intent and hasattr(primary_intent, "value") else "",
                    sentiment=str(result.get("sentiment", "neutral")),
                    entities=known_entities,  # P2: 实体列表真正传入转接
                )
                transfer_resp = await chat_client.create_session(transfer_req)

                # 将转人工摘要 + 实体写入 session 状态，供坐席端 assist_ready 读取
                with contextlib.suppress(Exception):
                    state.transfer_summary = transfer_summary
                    state.transfer_reason = transfer_reason
                    await session_manager._save_meta(state)
                # transfer_sid = chat-svc 生成的会话 id (session-xxxx), 客户页据此轮询坐席消息.
                # 此前仅回传 transfer_url (带 token 的 URL), 前端无真实 sid 可用.
                transfer_sid = transfer_resp.get("sessionId") or transfer_resp.get("session_id") or ""
                transfer_url = transfer_resp.get("pollUrl", "") or transfer_resp.get("poll_url", "")

                # 记录 chat-svc id ↔ Lumio id 映射, 让 chat-svc 回调(/api/session/update 等)
                # 能反解命中本题 Lumio 会话 (否则状态机在转人工后永远停在 queued)
                if transfer_sid and session_manager:
                    with contextlib.suppress(Exception):
                        await session_manager.bind_session_alias(session_id, transfer_sid)

                if transfer_url:
                    logger.info(
                        "转人工已创建: bot=%s star=%s",
                        session_id,
                        transfer_resp.get("sessionId", transfer_resp.get("session_id", "")),
                    )
            except Exception:
                logger.exception("转人工调用 chat-svc 失败")
                transfer_reason += "（人工客服系统暂不可用）"
        else:
            logger.warning("chat_client 未初始化，跳过转人工桥接: session=%s", session_id)

        await _finish_message(
            redis_client,
            session_id,
            reply,
            intent=str(primary_intent.value) if primary_intent else None,
            confidence=primary_confidence,
            source=source,
            extra={
                "is_transfer": True,
                "transfer_sid": transfer_sid,
                "transfer_url": transfer_url,
                "transfer_reason": transfer_reason,
            },
        )
        # 审计更新：转人工完成
        if _db_session_factory and orig_message_id:
            await update_chat_message(
                _db_session_factory,
                orig_message_id,
                processing_status=ChatMessageStatus.DONE,
                intent=str(primary_intent.value) if primary_intent else None,
                source=source,
            )
            # 合并消息的审计更新
            if merged_message_ids:
                for merged_id in merged_message_ids:
                    await update_chat_message(
                        _db_session_factory,
                        merged_id,
                        processing_status=ChatMessageStatus.DONE,
                        intent=str(primary_intent.value) if primary_intent else None,
                        source="merged",
                    )
        # 转接字段已通过 _finish_message 的 extra 一次写入, 不再事后补写 (避免轮询读删 key 的竞态)
        await redis_client.xack(CHAT_STREAM_KEY, CONSUMER_GROUP, msg_id)
        await _mark_processed(redis_client, orig_message_id)  # P1-2
        logger.info(
            "消息处理完成: session=%s intent=%s source=%s merged=%d",
            session_id,
            primary_intent.value if primary_intent and hasattr(primary_intent, "value") else "unknown",
            source,
            len(merged_message_ids) if merged_message_ids else 0,
        )
        return

    # 非转人工: 写 response + 通知
    await _finish_message(
        redis_client,
        session_id,
        reply,
        intent=str(primary_intent.value) if primary_intent else None,
        confidence=primary_confidence,
        source=source,
        extra={"verification": verification} if verification else None,
    )
    # 审计更新：处理完成
    if _db_session_factory and orig_message_id:
        await update_chat_message(
            _db_session_factory,
            orig_message_id,
            processing_status=ChatMessageStatus.DONE,
            intent=str(primary_intent.value) if primary_intent else None,
            source=source,
        )
        # 合并消息的审计更新
        if merged_message_ids:
            for merged_id in merged_message_ids:
                await update_chat_message(
                    _db_session_factory,
                    merged_id,
                    processing_status=ChatMessageStatus.DONE,
                    intent=str(primary_intent.value) if primary_intent else None,
                    source="merged",
                )
    await redis_client.xack(CHAT_STREAM_KEY, CONSUMER_GROUP, msg_id)
    # 清理重试计数（如果消息曾被 XAUTOCLAIM 认领过）
    with contextlib.suppress(Exception):
        await redis_client.hdel("lumio:chat:retry_count", str(msg_id))
    await _mark_processed(redis_client, orig_message_id)  # P1-2 幂等标记

    logger.info(
        "消息处理完成: session=%s intent=%s source=%s merged=%d",
        session_id,
        primary_intent.value if primary_intent and hasattr(primary_intent, "value") else "unknown",
        source,
        len(merged_message_ids) if merged_message_ids else 0,
    )


# ── Consumer 主循环 ─────────────────────────────────────────────


async def _consumer_loop(redis_client, agent) -> None:
    """消息消费主循环: XREADGROUP 批量消费 → create_task 异步并行"""
    consumer_name = f"bot-worker-{os.getpid()}"
    logger.info("消息消费循环已启动: consumer=%s", consumer_name)

    try:
        while True:
            try:
                # 阻塞式空轮询每 1s 一次: 抑制 redis 自动埋点, 避免每秒生成一条
                # ~1000ms 的 XREADGROUP span 淹没 Jaeger (真实消息耗时另由 _dispatch_message
                # 从 stream 恢复 trace context 单独成链, BOT_ANSWER_LATENCY 计量).
                from opentelemetry.instrumentation.utils import suppress_instrumentation

                with suppress_instrumentation():
                    result = await redis_client.xreadgroup(
                        groupname=CONSUMER_GROUP,
                        consumername=consumer_name,
                        streams={CHAT_STREAM_KEY: ">"},
                        count=10,
                        block=1000,
                    )
            except Exception:
                logger.exception("XREADGROUP 异常, 1s 后重试")
                await asyncio.sleep(1)
                continue

            if not result:
                continue

            for _stream_name, messages in result:
                for msg_id, fields in messages:
                    fields["_enqueue_time"] = asyncio.get_event_loop().time()
                    asyncio.create_task(_dispatch_message(redis_client, agent, msg_id, fields))

    except asyncio.CancelledError:
        logger.info("消息消费循环收到取消信号")
        raise


# ── 监控指标 ────────────────────────────────────────────────────

# 运行时指标（供 health check 和 Prometheus 读取）
_metrics: dict = {
    "p": 0,  # PEL pending count
    "sl": 0,  # Stream length
    "as": 0,  # Active session workers
    "su": 0.0,  # Semaphore utilization (0.0-1.0)
    "fr": 0,  # Fast reply count (累计)
    "to": 0,  # Timeout/skip count (累计)
    "mg": 0,  # Merge count (调用前队列检查合并的消息数, 累计)
}


async def _monitoring_loop(redis_client) -> None:
    """后台监控协程: 每 15s 采集一次指标"""
    while True:
        try:
            await asyncio.sleep(15)

            # PEL 待处理消息数
            try:
                pending = await redis_client.xpending(CHAT_STREAM_KEY, CONSUMER_GROUP)
                _metrics["p"] = pending.get("pending", 0) if isinstance(pending, dict) else len(pending)
            except Exception:
                pass

            # Stream 总长度
            with contextlib.suppress(Exception):
                _metrics["sl"] = await redis_client.xlen(CHAT_STREAM_KEY)

            # 活跃 session worker 数
            _metrics["as"] = len(_session_active)

            # Semaphore 利用率
            if _agent_semaphore:
                total = getattr(get_settings(), "bot", None)
                max_slots = total.max_concurrent_agents if total else 10
                _metrics["su"] = round(1.0 - (_agent_semaphore._value / max_slots), 2)
            else:
                _metrics["su"] = 0.0

            # 同步至 Prometheus 运行时 Gauge（与 /health 快照同源）
            BOT_STREAM_PENDING.set(_metrics["p"])
            BOT_STREAM_LENGTH.set(_metrics["sl"])
            BOT_ACTIVE_WORKERS.set(_metrics["as"])
            BOT_SEMAPHORE_UTILIZATION.set(_metrics["su"])

            logger.debug(
                "Bot 指标: pel=%d stream_len=%d active_workers=%d sem_util=%.2f fast_reply=%d timeout=%d",
                _metrics["p"],
                _metrics["sl"],
                _metrics["as"],
                _metrics["su"],
                _metrics["fr"],
                _metrics["to"],
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("监控采集异常")


# ── Lifespan helpers ────────────────────────────────────────────


async def start_bot_worker(app) -> None:
    """启动 Bot 后台协程 (在 lifespan 中调用)"""
    global _agent_semaphore, _db_session_factory, _rule_loader
    settings = get_settings()
    _agent_semaphore = asyncio.Semaphore(settings.bot.max_concurrent_agents)
    _db_session_factory = getattr(app.state, "db_session_factory", None)

    # 加载 L1 规则（DB → 内存，DB 不可用则 fallback 到内存种子规则）
    if _db_session_factory:
        await _rule_loader.load_from_db(_db_session_factory)
    if not _rule_loader.rules:
        _rule_loader.load_from_memory()

    redis_client = app.state.redis_client
    agent = app.state.agent  # LumioAgent 实例, 由 init_agent 注入

    # 启动规则热加载监听
    if _db_session_factory:
        await _rule_loader.start_hot_reload(redis_client, _db_session_factory)

    # 加载安全过滤敏感词 (S6 第五轮修复: 用代码定位代替相对路径 — 旧实现依赖 CWD,
    # 进程 CWD 不同时文件找不到 → 过滤被静默禁用 (check_input 恒安全))
    from pathlib import Path as _Path

    from lumio.shared.safety import safety_filter

    _words_path = _Path(__file__).resolve().parents[3] / "config" / "sensitive_words.txt"
    if not _words_path.exists():
        _words_path = _Path(__file__).resolve().parents[2] / "config" / "sensitive_words.txt"
    loaded = safety_filter.load_from_file(str(_words_path))
    if loaded == 0:
        logger.warning("敏感词加载为 0 个: %s (内容安全过滤可能未生效)", _words_path)

    # 确保 Stream 和 consumer group 存在
    await _init_stream_group(redis_client)

    # 启动消息消费循环
    consumer_task = asyncio.create_task(_consumer_loop(redis_client, agent))
    app.state._consumer_task = consumer_task

    # 启动 XAUTOCLAIM 兜底协程
    claim_task = asyncio.create_task(_claim_stale(redis_client, agent))
    app.state._claim_task = claim_task

    # 启动监控采集
    monitor_task = asyncio.create_task(_monitoring_loop(redis_client))
    app.state._monitor_task = monitor_task

    logger.info("Bot 后台工作器已启动 (Streams + Consumer Group + XAUTOCLAIM + Monitoring)")


async def stop_bot_worker(app) -> None:
    """停止 Bot 后台协程 (在 lifespan 中调用)"""
    for attr in ("_consumer_task", "_claim_task", "_monitor_task"):
        task = getattr(app.state, attr, None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    logger.info("Bot 后台工作器已停止")


# ── HTTP Endpoints ──────────────────────────────────────────────


@router.get("/health")
async def health_check(req: Request):
    """机器人服务健康检查（含依赖状态 + 运行时指标）"""
    from lumio.shared.health import aggregate_health, check_all_dependencies

    deps = await check_all_dependencies(req.app)
    overall, http_code = aggregate_health(deps)

    settings = get_settings()
    max_slots = settings.bot.max_concurrent_agents
    slots_available = _agent_semaphore._value if _agent_semaphore else max_slots

    from fastapi.responses import JSONResponse

    return JSONResponse(
        status_code=http_code,
        content={
            "status": overall,
            "service": "bot",
            "dependencies": deps,
            "agent_slots": {
                "total": max_slots,
                "available": slots_available,
            },
            "streams": {
                "pending": _metrics["p"],
                "stream_length": _metrics["sl"],
                "active_workers": _metrics["as"],
                "semaphore_utilization": _metrics["su"],
            },
            "stats": {
                "fast_reply_total": _metrics["fr"],
                "timeout_total": _metrics["to"],
                "merge_total": _metrics["mg"],
            },
            "rules": {
                "l1_rule_count": len(_rule_loader.rules),
            },
        },
    )


@router.get("/health/live")
@get_limiter().exempt  # P2-3: 健康探针豁免限流 (流量高峰误 429 → 误判实例不可用)
async def health_live():
    """Liveness 探针：进程存活即 200"""
    return {"status": "alive"}


@router.get("/health/ready")
@get_limiter().exempt  # P2-3
async def health_ready(req: Request):
    """Readiness 探针：检查核心依赖连通性"""
    from fastapi.responses import JSONResponse

    from lumio.shared.health import aggregate_health, check_all_dependencies

    deps = await check_all_dependencies(req.app)
    overall, http_code = aggregate_health(deps)
    return JSONResponse(
        status_code=http_code,
        content={"status": overall, "dependencies": deps},
    )


@router.post("/chat/send", response_model=ChatSendResponse)
# P2-3 第五轮修复: 差异化限流 (rate_limit_chat=30/min, 此前死配置) —
# 用户级 key 防 NAT 共享配额; 登录接口类的高频滥用通道单独收紧
@get_limiter().limit("30/minute")
async def chat_send(body: ChatSendRequest, request: Request, user: CurrentUser):
    """客户端发送消息接口

    消息写入 Redis Stream 持久化, 由后台 consumer 异步处理。
    客户端通过 GET /api/chat/poll 订阅通知获取结果。
    """
    # P0-1 第三轮修复: chat/session 端点强制认证 + JWT 声明 session_id 归属校验
    _ensure_session_owned(user, body.session_id)
    redis_client = getattr(request.app.state, "redis_client", None)
    if redis_client is None:
        # P3-4 整改: 走统一错误体 (ServiceOverloadedError → 503)
        from lumio.shared.exceptions import ServiceOverloadedError

        raise ServiceOverloadedError("Redis 未就绪, 无法接收消息")

    # 输入校验: 拒绝空消息 (含全角空格/零宽字符); 身份核验回传允许空 message
    from lumio.shared.exceptions import IntentUnrecognizedError

    verification_result = body.verification_result
    msg = (
        (body.message or "").replace("　", " ").replace("", "").replace("‌", "").replace("‍", "").replace("﻿", "").strip()
    )
    if not msg and verification_result is None:
        # P3-4 整改: 走统一错误体 (IntentUnrecognizedError → 400)
        raise IntentUnrecognizedError("消息内容不能为空")

    # P0-6 第三轮修复: A3 注入防护接入生产入口 (此前 check_user_input 仅测试引用)
    # 命中已知注入模式 (忽略指令/角色混淆) → 拒绝请求, 不进 agent 链路
    from lumio.shared.injection_guard import InjectionAction, get_guard

    injection_verdict = get_guard().check_user_input(msg)
    if injection_verdict.action in (InjectionAction.REJECT, InjectionAction.QUARANTINE):
        logger.warning(
            "注入拦截 (入口): session=%s pattern=%s",
            body.session_id or "-",
            injection_verdict.pattern,
        )
        raise IntentUnrecognizedError("消息内容不符合规范, 请重新描述您的需求")

    # 安全过滤：检测客户输入中的敏感词
    from lumio.shared.safety import safety_filter

    is_safe, hit_words = safety_filter.check_input(msg)
    if not is_safe:
        logger.warning("客户输入包含敏感词: session=%s hits=%s", body.session_id, hit_words)
        # 敏感词不拦截，但记录审计（银行场景需要知道客户提到了什么敏感话题）

    session_id = body.session_id or uuid_module.uuid4().hex

    # P2-16: per-customer 会话上限 — 同客户多设备/多标签页无限开会话会耗尽
    # Redis state + 守卫资源. 用 ZSET 记录客户活跃会话 (score=最后活跃时间),
    # 超过上限且新会话未建时拒绝 (已带 session_id 的续聊不受影响).
    if body.customer_id and not body.session_id:
        try:
            customer_sessions_key = f"lumio:customer_sessions:{body.customer_id}"
            now_ts = time.time()
            # 清理 24h 前的过期记录
            await redis_client.zremrangebyscore(customer_sessions_key, 0, now_ts - 24 * 3600)
            active = await redis_client.zcard(customer_sessions_key)
            max_sessions = get_settings().bot.max_sessions_per_customer
            if active >= max_sessions:
                logger.warning(
                    "per-customer 会话超限: customer=%s active=%d limit=%d",
                    body.customer_id,
                    active,
                    max_sessions,
                )
                from lumio.shared.exceptions import BusinessError

                raise BusinessError(f"同时进行的会话数量已达上限 ({max_sessions})，请先结束其他会话")
        except Exception as exc:
            if isinstance(exc, BusinessError):
                raise
            logger.debug("per-customer 会话检查跳过 (非阻断): err=%s", exc)

    # FIX-7: 客户端幂等 — 同一 client_message_id 重复提交直接返回已接受, 不进 Stream
    # (双击/网络重试场景: 第二次提交不重复执行 agent, 幂等键由消费侧 _mark_processed 维护)
    client_idem_key = f"lumio:processed:{body.client_message_id}" if body.client_message_id else None
    if client_idem_key:
        try:
            if await redis_client.get(client_idem_key):
                logger.info(
                    "客户端幂等命中, 跳过重复提交: session=%s client_message_id=%s", session_id, body.client_message_id
                )
                return ChatSendResponse(accepted=True, message_id=body.client_message_id, session_id=session_id)
        except Exception:
            pass  # 幂等检查失败时放行 (at-least-once 语义)

    message_id = body.client_message_id or uuid_module.uuid4().hex

    # P2-16: 记录客户 → 会话 (ZADD, 新会话建立时)
    if body.customer_id and not body.session_id:
        with contextlib.suppress(Exception):
            await redis_client.zadd(
                f"lumio:customer_sessions:{body.customer_id}",
                {session_id: time.time()},
            )

    # 注入 trace context 到 Stream 消息 (全链路串联)
    trace_ctx = ""
    try:
        from opentelemetry import trace as otel_trace

        span_ctx = otel_trace.get_current_span().get_span_context()
        if span_ctx.is_valid:
            trace_ctx = f"{span_ctx.trace_id:032x}:{span_ctx.span_id:016x}:{span_ctx.trace_flags:02x}"
    except Exception:
        pass

    # 写入 Stream (XADD 持久化, MAXLEN 防止无限增长)
    await redis_client.xadd(
        CHAT_STREAM_KEY,
        {
            "session_id": session_id,
            "message_id": message_id,
            "message": body.message,
            "verification_result": verification_result.model_dump_json() if verification_result else "",
            "_trace_context": trace_ctx,
            "customer_id": body.customer_id or "",
            "customer_name": body.customer_name or "",
            "channel": body.channel.value if body.channel else "web",
        },
        maxlen=STREAM_MAXLEN,
        approximate=True,
    )

    # 排队可见性: 立刻 ping 一次队列通知, 让前端首轮 poll 快速返回 queued + 排队位置,
    # 避免盲目长等一整轮 (此时结果未就绪, worker 完成后再 publish "ready").
    with contextlib.suppress(Exception):
        await redis_client.publish(f"{NOTIFY_CHANNEL_PREFIX}:{session_id}", "queued")

    return ChatSendResponse(
        accepted=True,
        message_id=message_id,
        session_id=session_id,
    )


@router.get("/chat/poll")
async def chat_poll(
    req: Request,
    user: CurrentUser,
    session_id: str = Query(...),
    timeout: int = Query(default=25, ge=1, le=60),
):
    """长轮询获取机器人回复

    通过 Redis Pub/Sub 监听完成通知, worker 完成即刻唤醒。
    返回不同状态: queued → processing → done / timeout
    """
    # P0-1 第三轮修复: 强制认证 + session 归属校验
    _ensure_session_owned(user, session_id)
    redis_client = getattr(req.app.state, "redis_client", None)
    if redis_client is None:
        return JSONResponse(content=_build_poll_json(status="timeout", suggestion="请稍后重试"))

    response_key = f"{RESPONSE_KEY_PREFIX}:{session_id}"
    notify_channel = f"{NOTIFY_CHANNEL_PREFIX}:{session_id}"

    # 1. 快速路径: 结果已就绪
    raw = await redis_client.get(response_key)
    if raw:
        await redis_client.delete(response_key)
        data = json.loads(raw)
        return Response(
            content=json.dumps(data, ensure_ascii=False).encode("utf-8"),
            media_type="application/json",
        )

    # 2. 订阅 Pub/Sub 等待通知 (带超时保护)
    try:
        result = await asyncio.wait_for(
            _wait_for_response(redis_client, session_id, response_key, notify_channel, timeout),
            timeout=timeout + 2,  # 硬超时 = 用户超时 + 2s 缓冲
        )
        return result
    except TimeoutError:
        return JSONResponse(
            content=_build_poll_json(
                status="timeout",
                suggestion="请稍后重试或输入'转人工'",
            )
        )


async def _wait_for_response(
    redis_client,
    session_id: str,
    response_key: str,
    notify_channel: str,
    timeout: int,
) -> Response:
    """等待 Pub/Sub 通知或超时, 返回 response 或 timeout JSON"""
    pubsub = redis_client.pubsub()
    if asyncio.iscoroutine(pubsub):
        pubsub = await pubsub
    await pubsub.subscribe(notify_channel)

    try:
        start_time = asyncio.get_event_loop().time()
        listener = pubsub.listen()
        # 兼容 mock 环境: pubsub.listen() 可能返回协程而非异步迭代器
        if asyncio.iscoroutine(listener):
            listener = await listener
        try:
            async for msg in listener:
                elapsed = asyncio.get_event_loop().time() - start_time

                if elapsed > timeout:
                    raw = await redis_client.get(response_key)
                    if raw:
                        await redis_client.delete(response_key)
                        data = json.loads(raw)
                        return Response(
                            content=json.dumps(data, ensure_ascii=False).encode("utf-8"),
                            media_type="application/json",
                        )
                    # 窗口耗尽仍无结果: 若该会话仍在排队/处理中 → 返回 queued + 位置
                    # (前端据此显示"排队中"并续轮询); 否则判定为超时.
                    session_q = _session_queues.get(session_id)
                    q_pending = session_q is not None and not session_q.empty()
                    if session_id in _session_active or q_pending:
                        position = session_q.qsize() if session_q is not None else 0
                        return JSONResponse(
                            content=_build_poll_json(
                                status="queued",
                                position=position,
                                est_wait=f"{min(position * 5, 30)}s" if position else "稍后",
                                suggestion="正在排队处理, 请稍候",
                            )
                        )
                    return JSONResponse(
                        content=_build_poll_json(
                            status="timeout",
                            suggestion="请稍后重试或输入'转人工'",
                        )
                    )

                if msg["type"] == "subscribe":
                    raw = await redis_client.get(response_key)
                    if raw:
                        await pubsub.unsubscribe(notify_channel)
                        await redis_client.delete(response_key)
                        data = json.loads(raw)
                        return Response(
                            content=json.dumps(data, ensure_ascii=False).encode("utf-8"),
                            media_type="application/json",
                        )

                elif msg["type"] == "message":
                    raw = await redis_client.get(response_key)
                    if raw:
                        await redis_client.delete(response_key)
                        data = json.loads(raw)
                        return Response(
                            content=json.dumps(data, ensure_ascii=False).encode("utf-8"),
                            media_type="application/json",
                        )
                    # 短暂等待 response key 就绪; 仍为空则不提前返回 — 继续监听,
                    # 让单次 poll 的调用方也能等到同窗口内随后到达的 done 结果.
                    await asyncio.sleep(0.05)
                    raw = await redis_client.get(response_key)
                    if raw:
                        await redis_client.delete(response_key)
                        data = json.loads(raw)
                        return Response(
                            content=json.dumps(data, ensure_ascii=False).encode("utf-8"),
                            media_type="application/json",
                        )
        except (TypeError, AttributeError):
            # listener 不支持异步迭代（mock 或异常环境），降级为超时
            pass

        # 迭代器耗尽或异常，返回超时
        return JSONResponse(
            content=_build_poll_json(
                status="timeout",
                suggestion="请稍后重试或输入'转人工'",
            )
        )

    finally:
        await pubsub.unsubscribe(notify_channel)
        # P2 第三轮修复: pubsub 连接必须 aclose 释放回池 —
        # 旧代码只 unsubscribe 不 aclose, 60/min/IP 轮询压力下耗尽 20 连接池
        with contextlib.suppress(Exception):
            await pubsub.aclose()


@router.post("/kb/retrieve", response_model=RetrieveResponse)
async def kb_retrieve(
    request: RetrieveRequest,
    es_client: ESClientDep,
    milvus_collection: MilvusCollectionDep,
    embedding_breaker: EmbeddingBreakerDep,
    reranker: RerankerProviderDep,
    es_breaker: ESBreakerDep,
    milvus_breaker: MilvusBreakerDep,
):
    """知识库检索接口

    支持混合检索（BM25 + 向量 + RRF 融合）、BM25 单路、向量单路三种模式，
    可选 Reranker 精排。自动降级：嵌入服务不可用时降级到 BM25 only。
    ES/Milvus 熔断器打开时跳过对应检索路。
    """
    embedding_provider = embedding_breaker.provider if embedding_breaker.is_available else None

    # 熔断器检查：打开时跳过对应检索路
    effective_es = es_client if (es_client and es_breaker and es_breaker.allow_request()) else None
    effective_milvus = (
        milvus_collection if (milvus_collection and milvus_breaker and milvus_breaker.allow_request()) else None
    )

    try:
        result = await retrieve(
            request=request,
            es_client=effective_es,
            milvus_collection=effective_milvus,
            embedding_provider=embedding_provider,
            reranker=reranker,
        )
        # 记录成功
        if es_breaker and effective_es:
            es_breaker.record_success()
        if milvus_breaker and effective_milvus:
            milvus_breaker.record_success()
        return result
    except Exception:
        # 记录失败
        if es_breaker and effective_es:
            es_breaker.record_failure()
        if milvus_breaker and effective_milvus:
            milvus_breaker.record_failure()
        raise


@router.post("/kb/documents")
async def upload_document(
    db: DbSession,
    es_client: ESClientDep,
    milvus_collection: MilvusCollectionDep,
    minio_client: MinioClientDep,
    embedding_breaker: EmbeddingBreakerDep,
    user: CurrentUser,
    file: UploadFile = File(...),  # noqa: B008
    category: str = Form(...),
    doc_type: str = Form(...),
    card_type: str | None = Form(None),
    customer_tier: str | None = Form(None),
    security_level: str = Form("internal"),
    version: str = Form("1.0"),
    effective_date: str | None = Form(None),
    expiry_date: str | None = Form(None),
    keywords: str = Form(""),
):
    """文档上传接口

    上传文件到 MinIO，创建知识库文档记录，触发摄入管线。
    支持格式：PDF, DOCX, HTML, MD, TXT, XLSX。
    """
    # 1. 校验文件扩展名
    filename = file.filename or ""
    suffix = ""
    if "." in filename:
        suffix = "." + filename.rsplit(".", 1)[-1].lower()

    if suffix not in _ALLOWED_EXTENSIONS:
        raise DocumentFormatError(f"不支持的文件格式: {suffix}，支持: {', '.join(sorted(_ALLOWED_EXTENSIONS))}")

    source_type_str = _EXT_TO_SOURCE[suffix]

    # 2. P3-7 整改: 文件大小上限 50MB (防 OOM 攻击)
    # Content-Length header 优先 (无须读 body), 缺失则 spools 50MB 上限给 fastapi 自动拒绝
    content_length = file.size if hasattr(file, "size") else None
    max_upload_size = 50 * 1024 * 1024
    if content_length is not None and content_length > max_upload_size:
        raise DocumentFormatError(f"文件过大: {content_length / 1024 / 1024:.1f}MB > 50MB 上限")

    # 3. 读取文件内容
    content_bytes = await file.read()
    file_size = len(content_bytes)
    if file_size > max_upload_size:
        raise DocumentFormatError(f"文件过大: {file_size / 1024 / 1024:.1f}MB > 50MB 上限")
    content_hash = hashlib.sha256(content_bytes).hexdigest()

    # 3. 上传到 MinIO
    minio_object_key = f"{category}/{filename}"
    if minio_client:
        from io import BytesIO

        settings = get_settings()
        bucket_name = settings.minio.bucket
        await asyncio.to_thread(
            minio_client.put_object,
            bucket_name,
            minio_object_key,
            BytesIO(content_bytes),
            file_size,
            content_type=file.content_type or "application/octet-stream",
        )

    # 4. 创建 KbDocument 记录
    from datetime import date as date_type

    kb_doc = KbDocument(
        title=filename,
        source_type=KbSourceType[source_type_str],
        file_path=minio_object_key,
        file_size=file_size,
        content_hash=content_hash,
        category=category,
        doc_type=doc_type,
        card_type=card_type,
        customer_tier=customer_tier,
        security_level=security_level,
        version=version,
        effective_date=date_type.fromisoformat(effective_date) if effective_date else None,
        expiry_date=date_type.fromisoformat(expiry_date) if expiry_date else None,
        status=KbDocStatus.PENDING,
        created_by=user.user_id,
    )
    db.add(kb_doc)
    await db.flush()

    # 5. 触发摄入管线
    from lumio.services.common.ingestion import ingest_document
    from lumio.shared.models import DocumentMetadata

    doc_metadata = DocumentMetadata(
        doc_id=str(kb_doc.id),
        category=category,
        doc_type=doc_type,
        keywords=[k.strip() for k in keywords.split(",") if k.strip()] if keywords else [],
        card_type=card_type,
        customer_tier=customer_tier,
        effective_date=effective_date,
        expiry_date=expiry_date,
        security_level=security_level,
        version=version,
    )

    embedding_provider = embedding_breaker.provider if embedding_breaker.is_available else None

    # 文本类型直接传内容; 二进制 (PDF/DOCX/XLSX) 解析器吃磁盘路径 —— 此前传的是
    # MinIO 对象键, fitz/Document 打不开必挂, 二进制上传从未成功过 (2026-08-29 修复)
    if source_type_str in ("MARKDOWN", "TXT", "HTML"):
        parse_input = content_bytes.decode("utf-8")
    else:
        import tempfile
        from pathlib import Path as _Path

        suffix = _Path(filename).suffix or ".bin"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(content_bytes)
        parse_input = tmp.name

    try:
        final_status = await ingest_document(
            doc_id=kb_doc.id,
            file_path=parse_input,
            source_type=KbSourceType[source_type_str],
            metadata=doc_metadata,
            embedding_provider=embedding_provider,
            db_session=db,
            es_client=es_client,
            milvus_collection=milvus_collection,
        )
        kb_doc.status = KbDocStatus.COMPLETED if final_status == "COMPLETED" else KbDocStatus.FAILED
    except Exception:
        kb_doc.status = KbDocStatus.FAILED

    # P0: get_db 收尾只 rollback 不 commit —— 不显式提交, kb_document/kb_chunk/
    # kb_ingestion_log 全部随请求回滚, 文档"上传成功却在管理页消失"
    await db.commit()

    if parse_input != content_bytes.decode("utf-8", errors="ignore"):
        try:
            import os as _os

            _os.unlink(parse_input)
        except Exception:
            pass

    return {
        "doc_id": str(kb_doc.id),
        "status": kb_doc.status.value,
        "chunk_count": kb_doc.chunk_count,
    }


# ── 会话管理 ──


class ChatEndRequest(BaseModel):
    session_id: str


@router.post("/chat/end")
async def chat_end(body: ChatEndRequest, req: Request, user: CurrentUser):
    """客户主动结束会话"""
    # P0-1 第三轮修复: 强制认证 + session 归属校验
    _ensure_session_owned(user, body.session_id)
    session_manager = getattr(req.app.state, "session_manager", None)
    if session_manager:
        with contextlib.suppress(Exception):  # 会话可能已结束
            await session_manager.transition_phase(
                body.session_id,
                SessionPhase.ENDED,
                reason="customer_ended",
            )
    return {"status": "ok", "session_id": body.session_id}


class ChatTransferRequest(BaseModel):
    session_id: str
    reason: str = "customer_request"


@router.post("/chat/transfer")
async def chat_transfer(body: ChatTransferRequest, req: Request, user: CurrentUser):
    """客户主动请求转人工"""
    # P0-1 第三轮修复: 强制认证 + session 归属校验 (防转人工轰炸)
    _ensure_session_owned(user, body.session_id)
    session_manager = getattr(req.app.state, "session_manager", None)
    if session_manager:
        with contextlib.suppress(Exception):
            await session_manager.transition_phase(
                body.session_id,
                SessionPhase.AGENT,
                new_sub_phase=SessionSubPhase.AG_QUEUED,
                reason=body.reason,
            )

    # 通知 chat-svc 创建转人工会话
    chat_client = getattr(req.app.state, "chat_svc_client", None)
    transfer_url = ""
    transfer_sid = ""
    if chat_client:
        try:
            transfer_req = chat_client.build_transfer_request(
                session_id=body.session_id,
                transfer_reason=body.reason or "",
            )
            result = await chat_client.create_session(transfer_req)
            transfer_sid = result.get("sessionId") or result.get("session_id") or ""
            transfer_url = result.get("pollUrl", "") or result.get("poll_url", "")
            # 记录 chat-svc id ↔ Lumio id 映射, 让 chat-svc 回调能反解命中本题会话
            if transfer_sid and session_manager:
                with contextlib.suppress(Exception):
                    await session_manager.bind_session_alias(body.session_id, transfer_sid)
        except Exception:
            logger.warning("chat-svc 转人工通知失败: session=%s", body.session_id)

    return {
        "status": "transferring",
        "session_id": body.session_id,
        "transfer_sid": transfer_sid,
        "transfer_url": transfer_url,
    }


# ── 客户反馈 ──


class ChatFeedbackRequest(BaseModel):
    session_id: str
    message_id: str
    rating: Literal["up", "down", ""] = ""
    comment: str = ""


class GDPRDeleteRequest(BaseModel):
    """P0-4: GDPR 删除请求 (管理端/客户本人发起)."""

    customer_id: str = Field(min_length=1, max_length=64)


@router.post("/chat/feedback")
async def chat_feedback(body: ChatFeedbackRequest, req: Request, user: CurrentUser):
    """客户对 Bot 回复的反馈（点赞/踩/评论）"""
    # P0-1 第三轮修复: 强制认证 + session 归属校验
    _ensure_session_owned(user, body.session_id)
    redis_client = getattr(req.app.state, "redis_client", None)
    if redis_client:
        feedback_key = f"lumio:feedback:customer:{body.message_id}"
        import json as _json

        await redis_client.setex(
            feedback_key,
            86400,
            _json.dumps(
                {
                    "session_id": body.session_id,
                    "message_id": body.message_id,
                    "rating": body.rating,
                    "comment": body.comment,
                },
                ensure_ascii=False,
            ),
        )
    # 闭环 §3.1 第一路: 差评 (rating=down) 自动采集 Badcase —— 此前只写 Redis,
    # 负面反馈从未进归因闭环。取会话最后一轮 (客户输入 + bot 回复) 作归因现场。
    if body.rating == "down":
        try:
            sf = getattr(req.app.state, "db_session_factory", None) or _db_session_factory
            if sf:
                last_user_input, last_bot_output = "", ""
                async with sf() as db:
                    from sqlalchemy import select

                    from lumio.shared.orm_models import DialogueLog

                    res = await db.execute(
                        select(DialogueLog.speaker, DialogueLog.content)
                        .where(DialogueLog.session_id == body.session_id)
                        .order_by(DialogueLog.timestamp.desc())
                        .limit(4)
                    )
                    rows = list(res.all())
                for speaker, content in reversed(rows):
                    if speaker == "customer" and not last_user_input:
                        last_user_input = content or ""
                    elif speaker in ("bot", "assistant") and not last_bot_output:
                        last_bot_output = content or ""
                if last_user_input:
                    from lumio.services.common.badcase_store import capture_badcase

                    await capture_badcase(
                        sf,
                        trace_id=body.session_id,
                        session_id=body.session_id,
                        signal_source="negative_feedback",
                        user_input=last_user_input,
                        customer_id=None,
                        bot_output=last_bot_output or None,
                        signal_detail={
                            "message_id": body.message_id,
                            "comment": body.comment[:200],
                            "stage": "auto",
                        },
                    )
        except Exception:
            logger.debug("差评 Badcase 采集失败(不阻断反馈)")
    return {"status": "ok"}


@router.post("/gdpr/delete")
async def gdpr_delete(body: GDPRDeleteRequest, req: Request, user: CurrentUser):
    """P0-4 第三轮修复: GDPR 删除请求 (客户删除自己的数据).

    认证 + 归属校验: 仅本人 (JWT sub == customer_id) 或 admin/agent 可发起.
    流程: 软删除 (30 天观察) + 立即清理活跃 session, 后台 worker 30 天后硬删除.
    """
    from lumio.shared.auth import AuthorizationError

    # 客户只能删自己; admin/agent 可代删
    if user.role == "customer" and user.user_id != body.customer_id:
        raise AuthorizationError("只能删除本人的数据")

    from lumio.services.common.gdpr import get_gdpr_service

    service = get_gdpr_service()
    req_result = await service.request_deletion(body.customer_id)
    return {"status": "ok", **req_result.to_dict()}


# ── 会话历史 ──


@router.get("/sessions")
async def list_sessions(
    req: Request,
    user: CurrentUser,
    limit: int = 20,
    offset: int = 0,
):
    """列出会话（从 Redis 扫描活跃会话）

    S3 第五轮修复: 按用户过滤 — customer 只返回自己的会话 (meta.customer_id == user_id),
    admin/agent 可全量枚举 (坐席工作台场景). 此前任何登录用户可枚举全系统会话.
    """
    redis_client = getattr(req.app.state, "redis_client", None)
    if not redis_client:
        return {"sessions": [], "total": 0}

    is_admin = user.role in ("admin", "agent")

    # 使用 SCAN 迭代（非阻塞），避免 KEYS 阻塞 Redis
    # P3-5 整改: 用 session_meta_scan_pattern() 公共 helper 代替硬编码
    from lumio.services.common.session import session_meta_scan_pattern

    sessions = []
    async for key in redis_client.scan_iter(match=session_meta_scan_pattern(), count=100):
        raw = await redis_client.get(key)
        if not raw:
            continue
        try:
            meta = json.loads(raw)
        except Exception:
            continue
        # customer 仅见自己的会话 (owner 校验)
        if not is_admin and meta.get("customer_id") not in (None, "", user.user_id):
            continue
        sessions.append(
            {
                "session_id": meta.get("session_id"),
                "current_phase": meta.get("current_phase"),
                "sub_phase": meta.get("sub_phase"),
                "turn_count": meta.get("turn_count", 0),
                "last_active_at": meta.get("last_active_at"),
                "agent_id": meta.get("agent_id"),
                "customer_id": meta.get("customer_id") if is_admin else None,  # 脱敏: 客户不可见他人 ID
            }
        )
        if len(sessions) >= offset + limit:
            break
    paged = sessions[offset : offset + limit]
    return {"sessions": paged, "total": len(sessions)}


@router.get("/sessions/{session_id}/messages")
async def get_session_messages(session_id: str, req: Request, user: CurrentUser, limit: int = 50):
    """获取会话消息历史"""
    # P0-1 第三轮修复: 强制认证 + session 归属校验 (横向越权读取防护)
    _ensure_session_owned(user, session_id)
    # S3 第五轮修复: customer 读取他人会话时按 meta owner 二次校验 —
    # JWT 无 session_id 声明的 customer token 此前直接放行, 配合枚举可越权
    redis_client = getattr(req.app.state, "redis_client", None)
    if redis_client and user.role == "customer":
        from lumio.services.common.session import session_meta_key

        raw_meta = await redis_client.get(session_meta_key(session_id))
        if raw_meta:
            from lumio.shared.auth import AuthorizationError

            try:
                meta_owner = json.loads(raw_meta).get("customer_id")
                if meta_owner and meta_owner != user.user_id:
                    raise AuthorizationError("无权访问该会话")
            except AuthorizationError:
                raise
            except Exception:
                pass

    # 优先读 PG 持久化的对话记录(dialogue_log): 轮次实时落库后即存在, 跨 TTL/重启仍可查,
    # 不再只依赖易失的 Redis 历史. 持久化为空(尚未触发/全被裁剪)时回退下方 Redis 路径.
    if _db_session_factory:
        try:
            from sqlalchemy import select

            from lumio.services.common.session import session_history_key
            from lumio.shared.orm_models import DialogueLog

            async with _db_session_factory() as db:
                res = await db.execute(
                    select(DialogueLog).where(DialogueLog.session_id == session_id).order_by(DialogueLog.timestamp)
                )
                rows = res.scalars().all()
            if rows:
                messages = [
                    {
                        "speaker": r.speaker,
                        "content": r.content,
                        "timestamp": r.timestamp.isoformat() if r.timestamp else None,
                        "intent": r.intent,
                        "response_source": r.response_source,
                        "confidence": r.confidence,
                        "turn_id": r.turn_id,
                    }
                    for r in rows
                ]
                return {"session_id": session_id, "messages": messages, "count": len(messages)}
        except Exception:
            logger.exception("读取 PG 对话历史失败, 回退 Redis: session=%s", session_id)

    if not redis_client:
        # P3-9 整改: 不再静默返回空列表 (会误导客户端 polling loop 空转)
        # 显式 503 走统一错误体, 客户端能识别"系统故障"vs"无消息"
        from lumio.shared.exceptions import ServiceOverloadedError

        raise ServiceOverloadedError("Redis 未就绪, 无法获取会话历史")

    # P3-5 整改: 用 session_history_key() 公共 helper 代替硬编码
    from lumio.services.common.session import session_history_key

    key = session_history_key(session_id)
    raw_list = await redis_client.lrange(key, -limit, -1)
    messages = []
    for raw in raw_list:
        try:
            turn = json.loads(raw)
            messages.append(
                {
                    "speaker": turn.get("speaker"),
                    "content": turn.get("content"),
                    "timestamp": turn.get("timestamp"),
                    "intent": turn.get("intent"),
                }
            )
        except Exception:
            continue
    return {"session_id": session_id, "messages": messages, "count": len(messages)}


# ── KB 管理 ──


@router.get("/kb/documents")
async def list_documents(
    db: DbSession,
    user: CurrentUser,
    category: str | None = None,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """列出知识库文档"""
    from sqlalchemy import func, select

    query = select(KbDocument).where(KbDocument.is_deleted == False)  # noqa: E712
    if category:
        query = query.where(KbDocument.category == category)
    if status:
        query = query.where(KbDocument.status == status)

    count_q = select(func.count()).select_from(query.subquery())
    total = (await db.execute(count_q)).scalar() or 0

    query = query.order_by(KbDocument.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    docs = result.scalars().all()

    return {
        "documents": [
            {
                "doc_id": str(d.id),
                "title": d.title,
                "source_type": d.source_type,
                "category": d.category,
                "doc_type": d.doc_type,
                "status": d.status,
                "chunk_count": d.chunk_count,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in docs
        ],
        "total": total,
    }


@router.delete("/kb/documents/{doc_id}")
async def delete_document(
    doc_id: str,
    db: DbSession,
    es_client: ESClientDep,
    milvus_collection: MilvusCollectionDep,
    user: CurrentUser,
):
    """软删除知识库文档，并同步清理 ES 索引与 Milvus 向量

    DB 软删提交后，尽力清理检索侧索引；单边失败不回滚 DB（文档已标记删除），
    但记录 warning 便于补偿，避免"删除后仍被检索到"的数据不一致。
    """
    from sqlalchemy import select

    result = await db.execute(select(KbDocument).where(KbDocument.id == _coerce_doc_id(doc_id)))
    doc = result.scalar_one_or_none()
    if not doc:
        from lumio.shared.exceptions import LumioError

        raise LumioError(code=2001, message=f"文档不存在: {doc_id}")

    doc.is_deleted = True
    doc.deleted_at = datetime.now()
    await db.commit()

    # ── 同步清理 ES 索引（按 doc_id 删除该文档的所有分块）──
    if es_client is not None:
        try:
            from lumio.shared.config import get_settings

            index_name = f"{get_settings().elasticsearch.index_prefix}_kb_chunks"
            await es_client.delete_by_query(
                index=index_name,
                body={"query": {"term": {"doc_id": str(doc_id)}}},
                refresh=True,  # 即时可见, 否则 refresh 窗口内已删 chunk 仍可被检索
            )
            logger.info("ES 索引清理完成: doc_id=%s", doc_id)
        except Exception as e:
            logger.warning("ES 索引清理失败(doc_id=%s，需补偿): %s", doc_id, e)

    # ── 同步清理 Milvus 向量（按 doc_id 表达式删除）──
    if milvus_collection is not None:
        try:
            milvus_collection.delete(expr=f'doc_id == "{doc_id}"')
            logger.info("Milvus 向量清理完成: doc_id=%s", doc_id)
        except Exception as e:
            logger.warning("Milvus 向量清理失败(doc_id=%s，需补偿): %s", doc_id, e)

    return {"status": "ok", "doc_id": doc_id}


@router.get("/kb/documents/{doc_id}/status")
async def get_document_status(doc_id: str, db: DbSession, user: CurrentUser):
    """查看文档摄入状态

    只选业务列: 物化 KbDocument 实体会触发 Uuid(native_uuid=False) 的
    asyncpg 'pgproto.UUID' 反序列化报错 (会话 48882b05 同源坑)。
    """
    from sqlalchemy import select

    from lumio.shared.orm_models import KbIngestionLog

    result = await db.execute(
        select(
            KbDocument.title,
            KbDocument.status,
            KbDocument.chunk_count,
        ).where(KbDocument.id == _coerce_doc_id(doc_id))
    )
    row = result.first()
    if not row:
        from lumio.shared.exceptions import LumioError

        raise LumioError(code=2001, message=f"文档不存在: {doc_id}")

    logs_result = await db.execute(
        select(
            KbIngestionLog.stage,
            KbIngestionLog.status,
            KbIngestionLog.duration_ms,
            KbIngestionLog.error_message,
        )
        .where(KbIngestionLog.document_id == _coerce_doc_id(doc_id))
        .order_by(KbIngestionLog.created_at)
    )
    logs = logs_result.all()

    return {
        "doc_id": doc_id,
        "title": row.title,
        "status": row.status,
        "chunk_count": row.chunk_count,
        "stages": [
            {
                "stage": log.stage,
                "status": log.status,
                "duration_ms": log.duration_ms,
                "error_message": log.error_message,
            }
            for log in logs
        ],
    }


def _coerce_doc_id(doc_id: str):
    """字符串 ID → uuid_utils.UUID (非法字符串原样透传, 交由"不存在"语义处理)。

    KbDocument.id 是 Uuid(native_uuid=False), 字符串直比会在 bind 阶段抛 StatementError。
    """
    import uuid_utils

    try:
        return uuid_utils.UUID(str(doc_id))
    except ValueError:
        return str(doc_id)


@router.get("/kb/documents/{doc_id}/source")
async def get_document_source(doc_id: str, request: Request, db: DbSession, user: CurrentUser):
    """查看原始文档

    文本类 (MARKDOWN/TXT/HTML) 直接返回内容供控制台预览;
    二进制类 (PDF/DOCX/XLSX) 返回 MinIO 预签名下载 URL (1 小时有效)。
    """
    from sqlalchemy import select

    from lumio.shared.exceptions import LumioError

    result = await db.execute(
        select(KbDocument.title, KbDocument.source_type, KbDocument.file_path).where(
            KbDocument.id == _coerce_doc_id(doc_id)
        )
    )
    row = result.first()
    if not row:
        raise LumioError(code=2001, message=f"文档不存在: {doc_id}")

    minio_client = getattr(request.app.state, "minio_client", None)
    if not minio_client:
        raise LumioError(code=5001, message="MinIO 未就绪")

    settings = get_settings()
    bucket = settings.minio.bucket

    if row.source_type in (KbSourceType.MARKDOWN, KbSourceType.TXT, KbSourceType.HTML):
        import asyncio

        resp = None
        try:
            resp = await asyncio.to_thread(minio_client.get_object, bucket, row.file_path)
            raw = await asyncio.to_thread(resp.read)
        except Exception as exc:
            raise LumioError(code=2001, message=f"源文件不存在: {row.file_path}") from exc
        finally:
            if resp is not None:
                try:
                    await asyncio.to_thread(resp.close)
                    await asyncio.to_thread(resp.release_conn)
                except Exception:
                    pass
        return {
            "doc_id": doc_id,
            "title": row.title,
            "file_path": row.file_path,
            "kind": "text",
            "content": raw.decode("utf-8", errors="replace"),
        }

    import asyncio
    from datetime import timedelta

    try:
        url = await asyncio.to_thread(
            minio_client.presigned_get_object, bucket, row.file_path, expires=timedelta(hours=1)
        )
    except Exception as exc:
        raise LumioError(code=2001, message=f"源文件不存在: {row.file_path}") from exc
    return {
        "doc_id": doc_id,
        "title": row.title,
        "file_path": row.file_path,
        "kind": "binary",
        "download_url": url,
    }


@router.post("/kb/documents/{doc_id}/reindex")
async def reindex_document(
    doc_id: str,
    request: Request,
    db: DbSession,
    user: CurrentUser,
    es_client: ESClientDep,
    milvus_collection: MilvusCollectionDep,
    embedding_breaker: EmbeddingBreakerDep,
    minio_client: MinioClientDep,
):
    """本地重建索引

    从 MinIO 拉取源文件, 走完整 Parse→Clean→Chunk→Embed→Dual-Write 管线,
    不依赖 kb-service (此前该路径不存在, 前端重试按钮一直打空)。
    """
    from sqlalchemy import select

    from lumio.shared.exceptions import LumioError

    result = await db.execute(
        select(
            KbDocument.id,
            KbDocument.title,
            KbDocument.source_type,
            KbDocument.file_path,
            KbDocument.category,
            KbDocument.doc_type,
            KbDocument.card_type,
            KbDocument.customer_tier,
            KbDocument.security_level,
            KbDocument.version,
            KbDocument.effective_date,
            KbDocument.expiry_date,
        ).where(KbDocument.id == _coerce_doc_id(doc_id))
    )
    row = result.first()
    if not row:
        raise LumioError(code=2001, message=f"文档不存在: {doc_id}")

    if not minio_client:
        raise LumioError(code=5001, message="MinIO 未就绪")
    embedding_provider = embedding_breaker.provider if embedding_breaker.is_available else None

    settings = get_settings()
    bucket = settings.minio.bucket
    import asyncio

    resp = None
    try:
        resp = await asyncio.to_thread(minio_client.get_object, bucket, row.file_path)
        raw = await asyncio.to_thread(resp.read)
    except Exception as exc:
        raise LumioError(code=2001, message=f"源文件不存在: {row.file_path}") from exc
    finally:
        if resp is not None:
            try:
                await asyncio.to_thread(resp.close)
                await asyncio.to_thread(resp.release_conn)
            except Exception:
                pass

    from lumio.services.common.ingestion import ingest_document
    from lumio.shared.models import DocumentMetadata

    text_like = row.source_type in (KbSourceType.MARKDOWN, KbSourceType.TXT, KbSourceType.HTML)
    if text_like:
        parse_input = raw.decode("utf-8", errors="replace")
    else:
        # 二进制 (PDF/DOCX/XLSX) 解析器吃本地文件路径, 落临时文件
        import tempfile
        from pathlib import Path as _Path

        suffix = _Path(row.file_path).suffix or ".bin"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(raw)
        parse_input = tmp.name

    doc_metadata = DocumentMetadata(
        doc_id=str(row.id),
        category=row.category,
        doc_type=row.doc_type,
        keywords=[],
        card_type=row.card_type,
        customer_tier=row.customer_tier,
        effective_date=row.effective_date.isoformat() if row.effective_date else None,
        expiry_date=row.expiry_date.isoformat() if row.expiry_date else None,
        security_level=row.security_level,
        version=row.version,
    )

    try:
        final_status = await ingest_document(
            doc_id=row.id,
            file_path=parse_input,
            source_type=row.source_type,
            metadata=doc_metadata,
            embedding_provider=embedding_provider,
            db_session=db,
            es_client=es_client,
            milvus_collection=milvus_collection,
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise LumioError(code=5000, message=f"重建索引失败: {exc}") from exc

    status_row = (
        await db.execute(select(KbDocument.status, KbDocument.chunk_count).where(KbDocument.id == row.id))
    ).first()

    return {
        "doc_id": str(row.id),
        "status": status_row.status.value if status_row and status_row.status else final_status,
        "chunk_count": status_row.chunk_count if status_row else None,
    }


# ── 闭环 P1 感知缝: 漂移聚合 / 有界留存 (管理面) ────────────────────────────


@router.get("/admin/classifier-sample/aggregate")
async def classifier_sample_aggregate(req: Request, user: CurrentUser):
    """闭环漂移观测: 汇总近 window_days 天各意图的采样数 + 平均置信度.

    仅 admin/agent 可见. 未启用感知缝时返回空列表.
    """
    if user.role not in ("admin", "agent"):
        from lumio.shared.auth import AuthorizationError

        raise AuthorizationError("仅管理员/坐席可查看分类样本统计")
    collector = getattr(req.app.state, "trap_collector", None)
    if collector is None:
        return {"enabled": False, "samples": []}
    window_days = int(req.query_params.get("window_days", 7))
    rows = await collector.aggregate(window_days=window_days, min_samples=1)
    return {"enabled": True, "window_days": window_days, "samples": rows}


@router.post("/admin/classifier-sample/purge")
async def classifier_sample_purge(req: Request, user: CurrentUser):
    """有界留存: 手动清理超过 days 天的感知样本. 仅 admin.

    正常运行下由后台调度周期性调用; 此端点用于运维手动清档/演示.
    """
    if user.role != "admin":
        from lumio.shared.auth import AuthorizationError

        raise AuthorizationError("仅管理员可清理分类样本")
    collector = getattr(req.app.state, "trap_collector", None)
    if collector is None:
        return {"enabled": False, "deleted": 0}
    days = int(req.query_params.get("days", 90))
    deleted = await collector.purge_older_than(days=days)
    return {"enabled": True, "days": days, "deleted": deleted}


# ── 闭环 P2 评估/归因: 四层根因 (管理面) ────────────────────────────────────


@router.get("/admin/closed-loop/root-causes")
async def closed_loop_root_causes(req: Request, user: CurrentUser):
    """闭环归因: 对最近 window_days 天的感知样本做多头一致性 + 结果弱标签 +
    四层根因聚合. 仅 admin/agent 可见.

    返回: 总样本数、按层/按 verdict 分布、可操作的失败样本 top (按重排权降序).
    """
    if user.role not in ("admin", "agent"):
        from lumio.shared.auth import AuthorizationError

        raise AuthorizationError("仅管理员/坐席可查看闭环归因")
    db_sf = getattr(req.app.state, "db_session_factory", None)
    if db_sf is None:
        from lumio.services.common.database import get_async_session_factory

        db_sf = get_async_session_factory()
    redis_client = getattr(req.app.state, "redis_client", None)
    window_days = int(req.query_params.get("window_days", 7))
    limit = int(req.query_params.get("limit", 200))
    from lumio.services.common.trap_eval import attribute_recent

    summary = await attribute_recent(
        db_session_factory=db_sf,
        redis=redis_client,
        window_days=window_days,
        limit=limit,
    )
    return {"enabled": True, "window_days": window_days, **summary}


# ── 闭环 P3 版本化优化: 模型注册表 + canary + 样本回流 (管理面) ────────────────


def _require_admin(user: CurrentUser) -> None:
    if user.role != "admin":
        from lumio.shared.auth import AuthorizationError

        raise AuthorizationError("仅管理员可操作")


def _get_registry(req: Request):
    from lumio.services.common.model_registry import ModelRegistry

    reg = getattr(req.app.state, "model_registry", None)
    if reg is None:
        from lumio.shared.config import get_settings

        reg = ModelRegistry(
            state_path=get_settings().classification.model_registry_path,
            allow_ungated=False,
        )
        req.app.state.model_registry = reg
    return reg


def _gates_runner_for(repo_path: str):
    """为候选版本构造 **异步** 评估门 runner (供事件循环内 promote 使用)."""
    from lumio.services.common.eval_gates import EvalGates

    try:
        from lumio.services.common.bert_classifier import BertIntentClassifier

        clf = BertIntentClassifier(model_path=repo_path)

        async def _predict(text: str) -> tuple[str, float]:
            try:
                res = await clf.classify(text)
                return res.primary_intent.value, float(res.primary_confidence)
            except Exception:  # 模型加载/推理失败 → 视为该点未命中 (门会判 FAIL)
                return "", 0.0

    except Exception as exc:  # 依赖不可用
        detail = f"候选模型不可加载: {exc}"

        async def _gates_unavailable(detail: str = detail) -> list[dict]:
            return [{"name": "golden", "passed": False, "detail": detail, "failures": []}]

        return _gates_unavailable

    async def _gates() -> list[dict]:
        results = await EvalGates().arun(_predict)
        return [r.to_dict() for r in results]

    return _gates


@router.get("/admin/model-registry")
async def model_registry_view(req: Request, user: CurrentUser):
    """查看模型注册表 (版本指针 + canary 状态). admin/agent 可见."""
    if user.role not in ("admin", "agent"):
        from lumio.shared.auth import AuthorizationError

        raise AuthorizationError("仅管理员/坐席可查看")
    return _get_registry(req).to_dict()


@router.post("/admin/model-registry/register")
async def model_registry_register(req: Request, user: CurrentUser):
    """登记新版本 (staging). body: {version, path, notes?}. 仅 admin."""
    _require_admin(user)
    body = await req.json()
    registry = _get_registry(req)
    v = registry.register(
        version_id=body["version"],
        path=body["path"],
        notes=body.get("notes", ""),
    )
    return {"ok": True, "version": v.to_dict()}


@router.post("/admin/model-registry/canary")
async def model_registry_canary(req: Request, user: CurrentUser):
    """设 canary + 灰度占比. body: {version, traffic?}. 仅 admin."""
    _require_admin(user)
    body = await req.json()
    registry = _get_registry(req)
    v = registry.set_canary(version_id=body["version"], traffic=float(body.get("traffic", 1.0)))
    return {"ok": True, "canary": v.to_dict(), "traffic": registry._canary_traffic}


@router.post("/admin/model-registry/promote")
async def model_registry_promote(req: Request, user: CurrentUser):
    """canary 过四门评估后升 active. 仅 admin. 不传 force 且门未全 PASS 则拒绝."""
    _require_admin(user)
    registry = _get_registry(req)
    if not registry._canary:
        return {"ok": False, "reason": "无 canary 版本", "report": []}
    canary_path = registry._versions[registry._canary].path
    runner = _gates_runner_for(canary_path)
    report = await runner()  # 事件循环内 await, 避免嵌套 asyncio.run
    ok, report = registry.promote(gate_runner=lambda: report)
    return {"ok": ok, "report": report, "active": registry._active}


@router.post("/admin/model-registry/rollback")
async def model_registry_rollback(req: Request, user: CurrentUser):
    """回退到上一 active. 仅 admin."""
    _require_admin(user)
    registry = _get_registry(req)
    prev = registry.rollback()
    return {"ok": prev is not None, "active": prev}


@router.get("/admin/closed-loop/backflow/candidates")
async def closed_loop_backflow_candidates(req: Request, user: CurrentUser):
    """精选失败样本写人审 staging. admin/agent 可见; 写文件仅 admin 可触发."""
    _require_admin(user)
    db_sf = getattr(req.app.state, "db_session_factory", None)
    if db_sf is None:
        from lumio.services.common.database import get_async_session_factory

        db_sf = get_async_session_factory()
    limit = int(req.query_params.get("limit", 100))
    max_n = int(req.query_params.get("max_n", 50))

    from sqlalchemy import select

    from lumio.services.common.sample_backflow import select_candidates, write_staging
    from lumio.services.common.trap_eval import AttribSample, AttributeEngine
    from lumio.shared.config import get_settings
    from lumio.shared.orm_models import ClassifierSample

    engine = AttributeEngine()
    pairs = []
    async with db_sf() as session:
        stmt = select(ClassifierSample).order_by(ClassifierSample.created_at.desc()).limit(limit)
        rows = (await session.execute(stmt)).scalars().all()
    for r in rows:
        s = AttribSample(
            sample_id=str(r.id),
            text=r.text,
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
        )
        pairs.append((s, engine.attribute(s)))
    candidates = select_candidates(pairs, max_n=max_n)
    cfg = get_settings().classification
    staged = write_staging(candidates, cfg.backflow_review_path)
    return {"ok": True, "staged": staged, "review_path": cfg.backflow_review_path}


@router.post("/admin/closed-loop/backflow/finalize")
async def closed_loop_backflow_finalize(req: Request, user: CurrentUser):
    """把 review 中人工批准的条目并入种子训练集 (human-in-loop). 仅 admin."""
    _require_admin(user)
    from lumio.services.common.sample_backflow import finalize_confirmed
    from lumio.shared.config import get_settings

    cfg = get_settings().classification
    added, version = finalize_confirmed(cfg.backflow_review_path, cfg.seed_dataset_path)
    return {"ok": True, "added": added, "version": version}
