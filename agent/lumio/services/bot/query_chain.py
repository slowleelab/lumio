"""查询直达链路（目标架构 ⑤B）

非金融类只读查询（查余额/明细/进度）：参数抽取（槽位 + 绑定卡号自动注入）
→ 参数齐全直连 MCP 工具调用（绕过 LLM 工具循环）→ Redis 回复缓存
→ 单次 LLM 摘要生成。参数缺失 → 槽位反问（补槽回流会话层）。

与交易办理链路的本质区别：只读、可缓存、无确认状态机、单次 LLM 调用。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

from lumio.services.common.card_binding import resolve_card_no, schema_declares_card_no
from lumio.shared.logger import setup_logger

logger = setup_logger(__name__)

_CACHE_TTL_SECONDS = 300
_CACHE_PREFIX = "lumio:query-chain:result:"


@dataclass
class QueryChainResult:
    """查询直达链路产出

    - content 非空 → 已生成回复（LLM 摘要或缓存命中）
    - content 为空且 missing_params 非空 → 需要反问澄清
    - content 为空且 error 非空 → 链路失败，调用方回落知识路径
    - mcp_ms/summarize_ms/total_ms → 分段耗时（审计链归因用）
    """

    content: str = ""
    missing_params: list[str] = field(default_factory=list)
    tool_name: str | None = None
    tool_args: dict = field(default_factory=dict)
    raw_result: str = ""
    cache_hit: bool = False
    error: str = ""
    mcp_ms: float = 0.0
    summarize_ms: float = 0.0
    total_ms: float = 0.0


def _normalize_question(user_input: str) -> str:
    """问题归一化: 去全部空白 + 小写 — 同一问法的书写差异共用缓存"""
    return re.sub(r"\s+", "", (user_input or "").lower())


def _cache_key(tool_name: str, args: dict, user_input: str) -> str:
    # 缓存的是**最终回复** (由问题+工具结果共同决定), key 必须带上归一化问题:
    # 只按 工具+参数 会把"账单日是几号"的缓存答案错配给同卡另一个问题
    # "最低还款多少" (smoke-qa-1788567861 决策链复盘)。
    payload = json.dumps(
        {"tool": tool_name, "args": args, "q": _normalize_question(user_input)},
        ensure_ascii=False,
        sort_keys=True,
    )
    return _CACHE_PREFIX + hashlib.sha256(payload.encode()).hexdigest()


class QueryChain:
    """查询轻链路执行器

    依赖注入与 ToolCallingExecutor 同源（mcp_client / redis / degradation_mgr），
    由 bot_agent 持有并传入。
    """

    def __init__(self, *, mcp_client: Any, redis_client: Any = None, degradation_mgr: Any = None) -> None:
        self._mcp = mcp_client
        self._redis = redis_client
        self._degradation = degradation_mgr

    # ── 参数抽取 ──

    def _pick_tool(self, tool_names: list[str] | None) -> tuple[str | None, dict | None]:
        """按白名单顺序选第一个非敏感查询工具，返回 (name, input_schema)"""
        for name in tool_names or []:
            spec = self._mcp.get_tool(name)
            if spec is None or spec.sensitive:
                continue
            return spec.name, spec.input_schema
        return None, None

    def build_args(self, input_schema: dict, slot_values: dict, customer_id: str | None) -> tuple[dict, list[str]]:
        """schema 驱动的参数组装: 槽位值按参数名填充 + card_no 绑定卡号自动注入。

        Returns: (args, missing_required) — missing 为 schema required 中无法提供的参数名。
        """
        slots = slot_values or {}
        properties = (input_schema or {}).get("properties") or {}
        required = (input_schema or {}).get("required") or []
        args: dict = {}

        for name in properties:
            if name in slots and slots[name] not in (None, ""):
                args[name] = slots[name]
        # 智能默认: 账单类查询 "本期" 是自然语义 (会话 chainb 实测: 反问账期体验差),
        # 缺省填当前 YYYY-MM, 仅当 schema 枚举明确不含该格式时不填
        if "period" in properties and "period" not in args:
            from datetime import date as _date

            args["period"] = _date.today().strftime("%Y-%m")
        # card_no 由实名绑定关系注入, 客户只给过后四位也按绑定关系解析完整卡号
        # (注入键名取 schema 实际声明 — card_no/cardNo 两种形态, 免 camelCase 缺参)
        card_key = schema_declares_card_no(input_schema)
        if card_key and card_key not in args:
            args[card_key] = resolve_card_no(customer_id)

        missing = [r for r in required if r not in args]
        if missing:
            logger.warning("查询链路缺参: tool=%s missing=%s", "query", missing)
        return args, missing

    # ── 缓存 ──

    async def _cache_get(self, key: str) -> str | None:
        if not self._redis:
            return None
        try:
            value = await self._redis.get(key)
            return str(value) if value is not None else None
        except Exception:
            return None

    async def _cache_set(self, key: str, value: str) -> None:
        if not self._redis:
            return
        try:
            await self._redis.set(key, value, ex=_CACHE_TTL_SECONDS)
        except Exception:
            logger.debug("查询链路缓存写入失败(不阻断): %s", key)

    # ── 主入口 ──

    async def run(
        self,
        *,
        intent_label: str,
        user_input: str,
        tool_names: list[str] | None,
        slot_values: dict,
        customer_id: str | None,
        history: list[dict[str, str]] | None = None,
    ) -> QueryChainResult:
        """执行查询直达链路

        Returns: QueryChainResult — content 空 + missing 非空 → 反问; error 非空 → 回落。
        """
        _t_total = time.monotonic()
        tool_name, schema = self._pick_tool(tool_names)
        logger.info(
            "查询直达入口: tool=%s schema=%s tools候选=%s",
            tool_name,
            bool(schema),
            tool_names,
        )
        if tool_name is None:
            logger.warning("查询直达无可用查询工具: 候选=%s", tool_names)
            return QueryChainResult(error="no_query_tool")

        slots: dict = slot_values if slot_values is not None else {}
        args, missing = self.build_args(schema, slots, customer_id)
        logger.info("查询直达参数: args=%s missing=%s", args, missing)
        if missing:
            logger.info("查询直达缺参反问: missing=%s", missing)
            return QueryChainResult(missing_params=missing, tool_name=tool_name)

        key = _cache_key(tool_name, args, user_input)
        cached = await self._cache_get(key)
        if cached:
            logger.info("查询直达缓存命中: tool=%s", tool_name)
            return QueryChainResult(
                content=cached,
                tool_name=tool_name,
                tool_args=args,
                raw_result="(cache)",
                cache_hit=True,
                total_ms=(time.monotonic() - _t_total) * 1000,
            )

        t0 = time.monotonic()
        try:
            resp = await self._mcp.call_tool(tool_name, args)
        except Exception as exc:
            logger.warning("查询链路工具调用失败: tool=%s err=%s", tool_name, exc)
            return QueryChainResult(error=f"tool_error: {exc}", tool_name=tool_name, tool_args=args)
        mcp_ms = (time.monotonic() - t0) * 1000
        if resp.get("is_error"):
            return QueryChainResult(
                error=f"tool_is_error: {resp.get('content', '')[:120]}", tool_name=tool_name, tool_args=args
            )
        raw = resp.get("content", "")
        logger.info("查询链路直连调用: tool=%s 耗时=%.0fms 缓存=miss", tool_name, mcp_ms)

        t1 = time.monotonic()
        content = await self._summarize(user_input, tool_name, raw, history)
        summarize_ms = (time.monotonic() - t1) * 1000
        if content:
            await self._cache_set(key, content)
        return QueryChainResult(
            content=content,
            tool_name=tool_name,
            tool_args=args,
            raw_result=raw,
            mcp_ms=mcp_ms,
            summarize_ms=summarize_ms,
            total_ms=(time.monotonic() - _t_total) * 1000,
        )

    async def _summarize(
        self, user_input: str, tool_name: str, raw_result: str, history: list[dict[str, str]] | None
    ) -> str:
        """单次 LLM 摘要（非工具循环）— LLM 不可用回落工具原文"""
        if self._degradation is None:
            return raw_result
        context = f"[工具 {tool_name} 查询结果]\n{raw_result}"
        try:
            result = await self._degradation.generate_with_fallback(
                system_prompt=(
                    "你是银行信用卡客服。基于工具查询结果用中文简洁回答客户问题,"
                    "只陈述结果中存在的信息, 结果为空或无法回答时如实说明并建议稍后重试或转人工。"
                ),
                user_input=user_input,
                context=context,
                history=history,
            )
            return str(result.content)
        except Exception as exc:
            logger.warning("查询链路摘要生成失败, 回落工具原文: %s", exc)
            return raw_result
