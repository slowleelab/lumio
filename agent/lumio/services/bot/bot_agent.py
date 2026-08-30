"""Bot 对话 Agent — 确定性路由实现

规则引擎做路由，LLM 做生成，asyncio 做并行。
不依赖任何 Agent 框架（LangGraph / PydanticAI）。

处理流程:
  classify_intent → 规则路由 {knowledge, business, fallback}
  → transfer_check → {respond, transfer}
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
import uuid as uuid_module
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from lumio.services.bot.input_gate import InputGate
from lumio.services.bot.prompts import (
    _SUMMARIZE_SYSTEM_PROMPT,
    BUSINESS_SYSTEM_PROMPT,
    BUSINESS_TRANSFER_TEMPLATE,
    CLARIFY_RESPONSE,
    CLARIFY_RESPONSES,
    CONFIRM_FOLLOWUP_RESPONSE,
    CRISIS_RESPONSE,
    FALLBACK_SYSTEM_PROMPT,
    FAREWELL_RESPONSE,
    GREETING_RESPONSE,
    KNOWLEDGE_SYSTEM_PROMPT,
    SENSITIVE_REPLY_BRIDGE_RESPONSE,
)
from lumio.services.bot.slot_tracker import _ENTITY_TO_SLOT, SlotTracker
from lumio.services.bot.tool_executor import ConfirmDecision, ToolCallingExecutor, detect_confirmation
from lumio.services.bot.tool_selection import TOOL_INTENTS, select_tools_for_intent
from lumio.services.common.bert_classifier import ood_verdict
from lumio.services.common.classifier import IntentClassifier, get_domain
from lumio.services.common.decision_log import DecisionAction, log_decision
from lumio.services.common.degradation import DegradationManager
from lumio.services.common.transfer import TRANSFER_CONFIDENT_CONF, TransferChecker
from lumio.shared.config import get_settings
from lumio.shared.metrics import TOOL_CONFIRMATIONS
from lumio.shared.models import (
    SENSITIVE_INTENTS,
    DegradationLevel,
    Entity,
    IntentLabel,
    IntentResult,
    PendingAction,
    RetrieveRequest,
    RetrieveResponse,
    SentimentLabel,
    SessionPhase,
    TransferTriggerLevel,
    VerificationRequest,
    VerificationResult,
    normalize_intent,
)
from lumio.shared.pii import pan_to_tail
from lumio.shared.token_utils import estimate_tokens as _token_estimate  # P1-8 统一入口
from lumio.shared.tracing import traced


def _effective_knowledge_source(raw_source: str, context: str) -> str:
    """回复来源记账: LLM 成功生成且本轮带 RAG 上下文 → knowledge.

    generate_with_fallback 的 source 表达"文本由谁产出"(llm/retrieval/template),
    但审计与 RAG 看板的口径是"有没有用知识" —— 带上下文的 LLM 生成此前恒记
    llm, 审计页看起来像"RAG 没搜到"(会话 9d64b59 复盘)。
    """
    return "knowledge" if raw_source == "llm" and context else raw_source


def _estimate_tokens(text: str) -> int:
    """bot_agent 历史的 token 估算包装, +4 消息格式开销 (role/content 包装).

    P1-8: 之前 bot_agent 有自己的 _estimate_tokens 实现, 与 token_utils.estimate_tokens 重复.
    现统一委托 token_utils, 但保留 +4 开销语义 (测试 TestEstimateTokens 依赖此行为).
    """
    return _token_estimate(text, base_overhead=4)


if TYPE_CHECKING:
    from elasticsearch import AsyncElasticsearch
    from pymilvus import Collection

    from lumio.services.bot.tool_executor import ToolCallingExecutor
    from lumio.services.common.embedding import EmbeddingCircuitBreaker
    from lumio.services.common.session import SessionManager
    from lumio.shared.models import PendingAction

logger = logging.getLogger(__name__)


# ── Token 估算（统一委托 lumio.shared.token_utils，避免分叉实现） ──
# P1-8: 之前 bot_agent 有自己的 _estimate_tokens 实现, 与 token_utils.estimate_tokens 重复
# 且 token 估算系数不一致 (这里 0.55/0.3/0.8 vs token_utils 0.5/0.3/0.75).
# 统一改用 token_utils, 保证预算/压缩/KV cache 估算口径一致.


# ── 重要性标记 ──

# 摘要触发的最小裁剪 token 量 (P2-28): 低于此省下的上下文预算不值一次 LLM 往返
# (且 LLM 摘要与业务生成在同一串行 GPU 槽抢占, 见 _load_history 注释)。
_SUMMARY_MIN_TRIMMED_TOKENS = 512

# 无检索上下文时的澄清门槛: 泛化意图(faq/闲聊)且置信低于此值才澄清。
# 高于此值视为"置信足够", 放行让 LLM 依托会话记忆续答/追问 (如 faq@0.6"那分期呢")。
_NO_CONTEXT_GATE_CONF = 0.5

# 把"transfer_agent"当真·客户主动转人工所需的最低置信 (与 transfer._check_l2 同源阈值):
# 低置信的 transfer_agent 多为乱码/连续低置信被 LLM 误判(把握通常 <0.5), 不能据此拉真人坐席;
# 真"转人工/按按钮"把握高(常 ≥0.7), 不受影响。敏感意图(挂失/投诉)走主意图即时转, 合规底线优先。

# L3『先确认再转』: 连续低置信/兜底累计触发转人工时, 不再直接派真人坐席, 改为挂一个待确认.
# 下一轮客户明确确认才真正转 —— 既防『灌乱码刷真人』滥用, 也不再把这类升级谎标成『客户主动请求』.
# 真·转人工(L1 关键词 / L2 高置信 transfer_agent / 敏感意图)不走此确认, 仍即时转。
_TRANSFER_OFFER_PROMPT = "这几次似乎还没能帮您解决问题，需要为您转接人工客服吗？（回复“是”或“需要”即可）"
_TRANSFER_OFFER_TIMEOUT = timedelta(seconds=120)

# 转人工邀请的确认词独立于通用 detect_confirmation: 邀请话术明确引导客户回"是/需要",
# 而通用的 confirm 关键词表不含这两个词 —— 客户按话术回复会被误判成 unclear → 邀请被无辜清除.
# 取消词需覆盖"不用/不需要"等否定表述 (取消优先判定, 规避"不..."被当作确认).
_TRANSFER_OFFER_CANCEL_KEYWORDS = frozenset({"不用", "不需要", "不要", "不了", "算了", "取消", "不转", "不用吧"})
_TRANSFER_OFFER_CONFIRM_KEYWORDS = frozenset(
    {"是", "需要", "要", "好的", "是的", "确认", "确定", "可以", "行", "转人工", "嗯"}
)

_IMPORTANT_KEYWORDS = [
    "投诉",
    "举报",
    "银保监",
    "银监会",
    "人行",
    "央行",
    "律师函",
    "法务",
    "法院",
    "起诉",
    "盗刷",
    "挂失",
    "冻结",
    "风险",
    "承诺",
    "保证",
    "一定解决",
    "转人工",
    "人工客服",
]


def _is_important(content: str) -> bool:
    return any(kw in content for kw in _IMPORTANT_KEYWORDS)


class LumioAgent:
    """Lumio 对话 Agent — 确定性路由

    规则引擎决定处理路径，LLM 仅用于内容生成。
    """

    def __init__(
        self,
        classifier: IntentClassifier,
        degradation_mgr: DegradationManager,
        transfer_checker: TransferChecker,
        session_manager: SessionManager,
        es_client: AsyncElasticsearch | None = None,
        milvus_collection: Collection | None = None,
        embedding_breaker: EmbeddingCircuitBreaker | None = None,
        tool_executor: ToolCallingExecutor | None = None,
        reranker: Any | None = None,  # P0-3 上下文工程: RerankerProvider
        es_breaker: Any | None = None,  # P1-13: ES 熔断器 (app.state.es_breaker)
        milvus_breaker: Any | None = None,  # P1-13: Milvus 熔断器
    ) -> None:
        self._classifier = classifier
        self._degradation_mgr = degradation_mgr
        self._transfer_checker = transfer_checker
        self._session_manager = session_manager
        self._es_client = es_client
        self._milvus_collection = milvus_collection
        self._embedding_breaker = embedding_breaker
        # 工具执行器（MCP_ENABLED=False 时为 None，走原有降级链，零回归）
        self._tool_executor = tool_executor

        # 目标架构 v2: 链 B 查询轻链路 + ⑦ 出站合规闸门 + ⑥ 引用标题缓存
        from lumio.services.bot.outbound_guard import OutboundGuard
        from lumio.services.bot.query_chain import QueryChain

        self._query_chain = QueryChain(
            mcp_client=getattr(tool_executor, "_mcp", None),
            redis_client=session_manager._redis if session_manager else None,
            degradation_mgr=degradation_mgr,
        )
        from lumio.shared.safety import safety_filter as _safety_filter

        self._outbound_guard = OutboundGuard(_safety_filter, CLARIFY_RESPONSE)
        self._citation_title_cache: dict[str, str] = {}
        self._last_citation_docids: list[str] = []
        # P0-3: 精排器 (loss-in-middle 缓解 + 相关性阈值过滤)
        self._reranker = reranker
        # P1-13: ES/Milvus 熔断器 — 检索前先查熔断状态, 避免双挂时被动靠异常降级
        self._es_breaker = es_breaker
        self._milvus_breaker = milvus_breaker
        # 后台 task 引用集合, 避免被 GC
        self._pending_tasks: set[asyncio.Task[Any]] = set()
        # P1-10: 摘要任务 per-session 锁 (多轮快速对话时串行化增量摘要)
        self._summary_locks: dict[str, asyncio.Lock] = {}
        # P2: 多信号噪声闸 (默认关, 由 config noise_gate_enabled 控制)
        self._gate = InputGate()
        # P3: 疑似误杀探针 — 上轮判"回话放行"的会话, 若紧接着又被噪声门拦, 标记回流.
        # 用定长 OrderedDict(按访问序)记录最近放行, 防长运行后无界增长; freshness 见常量.
        self._recent_reply_pass: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def _spawn_task(self, coro: Any) -> asyncio.Task[Any]:
        """创建并持有后台 task 引用, 完成后自动从集合移除."""
        task = asyncio.create_task(coro)
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)
        return task

    # ── 公共接口 ──

    @traced("Agent: bot_run")
    async def run(self, session_id: str, user_input: str, customer_id: str | None = None) -> dict[str, Any]:
        """运行 Bot Agent，返回与旧版兼容的 dict"""
        _turn_t0 = time.monotonic()  # 全链路总耗时锚点 (CHAIN_COMPLETE 埋点用)
        # ── 跨会话画像学习（异步，首次对话时从历史推断客户画像）──
        if customer_id and self._session_manager:
            try:
                from lumio.services.bot.customer_memory import apply_learned_profile

                db_sf = getattr(self, "_db_session_factory", None)
                if db_sf:
                    task = self._spawn_task(
                        apply_learned_profile(customer_id, session_id, db_sf, self._session_manager)
                    )

                    # 异常回调
                    def _on_profile_done(t: asyncio.Task[None]) -> None:
                        if exc := t.exception():
                            logger.error(
                                "apply_learned_profile 失败: session=%s, err=%s",
                                session_id,
                                exc,
                            )

                    task.add_done_callback(_on_profile_done)
            except Exception as exc:
                logger.warning("apply_learned_profile 调度失败: %s", exc)

        # ── 工具确认状态机拦截：存在未过期 pending_action 时，本轮解读为确认/取消 ──
        # P0 修复: 此前门控挂在 _tool_executor is not None 上, 而 MCP 关闭的环境恒为 None —
        # L3 转人工确认(TRANSFER_OFFER, 不需要任何工具)因此从未触发, 客户回"是"永远被
        # 当成普通消息重新分类。确认拦截只依赖会话状态, 工具类 pending 在 _handle_pending_action
        # 内自行兜底 (无执行器则清除并放行新消息)。
        result: dict[str, Any] | None = None
        if self._session_manager is not None:
            try:
                state = await self._session_manager.get_session(session_id)
            except Exception:
                state = None
            if state is not None and state.pending_action is not None:
                result = await self._handle_pending_action(session_id, user_input, state, customer_id)
                if not result.get("pending_released"):
                    return result
                # pending_released: 确认窗口连续无法判定已自动取消, 继续按新消息正常处理
                logger.info("确认窗口已自动取消, 继续处理新消息: session=%s", session_id)

        # P1-9 危机干预: 客户表达自伤/轻生意图 → 立即安抚话术 + 强制转人工.
        # 优先级最高 (先于问候/告别/意图分类), 不得走常规 LLM 应答.
        try:
            from lumio.shared.safety import safety_filter

            if safety_filter.is_crisis_input(user_input):
                logger.warning("危机干预触发: session=%s input=%r", session_id, user_input[:50])
                return self._build_result(
                    session_id,
                    user_input,
                    CRISIS_RESPONSE,
                    "template",
                    "crisis",
                    should_transfer=True,
                    transfer_reason="crisis_intervention: 客户表达自伤/轻生意图",
                )
        except Exception:
            pass  # 危机检测失败时放行 (不阻断正常对话)

        # ── 输入层护栏（生产级硬拦截）：身份/角色覆盖 + 第三方信息查询 ──
        # 在进入路由/LLM 之前由确定性规则直接拦截，返回固定话术，零 LLM 参与。
        # 优先级仅低于危机干预；正常金融提问不命中，零回归。
        try:
            from lumio.services.bot.input_guard import check_input_guard

            guard_hit = check_input_guard(user_input)
            if guard_hit is not None:
                logger.info(
                    "输入护栏拦截: category=%s session=%s input=%r",
                    guard_hit.category,
                    session_id,
                    user_input[:50],
                )
                return self._build_result(session_id, user_input, guard_hit.response, "guard", "faq")
        except Exception:
            pass  # 护栏异常时放行 (不阻断正常对话)

        # 快速路径：问候/告别不调 LLM
        if _is_greeting(user_input):
            return self._build_result(session_id, user_input, GREETING_RESPONSE, "template", "chitchat")
        if _is_farewell(user_input):
            # P1-11 修复: 告别时真正结束会话 + 清除 pending_action.
            # 此前只回模板话术, 会话停留 BOT_ACTIVE, pending 残留 —
            # 客户回来续聊时第一句会被当成确认/取消误判.
            if self._session_manager is not None:
                try:
                    state = await self._session_manager.get_session(session_id)
                    if state is not None and state.current_phase.value != "ended":
                        if state.pending_action is not None:
                            await self._clear_pending_action(session_id, state.version)
                        await self._session_manager.transition_phase(
                            session_id,
                            SessionPhase.ENDED,
                            reason="customer_farewell",
                        )
                        logger.info("告别结束会话: session=%s", session_id)
                except Exception as exc:
                    logger.debug("告别结束会话失败 (不阻断回复): session=%s err=%s", session_id, exc)
            return self._build_result(session_id, user_input, FAREWELL_RESPONSE, "template", "chitchat")
        # ── FAQ 三路匹配优先 (精确 Redis / 语义 Milvus faq_qa) ──
        # 命中直接返回标准答案, 免意图分类/检索/LLM; 检索日志落 kb_faq_search_log
        # (控制台 FAQ 命中率指标数据源)。此前客户链路从不调 search_faq, 发布的
        # FAQ 实际不参与应答 (语义索引写入也是桩函数, 一并修复)。
        # 位置: 危机/护栏/问候告别之后、意图分类之前 —— 业务域问题 (如积分有效期)
        # 会被路由进工具编排, 埋在 _retrieve 内永远走不到。
        try:
            from lumio.services.common.faq_service import get_faq, search_faq

            session_manager = self._session_manager
            faq_embedding = (
                self._embedding_breaker.provider
                if self._embedding_breaker and self._embedding_breaker.is_available
                else None
            )
            faq_res = await search_faq(
                query=user_input,
                redis_client=session_manager._redis if session_manager else None,
                embedding_provider=faq_embedding,
                milvus_collection=self._milvus_collection,
                user_role="customer",
                session_factory=(
                    session_manager._resolve_factory()
                    if session_manager is not None and hasattr(session_manager, "_resolve_factory")
                    else None
                ),
                session_id=session_id,
            )
            if faq_res["match_type"] in ("exact", "semantic") and faq_res["results"]:
                top = faq_res["results"][0]
                answer = top.get("answer")
                if not answer and top.get("faq_id"):
                    from lumio.services.common.faq_service import get_faq

                    faq_sf = (
                        session_manager._resolve_factory()
                        if session_manager is not None and hasattr(session_manager, "_resolve_factory")
                        else None
                    )
                    # chunk_id 带变体序号后缀 (faqid#0), 回查前剥离取真实 FAQ UUID
                    real_id = top["faq_id"].split("#", 1)[0]
                    detail = await get_faq(faq_sf, real_id) if faq_sf else None
                    answer = detail.get("answer") if detail else None
                if answer:
                    logger.info("FAQ 短路命中 (%s): %s", faq_res["match_type"], top.get("question", "")[:50])
                    try:
                        log_decision(
                            session_id=session_id,
                            agent_name="bot_agent",
                            action=DecisionAction.FAQ_DIRECT,
                            reasoning=f"FAQ 直出 ({faq_res['match_type']})",
                            evidence={"faq_id": top.get("faq_id", ""), "question": top.get("question", "")[:80]},
                            latency_ms=0.0,
                            turn_id=uuid_module.uuid4().hex[:16],
                            customer_id=customer_id,
                        )
                    except Exception:
                        logger.debug("decision_log 记录失败(不阻断)")
                    return self._build_result(session_id, user_input, answer, "faq", "faq")
        except Exception as faq_err:
            logger.warning("FAQ 优先匹配失败, 走常规流程: %s", faq_err)

        try:
            # 闭环 P1 感知缝: 提供会话/客户归属 (采样器被动读取, 业务不感知采样逻辑)
            from lumio.services.common.trap_collector import reset_trap_context, set_trap_context

            ctx = set_trap_context(session_id, customer_id)
            try:
                # 1. 意图分类 + 实体抽取 + 情感分析
                _t_intent = time.monotonic()
                intent_result, entities, sentiment = await self._classify(user_input, session_id)
                _intent_ms = (time.monotonic() - _t_intent) * 1000
                # 指代消解: 当前句回指历史实体时解析回具体所指(默认关 closed_loop 灰度).
                # 位于 _classify 之后、槽位填充/噪声门/路由之前, 使消解实体与各处共用同一份.
                entities = await self._resolve_anaphora(session_id, user_input, entities, intent_result.primary_intent)
            finally:
                reset_trap_context(ctx)

            # 2. 规则路由
            domain = get_domain(intent_result.primary_intent)
            history = await self._load_history(session_id)

            # E2 决策可解释: 记录意图分类决策 (涵盖每轮实际路由, 供监管/客户审计)
            try:
                log_decision(
                    session_id=session_id,
                    agent_name="bot_agent",
                    action=DecisionAction.INTENT_CLASSIFY,
                    reasoning=f"意图={intent_result.primary_intent.value} 置信={intent_result.primary_confidence:.2f} 域={domain}",
                    evidence={
                        "intent": intent_result.primary_intent.value,
                        "confidence": intent_result.primary_confidence,
                        "domain": domain,
                        # P: 记录候补意图 - 敏感/转人工穿透分析此前因缺失候补而无据可查,
                        # 本次会话 4d22 靠复现才定位, 现补进决策日志供事后审计直接锁定.
                        "alternatives": [a.value for a in (intent_result.alternatives or [])],
                        # P0 快慢分歧: 记录两路各自的意图/置信, 事后审计可直接定位
                        # "哪一路在幻觉" (会话 e33d1fa8 审核期间靠手工复现才拼出证据链).
                        "fast_conf": intent_result.fast_conf,
                        "fast_intent": intent_result.fast_intent.value if intent_result.fast_intent else None,
                    },
                    latency_ms=_intent_ms,
                    turn_id=uuid_module.uuid4().hex[:16],
                    customer_id=customer_id,
                )
            except Exception:
                logger.debug("decision_log 记录失败(不阻断): session=%s", session_id)

            # 2.5 渐进式工具暴露：仅当开关开启 + 有可用工具 + 命中查询类工具意图时，
            #     打通 MCP 工具编排路径（在 domain 分派之前）。开关关闭时整段不进入，路由 100% 同现状。
            # P-指代修复: 统一出口 —— 所有 handler 的 result 在此补入真实 entities.
            # 此前各 handler 调 _build_result 时不传 entities, 落库实体恒空,
            # 历史实体池(last_entities)从不累积, 指代消解与"[已知实体]"内存全部空转.
            result = None
            routing_v2 = get_settings().bot.routing_v2_enabled
            if routing_v2:
                # 目标架构 ④ 两级路由决策: 决策一交易性质 / 决策二只读四分流。
                # 既有闸门 (噪声门/敏感回话/等待快照) 均在 handler 内部, 位置不动。
                result = await self._dispatch_v2(
                    session_id,
                    user_input,
                    intent_result,
                    history,
                    entities,
                    sentiment,
                    domain,
                    customer_id,
                )
            elif (
                get_settings().mcp.progressive_disclosure_enabled
                and self._tool_executor is not None
                and self._tool_executor.has_tools()
                and normalize_intent(intent_result.primary_intent.value) in TOOL_INTENTS
            ):
                # 会话 48882b05 复盘: 本拦截在 domain 分派之前, 上面的 intent_classify 决策
                # 记的 域=knowledge(映射表) 与实际走工具编排不符, 审计会误判 "该走 RAG 却
                # 没走"。此处补一条实际路由决策, 显式声明劫持发生及原因。
                try:
                    log_decision(
                        session_id=session_id,
                        agent_name="bot_agent",
                        action=DecisionAction.TOOL_CALL,
                        reasoning=f"工具编排接管路由: intent={intent_result.primary_intent.value} 置信={intent_result.primary_confidence:.2f}",
                        evidence={
                            "actual_route": "tool_orchestration",
                            "declared_domain": domain,
                            "intent": intent_result.primary_intent.value,
                            "confidence": intent_result.primary_confidence,
                        },
                        turn_id=uuid_module.uuid4().hex[:16],
                        customer_id=customer_id,
                    )
                except Exception:
                    logger.debug("decision_log 记录失败(不阻断): session=%s", session_id)
                result = await self._handle_tool(
                    session_id, user_input, intent_result, history, entities, sentiment, customer_id
                )

            # P0 多轮治理: 噪声门回话豁免所需"上文缺槽快照", 提前到 domain 分派前读取,
            # 避免各 handler 内 _load_slot_prompt 因意图切换重置 tracker 而清掉"上文在等什么"。
            if result is None:
                gate_missing_slots: list[tuple[str, str]] = []
                if get_settings().classification.rephrase_guard_enabled:
                    gate_missing_slots = await self._missing_required_slots(session_id, intent_result.primary_intent)

                if domain == "knowledge":
                    result = await self._handle_knowledge(
                        session_id,
                        user_input,
                        intent_result,
                        history,
                        entities,
                        sentiment,
                        missing_slots=gate_missing_slots,
                    )
                elif domain in ("business", "risk", "complain", "transfer"):
                    # risk/complain/transfer 域与 business 同走 _handle_business:
                    # 敏感写直排人工/建工单、transfer 派发、工具编排的逻辑都在其中
                    # (draft-0.3 §2 下游契约; 域细分不改派发行为)
                    result = await self._handle_business(
                        session_id, user_input, intent_result, history, entities, sentiment, customer_id
                    )
                else:
                    result = await self._handle_fallback(
                        session_id,
                        user_input,
                        intent_result,
                        history,
                        entities,
                        sentiment,
                        missing_slots=gate_missing_slots,
                    )

            # 统一把增强后的实体写回 result, 保证 slot 填充/噪声门/持久化共用同一份数据
            if result and entities:
                result["entities"] = entities

            # ⑦ 出站合规闸门 (v2): 生成类回复过敏感词/幻觉检查, 拦截替换为澄清话术
            if result is not None and routing_v2 and result.get("response_source") in ("knowledge", "llm"):
                verdict = self._outbound_guard.check(
                    str(result.get("response", "")),
                    grounding_source=str(result.get("retrieval_context", "") or ""),
                )
                if not verdict.passed:
                    logger.warning("出站闸门拦截(%s): session=%s", verdict.reason, session_id)
                    result["response"] = verdict.reply
                    result["response_source"] = "clarify"
                    try:
                        log_decision(
                            session_id=session_id,
                            agent_name="bot_agent",
                            action=DecisionAction.OUTBOUND_GUARD,
                            reasoning=f"出站拦截({verdict.reason})",
                            evidence={"reason": verdict.reason},
                            latency_ms=0.0,
                            turn_id=uuid_module.uuid4().hex[:16],
                            customer_id=customer_id,
                        )
                    except Exception:
                        logger.debug("decision_log 记录失败(不阻断)")

            # 全链路完成埋点: 输入→输出的总耗时 (全链路监控的端到端锚点)
            if result is not None:
                total_ms = (time.monotonic() - _turn_t0) * 1000
                try:
                    log_decision(
                        session_id=session_id,
                        agent_name="bot_agent",
                        action=DecisionAction.CHAIN_COMPLETE,
                        reasoning=f"链路完成: source={result.get('response_source')} 总耗时 {total_ms:.0f}ms",
                        evidence={
                            "source": result.get("response_source"),
                            "intent": intent_result.primary_intent.value,
                            "total_ms": round(total_ms, 1),
                        },
                        latency_ms=total_ms,
                        turn_id=uuid_module.uuid4().hex[:16],
                        customer_id=customer_id,
                    )
                except Exception:
                    logger.debug("decision_log 记录失败(不阻断)")

            # P0 等待快照维护 (会话 1fb54681 复盘): 本轮回复若在"等客户补充参数/槽位"
            # (槽位追问/工具参数索取), 把缺槽快照落盘 -- 下一轮短回复无论被分类成什么,
            # 噪声门都据此判回话放行。其余回复(澄清/完整作答/转人工/确认)不再等任何槽,
            # 覆写清空防陈旧快照误豁免后续无关输入。
            if result is not None:
                await self._update_awaiting_snapshot(session_id, intent_result.primary_intent, result)

            # P0 修复: 澄清轮的 L3 补判 (run() 统一出口). 噪声门/低置信分支在 handler 内
            # 提前 return clarify, 永远到不了各路径末尾的 _check_transfer -- 连续低置信的
            # 客户只会在澄清话术里无限打转 (会话 178351b41: streak=30, 零次转人工邀请).
            # 越线那一轮 (streak == 阈值) 把澄清换成邀请; 客户明确确认才派真人.
            if result is not None and result.get("response_source") == "clarify" and not result.get("should_transfer"):
                offer = await self._maybe_offer_transfer_after_clarify(
                    session_id, user_input, intent_result, entities, sentiment
                )
                if offer is not None:
                    result = offer

            return result

        except Exception as e:
            logger.warning("Bot Agent 执行失败: %s", e)
            # P0-4 修复: 顶层异常路径也检查转人工 (异常多因上游故障, 客户诉求应尽快转人工)
            should_transfer = False
            transfer_reason = ""
            with contextlib.suppress(Exception):
                should_transfer, transfer_reason, transfer_level = await self._check_transfer(
                    user_input, None, SentimentLabel.NEUTRAL, session_id=session_id
                )
                # L3『先确认再转』: 累计低置信/兜底不派真人 (此降级路径仅返回兜底稿, 不挂确认).
                if transfer_level == TransferTriggerLevel.L3:
                    should_transfer = False
                    transfer_reason = ""
            return self._build_result(
                session_id,
                user_input,
                self._degradation_mgr._degrader.hardcoded_fallback(),
                "fallback",
                "faq",
                should_transfer=should_transfer,
                transfer_reason=transfer_reason,
            )

    # ── 路径处理 ──

    async def _resolve_anaphora(
        self, session_id: str, user_input: str, entities: list[Entity], intent: IntentLabel | None = None
    ) -> list[Entity]:
        """指代消解: 当前句回指历史实体时, 解析回某条具体所指并并入本轮实体。

        由 anaphora_resolver_enabled 控制(默认关)。从 领域 state.last_entities 读取历史
        实体池 —— 该池此前因 _build_result 不传 entities 而恒空, 由 A 阶段修复后真正累积。
        另读取"上轮缺槽"(missing_slots)喂给零主回指补槽。返回不变(未开启/无历史实体)。
        LLM 兜底层仅当 anaphora_llm_fallback_enabled 开启时联动。
        """
        from lumio.services.bot.anaphora_resolver import AnaphoraResolver
        from lumio.shared.config import get_settings

        if not get_settings().classification.anaphora_resolver_enabled or self._session_manager is None:
            return entities
        try:
            state = await self._session_manager.get_session(session_id)
        except Exception:
            state = None
        if state is None or not state.last_entities:
            return entities
        intent = intent or IntentLabel.FAQ
        # 上轮仍在等的必填槽(零主回指据此从历史补该槽唯一候选值)
        try:
            missing_slots = await self._missing_required_slots(session_id, intent)
        except Exception:
            missing_slots = []
        llm = getattr(self._degradation_mgr, "_llm", None)
        resolver = AnaphoraResolver(llm_client=llm or None)
        enriched, meta = await resolver.resolve(user_input, state.last_entities, entities, missing_slots=missing_slots)
        source = meta.get("source")
        if source not in (None, "none"):
            logger.info(
                "指代消解 %s: session=%s source=%s candidates=%s resolved=%s",
                user_input[:30],
                session_id,
                source,
                meta.get("candidates"),
                meta.get("resolved"),
            )
        return enriched

    async def _classify(
        self, user_input: str, session_id: str | None = None
    ) -> tuple[IntentResult, list[Entity], SentimentLabel]:
        """意图分类 + 实体抽取 + 情感分析 (接入全链路: Agent: intent_classify)

        session_id 存在时加载最近对话轮次作为多轮上下文给 BERT 快路径,
        让"上轮说分期, 这轮问费率"等追问能借上下文落在正确意图而非当全新单句。
        """
        try:
            history = await self._classify_context(session_id)
            from lumio.shared.tracing import _TRACING_ENABLED, _get_tracer

            tracer = _get_tracer() if _TRACING_ENABLED else None
            if tracer is None:
                intent_result, entities, sentiment, _ = await self._classifier.classify(user_input, history=history)
                return intent_result, entities, sentiment

            with tracer.start_as_current_span("Agent: intent_classify") as span:
                try:
                    intent_result, entities, sentiment, source = await self._classifier.classify(
                        user_input, history=history
                    )
                except Exception:
                    span.set_attribute("error", True)
                    raise
                span.set_attribute("intent", intent_result.primary_intent.value)
                span.set_attribute("confidence", float(intent_result.primary_confidence))
                span.set_attribute("source", str(source))  # bert / rule / llm / fallback
                return intent_result, entities, sentiment
        except Exception:
            return IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.0), [], SentimentLabel.NEUTRAL

    async def _classify_context(self, session_id: str | None) -> list[dict[str, Any]] | None:
        """拉取最近对话轮次, 拼成 BERT 多轮上下文 (speaker/content, 时间升序)。

        轻量取近几轮 (不触发 _load_history 的 LLM 摘要/压缩), 失败静默返回 None,
        退化为单句分类, 不阻断主线。
        """
        if not session_id or self._session_manager is None:
            return None
        try:
            turns = await self._session_manager.get_history(session_id, limit=6)
        except Exception:
            return None
        return [
            {
                "speaker": t.speaker,
                "content": t.content,
                # 供上下文过滤使用: clarify 收尾的乱码轮对不进 BERT 输入 (见 _build_dialog_input)
                "confidence": t.confidence,
                "response_source": t.response_source,
            }
            for t in turns
            if t.speaker in ("customer", "bot") and t.content
        ][-6:]

    # ── 目标架构 v2 两级路由: 决策一/二 与四条执行链 ──

    async def _dispatch_v2(
        self,
        session_id: str,
        user_input: str,
        intent_result: IntentResult,
        history: list[dict[str, str]],
        entities: list[Entity] | None,
        sentiment: SentimentLabel,
        domain: str,
        customer_id: str | None,
    ) -> dict[str, Any]:
        """④ 两级路由决策

        决策一: 交易性质三分流 (高风险转人工 / 金融交易链 A / 查询链 B);
        决策二: 只读咨询四分流 (并行竞速 / 复合意图 / RAG 链路)。
        任何链内部失败均回落既有知识/业务路径, 不产生无回复出口。
        """
        from lumio.services.bot.routing import (
            TrafficClass,
            classify_traffic,
            decision_two,
            detect_composite,
        )

        intent = intent_result.primary_intent
        confidence = intent_result.primary_confidence
        traffic = classify_traffic(intent)
        composite = detect_composite(intent, list(intent_result.alternatives or []), user_input)
        try:
            log_decision(
                session_id=session_id,
                agent_name="bot_agent",
                action=DecisionAction.TOOL_CALL
                if traffic != TrafficClass.CONSULTING
                else DecisionAction.INTENT_CLASSIFY,
                reasoning=f"v2 路由: 决策一={traffic.value} 决策二预备 composite={composite}",
                evidence={
                    "traffic_class": traffic.value,
                    "confidence": confidence,
                    "composite": composite,
                    "alternatives": [a.value for a in (intent_result.alternatives or [])],
                },
                turn_id=uuid_module.uuid4().hex[:16],
                customer_id=customer_id,
            )
        except Exception:
            logger.debug("decision_log 记录失败(不阻断): session=%s", session_id)

        # ── 决策一 ──
        if traffic == TrafficClass.HIGH_RISK:
            # 投诉/争议/转人工诉求: 保留 _handle_business 既有语义 (高置信敏感直转人工
            # + 建工单; 低置信走内部噪声门澄清), 决策留痕已声明分类依据
            return await self._handle_business(
                session_id, user_input, intent_result, history, entities, sentiment, customer_id
            )
        if traffic == TrafficClass.FINANCIAL_TRANSACTION:
            # 链 A · 交易链路: 工具编排 + 敏感确认状态机; 无工具回落知识
            if self._tool_executor is not None and self._tool_executor.has_tools():
                return await self._handle_tool(
                    session_id, user_input, intent_result, history, entities, sentiment, customer_id
                )
            return await self._handle_knowledge(session_id, user_input, intent_result, history, entities, sentiment)
        if traffic == TrafficClass.READ_ONLY_QUERY:
            # 复合意图 (查询 + 解释/FAQ 诉求) 优先走链 C: 纯定义类问题会被
            # 查询链路的缺参反问卡死 (会话 2b3b2613 实测: "信用额度是什么"三连问
            # 卡号, 用户永远出不去)。链 C 缺参/无工具时自然回落知识路径。
            if composite:
                comp = await self._handle_composite(
                    session_id, user_input, intent_result, history, entities, sentiment, customer_id
                )
                if comp is not None:
                    return comp
            # 链 B · 查询轻链路: 直连工具 + 缓存; 无工具/链失败回落
            if self._tool_executor is not None and self._tool_executor.has_tools():
                qc = await self._handle_query_chain(
                    session_id, user_input, intent_result, history, entities, sentiment, customer_id
                )
                if qc is not None:
                    return qc
            return await self._handle_tool(
                session_id, user_input, intent_result, history, entities, sentiment, customer_id
            )

        # ── 决策二 (CONSULTING) ──
        route = decision_two(confidence, composite)
        if route.value == "parallel_race":
            return await self._handle_parallel_race(session_id, user_input, intent_result, history, entities, sentiment)
        if route.value == "composite":
            comp = await self._handle_composite(
                session_id, user_input, intent_result, history, entities, sentiment, customer_id
            )
            if comp is not None:
                return comp
        return await self._handle_knowledge(session_id, user_input, intent_result, history, entities, sentiment)

    async def _handle_query_chain(
        self,
        session_id: str,
        user_input: str,
        intent_result: IntentResult,
        history: list[dict[str, str]],
        entities: list[Entity] | None,
        sentiment: SentimentLabel,
        customer_id: str | None,
    ) -> dict[str, Any] | None:
        """链 B · 查询轻链路: 槽位参数 → 直连工具 → 单次摘要。

        Returns: None = 链路不适用 (无工具/失败), 调用方回落; 非 None = 已产出回复。
        """
        from lumio.services.bot.tool_selection import select_tools_for_intent

        # 填槽并持久化 (跨轮继承/历史反填复用既有机制), 再读回槽位值
        await self._load_slot_prompt(session_id, intent_result.primary_intent, entities or [], user_input)
        slot_values: dict = {}
        if self._session_manager is not None:
            try:
                state = await self._session_manager.get_session(session_id)
                for k, v in (getattr(state, "slot_values", None) or {}).items():
                    slot_values[k] = getattr(v, "value", v)
            except Exception:
                pass

        tool_names = select_tools_for_intent(
            intent_result.primary_intent, intent_result.primary_confidence, get_settings().mcp
        )
        qc = await self._query_chain.run(
            intent_label=intent_result.primary_intent.value,
            user_input=user_input,
            tool_names=tool_names,
            slot_values=slot_values,
            customer_id=customer_id,
            history=history,
        )
        if qc.error:
            logger.info("查询链路失败回落: session=%s err=%s", session_id, qc.error)
            return None
        if qc.missing_params:
            # 缺参先探知识 (会话 2b3b2613 复盘: "信用额度是什么"缺卡号被反问,
            # 但定义类问题知识库可答 —— 反问个人参数前先看 RAG 有据与否)
            try:
                probe = await self._retrieve(user_input)
            except Exception:
                probe = ""
            if probe:
                logger.info("查询链路缺参但 RAG 有据, 转知识路径: session=%s", session_id)
                return await self._handle_knowledge(session_id, user_input, intent_result, history, entities, sentiment)
            # 反问澄清 (TW6→S1 补槽回流): 槽位 prompt 过弱时用参数中文名直问
            param_zh = {
                "period": "账期（如 2026-08）",
                "card_type": "卡种",
                "amount": "金额",
                "card_no": "卡号后四位",
                "cardNo": "卡号后四位",
                "card": "卡号后四位",
                "month": "账期月份",
            }
            prompt = await self._load_slot_prompt(session_id, intent_result.primary_intent, entities or [], user_input)
            if not prompt or prompt.strip() == "[槽位状态]" or "（无所需信息" in prompt:
                zh = "、".join(param_zh.get(m, m) for m in qc.missing_params)
                prompt = f"请告诉我{zh}，我来为您查询。"
            return self._build_result(
                session_id,
                user_input,
                prompt or CLARIFY_RESPONSE,
                "slot_hint",
                intent_result.primary_intent.value,
                intent_result.primary_confidence,
                entities=entities,
                sentiment=sentiment,
            )
        return self._build_result(
            session_id,
            user_input,
            qc.content,
            "tool",
            intent_result.primary_intent.value,
            intent_result.primary_confidence,
            entities=entities,
            sentiment=sentiment,
        )

    async def _handle_parallel_race(
        self,
        session_id: str,
        user_input: str,
        intent_result: IntentResult,
        history: list[dict[str, str]],
        entities: list[Entity] | None,
        sentiment: SentimentLabel,
    ) -> dict[str, Any]:
        """链 D · 低置信并行竞速: FAQ 与 RAG 双路并发, 归并取高分。"""
        from lumio.services.bot.parallel_race import race

        embedding_provider = (
            self._embedding_breaker.provider
            if self._embedding_breaker and self._embedding_breaker.is_available
            else None
        )
        session_manager = self._session_manager

        async def _faq_fn():
            from lumio.services.common.faq_service import search_faq

            return await search_faq(
                query=user_input,
                redis_client=session_manager._redis if session_manager else None,
                embedding_provider=embedding_provider,
                milvus_collection=self._milvus_collection,
                user_role="customer",
                session_factory=(
                    session_manager._resolve_factory()
                    if session_manager is not None and hasattr(session_manager, "_resolve_factory")
                    else None
                ),
                session_id=session_id,
            )

        async def _rag_fn():
            return await self._retrieve(user_input)

        outcome = await race(_faq_fn, _rag_fn)
        if outcome.winner == "faq" and outcome.faq_answer:
            return self._build_result(
                session_id,
                user_input,
                outcome.faq_answer,
                "faq",
                intent_result.primary_intent.value,
                intent_result.primary_confidence,
                entities=entities,
                sentiment=sentiment,
            )
        # RAG 有据 → 知识路径 (内部含门控与引用标注); 双路皆空同样交其澄清
        return await self._handle_knowledge(session_id, user_input, intent_result, history, entities, sentiment)

    async def _handle_composite(
        self,
        session_id: str,
        user_input: str,
        intent_result: IntentResult,
        history: list[dict[str, str]],
        entities: list[Entity] | None,
        sentiment: SentimentLabel,
        customer_id: str | None,
    ) -> dict[str, Any] | None:
        """链 C · 复合意图: 查询取数 → 数据注入 RAG → 联合生成。

        Returns: None = 查询链不适用 (无工具/缺参数), 调用方回落纯知识路径。
        """
        qc = await self._handle_query_chain(
            session_id, user_input, intent_result, history, entities, sentiment, customer_id
        )
        if qc is None or qc.get("response_source") != "tool":
            return None
        extra = qc.get("retrieval_context") or ""
        if not extra:
            return None
        return await self._handle_knowledge(
            session_id,
            user_input,
            intent_result,
            history,
            entities,
            sentiment,
            extra_context=extra,
        )

    async def _handle_knowledge(
        self,
        session_id: str,
        user_input: str,
        intent: IntentResult,
        history: list[dict[str, str]],
        entities: list[Entity] | None = None,
        sentiment: SentimentLabel = SentimentLabel.NEUTRAL,
        missing_slots: list[tuple[str, str]] | None = None,
        _sensitive_rerouted: bool = False,
        extra_context: str = "",
    ) -> dict[str, Any]:
        """知识问答: RAG 检索 + LLM 生成.

        A0/A1/A2 改造:
        - A0: 使用分层消息构建器, 最大化 KV cache 命中率
        - A1: 历史超出 token 预算时, 启用 Selective Context 压缩
        - A2: 按 layer 分配 token 预算 (静态 800 / 客户 400 / RAG 1200 / 历史 1500)
        - A4: RAG 检索内容进 LLM 前过 ContentSanitizer
        """
        # P1-7: 重复提问直接复用上次回答 (不重检索/不重生成)
        repeat_reply = await self._detect_repeat_question(session_id, user_input)
        if repeat_reply:
            logger.info("重复提问命中, 复用上次回答: session=%s", session_id)
            return self._build_result(session_id, user_input, repeat_reply, "repeat", "knowledge")

        # P1 反问确认 (会话 b561cd04 复盘): bot 上轮自由反问("需要帮助查询吗?")后,
        # 用户回确认词(是的/好的)既不是新意图也不是噪声 — 轻量跟进, 不进澄清/检索。
        # 仅无缺槽时生效: 有缺槽说明上文是槽位追问, 确认词沿用回话豁免原链路。
        if _is_confirm_after_question(user_input, history, missing_slots):
            return self._build_result(session_id, user_input, CONFIRM_FOLLOWUP_RESPONSE, "confirm_followup", "chitchat")

        slot_prompt = await self._load_slot_prompt(session_id, intent.primary_intent, entities or [], user_input)
        session_memory = await self._build_session_memory(session_id)

        # 乱答防上线(CRITICAL): 分类器判定"不识别"(conf < 下限)或内容级噪声的输入,
        # 无论有无会话记忆/检索上下文都绝不编造作答 — 直接确定性澄清。典型: 乱码/按键误触。
        # P0 多轮治理: 噪声门统一走 _evaluate_noise_gate —— 先判"本句是否在回上文话"
        # (填了缺的必填槽/纯数字)。回话一律放行(防"上轮问金额/卡号, 本句答 4444"被误判成
        # 噪声), 不因低置信/像噪声而澄清; 真实乱码(hjfw/22)不填任何槽, 仍被拦截,
        # 保住"乱答防上线"与"先问候再乱打"两处漏点都在。
        gate_reason, gate_evidence = await self._evaluate_noise_gate(
            session_id, user_input, intent, entities, missing_slots, history
        )
        if gate_reason is not None:
            logger.info(
                "乱答防拦截: action=%s session=%s input=%r conf=%.2f",
                gate_reason,
                session_id,
                user_input,
                intent.primary_confidence,
            )
            try:
                log_decision(
                    session_id=session_id,
                    agent_name="bot_agent",
                    action=DecisionAction.NOISE_BLOCKED,
                    reasoning=f"噪声门拦截({gate_reason})",
                    evidence=gate_evidence,
                    turn_id=uuid_module.uuid4().hex[:16],
                )
            except Exception:
                logger.debug("decision_log 记录失败(不阻断): session=%s", session_id)
            # P3 误杀探针: 上轮"回话放行"后本轮被拦 → 疑似误杀, 回流人审
            await self._maybe_flag_mis_kill(session_id, user_input, gate_reason)
            # 近线槽位追问: 低置信/分歧门拦截但意图落在有必填缺槽的业务意图时,
            # 与其回通用"没太理解"或让澄清轮补判替换成转人工邀约, 不如按槽位定义追问
            # —— 追问不回答任何实质内容 (零幻觉风险), 且能避免客户在澄清话术里打转
            # (会话 f08227d4: 分期@0.2986 差澄清线 0.0014; 会话 fb87b1a4: 分期@0.8
            #  慢通道正确却被分歧门拦, 澄清轮又撞 streak==3 直接变转人工邀约)。
            # source=slot_hint 独立于 clarify: 不进 L3 澄清轮补判, 也不被上下文过滤剔除。
            reply = await self._pick_clarify_response(session_id)
            response_source = "clarify"
            missing_names = [s[0] for s in missing_slots] if missing_slots else []
            result_extra: dict[str, Any] = {}
            if self._prefer_slot_hint(gate_reason, intent.primary_confidence, bool(missing_names)):
                reply = self._build_slot_hint(intent.primary_intent, missing_names)
                response_source = "slot_hint"
                # P0 等待快照: 槽位追问轮声明"本轮在等的缺槽", 供 run() 出口落盘
                result_extra["missing_slots"] = missing_slots or []
                logger.info(
                    "槽位追问替代澄清: session=%s reason=%s intent=%s conf=%.3f slots=%s",
                    session_id,
                    gate_reason,
                    intent.primary_intent.value,
                    intent.primary_confidence,
                    missing_names,
                )
            slot_hint_result = self._build_result(
                session_id,
                user_input,
                reply,
                response_source,
                intent.primary_intent.value,
                intent.primary_confidence,
                entities=entities,
                sentiment=sentiment,
            )
            slot_hint_result.update(result_extra)
            return slot_hint_result
        if gate_evidence.get("awaiting_hit") and not _sensitive_rerouted:
            # P0 等待快照放行 (会话 1fb54681): 裸数字"3"答期数被分类成 faq@0.00,
            # 全靠上文快照兜住。放行后不能拿 faq 意图走知识问答(零检索零作答),
            # 换回快照里的等待意图交工具编排续办 -- 工具编排带会话记忆/历史,
            # 能把"卡号已给 + 3 期"拼成完整工具调用。无工具环境走业务链路(含槽位追问)。
            await_intent_name = gate_evidence.get("awaiting_intent")
            logger.info(
                "等待快照放行, 换回等待意图续办: session=%s awaiting=%s input=%r",
                session_id,
                await_intent_name,
                user_input[:20],
            )
            if await_intent_name:
                try:
                    await_intent = IntentLabel(normalize_intent(await_intent_name))
                except ValueError:
                    await_intent = intent.primary_intent
                swapped = IntentResult(
                    primary_intent=await_intent,
                    primary_confidence=max(intent.primary_confidence, 0.6),  # 上文已确认的意图, 不按本轮 0.00 记账
                    alternatives=intent.alternatives,
                    energy=intent.energy,
                    fast_conf=intent.fast_conf,
                    fast_intent=intent.fast_intent,
                )
                if self._tool_executor is not None and self._tool_executor.has_tools():
                    return await self._handle_tool(session_id, user_input, swapped, history, entities, sentiment)
                return await self._handle_business(session_id, user_input, swapped, history, entities, sentiment)
        if gate_evidence.get("is_replying"):
            try:
                log_decision(
                    session_id=session_id,
                    agent_name="bot_agent",
                    action=DecisionAction.CONTEXT_REPLY_PASS,
                    reasoning="多轮回话判定放行",
                    evidence=gate_evidence,
                    turn_id=uuid_module.uuid4().hex[:16],
                )
            except Exception:
                logger.debug("decision_log 记录失败(不阻断): session=%s", session_id)
            # P3 误杀探针: 记录本会话刚"回话放行", 供下一轮被拦时标记疑似误杀
            self._mark_reply_pass(session_id, user_input)
        if gate_evidence.get("sensitive_reply"):
            # P-a 敏感凭证回复 (会话 956a5fd2 复盘: "卡号后四位"→"8765"被噪声门双杀):
            # 工具可用时交回工具编排路径续办 — MCP 工具(如 apply_bill_installment)自带
            # 完整卡号参数 schema + 敏感写 pending 确认状态机, 由状态机背书收集凭证;
            # 无工具环境才回确定性桥接话术引导人工。绝不把数字交给生成链路硬编"已办理"。
            # _sensitive_rerouted 防死循环: 工具编排失败会回落本路径, 不得二次重路由。
            # P0 (会话 1efbd1ad): 等待快照有效时客户在办业务(如分期给卡号后四位), 无工具
            # 环境也不该一刀切转人工 —— 优先换回等待意图走业务链路继续收集/作答。
            if not _sensitive_rerouted and gate_evidence.get("awaiting_hit"):
                await_intent_name = gate_evidence.get("awaiting_intent")
                if await_intent_name:
                    try:
                        await_intent = IntentLabel(normalize_intent(await_intent_name))
                    except ValueError:
                        await_intent = intent.primary_intent
                    swapped = IntentResult(
                        primary_intent=await_intent,
                        primary_confidence=max(intent.primary_confidence, 0.6),  # 上文已确认意图, 不按本轮 0.00 记账
                        alternatives=intent.alternatives,
                        energy=intent.energy,
                        fast_conf=intent.fast_conf,
                        fast_intent=intent.fast_intent,
                    )
                    if self._tool_executor is not None and self._tool_executor.has_tools():
                        return await self._handle_tool(session_id, user_input, swapped, history, entities, sentiment)
                    return await self._handle_business(session_id, user_input, swapped, history, entities, sentiment)
            if not _sensitive_rerouted and self._tool_executor is not None and self._tool_executor.has_tools():
                logger.info("敏感凭证回复重新路由到工具编排: session=%s input=%r", session_id, user_input[:20])
                return await self._handle_tool(session_id, user_input, intent, history, entities, sentiment)
            # 落库保留真实分类意图 (会话 1efbd1ad: 硬编码 faq 把"在办分期"错记成 faq@0.0)
            return self._build_result(
                session_id,
                user_input,
                SENSITIVE_REPLY_BRIDGE_RESPONSE,
                "template",
                intent.primary_intent.value,
                intent.primary_confidence,
            )
        _rag_t0 = time.monotonic()
        context = await self._retrieve(user_input)
        if extra_context:
            context = f"{extra_context}\n\n{context}" if context else extra_context
        # E2 决策可解释: 记录 RAG 检索决策 (命中与否)
        try:
            log_decision(
                session_id=session_id,
                agent_name="bot_agent",
                action=DecisionAction.RAG_RETRIEVE,
                reasoning=f"RAG 检索{'命中' if context else '未命中'}",
                evidence={"hit": bool(context), "context_len": len(context or "")},
                turn_id=uuid_module.uuid4().hex[:16],
                latency_ms=(time.monotonic() - _rag_t0) * 1000,
            )
        except Exception:
            logger.debug("decision_log 记录失败(不阻断): session=%s", session_id)
        if context:
            from lumio.services.bot.knowledge_graph import enrich_retrieval_context

            context = enrich_retrieval_context(user_input, [context])

        # A4: RAG 内容防注入
        if context:
            from lumio.shared.injection_guard import get_guard

            context, verdict = get_guard().sanitize_rag_content(context)
            if verdict.is_blocked:
                logger.info("RAG 内容已净化: pattern=%s", verdict.pattern)

        # 无检索上下文时的"依据门控": 防 LLM 空想编造(如把 "adb" 误认成某银行),
        # 同时保住追问场景。
        # - 无检索 且 意图不确定(faq/泛化) → 评估依据.
        #   无对话依据(首句即无意义输入, 如 "adb")或置信不足(faq@<0.5, 如"阿萨法上课呢"@0.30,
        #   即便因前文有历史被算作"有依据")一律返回固定澄清话术 —— 确定性、零幻觉、秒回。
        #   纠正是唯一诚实回复, 且省掉一次在 Ollama 高并发下 4-9s 的 LLM 生成。
        # - 无检索 但 有对话依据且置信足够(faq@≥0.5) 或 意图明确(账单/积分等具体查询)
        #   → 放行, 让 LLM 依托会话记忆续答/追问 (保住 "那分期呢"@0.6 这类真追问)。
        if (
            not context
            and _is_uncertain_intent(intent)
            and (not _has_grounding(session_memory, history) or intent.primary_confidence < _NO_CONTEXT_GATE_CONF)
        ):
            logger.info(
                "无检索且意图不确定且(无依据或低置信), 直接澄清: session=%s input=%r intent=%s conf=%.2f",
                session_id,
                user_input,
                intent.primary_intent.value,
                intent.primary_confidence,
            )
            clarify_reply = await self._pick_clarify_response(session_id)
            return self._build_result(
                session_id,
                user_input,
                clarify_reply,
                "clarify",
                intent.primary_intent.value,
                intent.primary_confidence,
                entities=entities,
                sentiment=sentiment,
            )

        # P2 上下文工程: few-shot 动态选择注入生产路径 (此前 select_few_shot 零生产调用)
        few_shot_text = ""
        try:
            from lumio.services.bot.prompts.few_shot import select_few_shot

            examples = select_few_shot(intent.primary_intent.value, user_input, top_k=3)
            if examples:
                few_shot_text = "\n".join(f"客户问: {e['question']}\n客服答: {e['answer']}" for e in examples)
        except Exception:
            logger.debug("few-shot 选择失败, 跳过: session=%s", session_id)

        # A0: 分层消息构建 (替代原 f-string 拼接)
        from lumio.services.bot.kv_cache import build_layered_messages, estimate_cache_metrics

        messages = build_layered_messages(
            domain_prompt=KNOWLEDGE_SYSTEM_PROMPT,
            user_input=user_input,
            customer_context=session_memory,
            session_memory="",  # 已合并到 customer_context
            slot_prompt=slot_prompt or "",
            rag_context=context or "",
            history=history,
            few_shot_examples=few_shot_text,
        )

        # 上报 KV cache 指标
        from lumio.shared.config import get_settings as _gs

        estimate_cache_metrics(messages, _gs().llm.primary_model)

        # P0-2 汇总校验: Σ各层 ≤ context − reserved (前沿预算框架强制项)
        settings_b = _gs()
        total_est = sum(_estimate_tokens(m.get("content", "")) for m in messages)
        budget_total = settings_b.llm.max_context_tokens - settings_b.llm.reserved_tokens
        if total_est > budget_total:
            # P2-14 修复: 超限时实际截断 (此前只 log warning, 超长上下文直接进 LLM).
            # 策略: 从最旧的 history 层开始丢弃 (RAG/当前轮保留, 历史可被摘要覆盖).
            logger.warning("上下文总预算超限: %d > %d tokens, 裁剪历史层", total_est, budget_total)
            trimmed_messages: list[dict[str, str]] = []
            used_after = 0
            for m in messages:
                est = _estimate_tokens(m.get("content", ""))
                if m.get("role") == "user" and m.get("content", "").startswith(
                    ("（用户连续发送", "<retrieved_context>", "客户问:")
                ):
                    trimmed_messages.append(m)
                    used_after += est
                    continue
                if used_after + est > budget_total and m.get("role") == "user":
                    continue  # 丢弃最旧 user 历史消息
                trimmed_messages.append(m)
                used_after += est
            messages = trimmed_messages
            logger.info(
                "上下文裁剪完成: %d → %d tokens",
                total_est,
                sum(_estimate_tokens(m.get("content", "")) for m in messages),
            )

        # P0-1 上下文工程修复: 分层消息直传 LLM (不再压平成单一 system prompt).
        # 保留 L1 静态锚点 / L2 半稳态 / L3 动态 / RAG user-role 物理隔离结构,
        # 最大化前缀缓存命中; 失败时由 generate_with_fallback 走既有降级链.
        _t_llm = time.monotonic()
        result = await self._degradation_mgr.generate_with_fallback(
            system_prompt=KNOWLEDGE_SYSTEM_PROMPT,
            user_input=user_input,
            context=context,
            intent_label=intent.primary_intent,
            history=history,
            messages=messages,
        )
        _llm_ms = (time.monotonic() - _t_llm) * 1000
        # E2 决策可解释: 记录 LLM 生成决策 (含降级来源)
        try:
            log_decision(
                session_id=session_id,
                agent_name="bot_agent",
                action=DecisionAction.LLM_GENERATE,
                reasoning=f"knowledge 生成, 来源={getattr(result, 'source', '')}",
                evidence={"source": getattr(result, "source", ""), "rag_used": bool(context)},
                latency_ms=_llm_ms,
                turn_id=uuid_module.uuid4().hex[:16],
            )
        except Exception:
            logger.debug("decision_log 记录失败(不阻断): session=%s", session_id)
        should_transfer, transfer_reason, transfer_level = await self._check_transfer(
            user_input, intent, sentiment, session_id=session_id
        )
        # L3『先确认再转』: 连续低置信/兜底累计不派真人, 先挂确认回话 (真·明确转人工 L1/L2 仍即时转)。
        # 已生成真实答案时先答后问 (答案 + 邀约追加), 不再整条替换。
        # 明确业务轮不挂邀约 (会话 956a5fd2 复盘): 用户已回到正轨, 邀约是打扰。
        if transfer_level == TransferTriggerLevel.L3 and not self._is_confident_business_intent(intent):
            return await self._offer_transfer(
                session_id,
                user_input,
                intent,
                entities or [],
                sentiment,
                transfer_reason,
                answer=result.content if result.source == "llm" else None,
            )
        # P1-8 修复: 降级回复 (模板/兜底) 不只是文案说"转人工" — 真正触发转人工.
        # 此前 should_transfer 恒 False, 客户看到"请输入转人工"还要再发一条消息.
        if result.source in ("template", "fallback") and not should_transfer:
            should_transfer = True
            transfer_reason = f"degraded_{result.source}: LLM 不可用, 降级回复"
        # ⑥ 引用来源标注: 知识生成回复尾部附《文档标题》(doc_id → 标题, 带缓存)
        reply = result.content
        if context and result.source in ("llm", "retrieval"):
            footer = await self._citation_footer()
            if footer:
                reply = f"{reply}{footer}"

        return self._build_result(
            session_id,
            user_input,
            reply,
            _effective_knowledge_source(result.source, context),
            intent.primary_intent.value,
            intent.primary_confidence,
            should_transfer,
            transfer_reason,
            entities,
            sentiment,
            retrieval_context=context,
        )

    async def _citation_footer(self) -> str:
        """⑥ 引用来源: 本轮检索 chunk 的 source_doc → kb_document 标题 (带缓存)"""
        docids = [d for d in dict.fromkeys(getattr(self, "_last_citation_docids", None) or []) if d]
        if not docids:
            return ""
        sf = (
            self._session_manager._resolve_factory()
            if self._session_manager is not None and hasattr(self._session_manager, "_resolve_factory")
            else None
        )
        if sf is None:
            return ""
        import uuid_utils
        from sqlalchemy import select

        from lumio.shared.orm_models import KbDocument

        titles: list[str] = []
        try:
            async with sf() as db:
                for d in docids:
                    if d in self._citation_title_cache:
                        titles.append(self._citation_title_cache[d])
                        continue
                    try:
                        u = uuid_utils.UUID(d)
                    except ValueError:
                        continue
                    row = (await db.execute(select(KbDocument.title).where(KbDocument.id == u))).first()
                    if row and row.title:
                        self._citation_title_cache[d] = row.title
                        titles.append(row.title)
        except Exception as exc:
            logger.debug("引用标题查询失败(不阻断): %s", exc)
            return ""
        unique = list(dict.fromkeys(titles))
        return ("\n\n来源：" + "、".join(f"《{t}》" for t in unique)) if unique else ""

    async def _handle_business(
        self,
        session_id: str,
        user_input: str,
        intent: IntentResult,
        history: list[dict[str, str]],
        entities: list[Entity] | None = None,
        sentiment: SentimentLabel = SentimentLabel.NEUTRAL,
        customer_id: str | None = None,
    ) -> dict[str, Any]:
        """业务办理: 挂失/投诉直接转，否则（有工具）走工具编排 / （无工具）LLM 生成"""
        # ── 统一"是否即时派真人"判定 (与 transfer._check_l2 同源阈值, 堵低置信/幻觉拉真人) ──
        # 仅在主意图给出确定性信号时才即时转人工, 且敏感写主意图同样受置信门槛:
        #   * 敏感写(挂失/投诉) 主意图 且 置信 ≥ TRANSFER_CONFIDENT_CONF → 合规即时转 (+投诉建工单)
        #   * transfer_agent 主意图 且 置信 ≥ TRANSFER_CONFIDENT_CONF → 客户主动要求人工
        # 低置信的敏感写主意图被分类器幻觉出来 (见会话 fdf8: 乱码"gbfgbf"→complaint@0.21) 不得据此
        # 即时派真人/误建工单 —— 显式的真实诉求("投诉"/"卡丢了")要么命中 L1 关键词要么高置信, 不受影响。
        primary = intent.primary_intent
        alts = set(intent.alternatives or [])
        conf = intent.primary_confidence
        confident = conf >= TRANSFER_CONFIDENT_CONF
        sensitive_primary = primary in SENSITIVE_INTENTS
        transfer_primary_confident = primary == IntentLabel.TRANSFER_AGENT and confident
        # P0 快慢分歧: 慢路径覆盖快路径且两路意图不同(乱码/极含糊输入的典型特征,
        # 见会话 e33d1fa8 与架构审核 R1) -- business 路径没有噪声门, 分歧信号在此
        # 自行拦截: 不派真人、不建工单、不进工具编排, 回确定性澄清. 防"慢路径置信
        # 通胀 -> 幻觉 transfer_agent@0.7 越过统一置信门拉真人"的换通道滥用.
        disagreement = _fast_slow_disagreement(intent)
        transfer_needed = ((sensitive_primary and confident) or transfer_primary_confident) and not disagreement

        # 低置信的敏感写/转人工 -- 无论是作主意图还是仅作候补, 一律不即时派真人、不建工单。
        # (主意图低置信敏感 = 乱码被幻觉成 complaint/挂失; 候补低置信敏感 = 会话 4d22 同款穿透路径)
        low_conf_transfer_primary = primary == IntentLabel.TRANSFER_AGENT and not confident
        low_conf_sensitive_primary = sensitive_primary and not confident
        low_conf_alt_only = (
            not transfer_needed and not confident and bool(alts & (SENSITIVE_INTENTS | {IntentLabel.TRANSFER_AGENT}))
        )
        low_conf_signal = low_conf_transfer_primary or low_conf_sensitive_primary or low_conf_alt_only or disagreement

        if transfer_needed:
            reason_map = {
                IntentLabel.CARD_LOSS: "挂失业务",
                IntentLabel.COMPLAINT: "投诉处理",
                IntentLabel.TRANSFER_AGENT: "客户主动请求",
                IntentLabel.CARD_LOSS_REPORT: "挂失业务",
                IntentLabel.DISPUTE_SUBMIT: "投诉处理",
            }
            reason = reason_map.get(primary, "业务办理")
            # P2-15: 投诉工单创建 — 投诉不是"转人工就完事", 需要可跟踪的闭环状态
            # (open → resolved, 由 admin 端点关闭; Redis hash 轻量实现, 不引新表)。
            # 仅在置信充分的 complaints 上建工单, 低置信幻觉不作数 (见 fdf8 误建投诉工单)。
            # 归一化后比较: 旧 flat "complaint" 与主名 dispute_submit 都建工单。
            if normalize_intent(primary.value) == IntentLabel.DISPUTE_SUBMIT:
                try:
                    await self._create_complaint_ticket(session_id, user_input, customer_id)
                except Exception as exc:
                    logger.debug("投诉工单创建失败 (不阻断转人工): session=%s err=%s", session_id, exc)
            return self._build_result(
                session_id,
                user_input,
                BUSINESS_TRANSFER_TEMPLATE.format(reason=reason),
                "template",
                primary.value,
                conf,
                should_transfer=True,
                transfer_reason=reason,
            )

        if low_conf_signal:
            # 把握不足的"转人工/敏感"判定或快慢分歧: 不擅自拉真人坐席、不误建工单, 回确定性澄清。
            logger.info(
                "低置信敏感/转人工或快慢分歧信号不回执真人, 回澄清: session=%s input=%r intent=%s conf=%.2f "
                "alts=%s fast=%s disagreement=%s",
                session_id,
                user_input,
                primary.value,
                conf,
                sorted(a.value for a in alts),
                f"{intent.fast_intent.value if intent.fast_intent else None}@{intent.fast_conf}",
                disagreement,
            )
            clarify_reply = await self._pick_clarify_response(session_id)
            return self._build_result(
                session_id,
                user_input,
                clarify_reply,
                "clarify",
                primary.value,
                conf,
                entities=entities,
                sentiment=sentiment,
            )

        # 结构化会话记忆注入 system prompt
        session_memory = await self._build_session_memory(session_id)
        system_prompt = BUSINESS_SYSTEM_PROMPT
        if session_memory:
            system_prompt = f"{BUSINESS_SYSTEM_PROMPT}\n\n## 会话记忆\n{session_memory}"
        # 槽位状态注入(与 knowledge 路径同一真相源, 引导 Bot 知道自己已收集/还缺哪些信息)
        slot_prompt = await self._load_slot_prompt(session_id, intent.primary_intent, entities or [], user_input)
        if slot_prompt:
            system_prompt = f"{system_prompt}\n\n{slot_prompt}"

        # 工具编排路径：MCP 启用且有可用工具时，尝试 tool-calling；任何异常回落降级链
        if self._tool_executor is not None and self._tool_executor.has_tools():
            try:
                tool_result = await self._tool_executor.run_conversation(
                    system_prompt=system_prompt,
                    user_input=user_input,
                    history=history,
                    session_id=session_id,
                    actor_id=customer_id or session_id,
                    actor_role="customer",
                )
                if tool_result.pending_action is not None:
                    # 敏感操作：暂存待确认，返回身份核验信号（前端弹框），不执行
                    await self._save_pending_action(session_id, tool_result.pending_action)
                    return self._build_result(
                        session_id,
                        user_input,
                        tool_result.content,
                        "tool_confirm",
                        intent.primary_intent.value,
                        intent.primary_confidence,
                        verification=tool_result.verification,
                    )
                return self._build_result(
                    session_id,
                    user_input,
                    tool_result.content,
                    tool_result.source,
                    intent.primary_intent.value,
                    intent.primary_confidence,
                    # P2-19: 护栏拒绝 → 真实转人工
                    should_transfer=tool_result.should_transfer,
                    transfer_reason=tool_result.transfer_reason,
                    entities=entities,
                    sentiment=sentiment,
                )
            except Exception as exc:
                logger.warning("工具编排失败，回落降级链: %s", exc)

        # 无工具/工具失败 → 检索上下文再生成 (draft-0.3 §4.2 前置条件: business 路径
        # 必须带 RAG 兜底, 否则查询类意图翻到 business 主路径后, 个人账户数据查询
        # ("我欠多少") 会零上下文硬编。检索不中时由噪声门 grounding 兜底回澄清。
        try:
            context = await self._retrieve(user_input)
        except Exception:
            logger.debug("business 路径 RAG 兜底检索失败(不阻断): session=%s", session_id)
            context = ""
        _t_llm = time.monotonic()
        result = await self._degradation_mgr.generate_with_fallback(
            system_prompt=system_prompt,
            user_input=user_input,
            context=context,
            intent_label=intent.primary_intent,
            history=history,
        )
        _llm_ms = (time.monotonic() - _t_llm) * 1000
        # E2 决策可解释: 记录业务路径 LLM 生成决策
        try:
            log_decision(
                session_id=session_id,
                agent_name="bot_agent",
                action=DecisionAction.LLM_GENERATE,
                reasoning=f"business 生成, 来源={getattr(result, 'source', '')}",
                evidence={"source": getattr(result, "source", ""), "domain": "business", "rag_used": bool(context)},
                latency_ms=_llm_ms,
                turn_id=uuid_module.uuid4().hex[:16],
            )
        except Exception:
            logger.debug("decision_log 记录失败(不阻断): session=%s", session_id)
        should_transfer, transfer_reason, transfer_level = await self._check_transfer(
            user_input, intent, sentiment, session_id=session_id
        )
        # L3『先确认再转』(同上, 已生成真实答案时先答后问; 明确业务轮不挂邀约)
        if transfer_level == TransferTriggerLevel.L3 and not self._is_confident_business_intent(intent):
            return await self._offer_transfer(
                session_id,
                user_input,
                intent,
                entities or [],
                sentiment,
                transfer_reason,
                answer=result.content if result.source == "llm" else None,
            )
        # P1-8: 降级回复真正触发转人工 (同上)
        if result.source in ("template", "fallback") and not should_transfer:
            should_transfer = True
            transfer_reason = f"degraded_{result.source}: LLM 不可用, 降级回复"
        return self._build_result(
            session_id,
            user_input,
            result.content,
            _effective_knowledge_source(result.source, context),
            intent.primary_intent.value,
            intent.primary_confidence,
            should_transfer,
            transfer_reason,
            entities,
            sentiment,
            retrieval_context=context,
        )

    async def _handle_tool(
        self,
        session_id: str,
        user_input: str,
        intent: IntentResult,
        history: list[dict[str, str]],
        entities: list[Entity] | None = None,
        sentiment: SentimentLabel = SentimentLabel.NEUTRAL,
        customer_id: str | None = None,
    ) -> dict[str, Any]:
        """工具编排路径（渐进式暴露）：查询类意图打通 MCP 工具

        - 依据意图/置信度选择工具子集（``None`` = 暴露全量）。
        - 敏感写操作返回 ``pending_action``，暂存待确认（复用确认状态机）。
        - 任何异常回落知识问答（RAG），保证优雅降级。
        """
        tool_names = select_tools_for_intent(intent.primary_intent, intent.primary_confidence, get_settings().mcp)

        session_memory = await self._build_session_memory(session_id)
        system_prompt = BUSINESS_SYSTEM_PROMPT
        if session_memory:
            system_prompt = f"{BUSINESS_SYSTEM_PROMPT}\n\n## 会话记忆\n{session_memory}"

        try:
            tool_result = await self._tool_executor.run_conversation(  # type: ignore[union-attr]
                system_prompt=system_prompt,
                user_input=user_input,
                history=history,
                session_id=session_id,
                actor_id=customer_id or session_id,
                actor_role="customer",
                tool_names=tool_names,
            )
        except Exception as exc:
            logger.warning("工具编排失败，回落知识问答: %s", exc)
            return await self._handle_knowledge(
                session_id, user_input, intent, history, entities, sentiment, _sensitive_rerouted=True
            )

        # 工具编排无工具被调用时的裸 LLM 直答缺知识 grounding: 查询类问题
        # (如"信用额度是什么")被 TOOL_INTENTS 路由进来, LLM 判断无需调工具直接回答,
        # 结果零 RAG —— 审计上就是"RAG 没搜到"(会话 9d64b59 复盘)。有知识可依时
        # 交由知识路径带 RAG 重新生成, 保证 grounding 与检索证据落库。
        if tool_result.pending_action is None and tool_result.source == "llm" and not tool_result.executed_tools:
            try:
                rag_context = await self._retrieve(user_input)
            except Exception:
                rag_context = ""
            if rag_context:
                logger.info("工具编排无工具调用且检索有据, 转知识路径 grounding: session=%s", session_id)
                return await self._handle_knowledge(
                    session_id, user_input, intent, history, entities, sentiment, _sensitive_rerouted=True
                )

        if tool_result.pending_action is not None:
            # 敏感操作：暂存待确认，返回身份核验信号（前端弹框），不执行
            await self._save_pending_action(session_id, tool_result.pending_action)
            return self._build_result(
                session_id,
                user_input,
                tool_result.content,
                "tool_confirm",
                intent.primary_intent.value,
                intent.primary_confidence,
                verification=tool_result.verification,
            )
        # 会话 48882b05 复盘: 工具编排只回"参数追问"(分几期/卡号后四位)时零知识内容,
        # 且此路径不像 knowledge/business 有 RAG 兜底 —— 客户等了 14s 只得到反问。
        # 命中索参数式澄清时拼一段 RAG 检索摘要(首条 chunk 截断), 让追问轮也带上
        # 办理条件/费率参考; 检索失败不阻断。
        if tool_result.source in ("llm", "tool") and _asks_for_parameters(tool_result.content):
            try:
                rag_context = await self._retrieve(user_input)
                if rag_context:
                    from lumio.shared.injection_guard import get_guard

                    rag_context, verdict = get_guard().sanitize_rag_content(rag_context)
                    if rag_context and not verdict.is_blocked:
                        first_seg = re.sub(r"^\[\d+\]\s*", "", rag_context.split("\n\n")[0]).strip()
                        if len(first_seg) > 200:
                            first_seg = first_seg[:200] + "…"
                        tool_result.content = f"{tool_result.content}\n\n供您参考：{first_seg}"
            except Exception:
                logger.debug("工具编排 RAG 知识兜底失败(不阻断): session=%s", session_id)
        return self._build_result(
            session_id,
            user_input,
            tool_result.content,
            tool_result.source,
            intent.primary_intent.value,
            intent.primary_confidence,
            # P2-19: 护栏拒绝 → 真实转人工
            should_transfer=tool_result.should_transfer,
            transfer_reason=tool_result.transfer_reason,
            entities=entities,
            sentiment=sentiment,
        )

    # ── 工具确认状态机 ──

    async def _handle_pending_action(
        self,
        session_id: str,
        user_input: str,
        state: Any,
        customer_id: str | None,
    ) -> dict[str, Any]:
        """处理待确认的敏感工具操作（confirm/cancel/unclear/expired）"""
        pending: PendingAction = state.pending_action
        # L3『先确认再转』的转人工邀请: 独立于敏感工具确认, 走专用状态机。
        if pending.tool_name == "TRANSFER_OFFER":
            return await self._handle_transfer_offer(session_id, user_input, state)
        # 身份核验态守卫 (会话 564db34d): 已发起前端核验、等核验结果回传期间,
        # 客户的任意文本(包括"确认"/"取消")都不该进入 confirm/cancel 判定 ——
        # 此时等待的是核验回传, 不是文本确认。回引导话术, 保持 pending 不消费。
        if pending.verification_state == "pending":
            return self._build_result(
                session_id,
                user_input,
                "身份核验尚未完成，请在弹窗中完成验证后继续办理。",
                "template",
                "faq",
            )
        # 工具类确认需要工具执行器; 无工具装配的环境 (MCP 关闭) 不会产生工具类 pending,
        # 防御性兜底: 清除残留并放行新消息, 避免客户被卡在确认态。
        if self._tool_executor is None:
            logger.warning(
                "工具 pending 无执行器可确认, 清除并放行: session=%s tool=%s",
                session_id,
                pending.tool_name,
            )
            await self._clear_pending_action(session_id, state.version)
            return {"pending_released": True}
        actor_id = customer_id or state.customer_id or session_id

        # 过期：清除并提示重新发起
        if pending.expires_at is not None and datetime.now(UTC) > pending.expires_at:
            await self._clear_pending_action(session_id, state.version)
            TOOL_CONFIRMATIONS.labels(decision="expired").inc()
            await self._tool_executor.audit_decision(  # type: ignore[union-attr]
                session_id=session_id,
                actor_id=actor_id,
                actor_role="customer",
                tool_name=pending.tool_name,
                decision="expired",
            )
            return self._build_result(
                session_id, user_input, "您上一步的操作请求已超时失效，如仍需办理请重新告知我。", "template", "faq"
            )

        decision = detect_confirmation(user_input)

        if decision == "confirm":
            await self._tool_executor.audit_decision(  # type: ignore[union-attr]
                session_id=session_id,
                actor_id=actor_id,
                actor_role="customer",
                tool_name=pending.tool_name,
                decision="confirm",
            )
            # P1-1 第三轮修复: 幂等键 — 防止 at-least-once 重投递 / CAS 清除失败后重复执行敏感操作.
            # 以 pending.tool_call_id 为键 SETNX: 已执行过 → 不重复调用工具, 直接提示完成.
            idem_key = f"lumio:tool:executed:{pending.tool_call_id or pending.created_at.isoformat()}"
            already_executed = False
            # 幂等检查尽力而为: 会话管理器未暴露 _redis (鸭子类型) 时视为无后端, 走放行一次
            redis = getattr(self._session_manager, "_redis", None) if self._session_manager else None
            try:
                if pending.tool_call_id and redis is not None:
                    already_executed = bool(await redis.get(idem_key))
            except Exception:
                pass  # 幂等检查失败时保守放行一次 (fail-closed 会误伤, 此处 fallback 到执行)
            if already_executed:
                logger.info("确认操作幂等跳过 (已执行): session=%s tool=%s", session_id, pending.tool_name)
                await self._clear_pending_action(session_id, state.version)
                TOOL_CONFIRMATIONS.labels(decision="confirm_dup").inc()
                return self._build_result(
                    session_id,
                    user_input,
                    pending.confirm_prompt.replace("请问是否办理", "该操作已完成，无需重复办理"),
                    "template",
                    "faq",
                )
            try:
                session_memory = await self._build_session_memory(session_id)
                system_prompt = BUSINESS_SYSTEM_PROMPT
                if session_memory:
                    system_prompt = f"{BUSINESS_SYSTEM_PROMPT}\n\n## 会话记忆\n{session_memory}"
                history = await self._load_history(session_id)
                tool_result = await self._tool_executor.execute_confirmed_action(  # type: ignore[union-attr]
                    pending=pending,
                    system_prompt=system_prompt,
                    history=history,
                    session_id=session_id,
                    actor_id=actor_id,
                    actor_role="customer",
                )
                # 执行成功后标记幂等键
                try:
                    if pending.tool_call_id and redis is not None:
                        await redis.setex(idem_key, 24 * 3600, "1")  # 24h 内不重复执行
                except Exception:
                    pass
                await self._clear_pending_action(session_id, state.version)
                TOOL_CONFIRMATIONS.labels(decision="confirm").inc()
                return self._build_result(session_id, user_input, tool_result.content, tool_result.source, "faq")
            except Exception as exc:
                logger.warning("确认执行工具失败，清除待确认并降级: %s", exc)
                await self._clear_pending_action(session_id, state.version)
                return self._build_result(
                    session_id, user_input, self._degradation_mgr._degrader.hardcoded_fallback(), "fallback", "faq"
                )

        if decision == "cancel":
            await self._clear_pending_action(session_id, state.version)
            TOOL_CONFIRMATIONS.labels(decision="cancel").inc()
            await self._tool_executor.audit_decision(  # type: ignore[union-attr]
                session_id=session_id,
                actor_id=actor_id,
                actor_role="customer",
                tool_name=pending.tool_name,
                decision="cancel",
            )
            return self._build_result(
                session_id, user_input, "好的，已为您取消该操作。还有什么可以帮您的吗？", "template", "faq"
            )

        # unclear: 无法判定为确认/取消 — 计数, 达到上限自动取消并放行新消息
        # (业务场景: 确认窗口内用户发新问题, 若一直吞掉会被卡死, 需给逃生路径)
        new_count = (pending.unclear_count or 0) + 1
        if new_count >= get_settings().mcp.unclear_auto_cancel_threshold:
            await self._clear_pending_action(session_id, state.version)
            TOOL_CONFIRMATIONS.labels(decision="cancel").inc()
            await self._tool_executor.audit_decision(  # type: ignore[union-attr]
                session_id=session_id,
                actor_id=actor_id,
                actor_role="customer",
                tool_name=pending.tool_name,
                decision="unclear_auto_cancel",
            )
            # 返回 released 标记: run() 检测后不 return, 继续按新消息正常处理
            return {
                **self._build_result(
                    session_id,
                    user_input,
                    "您刚才的操作请求已为您取消。如需办理请重新告诉我，现在继续为您解答：",
                    "template",
                    "faq",
                ),
                "pending_released": True,
            }
        # 未达上限: 更新计数 + 重复确认话术, 并提示逃生路径
        TOOL_CONFIRMATIONS.labels(decision="unclear").inc()
        try:
            if self._session_manager is not None:
                await self._session_manager.patch_state(
                    session_id=session_id,
                    expected_version=state.version,
                    patches={"pending_action": {**pending.model_dump(mode="json"), "unclear_count": new_count}},
                )
        except Exception as exc:
            logger.warning("unclear 计数更新失败: session=%s err=%s", session_id, exc)
        remaining = get_settings().mcp.unclear_auto_cancel_threshold - new_count
        return self._build_result(
            session_id,
            user_input,
            f"{pending.confirm_prompt}（若需咨询其他问题，可回复『取消』放弃当前操作，"
            f"或再回复 {remaining} 次其他内容将自动取消）",
            "template",
            "faq",
        )

    async def _handle_transfer_offer(self, session_id: str, user_input: str, state: Any) -> dict[str, Any]:
        """L3『先确认再转』的确认态处理: 客户明确回复"是/需要"才真正派真人坐席。

        任何真人派发都必须来自客户明确确认 —— 绝不因连续低置信轮次自动派坐席。
        确认 → 派真人; 取消 → 撤销邀请; 其余输入(业务诉求/乱码) → 清邀请并放行
        正常流程, 由分类器/噪声门按其语义处理 (会话 956a5fd2 复盘: "办理账单f分期"
        被过期邀约吞掉只回超时模板, 真实业务根本没进分类器)。
        """
        pending: PendingAction = state.pending_action
        expired = pending.expires_at is not None and datetime.now(UTC) > pending.expires_at
        if not expired:
            decision = self._detect_transfer_offer(user_input)
            if decision == "confirm":
                await self._clear_pending_action(session_id, state.version)
                reason = (pending.arguments or {}).get("transfer_reason") or "连续多轮未理解后客户确认转人工"
                logger.info("L3 转人工已确认, 派真人: session=%s reason=%s", session_id, reason)
                return self._build_result(
                    session_id,
                    user_input,
                    BUSINESS_TRANSFER_TEMPLATE.format(reason="客户确认转人工"),
                    "template",
                    "faq",
                    should_transfer=True,
                    transfer_reason=f"confirm:{reason}",
                )
            if decision == "cancel":
                await self._clear_pending_action(session_id, state.version)
                return self._build_result(
                    session_id, user_input, "好的，继续为您服务。如需人工客服，随时说“转人工”即可。", "template", "faq"
                )
            # 非确认/取消: 清邀请并放行, 让本句走正常分类 (run() 识别 pending_released 后继续)
            await self._clear_pending_action(session_id, state.version)
            logger.info("转人工邀请对无关输入放行: session=%s input=%r", session_id, user_input[:40])
            return {"pending_released": True}
        # 过期: 仅对"仍想转人工"的确认/取消词回超时模板(确认不可再派真人), 其余放行
        decision = self._detect_transfer_offer(user_input)
        await self._clear_pending_action(session_id, state.version)
        if decision != "unclear":
            return self._build_result(
                session_id, user_input, "转人工邀请已超时。如需人工客服，请直接回复“转人工”。", "template", "faq"
            )
        logger.info("过期转人工邀请对无关输入放行: session=%s input=%r", session_id, user_input[:40])
        return {"pending_released": True}

    @staticmethod
    def _detect_transfer_offer(text: str) -> ConfirmDecision:
        """转人工邀请的确认判定 (独立于通用 detect_confirmation).

        邀请话术明确引导客户回"是/需要", 但通用 confirm 关键词表不含这两个词,
        客户按话术回复会被误判成 unclear → 邀请被无辜清除, 功能形同虚设.
        这里对邀请话术引导的回复词单独识别, 其余交给通用检测.
        """
        if not text:
            return "unclear"
        core = re.sub(r"[\s，。！？,.!?、～~嗯哦啊呀呢吧]", "", text.strip())
        # 取消优先 (否定表述优先): 明确的"不用/不需要/算了...
        if core in _TRANSFER_OFFER_CANCEL_KEYWORDS or any(
            core.startswith(kw) for kw in ("不用", "不需要", "不要", "不了")
        ):
            return "cancel"
        for kw in _TRANSFER_OFFER_CONFIRM_KEYWORDS:
            if core == kw or core.startswith(kw):
                return "confirm"
        return detect_confirmation(text)

    async def _save_pending_action(self, session_id: str, pending: PendingAction) -> None:
        """将待确认操作写入会话状态（CAS）"""
        if self._session_manager is None:
            return
        try:
            state = await self._session_manager.get_session(session_id)
            if state is None:
                return
            await self._session_manager.patch_state(
                session_id=session_id,
                expected_version=state.version,
                patches={"pending_action": pending.model_dump(mode="json")},
                writer="bot_agent:tool_confirm",
            )
        except Exception:
            logger.debug("写入 pending_action 失败: session=%s", session_id)

    async def handle_verification_result(self, session_id: str, result: VerificationResult) -> dict[str, Any]:
        """处理前端身份核验回传 (会话 564db34d 复盘).

        核验成功 -> 用 token 换授权卡号注入 arguments -> pending 进入 verified 态 ->
        生成带具体参数的确认话术, 等待客户"确认"后执行。
        核验取消/失败 -> 清除 pending, 回取消话术, 不执行。
        """
        state = await self._session_manager.get_session(session_id) if self._session_manager else None
        if state is None or state.pending_action is None:
            return self._build_result(session_id, "", "核验请求已失效，请重新发起办理。", "template", "faq")
        pending: PendingAction = state.pending_action

        # 令牌不匹配或不在核验态 -> 保守拒绝, 不推进状态
        if pending.verification_state != "pending" or pending.verification_token != result.token:
            logger.warning(
                "核验回传令牌不匹配/状态异常: session=%s state=%s",
                session_id,
                pending.verification_state,
            )
            return self._build_result(session_id, "", "核验请求不匹配，请重新发起办理。", "template", "faq")

        if result.status == "success":
            # 用核验令牌换取已授权身份绑定的完整卡号 (mock; 真实实现接实名渠道)
            try:
                card_no = await self._resolve_card_from_verification(result.token, session_id)
            except Exception as exc:
                logger.warning("核验通过后取卡号失败: session=%s err=%s", session_id, exc)
                card_no = ""
            if not card_no:
                await self._clear_pending_action(session_id, state.version)
                return self._build_result(
                    session_id, "", "身份核验已通过，但暂未获取到您的绑定卡片，请联系人工客服办理。", "template", "faq"
                )
            pending.arguments["card_no"] = card_no
            pending.verification_state = "verified"
            pending.confirm_prompt = ToolCallingExecutor.format_confirm_prompt(pending.tool_name, pending.arguments)
            await self._save_pending_action(session_id, pending)
            return self._build_result(
                session_id,
                "",
                pending.confirm_prompt,
                "tool_confirm",
                pending.tool_name,
                0.0,
            )
        # cancel / failed: 清除 pending, 不执行
        await self._clear_pending_action(session_id, state.version)
        return self._build_result(session_id, "", "好的，已取消该操作。如需办理请重新告知。", "template", "faq")

    async def _resolve_card_from_verification(self, token: str, session_id: str) -> str:
        """核验通过后换取授权绑定的完整卡号 (会话 564db34d 复盘).

        mock 实现: 返回测试卡号, 代表"实名渠道已授权"的绑定卡。生产环境应接
        实名核验服务, 用 token 换取客户已授权身份下的卡号, 绝不在对话里收集。
        """
        return "6225880012346780"

    async def _clear_pending_action(self, session_id: str, expected_version: int) -> None:
        """清除待确认操作 (P1-1: CAS 循环重试 + 失败升级 WARNING).

        旧实现单次 CAS, 失败仅 logger.debug 静默 → pending 残留 → 下轮"好的"再次触发敏感工具.
        patch_state 支持 max_retries, 冲突时重读 version 重试.
        """
        if self._session_manager is None:
            return
        try:
            await self._session_manager.patch_state(
                session_id=session_id,
                expected_version=expected_version,
                patches={"pending_action": None},
                writer="bot_agent:tool_confirm",
                max_retries=3,
            )
        except Exception:
            logger.warning("清除 pending_action 失败 (下轮确认将被幂等键拦截): session=%s", session_id)

    async def _handle_fallback(
        self,
        session_id: str,
        user_input: str,
        intent: IntentResult,
        history: list[dict[str, str]],
        entities: list[Entity] | None = None,
        sentiment: SentimentLabel = SentimentLabel.NEUTRAL,
        missing_slots: list[tuple[str, str]] | None = None,
    ) -> dict[str, Any]:
        """闲聊/兜底: 快速匹配 或 LLM 生成"""
        # P1 反问确认 (会话 b561cd04 复盘): 自由反问后的确认词轻量跟进, 不进澄清/LLM。
        # 与 knowledge 路径同一分支语义; 有缺槽时沿用回话豁免原链路。
        if _is_confirm_after_question(user_input, history, missing_slots):
            return self._build_result(session_id, user_input, CONFIRM_FOLLOWUP_RESPONSE, "confirm_followup", "chitchat")

        # 结构化会话记忆注入 system prompt
        session_memory = await self._build_session_memory(session_id)

        # 乱答防上线(CRITICAL): 兜底是最后一档"能答就答"的路径, 但**分类器判定不识别**
        # (conf < 下限, 即 source=fallback)的输入(典型如乱码/按键误触 "sncjao"、"889")
        # 绝不能交给 LLM 硬编一个答复——那正是"乱答"来源(conf≈0 还被当普通闲聊生成)。
        # 与 knowledge 路径同一判定标准: 低于下限 → 直接回确定性澄清话术(零幻觉、秒回)。
        # 另有**内容级噪声门**(纯数字/纯符号, 如 "22")即使高置信被快路径认成闲聊
        # 也一并澄清, 堵住 BERT 快路径把纯数字噪音高置信误判为闲聊而漏走的漏点。
        # 乱答防上线(CRITICAL): 兜底是最后一档"能答就答"的路径, 分类器判定不识别
        # (conf < 下限)或内容级噪声的输入绝不能交给 LLM 硬编答复 — 直接确定性澄清。
        # P0 多轮治理: 与 knowledge 路径同一噪声门(_evaluate_noise_gate); 本句在回上文话
        # (填缺的必填槽/纯数字)时放行, 防"上轮问金额/卡号, 本句答 4444"被误判成噪声。
        gate_reason, gate_evidence = await self._evaluate_noise_gate(
            session_id, user_input, intent, entities, missing_slots, history
        )
        if gate_reason is not None:
            logger.info(
                "兜底路径噪声门拦截: action=%s session=%s input=%r conf=%.2f",
                gate_reason,
                session_id,
                user_input,
                intent.primary_confidence,
            )
            try:
                log_decision(
                    session_id=session_id,
                    agent_name="bot_agent",
                    action=DecisionAction.NOISE_BLOCKED,
                    reasoning=f"噪声门拦截({gate_reason})",
                    evidence=gate_evidence,
                    turn_id=uuid_module.uuid4().hex[:16],
                )
            except Exception:
                logger.debug("decision_log 记录失败(不阻断): session=%s", session_id)
            # P3 误杀探针: 上轮"回话放行"后本轮被拦 → 疑似误杀, 回流人审
            await self._maybe_flag_mis_kill(session_id, user_input, gate_reason)
            clarify_reply = await self._pick_clarify_response(session_id)
            return self._build_result(
                session_id,
                user_input,
                clarify_reply,
                "clarify",
                intent.primary_intent.value,
                intent.primary_confidence,
                entities=entities,
                sentiment=sentiment,
            )
        if gate_evidence.get("awaiting_hit"):
            # P0 等待快照放行 (与 knowledge 路径同一语义): 换回等待意图交工具编排续办。
            await_intent_name = gate_evidence.get("awaiting_intent")
            logger.info(
                "等待快照放行(fallback 路径), 换回等待意图续办: session=%s awaiting=%s input=%r",
                session_id,
                await_intent_name,
                user_input[:20],
            )
            if await_intent_name:
                try:
                    await_intent = IntentLabel(normalize_intent(await_intent_name))
                except ValueError:
                    await_intent = intent.primary_intent
                swapped = IntentResult(
                    primary_intent=await_intent,
                    primary_confidence=max(intent.primary_confidence, 0.6),
                    alternatives=intent.alternatives,
                    energy=intent.energy,
                    fast_conf=intent.fast_conf,
                    fast_intent=intent.fast_intent,
                )
                if self._tool_executor is not None and self._tool_executor.has_tools():
                    return await self._handle_tool(session_id, user_input, swapped, history, entities, sentiment)
                return await self._handle_business(session_id, user_input, swapped, history, entities, sentiment)
        if gate_evidence.get("is_replying"):
            try:
                log_decision(
                    session_id=session_id,
                    agent_name="bot_agent",
                    action=DecisionAction.CONTEXT_REPLY_PASS,
                    reasoning="多轮回话判定放行",
                    evidence=gate_evidence,
                    turn_id=uuid_module.uuid4().hex[:16],
                )
            except Exception:
                logger.debug("decision_log 记录失败(不阻断): session=%s", session_id)
            # P3 误杀探针: 记录本会话刚"回话放行", 供下一轮被拦时标记疑似误杀
            self._mark_reply_pass(session_id, user_input)
        if gate_evidence.get("sensitive_reply"):
            # P-a (与 knowledge 路径同一语义): 工具可用交回工具编排, 无工具回桥接话术。
            # 工具编排失败回落 knowledge(带防循环标志), 不会回到本路径, 无死循环风险。
            # P0 (会话 1efbd1ad): 等待快照有效时客户在办业务, 无工具环境优先走业务链路。
            if gate_evidence.get("awaiting_hit"):
                await_intent_name = gate_evidence.get("awaiting_intent")
                if await_intent_name:
                    try:
                        await_intent = IntentLabel(normalize_intent(await_intent_name))
                    except ValueError:
                        await_intent = intent.primary_intent
                    swapped = IntentResult(
                        primary_intent=await_intent,
                        primary_confidence=max(intent.primary_confidence, 0.6),
                        alternatives=intent.alternatives,
                        energy=intent.energy,
                        fast_conf=intent.fast_conf,
                        fast_intent=intent.fast_intent,
                    )
                    if self._tool_executor is not None and self._tool_executor.has_tools():
                        return await self._handle_tool(session_id, user_input, swapped, history, entities, sentiment)
                    return await self._handle_business(session_id, user_input, swapped, history, entities, sentiment)
            if self._tool_executor is not None and self._tool_executor.has_tools():
                logger.info(
                    "敏感凭证回复重新路由到工具编排(fallback 路径): session=%s input=%r",
                    session_id,
                    user_input[:20],
                )
                return await self._handle_tool(session_id, user_input, intent, history, entities, sentiment)
            # 落库保留真实分类意图 (会话 1efbd1ad: 硬编码 faq 把"在办分期"错记成 faq@0.0)
            return self._build_result(
                session_id,
                user_input,
                SENSITIVE_REPLY_BRIDGE_RESPONSE,
                "template",
                intent.primary_intent.value,
                intent.primary_confidence,
            )

        system_prompt = FALLBACK_SYSTEM_PROMPT
        if session_memory:
            system_prompt = f"{FALLBACK_SYSTEM_PROMPT}\n\n## 会话记忆\n{session_memory}"
        # 槽位状态注入(若上轮在等必填信息, 本句可能是在回话)
        slot_prompt = await self._load_slot_prompt(session_id, intent.primary_intent, entities or [], user_input)
        if slot_prompt:
            system_prompt = f"{system_prompt}\n\n{slot_prompt}"
        _t_llm = time.monotonic()
        result = await self._degradation_mgr.generate_with_fallback(
            system_prompt=system_prompt,
            user_input=user_input,
            context="",
            intent_label=IntentLabel.CHITCHAT,
            history=history,
        )
        _llm_ms = (time.monotonic() - _t_llm) * 1000
        # E2 决策可解释: 记录兜底 LLM 生成决策 (含降级来源)
        try:
            log_decision(
                session_id=session_id,
                agent_name="bot_agent",
                action=DecisionAction.LLM_GENERATE,
                reasoning=f"fallback 生成, 来源={getattr(result, 'source', '')}",
                evidence={"source": getattr(result, "source", ""), "domain": "chitchat"},
                latency_ms=_llm_ms,
                turn_id=uuid_module.uuid4().hex[:16],
            )
        except Exception:
            logger.debug("decision_log 记录失败(不阻断): session=%s", session_id)
        # P0-4 修复: 兜底路径也做转人工检查 — 此前 _handle_fallback 完全跳过,
        # 客户被敷衍 N 轮也不会转人工 (只能靠下一轮主动说"转人工"被分类)
        should_transfer = False
        transfer_reason = ""
        if result.source in ("template", "fallback"):
            should_transfer, transfer_reason, transfer_level = await self._check_transfer(
                user_input, None, sentiment, session_id=session_id
            )
            # L3『先确认再转』: 兜底路径同样不自动派真人, 改为挂确认回话。
            # 明确业务轮不挂邀约 (与 knowledge/business 路径同一护栏)
            if transfer_level == TransferTriggerLevel.L3 and not self._is_confident_business_intent(intent):
                return await self._offer_transfer(
                    session_id, user_input, intent, entities or [], sentiment, transfer_reason
                )
        return self._build_result(
            session_id,
            user_input,
            result.content,
            result.source,
            intent.primary_intent.value,
            # P2 修复: 此前硬编码 0.0 — chitchat@0.9 过线作答的轮次在会话状态里仍记成
            # 低置信, streak 虚涨把后续真实问题推过 L3 线误挂转人工邀约 (会话 fb87b1a4)。
            # 记账必须用真实分类置信, streak 才准确反映"连续没听懂"。
            intent.primary_confidence,
            entities=entities,
            sentiment=sentiment,
            should_transfer=should_transfer,
            transfer_reason=transfer_reason,
        )

    # ── 辅助方法 ──

    async def _create_complaint_ticket(self, session_id: str, user_input: str, customer_id: str | None) -> str:
        """P2-15: 创建投诉工单 (Redis hash, 轻量闭环跟踪).

        状态机: open → resolved (admin 端点关闭); TTL 30 天供对账.
        """
        if self._session_manager is None:
            return ""
        redis = self._session_manager._redis
        if redis is None:
            return ""
        ticket_id = f"CP-{uuid_module.uuid4().hex[:10].upper()}"
        key = f"lumio:complaint:{ticket_id}"
        await redis.hset(
            key,
            mapping={
                "ticket_id": ticket_id,
                "session_id": session_id,
                "customer_id": customer_id or "",
                "status": "open",
                "content": user_input[:500],
                "created_at": datetime.now(UTC).isoformat(),
            },
        )
        await redis.expire(key, 30 * 24 * 3600)  # 30 天对账窗口
        # 会话 → 工单索引 (坐席端/管理端按 session 查)
        await redis.set(f"lumio:complaint:session:{session_id}", ticket_id, ex=30 * 24 * 3600)
        logger.info("投诉工单已创建: %s session=%s", ticket_id, session_id)
        return ticket_id

    async def _detect_repeat_question(self, session_id: str, user_input: str) -> str | None:
        """P1-7 重复提问检测: 与上一轮客户消息归一化后相同 → 返回上次 Bot 回答, 不再重检索/重生成.

        业务场景: "额度多少" 问 3 次 = 3 次全量重检索 + 3 次 LLM + 3 次摘要.
        仅做轻量归一化 (去标点/空白/语气词) 精确匹配 — 不引入语义相似度,
        避免误伤"额度多少"→"额度怎么提升"这类真实新问题.
        """
        if not user_input:
            return None
        norm = self._normalize_question(user_input)
        if len(norm) < 4:  # 过短消息不判定 (避免"好的"/"嗯"误判)
            return None
        try:
            history = await self._session_manager.get_history(session_id, limit=4) if self._session_manager else []
            for turn in reversed(history):
                speaker = getattr(turn, "speaker", "")
                if speaker != "customer":
                    continue
                prev_norm = self._normalize_question(getattr(turn, "content", ""))
                if prev_norm and prev_norm == norm:
                    # 找到上一轮相同问题 → 找其后的 Bot 回答
                    found = False
                    for later in history:
                        if later is turn and not found:
                            found = True
                            continue
                        if found and getattr(later, "speaker", "") in ("bot", "assistant"):
                            content = getattr(later, "content", "")
                            if content and len(content) > 4:
                                return content
                        if found and getattr(later, "speaker", "") == "customer":
                            break
                    break
        except Exception as exc:
            logger.debug("重复提问检测异常 (放行): session=%s err=%s", session_id, exc)
        return None

    @staticmethod
    def _normalize_question(text: str) -> str:
        """轻量归一化: 全角→半角、去标点空白、去语气词"""
        import re

        t = text.strip()
        t = (
            t.replace("？", "?")
            .replace("！", "!")
            .replace("，", ",")
            .replace("。", ".")
            .replace("、", ",")
            .replace("？", "")
            .replace("呢", "")
            .replace("啊", "")
            .replace("呀", "")
            .replace("吧", "")
            .replace("哦", "")
        )
        return re.sub(r"[\s,?,.!;:；：]+", "", t)

    async def _retrieve(self, query: str) -> str:
        """RAG 检索 (P0-3 上下文工程: 接线 reranker + 相关性阈值 + 首尾重排 + RAG 预算截断)"""
        if self._degradation_mgr.level == DegradationLevel.FALLBACK:
            return ""
        # P1-13 修复: 检索前查 ES/Milvus 熔断器 — 熔断打开时主动跳过检索,
        # 直接走无知识降级 (此前不查熔断, 双挂时靠异常被动降级, 每次失败都打满超时)
        try:
            if (
                self._es_breaker is not None
                and self._es_breaker.is_open
                and self._milvus_breaker is not None
                and self._milvus_breaker.is_open
            ):
                logger.info("ES/Milvus 双熔断, 跳过检索: session 走无知识降级")
                return ""
        except Exception:
            pass
        try:
            from lumio.services.common.retrieval import retrieve as do_retrieve

            settings = get_settings()
            embedding_provider = (
                self._embedding_breaker.provider
                if self._embedding_breaker and self._embedding_breaker.is_available
                else None
            )

            resp: RetrieveResponse = await do_retrieve(
                request=RetrieveRequest(query=query, top_k=settings.rag.top_k, rerank=True),
                es_client=self._es_client,
                milvus_collection=self._milvus_collection,
                embedding_provider=embedding_provider,
                reranker=self._reranker,  # P0-3: 此前传 None, 精排从未生效
                # P1-7 修复: 接线 Redis 检索缓存 — 此前所有调用方未传 redis_client,
                # 缓存是死代码, 相同问题 5 分钟内重复问 = 重复打 ES/Milvus + embedding + rerank
                redis_client=self._session_manager._redis if self._session_manager else None,
            )
            self._last_citation_docids = [r.source_doc for r in (resp.results or [])]
            if resp.results:
                # P0-3 首尾重排 (LongLLMLingua reorder_context="sort"):
                # lost-in-middle — 模型对中段注意力最弱, 相关性最高的文档放最前,
                # 次高放最后, 其余居中
                ordered = resp.results[1:-1] if len(resp.results) > 2 else resp.results[1:]
                ordered = [resp.results[0], *ordered]
                if len(resp.results) > 1:
                    ordered.append(resp.results[-1])
                # P0-2 RAG 预算截断: 按 budget_rag (1200) 截断, 防超长 context
                budget_rag = settings.llm.budget_rag
                parts: list[str] = []
                used = 0
                for i, r in enumerate(ordered):
                    est = _estimate_tokens(r.content)
                    if used + est > budget_rag and parts:
                        break
                    parts.append(f"[{i + 1}] {r.content}")
                    used += est
                return "\n\n".join(parts)
        except Exception as e:
            logger.warning("知识检索失败: %s", e)
        return ""

    async def _check_transfer(
        self,
        text: str,
        intent: IntentResult | None = None,
        sentiment: SentimentLabel = SentimentLabel.NEUTRAL,
        session_id: str = "",
    ) -> tuple[bool, str, TransferTriggerLevel | None]:
        """判断是否需要转人工

        P0-3 修复: 此前恒传 session=None 导致 L3 累计触发 (连续低置信/多次兜底)
        是死代码 — 客户被 Bot 敷衍多轮也永远不会自动转人工. 现加载会话状态传入.
        """
        if self._transfer_checker is None:
            return False, "", None
        try:
            session = None
            if session_id and self._session_manager is not None:
                try:
                    state = await self._session_manager.get_session(session_id)
                    if state is not None:
                        session = state
                except Exception:
                    pass
            should, level, reason = self._transfer_checker.check(
                text=text,
                intent=intent or IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.0),
                sentiment=sentiment,
                session=session,
            )
            return should, reason, level
        except Exception:
            return False, "", None

    async def _offer_transfer(
        self,
        session_id: str,
        user_input: str,
        intent: IntentResult,
        entities: list[Entity],
        sentiment: SentimentLabel,
        reason: str = "",
        answer: str | None = None,
    ) -> dict[str, Any]:
        """L3『先确认再转』: 连续低置信/兜底累计触发的转人工不直接派真人坐席,
        而是挂一个待确认 (pending_action) 并返回固定确认话术 —— 下一轮客户明确回复"是/需要"
        才真正转人工。

        answer 非空时 (本轮已生成真实答案) 先答后问: 答案照常下发, 邀约话术追加在末尾,
        避免客户连问真实问题只得到"要转人工吗?"; 无答案 (降级稿/澄清轮) 仍只挂邀约。

        防『灌乱码刷真人』: 不能仅凭"连续 N 轮低置信"就自动把真人坐席派给输入; 也避免把
        这类自动升级谎标成『客户主动请求』。真·明确转人工 (L1 关键词 / L2 高置信 transfer_agent /
        敏感意图) 不走此确认, 仍即时转。
        """
        if self._session_manager is not None:
            try:
                pending = PendingAction(
                    tool_name="TRANSFER_OFFER",
                    confirm_prompt=_TRANSFER_OFFER_PROMPT,
                    expires_at=datetime.now(UTC) + _TRANSFER_OFFER_TIMEOUT,
                    arguments={"transfer_reason": reason},
                )
                await self._save_pending_action(session_id, pending)
                logger.info("L3 转人工改为先确认: session=%s input=%r answer=%s", session_id, user_input, bool(answer))
            except Exception:
                logger.debug("挂转人工待确认失败, 本轮仅澄清不转: session=%s", session_id)
        if answer:
            reply = f"{answer}\n\n{_TRANSFER_OFFER_PROMPT}"
            response_source = "llm"
        else:
            reply = _TRANSFER_OFFER_PROMPT
            response_source = "clarify"
        return self._build_result(
            session_id,
            user_input,
            reply,
            response_source,
            intent.primary_intent.value,
            intent.primary_confidence,
            entities=entities,
            sentiment=sentiment,
        )

    async def _maybe_offer_transfer_after_clarify(
        self,
        session_id: str,
        user_input: str,
        intent: IntentResult,
        entities: list[Entity],
        sentiment: SentimentLabel,
    ) -> dict[str, Any] | None:
        """澄清轮的 L3『先确认再转』补判 (run() 统一出口调用).

        噪声门/低置信分支在 handler 内提前 return, 原本永远到不了路径末尾的
        _check_transfer, 导致 streak 涨到几十也没有任何升级出口 (会话 178351b41:
        streak=30 零邀请, 真被困客户只能在澄清话术里打转). 这里按"越线轮"
        (streak == 阈值) 补挂邀请: 每个连续低置信周期只邀请一次不刷屏;
        客户明确回复"是/需要"才真派真人, 与 L3 防"灌乱码刷人工"的语义一致.
        """
        if self._session_manager is None:
            return None
        try:
            state = await self._session_manager.get_session(session_id)
        except Exception:
            return None
        if state is None:
            return None
        threshold = get_settings().session.low_confidence_threshold
        if state.low_confidence_streak != threshold:
            return None
        logger.info(
            "澄清轮 L3 越线补判: session=%s streak=%d input=%r",
            session_id,
            state.low_confidence_streak,
            user_input[:50],
        )
        return await self._offer_transfer(
            session_id,
            user_input,
            intent,
            entities,
            sentiment,
            f"L3_LOW_CONFIDENCE_STREAK: 连续 {state.low_confidence_streak} 轮低置信度",
        )

    async def _load_history(self, session_id: str) -> list[dict[str, str]]:
        """加载对话历史作为 LLM 上下文

        三层上下文策略（银行客服最佳实践）:
        - Layer 1: 结构化会话记忆 + 对话摘要（注入 system prompt，永不裁剪）
        - Layer 2: 近期对话历史（token 预算裁剪，被裁剪部分生成摘要）
        - Layer 3: 检索知识（RAG context，由调用方传入）

        摘要触发条件: 当 token 预算导致轮次被裁剪时，对被裁剪的轮次生成摘要，
        保存到 SessionState.conversation_summary。后续轮次复用已有摘要，
        只对新增的裁剪轮次增量摘要（避免每轮都调 LLM）。
        """
        try:
            turns = await self._session_manager.get_history(session_id, limit=20)
            if not turns:
                return []

            settings = get_settings()
            # P0-2 上下文工程修复: 历史预算改用分层预算 budget_history (1500),
            # 而非 8192-1024=7168 (占上下文 87%, 违反前沿分配框架 system 10-15%/
            # history 25-30%/output 20-25%). 被裁剪轮次自动触发增量摘要, 语义不丢.
            budget = max(settings.llm.budget_history, 1024)

            # P1-1 上下文工程修复: 超预算时先尝试选择性压缩 (而非直接裁剪丢弃),
            # 压缩不达标 (质量门) 才走摘要裁剪. 压缩器此前生产零接线.
            total_est = sum(_estimate_tokens(t.content) for t in turns)
            if total_est > budget and len(turns) >= getattr(settings.compression, "min_history_turns", 8):
                try:
                    from lumio.services.bot.context_compressor import compress_history

                    dict_turns: list[dict[str, str]] = [
                        {"role": "user" if t.speaker == "customer" else "assistant", "content": t.content}
                        for t in turns
                    ]
                    compressed = compress_history(dict_turns, max_tokens=budget)
                    # 压缩生效 (有 _compressed 标记) 时改用压缩结果
                    if any("_compressed" in m for m in compressed):
                        turns = [
                            t.model_copy(update={"content": m["content"]})
                            for m, t in zip(compressed, turns, strict=False)
                        ]
                except Exception:
                    logger.debug("历史压缩失败, 走摘要裁剪: session=%s", session_id)

            # 从最近向前累加，找出 token 预算内的轮次
            # 关键轮次（投诉/承诺/转人工）不会被裁剪
            kept_turns: list = []
            used = 0
            split_idx = len(turns)
            for i in range(len(turns) - 1, -1, -1):
                t = turns[i]
                est = _estimate_tokens(t.content)
                is_important = _is_important(t.content)
                if used + est > budget and kept_turns and not is_important:
                    split_idx = i + 1
                    break
                kept_turns.insert(0, t)
                used += est

            # 如果有轮次被裁剪，异步触发摘要压缩（不阻塞用户请求）.
            # P2-28 修复: 仅当裁剪量确实省下预算时才摘要 — 过去每轮小裁剪(2-4 轮,
            # ~300 token)都触发 LLM 摘要, 与当前轮业务生成在同一串行 Ollama GPU 槽抢占,
            # 且两种不同 prompt 的切换击穿 KV 前缀缓存, 把单轮从 4s 顶到 11s. 阈值以下
            # 直接丢弃旧轮(等价"无摘要降级"), 长会话大裁剪时仍正常压缩, 短会话不再每轮付 GPU 代价。
            trimmed_turns = turns[:split_idx]
            trimmed_est = sum(_estimate_tokens(t.content) for t in trimmed_turns)
            if trimmed_turns and trimmed_est >= _SUMMARY_MIN_TRIMMED_TOKENS:
                self._spawn_task(self._ensure_summary(session_id, trimmed_turns))

            return [
                {"role": "user" if t.speaker == "customer" else "assistant", "content": t.content} for t in kept_turns
            ]
        except Exception:
            return []

    async def _ensure_summary(self, session_id: str, trimmed_turns: list) -> None:
        """确保被裁剪的轮次已生成摘要

        用 last_summarized_turn_id 精确追踪已摘要位置，避免 LTRIM 导致计数失准。

        增量策略:
        - 在 trimmed_turns 中查找 last_summarized_turn_id 的位置
        - 如果找到：对该位置之后的轮次生成增量摘要
        - 如果未找到（LTRIM 删除了已摘要的轮次）：对所有 trimmed_turns 重新生成摘要
        - LLM 不可用时跳过（降级为无摘要，结构化记忆仍保证关键实体不丢）

        P1-10 修复: per-session 锁串行化 — 多轮快速对话每轮 spawn 一个摘要任务,
        CAS 只重试 1 次, 后到者失败导致摘要滞后. 锁保证同会话摘要任务串行执行.
        """
        lock = self._summary_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            await self._summary_locked(session_id, trimmed_turns)

    async def _summary_locked(self, session_id: str, trimmed_turns: list) -> None:
        try:
            state = await self._session_manager.get_session(session_id)
            if state is None:
                return

            last_summarized_id = state.last_summarized_turn_id
            last_turn = trimmed_turns[-1]

            # 最后一个裁剪轮次已被摘要，无需更新
            if last_turn.turn_id == last_summarized_id:
                return

            # 在 trimmed_turns 中查找已摘要位置
            split_idx = 0
            if last_summarized_id:
                for i, t in enumerate(trimmed_turns):
                    if t.turn_id == last_summarized_id:
                        split_idx = i + 1
                        break
                # 如果未找到（LTRIM 删除了已摘要轮次），split_idx 保持 0，重新摘要全部

            new_turns = trimmed_turns[split_idx:]
            if not new_turns:
                return

            # 构造摘要 prompt
            conversation = "\n".join(
                f"[{ {'customer': '客户', 'agent': '坐席', 'bot': '机器人'}.get(t.speaker, t.speaker) }] {t.content}"
                for t in new_turns
            )

            existing_summary = state.conversation_summary if split_idx > 0 else ""

            summary_prompt = _SUMMARIZE_SYSTEM_PROMPT
            user_content = (
                f"已有摘要：\n{existing_summary}\n\n新增对话：\n{conversation}"
                if existing_summary
                else f"对话记录：\n{conversation}"
            )

            # 获取 LLM client
            llm_client = self._degradation_mgr._llm
            if llm_client is None:
                return

            try:
                new_summary = await llm_client.chat(
                    messages=[
                        {"role": "system", "content": summary_prompt},
                        {"role": "user", "content": user_content},
                    ],
                    # P2-28 修复: 3s 对本地 qwen2.5:7b 会话摘要过紧 — 孤立调用即需 2-3s,
                    # 与上一轮业务生成在同一串行 Ollama GPU 槽发生时被顶到 10s+,
                    # 3s 下必超时→重试→占用 GPU 21s, 反串行阻塞下一轮业务生成 (实测 20s 尖峰)。
                    # 抬到与生成同源 generate_timeout, 让慢但能成功的单次调用一次走完, 不重试翻倍。
                    timeout=max(20.0, get_settings().llm.generate_timeout),
                )
            except Exception:
                logger.debug("对话摘要生成失败: session=%s", session_id)
                return

            if not new_summary or not new_summary.strip():
                return

            # P1-1 上下文工程修复: 摘要 token 上限 (≤ budget_history) —
            # 旧实现 LLM 返回什么存什么, 摘要无界膨胀后反噬下次预算 (累积压缩损失)
            from lumio.shared.config import get_settings as _gs

            summary = new_summary.strip()
            budget_summary = max(_gs().llm.budget_history, 1024)
            if _estimate_tokens(summary) > budget_summary:
                summary = summary[: budget_summary * 2] + "\n...[摘要已截断]"
                logger.info("对话摘要超预算截断: session=%s", session_id)

            # 用 patch_state 增量写入摘要
            result = await self._session_manager.patch_state(
                session_id=session_id,
                expected_version=state.version,
                patches={
                    "conversation_summary": summary,
                    "summary_turn_count": len(trimmed_turns),
                    "last_summarized_turn_id": last_turn.turn_id,
                },
                writer="bot_agent:summary",
            )
            if result.get("ok"):
                logger.info(
                    "对话摘要已更新: session=%s trimmed=%d new=%d",
                    session_id,
                    len(trimmed_turns),
                    len(new_turns),
                )
            else:
                logger.warning("对话摘要 CAS 写入失败: session=%s", session_id)
        except Exception:
            logger.debug("摘要更新异常: session=%s", session_id)

    async def _build_session_memory(self, session_id: str) -> str:
        """构建结构化会话记忆（注入 system prompt，永不裁剪）

        银行客服核心需求：即使对话历史被裁剪，关键实体（卡号、金额、日期）
        和意图栈仍需保留，避免 Bot 重复收集敏感信息。

        记忆内容:
        - 对话摘要: 被裁剪轮次的摘要（对话脉络、Bot 承诺、处理进度）
        - 客户画像: VIP等级、卡种、风险偏好
        - 实体池: 对话中已抽取的实体（卡号、金额、日期等）
        - 意图栈: 客户的意图历史
        """
        try:
            state = await self._session_manager.get_session(session_id)
            if state is None:
                return ""

            parts: list[str] = []

            # 对话摘要（被裁剪轮次的脉络，最高优先级）
            if state.conversation_summary:
                parts.append(f"[对话摘要]\n{state.conversation_summary}")

            # 客户画像
            profile_parts: list[str] = []
            if state.vip_level and state.vip_level != "普通":
                profile_parts.append(f"VIP等级={state.vip_level}")
            if state.card_types:
                profile_parts.append(f"卡种={','.join(state.card_types)}")
            if state.risk_tolerance and state.risk_tolerance != "R2":
                profile_parts.append(f"风险偏好={state.risk_tolerance}")
            if profile_parts:
                parts.append(f"[客户画像] {', '.join(profile_parts)}")

            # 实体池（从 last_entities 读取，已由 add_turn 维护）
            # A6: PII 防护 - 跨会话 entity_pool 仅带入白名单内的 entity_type,
            # 过滤 card_number / id_number / phone / cvv / password 等敏感字段
            if state.last_entities:
                from lumio.shared.config import get_settings

                allowlist = set(get_settings().guard.entity_pool_allowlist)
                filtered = [e for e in state.last_entities if e.entity_type in allowlist and e.value]
                if filtered:
                    entity_strs = [f"{e.entity_type}={e.value}" for e in filtered]
                    parts.append(f"[已知实体] {', '.join(entity_strs)}")

            # 意图栈
            if state.intent_stack:
                intent_strs = [i.value if hasattr(i, "value") else str(i) for i in state.intent_stack]
                parts.append(f"[意图历史] {' → '.join(intent_strs)}")

            # 最近意图
            if state.last_intent:
                parts.append(f"[当前意图] {state.last_intent.value}")

            if not parts:
                return ""

            memory = "\n".join(parts)
            # P0-2 上下文工程: L3 动态层预算截断 (budget_customer=400) —
            # 记忆注入 system prompt 需受控, 防长会话下无界膨胀破坏前缀缓存
            from lumio.shared.config import get_settings as _gs

            budget_customer = _gs().llm.budget_customer
            if _estimate_tokens(memory) > budget_customer:
                truncated = memory[: budget_customer * 2]  # 粗截 (token 估算 ~0.5/字符)
                logger.info(
                    "会话记忆超预算截断: %d → %d chars",
                    len(memory),
                    len(truncated),
                )
                memory = truncated + "\n...[记忆已截断]"
            return memory
        except Exception:
            logger.debug("构建会话记忆失败: session=%s", session_id)
            return ""

    async def _load_slot_prompt(
        self, session_id: str, intent: IntentLabel, entities: list[Entity], user_input: str = ""
    ) -> str:
        """加载/更新槽位状态并持久化到会话状态(SessionState.slot_values), 返回槽位 prompt 段

        生产级语义(替代旧独立 key lumio:slot:* 裸读写):
        - 单一真相源: 槽位已填值随会话 meta 持久化, 经 patch_state 版本 CAS + 槽名级 union 写入,
          并发两轮各自填不同槽互不覆盖; TTL 随会话, 消除硬编码 3600 与生命周期不一致。
        - 历史反填: 本轮实体 ∪ last_entities ∪ entity_pool 共同补槽(不再只填本轮)。
        - 跨意图继承: 意图切换不再重建 tracker 清空已填槽, 同名槽直接复用(如 挂失→补卡)。
        - 派生: 满卡号 CARD_NUMBER 已填而卡尾缺失 → 派生后四位, 避免"有满卡仍追问尾号"。
        - COMPLAINT: issue_detail 无法由实体抽取命中, 原文回填, 解除必填食位对噪声门的互锁。
        - 值归一: 号码类去分隔符 + 长度上限 64, 防 prompt 膨胀。
        仅当当前意图有已定义槽位时才返回非空 prompt。
        """
        from lumio.services.bot.entity_extractor import normalize_entity_type
        from lumio.services.bot.slot_tracker import _ENTITY_TO_SLOT
        from lumio.shared.models import SlotValue

        state = None
        if self._session_manager is not None:
            try:
                state = await self._session_manager.get_session(session_id)
            except Exception:
                state = None
        fills: dict[str, SlotValue] = dict(state.slot_values) if state else {}

        # 候选实体: 本轮 ∪ 历史 last_entities ∪ entity_pool (跨轮反填)
        candidates: list[Entity] = list(entities or [])
        if state is not None:
            candidates += list(state.last_entities or [])
            candidates += list(state.entity_pool or [])

        def _clean(kind: str, raw: str) -> str:
            text = (raw or "").strip()
            if kind in ("card_number", "card_tail", "phone_number"):
                text = re.sub(r"[\s\- ]", "", text)
            return text[:64]

        # 实体 → 槽位 (首值优先, 已填不覆盖)
        for e in candidates:
            etype = normalize_entity_type(e.entity_type)
            if not etype or not e.value:
                continue
            slot_name = _ENTITY_TO_SLOT.get(etype)
            if not slot_name or slot_name in fills:
                continue
            value = _clean(slot_name, e.value)
            # P1a: 完整卡号槽位只落尾四位 (PAN 不入会话态/审计日志, PCI 合规;
            # 会话 1fb54681 复盘: 16 位明文卡号同时进了 slot_values 与历史实体池)
            if slot_name == "card_number":
                value = pan_to_tail(value)
            fills[slot_name] = SlotValue(name=slot_name, value=value, source="entity")

        # 派生: 卡号槽(已折叠为****尾四位) -> 卡尾
        card_full = fills.get("card_number")
        if card_full and card_full.value and "card_tail" not in fills:
            tail = card_full.value[-4:] if len(card_full.value) >= 4 else card_full.value
            if tail:
                fills["card_tail"] = SlotValue(name="card_tail", value=tail, source="derived")

        # COMPLAINT(旧)/dispute_submit(主名): 必填槽位 issue_detail 原文回填
        if normalize_intent(intent.value) == IntentLabel.DISPUTE_SUBMIT and "issue_detail" not in fills:
            text = (user_input or "").strip()
            if len(text) >= 4:
                fills["issue_detail"] = SlotValue(name="issue_detail", value=text[:256], source="message")

        # CAS 持久化(改经 patch_state, 继承版本 CAS + 槽名级 union)
        if state is not None and self._session_manager is not None and fills:
            try:
                patch = {"slot_values": {k: v.model_dump() for k, v in fills.items()}}
                await self._session_manager.patch_state(session_id, state.version, patch, writer="bot_slot")
            except Exception as exc:
                logger.debug("槽位状态写入失败(不阻断): session=%s err=%s", session_id, exc)

        tracker = SlotTracker.for_intent(intent)
        tracker.apply_fills(fills)
        return tracker.build_prompt() if tracker.has_slots else ""

    async def _save_awaiting_slots(self, session_id: str, intent: IntentLabel, missing: list[tuple[str, str]]) -> None:
        """把"bot 正在等什么槽"写进会话态 (P0, 会话 1fb54681 复盘).

        bot 发出槽位追问/参数索取的那一轮调用: 下轮短回复("3"/"3000")无论被分类
        成什么(裸数字必然 faq@0.00), 噪声门都读这份**上文快照**判回话, 不再依赖
        本轮意图重算 tracker -- "在等什么"是上文的属性, 不该由最不可靠的短填充推导。
        只存槽名/标签元信息(不含值); 消费(放行)后由 _clear_awaiting_slots 整体清空。
        """
        if self._session_manager is None or not missing:
            return
        snapshot = {
            "intent": intent.value,
            "slots": [list(s) for s in missing],
            "updated_at": datetime.now(UTC).isoformat(),
        }
        with contextlib.suppress(Exception):
            state = await self._session_manager.get_session(session_id)
            if state is not None:
                await self._session_manager.patch_state(
                    session_id,
                    state.version,
                    {"awaiting_slots": snapshot},
                    writer="bot:awaiting_slots",
                )

    async def _clear_awaiting_slots(self, session_id: str, version: int) -> None:
        """消费/轮转后清空等待快照, 防陈旧快照豁免后续无关噪声输入."""
        if self._session_manager is None:
            return
        with contextlib.suppress(Exception):
            await self._session_manager.patch_state(
                session_id, version, {"awaiting_slots": {}}, writer="bot:awaiting_slots"
            )

    async def _session_awaiting_slots(self, session_id: str) -> tuple[str | None, list[tuple[str, str]]]:
        """读会话态等待快照: (等待意图主名, [(槽名, 标签), ...])。无快照返回 (None, [])."""
        if self._session_manager is None:
            return None, []
        try:
            state = await self._session_manager.get_session(session_id)
        except Exception:
            return None, []
        if state is None:
            return None, []
        raw = getattr(state, "awaiting_slots", None) or {}
        intent_name = raw.get("intent") if isinstance(raw, dict) else None
        slots_raw = raw.get("slots") if isinstance(raw, dict) else None
        slots = (
            [(str(s[0]), str(s[1])) for s in slots_raw if isinstance(s, list | tuple) and len(s) >= 2]
            if slots_raw
            else []
        )
        return (str(intent_name) if intent_name else None), slots

    async def _update_awaiting_snapshot(self, session_id: str, intent: IntentLabel, result: dict[str, Any]) -> None:
        """按本轮回复维护会话态等待快照 (P0, 会话 1fb54681).

        回复**在等客户补充信息**才落快照, 判据(按可靠度排序):
        1. response_source == "slot_hint" -- 确定性槽位追问, 缺槽就在 missing_names;
        2. _build_result 的 result["missing_slots"] -- handler 显式声明本轮在追问缺槽;
        3. 工具编排向客户索参数/敏感确认: response_source 为 "tool_confirm" 或
           tool/llm 回复中含参数索取话术(请提供/请告知+金额/期数/卡号等)。
        其余(澄清/完整作答/转人工/重复问答/危机)一律清空 -- 这些回复不等任何槽,
        陈旧快照会把后续无关输入误豁免成"回话"。
        """
        if self._session_manager is None or result is None:
            return
        src = result.get("response_source") or ""
        missing: list[tuple[str, str]] = []
        if src == "slot_hint":
            declared = result.get("missing_slots") or []
            missing = [(str(s[0]), str(s[1])) for s in declared]
            if not missing:
                # slot_hint 未透传缺槽明细 -> 按意图定义 + 已填值现算
                missing = await self._missing_required_slots(session_id, intent)
        elif result.get("missing_slots"):
            missing = [(str(s[0]), str(s[1])) for s in result["missing_slots"]]
        elif src == "tool_confirm":
            # 敏感写确认轮: pending 状态机在等"确认/取消", 非槽位, 不落快照
            missing = []
        else:
            content = str(result.get("response") or "")
            if src in ("tool", "llm") and _asks_for_parameters(content):
                missing = await self._missing_required_slots(session_id, intent)
                # 业务意图无必填槽定义但话术在索参数(如工具 schema 缺口) -> 记通用参数槽,
                # 只为噪声门回话豁免提供"上文在等数字/短串"的信号。
                if not missing:
                    missing = [("__param__", "参数")]
        if missing:
            await self._save_awaiting_slots(session_id, intent, missing)
        else:
            state = None
            with contextlib.suppress(Exception):
                state = await self._session_manager.get_session(session_id)
            if state is not None and getattr(state, "awaiting_slots", None):
                await self._clear_awaiting_slots(session_id, state.version)

    async def _missing_required_slots(self, session_id: str, intent: IntentLabel) -> list[tuple[str, str]]:
        """读会话状态 slot_values + 当前意图静态必填定义, 返回尚未填充的必填槽 [(name, label), ...].

        用于噪声门先判"本句是否在回上文话"+ 指代零主回指补槽: 存在缺的必填槽说明 bot 刚在等
        这类信息, 本句把它填上即视为回话, 避免"上轮问卡号/金额, 本句答数字"被误判成噪声澄清。
        只读不写, 与 _load_slot_prompt 共用同一真相源(SessionState.slot_values)。
        无会话/异常时返回空(等于不启用回话豁免)。
        """
        if not session_id or self._session_manager is None:
            return []
        try:
            state = await self._session_manager.get_session(session_id)
        except Exception:
            state = None
        if state is None:
            return []
        fills = state.slot_values or {}
        tracker = SlotTracker.for_intent(intent)
        tracker.apply_fills(fills)
        return [(s.name, s.label) for s in tracker.missing_required]

    @staticmethod
    def _build_slot_hint(intent_label: IntentLabel, missing_names: list[str]) -> str:
        """按意图槽位定义拼追问话术 (近线低置信替代通用澄清用, 只问不答)。

        只取缺的必填槽 (missing_names 来自会话状态计算), 最多拼 2 条追问,
        输出如 "请问您想分期的金额是多少？您希望分几期？"。
        """
        tracker = SlotTracker.for_intent(intent_label)
        ask_by_name = {s.name: s.ask_prompt for s in tracker.slots if s.ask_prompt}
        prompts = [ask_by_name[n] for n in missing_names if ask_by_name.get(n)]
        return "".join(prompts[:2])

    @staticmethod
    def _prefer_slot_hint(gate_reason: str, confidence: float, has_missing_slots: bool) -> bool:
        """噪声门拦截后是否优先给槽位追问而非通用澄清。

        - 无必填缺槽 → False (无槽可问, 保持澄清/邀约链路)。
        - low_confidence: 近线带 (≥ _SLOT_HINT_CONF_FLOOR) 的真实意图 → True。
        - fast_slow_disagreement: 慢通道高置信 (≥ _DISAGREE_SLOT_HINT_FLOOR) 且意图
          有必填缺槽 → True; 槽位追问只问不答, 即便输入真是乱码也零风险。
        - 其余拦截原因 (noise/ood/arbiter/input_gate) → False, 维持既有保守链路。
        """
        if not has_missing_slots:
            return False
        if gate_reason == "low_confidence":
            return confidence >= _SLOT_HINT_CONF_FLOOR
        if gate_reason == "fast_slow_disagreement":
            return confidence >= _DISAGREE_SLOT_HINT_FLOOR
        return False

    @staticmethod
    def _is_confident_business_intent(intent: IntentResult | None) -> bool:
        """本句是否为明确业务意图。

        L3 转人工邀约不应贴在正常业务回复上 (会话 956a5fd2 第 9 轮复盘: 分期@0.80 的
        知识回复尾部被追加"是否转人工", 用户已回到正轨仍被邀约, 画蛇添足)。
        明确业务意图 = 高置信(≥0.7) 且不是泛化兜底类(faq/chitchat)。
        """
        if intent is None:
            return False
        primary = normalize_intent(intent.primary_intent.value)
        if primary in (IntentLabel.FAQ, IntentLabel.FAQ_PRODUCT, IntentLabel.NB_CHITCHAT, IntentLabel.CHITCHAT):
            return False
        return intent.primary_confidence >= 0.7

    async def _pick_clarify_response(self, session_id: str) -> str:
        """按 low_confidence_streak 轮换澄清话术 (会话 bcf51ded 复盘: 连续两轮一字不差
        回"您的意思我还没太理解。"太生硬)。首轮(streak=0)即用软化变体;
        旧 CLARIFY_RESPONSE 仅会话状态不可用时回退。
        """
        state = None
        if self._session_manager is not None:
            with contextlib.suppress(Exception):
                state = await self._session_manager.get_session(session_id)
        if state is None:
            return CLARIFY_RESPONSE
        streak = int(getattr(state, "low_confidence_streak", 0) or 0)
        return CLARIFY_RESPONSES[streak % len(CLARIFY_RESPONSES)]

    async def _evaluate_noise_gate(
        self,
        session_id: str,
        user_input: str,
        intent: IntentResult,
        entities: list[Entity] | None,
        missing_slots: list[tuple[str, str]] | None = None,
        history: list[dict[str, Any]] | None = None,
    ) -> tuple[str | None, dict[str, Any]]:
        """噪声门统一判定(多轮治理 P0): 返回 (block_reason|None, evidence)。

        block_reason:
          - "low_confidence"  分类器不识别(conf < 下限)
          - "noise"           内容级噪声(纯数字/零元音乱码)且非回话
          - None              放行
        回话豁免: missing_slots 由 run() 在 domain 分派前预取(见 run 注释), 避免各 handler 的
        _load_slot_prompt 因意图切换重置 tracker 而清掉"上文在等什么"; 本句填上缺的必填槽/
        纯数字 -> 放行, 即便形状像噪声或低置信。开关关时 run() 传 [] -> 行为等同旧版。
        P0 联合快照(会话 1fb54681): 上轮槽位追问落盘的 awaiting_slots 也并入 missing --
        裸数字"3"被分类成 faq@0.00 时, 本轮意图快照必然为空, 只有会话态快照能兜住;
        evidence.awaiting_hit=True 标记"靠上文快照放行", 供消费端换回等待意图续办。
        敏感索取豁免(P-a, 会话 956a5fd2): 上轮 bot 索取卡号/验证码等敏感凭证时, 本句 4-6 位
        纯数字视为"回应索取"放行, 证据标记 sensitive_reply 供 handler 走确定性桥接话术。
        """
        guard_on = get_settings().classification.rephrase_guard_enabled
        missing: list[tuple[str, str]] = list(missing_slots or [])
        # 会话态等待快照(上文属性) 并入本轮意图快照; 槽名去重(本轮意图优先)
        await_intent, awaiting = await self._session_awaiting_slots(session_id)
        if awaiting:
            current_names = {name for name, _ in missing}
            missing += [s for s in awaiting if s[0] not in current_names]
        awaiting_hit = bool(awaiting) and not bool(missing_slots)
        ask_marker = _last_bot_turn_asked_sensitive(history)
        normalized_input = _normalize_text(user_input)
        if ask_marker == "卡号":
            sensitive_reply = bool(re.fullmatch(r"\d{13,19}", normalized_input))
        elif ask_marker:
            sensitive_reply = bool(re.fullmatch(r"\d{4,6}", normalized_input))
        else:
            sensitive_reply = False
        is_replying = _is_replying_to_context(user_input, entities, missing) or sensitive_reply
        low_conf = intent.primary_confidence < CLARIFY_CONFIDENCE_FLOOR
        is_noise = _is_noise_input(user_input)
        # P1: energy-OOD "认不认"信号 (仅 ood_enabled 且 BERT 快路径有 energy 时启用).
        # energy "unknown" → 倾向 OOD/噪声; "ambiguous" → 落在模糊带, 交 LLM 仲裁.
        cls_settings = get_settings().classification
        energy = None
        verdict = None
        if cls_settings.ood_enabled:
            # P1 并发修复: energy 随本次分类的 IntentResult 透传(intent.energy),
            # 关闭对共享 classifier._last_energy 的跨 await 读取, 避免多会话串线.
            # 只要判定算过 energy 就必然挂在本次结果上; None 即本次确实没算到, 不兜底旧值.
            energy = getattr(intent, "energy", None)
            if energy is not None:
                verdict = ood_verdict(energy, cls_settings.ood_energy_threshold, cls_settings.ood_ambiguous_band)
        evidence: dict[str, Any] = {
            "rephrase_guard_enabled": guard_on,
            "is_replying": is_replying,
            "missing_slots": missing,
            "is_noise_shape": is_noise,
            "low_confidence": low_conf,
            "confidence": intent.primary_confidence,
            "sensitive_reply": sensitive_reply,
            "awaiting_hit": awaiting_hit,
            "awaiting_intent": await_intent if awaiting_hit else None,
            "energy": energy,
            "ood_verdict": verdict,
            # P0 快慢分歧证据: 事后审计可直接从决策日志定位"哪一路在幻觉".
            "fast_conf": intent.fast_conf,
            "fast_intent": intent.fast_intent.value if intent.fast_intent else None,
            "fast_slow_disagreement": _fast_slow_disagreement(intent),
        }
        if is_replying:
            return None, evidence
        # P1: energy 强"不认" -> 直接当噪声拦截 (非回话), 高于低置信的澄清强度
        if verdict == "unknown":
            return "ood_unknown", evidence
        if low_conf:
            # 分类器失败兜底 (conf=0.0) 不拦: 放行走 RAG, 由检索 grounding/
            # 词法证据门决定答还是澄清 —— GPU 争抢下分类高频超时, 一刀切
            # 澄清把有据可答的真问题全误杀 (会话 f1fec705/9d64b59 实测)。
            # 真正"不认识"由规则路径给出 0<conf<floor, 仍走澄清。
            if intent.primary_confidence <= 0.0 and not _is_noise_input(user_input):
                # 乱码 (噪声字形) 仍拦; 非乱码的真问题放行
                evidence["classify_fallback_passthrough"] = True
                return None, evidence
            return "low_confidence", evidence
        if is_noise:
            return "noise", evidence
        # P0 快慢分歧: 慢路径把乱码/含糊输入幻觉成另一意图且置信通胀(最终置信
        # 越过上面所有以它为触发条件的门) -> 拦回确定性澄清 (会话 e33d1fa8).
        # 收窄 (会话 9d64b59 复盘): 仅"危险分歧"拦截 —— 慢路径低置信(<0.5) 或
        # 慢意图属敏感/转人工类; 慢路径强置信的正常业务意图放行, 不再误拦
        # "信用卡额度是什么"这类真问题。
        if _fast_slow_disagreement(intent) and _disagreement_is_dangerous(intent):
            return "fast_slow_disagreement", evidence
        # P1: 模糊带宽仲裁 — 语义吃不准(BERT 中段 energy / 低置信但非噪声)时交 LLM 裁决.
        # LLM 结果作为弱信号投票, 不单独放行: 仅当 LLM 明确判 business 时结合实体/槽位放行,
        # 判 noise 则拦截; chitchat/unknown 交由下方既有语义路径(不硬拦, 也不硬答).
        # P2 重接线: 触发频带从"最终置信<0.5"扩到"快路径证据弱(fast_conf<0.5)". 慢路径置信
        # 在乱码上会通胀(e33d1fa8: 0.39 被覆盖成 0.7), 以最终置信为触发条件的仲裁器永远等不到
        # 通胀结果进来; 快路径分数是独立信号, "弱快证据 + 高慢置信"(两路同意图的弱输入,
        # 分歧门因意图一致不覆盖)正是该让 LLM 看一眼是不是噪声的形态.
        fast_weak = intent.fast_conf is not None and intent.fast_conf < _DISAGREEMENT_FAST_FLOOR
        if (
            cls_settings.llm_arbiter_enabled
            and (verdict == "ambiguous" or (verdict is None and (intent.primary_confidence < 0.5 or fast_weak)))
            and self._classifier is not None
            and getattr(self._classifier, "_llm", None) is not None
        ):
            verdict_domain = await self._arbitrate_domain(user_input, getattr(self._classifier, "_llm", None))
            evidence["arbiter_domain"] = verdict_domain["domain"]
            if verdict_domain["domain"] == "noise":
                return "arbiter_noise", evidence
        # P2: 多信号噪声闸 (InputGate) 额外投票 — 惊讶度异常 + 不认/吃不准信号佐证才拦.
        # 回话/实体填槽由 InputGate 内部硬否决, 不因惊讶度误杀; 开关默认关(观测).
        if cls_settings.noise_gate_enabled:
            ig = self._gate
            ig_verdict = await asyncio.to_thread(
                ig.evaluate,
                text=user_input,
                is_replying=is_replying,
                low_conf=low_conf,
                noise_shape=is_noise,
                energy_verdict=verdict,
            )
            evidence["input_gate"] = ig_verdict.signals
            if ig_verdict.decision == "block":
                return f"input_gate_{ig_verdict.reason}", evidence
        return None, evidence

    async def _arbitrate_domain(self, user_input: str, llm: Any) -> dict[str, Any]:
        """P1: 把 LLM 仲裁调用薄封装, 失败返回 unknown(不阻断)."""
        try:
            if hasattr(llm, "arbitrate"):
                arbitrated = await llm.arbitrate(user_input)
                if isinstance(arbitrated, dict):
                    return arbitrated
                logger.debug("LLM 仲裁返回非结构化结果(视为 unknown): %r", user_input)
            return {"domain": "unknown", "confidence": 0.0, "structured": False}
        except Exception:
            logger.debug("LLM 仲裁异常(视为 unknown 弱信号): %r", user_input)
            return {"domain": "unknown", "confidence": 0.0, "structured": False}

    # ── P3 误杀回流 (感知缝): 探针式标记 + staging 落盘 ──
    def _mark_reply_pass(self, session_id: str, user_input: str) -> None:
        """记录某会话刚被"回话放行"(CONTEXT_REPLY_PASS).

        定长 LRU: 超过上限时淘汰最早未被访问者; 每次访问把该会话移到队尾。
        """
        if not session_id:
            return
        entry = self._recent_reply_pass.pop(session_id, None) or {}
        entry.update({"input": user_input or "", "ts": time.monotonic()})
        self._recent_reply_pass[session_id] = entry
        while len(self._recent_reply_pass) > _REPLY_PASS_MAX_SESSIONS:
            self._recent_reply_pass.popitem(last=False)

    async def _maybe_flag_mis_kill(self, session_id: str, user_input: str, gate_reason: str) -> None:
        """上轮判"回话放行"、本轮又被噪声门拦 → 疑似误杀, 探针式回流 + 决策留痕.

        感知→归因: 把样本写进 backflow staging (人审/回灌种子), 并打 MIS_KILL_CANDIDATE。
        只认新鲜窗口内的放行 (过久未触发说明非"紧接着回话被拦", 不误归因); 失败不阻断主链路。
        """
        if not session_id:
            return
        prev = self._recent_reply_pass.pop(session_id, None)
        if not prev:
            return
        # 放行距本次拦截太久 → 不算"误杀", 避免把无关的历史回话错归因到这个样本
        if time.monotonic() - prev.get("ts", 0.0) > _REPLY_PASS_FRESH_SECONDS:
            return
        try:
            from lumio.services.common.trap_collector import default_trap_collector

            default_trap_collector().record_backflow(
                text=user_input,
                session_id=session_id,
                reason=f"reply_pass_then_blocked:{gate_reason}",
                extra={"prev_reply_input": prev["input"]},
            )
            log_decision(
                session_id=session_id,
                agent_name="bot_agent",
                action=DecisionAction.MIS_KILL_CANDIDATE,
                reasoning=f"疑似误杀: 上轮回话放行后本轮被噪声门拦({gate_reason})",
                evidence={"prev_reply_input": prev["input"], "reblocked_reason": gate_reason},
                turn_id=uuid_module.uuid4().hex[:16],
            )
        except Exception:
            logger.debug("疑似误杀标记失败(不阻断): session=%s", session_id)

    def _build_result(
        self,
        session_id: str,
        user_input: str,
        response: str,
        response_source: str,
        primary_intent: str = "faq",
        primary_confidence: float = 0.0,
        should_transfer: bool = False,
        transfer_reason: str = "",
        entities: list[Entity] | None = None,
        sentiment: SentimentLabel = SentimentLabel.NEUTRAL,
        verification: VerificationRequest | None = None,
        retrieval_context: str = "",
    ) -> dict[str, Any]:
        try:
            intent_label = IntentLabel(primary_intent)
        except ValueError:
            intent_label = IntentLabel.FAQ

        result = {
            "session_id": session_id,
            "user_input": user_input,
            "intent": IntentResult(primary_intent=intent_label, primary_confidence=primary_confidence),
            "entities": entities or [],
            "sentiment": sentiment,
            "classify_source": "",
            "domain": get_domain(intent_label),
            "retrieval_context": retrieval_context,
            "response": response,
            "response_source": response_source,
            "should_transfer": should_transfer,
            "transfer_reason": transfer_reason,
            "session_state": None,
        }
        if verification is not None:
            result["verification"] = verification.model_dump()
        return result


# ── 快速路径判断 ──


def _normalize_text(text: str) -> str:
    """归一化: 去首尾空白 + 去常见全半角标点 + 小写 (判定问候/告别用)."""
    return re.sub(r"[\s，。！？；、—…,.!?;:：~～]+", "", text).lower()


def _is_greeting(text: str) -> bool:
    return _normalize_text(text) in {"你好", "您好", "嗨", "hi", "hello", "在吗", "在不在"}


# 收尾对话句式: 命中即视为告别 (快速路径), 避免"谢谢，没有其他问题了"这类
# 无害收尾语被判成 faq 白白走一次 LLM (实测 13s + 一次模型调用).
_FAREWELL_WORDS = {"再见", "拜拜", "bye", "谢谢", "感谢", "没了", "没有了"}
_FAREWELL_CLOSERS = (
    "没有其他问题了",
    "没有别的问题了",
    "没有其它问题了",
    "没有问题了",
    "没别的事了",
    "没什么事了",
    "就这些",
    "就这些问题",
    "暂时没有了",
    "不用了谢谢",
)


def _is_farewell(text: str) -> bool:
    norm = _normalize_text(text)
    return bool(norm) and (norm in _FAREWELL_WORDS or any(c in norm for c in _FAREWELL_CLOSERS))


# 乱答防上线判决阈值: 意图分类置信低于此即视为"分类未识别"(与 classifier 的
# fallback 判定阈值一致)。knowledge 与 fallback 两条生成路径共同遵守——低于此
# 时不交给 LLM 编造作答, 一律回确定性澄清话术(零幻觉、秒回)。
CLARIFY_CONFIDENCE_FLOOR = 0.3

# 近线槽位追问下限: 置信低于澄清线但 ≥ 此值、且意图有必填缺槽时, 用槽位追问
# 替代通用澄清话术 (只问不答, 零幻觉风险)。低于此值的乱码仍是通用澄清。
# 实测依据 (会话 f08227d4): 乱码落点 ~0.18-0.29, 真实"分期"被乱码历史拖到 0.2986;
# 0.25 能把"近线真实意图"与"纯乱码"分开, 且不改变 P0 防线 (仍不进 LLM 生成)。
_SLOT_HINT_CONF_FLOOR = 0.25

# 分歧门拦截后的槽位追问下限 (慢通道置信): 慢通道高置信 + 意图有必填缺槽时才改给
# 槽位追问。会话 fb87b1a4: 分期慢通道 0.8 正确、快通道误判 transfer_agent@0.325,
# 被分歧门拦后澄清轮撞 streak==3 直接变转人工邀约 —— 客户连问真实问题得不到答案。
# 乱码被慢通道通胀成无必填槽的意图 (如 bill_query@0.7, e33d1fa8) 时 missing_slots 为空,
# 不会走此路径, P0 防线不受影响。
_DISAGREE_SLOT_HINT_FLOOR = 0.7

# P3 误杀探针生命周期: 放行记录定长上限 + "紧接着被拦"的新鲜度窗口。
# 长运行服务内存有界; 只有窗口内(紧接上一轮)被拦才算疑似误杀, 过久不归因。
_REPLY_PASS_MAX_SESSIONS = 5000
_REPLY_PASS_FRESH_SECONDS = 180.0


def _has_grounding(session_memory: str, history: list | None) -> bool:
    """是否有可依托的对话依据(判断是否"追问"而非全新乱码).

    追问能答的前提是存在真实对话脉络(上一轮的意图/实体/摘要)或已发生多轮对话;
    否则会话记忆为空/仅兜底, 属于"首句即无意义输入", 不应对其编造。
    """
    if not session_memory and not history:
        return False
    markers = ("[对话摘要]", "[已知实体]", "[意图历史]")
    if any(m in session_memory for m in markers):
        return True
    return bool(history)


def _is_noise_input(text: str) -> bool:
    """内容级噪声判定: 去掉数字与标点后没有可表义字符(纯数字/纯符号/仅空白),
    或余下为纯拉丁字母且无任何元音的乱码串(如 "YRNN"/"HTY"/"HWS46"/"jdfkl"),
    或余下为超短重复结构(同字≥4连/ABAB/AABB, 如 "卡卡卡卡"/"看快看快"/"嗯嗯啊啊")。

    用于堵住快路径乱答漏点: BERT 快路径可能把这类噪音高置信(≥0.7)判成闲聊/FAQ
    直接返回, 置信度门(conf<0.3)盖不住, 若放行会给 LLM 硬编答复。这类输入无法被
    LLM 正确作答, 必须确定性澄清。带真实语义的内容("22元"、"卡号后四位 4444")因
    中文回落; 真实英文词("refund"/"bill"/"transfer"/"happy")恒含元音, 不被误伤;
    三字内口语("哈哈哈")与真实短句不命中重复结构, 不被误伤。
    """
    stripped = re.sub(r"[\d０-９\s，。、；：！？!?,.;:·…─~～#@*&%$()（）\"'\-]+", "", text)
    if not stripped:
        return True
    # 纯拉丁字母且零元音的乱码串(≥3 位)-> 噪音. 用 fullmatch 限定"余下全是字母",
    # 避免把含中文的串(如 "HWS46元")误伤; 只判零元音(而非元音≤1), 避免误伤 bill/happy.
    if re.fullmatch(r"[A-Za-z]{3,}", stripped) and not any(ch in "aeiouAEIOU" for ch in stripped):
        return True
    # P0 中文乱码形状: 整串(≥4位)为同一字连排/两字交替/两字各连排 -- 键盘乱打或
    # 输入法误触的典型形态. fullmatch 限定"整串只有该结构", 真实句子里局部重复
    # ("好的好的我知道了")不受影响; 真实叠词口语(哈哈哈=3字)因长度门槛不拦.
    return bool(
        len(stripped) >= 4
        and (
            re.fullmatch(r"(.)\1{3,}", stripped)  # AAAA: 卡卡卡卡
            or re.fullmatch(r"(.)\1+(.)\2+", stripped)  # AABB: 嗯嗯啊啊
            or re.fullmatch(r"(..)\1+", stripped)  # ABAB: 看快看快
        )
    )


# P0 快慢分歧门: BERT 快路径与 LLM 慢路径对同一输入给出不同意图, 且快路径置信偏低
# (< 0.5)或慢路径置信也不高(< 0.4)时, 判为"两路分歧" -- 乱码/极含糊输入的典型特征
# (会话 e33d1fa8: 乱码 BERT limit_query@0.39 / LLM bill_query@0.7, 慢路径置信通胀
# 越过一切以"最终置信"为触发条件的闸门, 架构审核根因 R1). 实测 seed_dataset 191
# 真值误伤 1 条(0.5%, 代价一轮澄清); 16 条手工乱码拦 12 条.
_DISAGREEMENT_FAST_FLOOR = 0.5
_DISAGREEMENT_SLOW_FLOOR = 0.4


def _disagreement_is_dangerous(intent: IntentResult) -> bool:
    """快慢分歧是否值得拦截: 慢路径低置信, 或慢意图属敏感/转人工类 (幻觉成
    转人工/挂失等才有真实危害; 幻觉成普通业务意图由下游门控兜底)。"""
    from lumio.shared.models import SENSITIVE_INTENTS

    if intent.primary_confidence < 0.5:
        return True
    try:
        normalized = normalize_intent(intent.primary_intent.value)
    except Exception:
        return True
    return normalized in SENSITIVE_INTENTS or normalized == IntentLabel.TRANSFER_AGENT


def _fast_slow_disagreement(intent: IntentResult) -> bool:
    """快慢路径是否在低把握下分歧(仅慢路径覆盖快路径时有定义, 否则恒 False)."""
    if intent.fast_conf is None or intent.fast_intent is None:
        return False
    if intent.fast_intent == intent.primary_intent:
        return False
    return intent.fast_conf < _DISAGREEMENT_FAST_FLOOR or intent.primary_confidence < _DISAGREEMENT_SLOW_FLOOR


# 参数索取话术信号 (P0 会话 1fb54681): 工具编排/LLM 回复向客户索要办理参数时,
# 视为"本轮在等参数" -> 落等待快照, 下轮短回复(金额/期数数字)可凭快照豁免噪声门。
# 只匹配索取动词与参数名词的共现, 陈述式回答(如"费率为 0.75%")不会命中。
# 会话 48882b05: LLM 实际说的是"请告诉我更多细节", 不在动词表里 -> 等待快照没落上,
# 下轮裸数字会被噪声门误杀 —— 口语化索取措辞必须进表。
_ASK_VERBS = ("请提供", "请告知", "请输入", "请发送", "需要您提供", "请确认您", "请告诉我", "请问")
_ASK_PARAM_NOUNS = (
    "金额",
    "期数",
    "几期",
    "卡号",
    "后四位",
    "尾号",
    "账单周期",
    "哪个月",
    "时间段",
    "手机号",
    "验证码",
    "密码",
)


def _asks_for_parameters(content: str) -> bool:
    """回复话术是否在向客户索取办理参数(金额/期数/卡号等)."""
    if not content:
        return False
    has_verb = any(v in content for v in _ASK_VERBS)
    if not has_verb:
        return False
    return any(n in content for n in _ASK_PARAM_NOUNS)


def _is_replying_to_context(
    user_input: str, entities: list[Entity] | None, missing_slots: list[tuple[str, str]]
) -> bool:
    """本句是否在"回上文的话"(而非瞎按键盘)。

    当且仅当【上文在等一个必填槽】(missing_slots 非空)且本句把它回应上了才算回话:
    - 抽到实体命中缺的槽(_ENTITY_TO_SLOT), 或
    - 是纯数字短串(金额/卡号/卡尾/期数等短填充), 或
    - 是确认词(嗯对/是的/确认等)——bot 刚在等卡号/金额, 用户"嗯对/好的"即回话背书。

    判定刻意保守: 宁可放过一条[上文漏网的]真噪声(顶多多一句澄清), 也不误杀一句真实回话。
    非回话(含"先问候再乱打 hjfw"——不填任何槽、也不是确认词)仍由调用方按噪声处理。
    """
    if not missing_slots:
        return False
    missing_names = {name for name, _ in missing_slots}
    for e in entities or []:
        if _ENTITY_TO_SLOT.get(e.entity_type) in missing_names:
            return True
    if _is_confirmation(user_input):
        return True
    # 纯数字回复: 放宽到 1-19 位兜住金额/卡号/卡尾等真实回话。此分支仅在"上文在等
    # 必填槽"(missing_slots 非空)时才会触达, 故不必担心把独立乱数字误当回话; 卡号
    # 16 位/身份证 18 位都落在范围内, 避免真实回话被当噪声拦。
    # __param__ 通用槽(会话 1fb54681): 业务意图无槽位定义但话术在索参数时,
    # 短数字/短含数字串同样算回话("3"/"3000"/"分3期")。
    if "__param__" in missing_names:
        return bool(re.search(r"\d", _normalize_text(user_input))) and len(_normalize_text(user_input)) <= 16
    return bool(re.fullmatch(r"\d{1,19}", _normalize_text(user_input)))


# 确认词: 上一轮 bot 要求提供某必填信息(卡号/金额/期数)后, 用户用这些短词确认/背书
# 即视为回话放行, 避免"要卡号 → 用户回 嗯对"被误判成噪声澄清。整串归一化后命中才认,
# 避免把 "对, 那再帮我查个额度" 这种带继续提问的正常话误判成回话。
_CONFIRMATION_WORDS = {
    "嗯",
    "嗯嗯",
    "嗯对",
    "嗯好的",
    "嗯好",
    "好的",
    "好",
    "对",
    "对的",
    "是的",
    "是",
    "没错",
    "确认",
    "可以",
    "好的对",
    "就这个",
}


def _is_confirmation(text: str) -> bool:
    return _normalize_text(text) in _CONFIRMATION_WORDS


def _last_bot_turn_asked(history: list[dict[str, Any]] | None) -> bool:
    """上轮 bot 回复是否以"反问"收尾 (自由提问, 非槽位追问的陈述句)。

    判定: 最近一条 bot 消息以 "?"/"？" 结尾, 或结尾 8 字内含 "吗"。
    槽位追问(如"请提供您信用卡的后四位以便验证身份")不带问号, 不受影响;
    带问号的槽位追问走既有回话豁免(缺槽时确认词放行), 与此互补不冲突。
    兼容两种历史格式: 原始 "speaker"(customer/bot) 与 _load_history 归一化后的
    "role"(user/assistant)。
    """
    for turn in reversed(history or []):
        speaker = turn.get("speaker") or turn.get("role")
        if speaker in ("bot", "assistant"):
            content = str(turn.get("content") or "").strip()
            return bool(content) and (content.endswith("?") or content.endswith("？") or "吗" in content[-8:])
        if speaker in ("customer", "user"):
            return False
    return False


def _is_confirm_after_question(
    user_input: str, history: list[dict[str, Any]] | None, missing_slots: list[tuple[str, str]] | None
) -> bool:
    """bot 上轮自由反问后, 本句为确认词且无缺槽 → "回应反问", 不是噪声/新意图。

    会话 b561cd04 复盘: bot 问"需要帮助查询吗?" → 用户回"是的"被判 faq@0.25 低置信
    澄清, 对话死胡同。缺槽场景(槽位追问)不触发本分支, 沿用回话豁免原链路。
    """
    if missing_slots:
        return False
    return _is_confirmation(user_input) and _last_bot_turn_asked(history)


# 敏感索取标记词 (会话 956a5fd2 复盘: bot 要"卡号后四位"→ 用户回"8765"被形状门+低置信门双杀;
# E2E 联调再补: MCP 工具编排按 schema 索要**完整卡号**, 用户回 16 位数字同样被形状门杀)。
# 长短类区分: "卡号后四位"等短语 → 4-6 位数字; 裸"卡号" → 13-19 位完整卡号。匹配顺序先长后短,
# 否则"卡号后四位"会被裸"卡号"先命中而错分到全卡类。
_SHORT_SENSITIVE_MARKERS = ("卡号后四位", "卡号后4位", "卡尾号", "验证码", "短信验证码", "预留手机号", "身份证号")


def _last_bot_turn_asked_sensitive(history: list[dict[str, Any]] | None) -> str | None:
    """上轮 bot 消息索取的敏感凭证标记词 (兼容 speaker/role 两种历史格式); 未索取返回 None。"""
    for turn in reversed(history or []):
        speaker = turn.get("speaker") or turn.get("role")
        if speaker in ("bot", "assistant"):
            content = str(turn.get("content") or "")
            for marker in _SHORT_SENSITIVE_MARKERS:
                if marker in content:
                    return marker
            if "卡号" in content:
                return "卡号"
            return None
        if speaker in ("customer", "user"):
            return None
    return None


def _is_uncertain_intent(intent: Any) -> bool:
    """意图是否"没有明确可作答方向": 泛化意图(FAQ/闲聊)或置信度过低.

    用于无检索上下文时的澄清门控: 只有连意图都不确定(如 "adb"/"889" 被归为
    faq/空置信)才澄清; 已有明确具体意图(如账单/积分查询)即使检索为空也不该被
    当成乱码拒答, 而应继续走生成/追问流程。
    """
    primary = getattr(intent, "primary_intent", None)
    confidence = getattr(intent, "primary_confidence", 0.0) or 0.0
    if primary in (IntentLabel.FAQ, IntentLabel.CHITCHAT):
        return True
    return confidence < 0.5
