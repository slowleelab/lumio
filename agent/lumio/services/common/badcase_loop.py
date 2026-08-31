"""事后优化闭环核心 (目标架构 ⑧, 方案 v2.0)

模块一/二: 五路信号采集 + 粗筛去重
模块A:   Badcase 根因自动归因 (LLM-as-Judge, n=3 自一致性 + 人工兜底闸门)
模块三:  根因 → 四张分流表路由 (A 知识库 / B 意图库 / C 规则 / D 模型)
模块B:   金标评测集自动扩充 (规则模板 + LLM 改写 + 过滤)

设计纪律 (方案 §2.2):
- 归因只归到"第一处输出偏离"的层
- LLM 归因只做首轮, confidence<0.7 / uncertain / 多数票不齐 → 人工队列
- 金标集只增不减; 噪声样本进金标集比缺样本更糟
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ── 信号源枚举 (方案 §3.1 五路) ──
SIGNAL_SOURCES = (
    "negative_feedback",  # 用户负面反馈
    "transfer",  # 转人工事件
    "agent_revoke",  # 人工撤回/修正
    "behavior_anomaly",  # 行为异常 (重复提问/长停顿退出)
    "compliance_alert",  # 合规/幻觉拦截告警
)

# ── 根因四维 (方案 §4.3) ──
ROOT_CAUSE_CATEGORIES = ("semantic", "knowledge", "process", "coverage", "uncertain")

# ── 归因层枚举 (八层) ──
ROOT_CAUSE_LAYERS = (*tuple(f"layer_{i}" for i in range(1, 8)), "uncertain")

# ── 修复分流表 (方案 §5.1 四张) ──
FIX_TABLES = ("A_knowledge", "B_intent", "C_rule", "D_model", "none")

# 根因层 → 默认分流表 (方案 §5.1; 人工可覆盖)
_LAYER_TO_FIX_TABLE = {
    "layer_3": "B_intent",  # 语义/意图误判
    "layer_5": "A_knowledge",  # 检索不命中/知识缺口
    "layer_6": "D_model",  # 生成幻觉/Prompt 失效
    "layer_7": "C_rule",  # 合规拦截漏配
    "layer_4": "C_rule",  # 路由属性
    "layer_2": "C_rule",  # 会话/槽位
    "layer_1": "C_rule",  # 预处理
}


def fix_table_for_layer(layer: str | None) -> str:
    """根因层 → 默认分流表"""
    return _LAYER_TO_FIX_TABLE.get(layer or "", "none")


# ── 粗筛去重 key (方案 §4.1: 向量相似 > 0.95 合并; v1 用文本哈希前缀粗分组) ──


def dedup_key(user_input: str) -> str:
    """粗去重分组键: 去标点小写前 32 字符哈希 (细粒度向量去重由归因阶段做)"""
    normalized = "".join(ch for ch in user_input.lower().strip() if ch.isalnum())
    return hashlib.sha256(normalized[:32].encode()).hexdigest()[:16]


# ── 模块 A: LLM-as-Judge 自动归因 ──

_JUDGE_SYSTEM_PROMPT = """你是银行智能客服系统的故障根因分析专家。系统采用八层架构：
①预处理 → ②会话管理 → ③意图识别(三级漏斗) → ④路由决策
→ ⑤RAG检索 → ⑥回复生成 → ⑦风控合规 → ⑧监控闭环
你的任务：根据给定的 Badcase 上下文（用户输入 + 各层中间产物），
判断这个 Badcase 的根因出在哪一层，并给出依据。
判定纪律：
1. 找"第一处输出偏离"的层——后续层的错误往往是上游错误的传导，归因只归到源头
2. 只依据给定的中间产物判断，不得推测未给出的信息
3. 如果证据不足以确定根因，root_cause_layer 填 "uncertain"，不要强行归因
4. 输出严格的 JSON，不要输出任何 JSON 之外的文字
"""

_JUDGE_USER_TEMPLATE = """<badcase_context>
用户输入: {user_input}
用户反馈: {user_feedback}
<layer_outputs>
layer_3_intent:
  predicted_intent: {intent}
  confidence: {confidence}
layer_4_route:
  traffic_class: {traffic_class}
layer_5_rag:
  retrieval_hit: {rag_hit}
  context_len: {context_len}
layer_6_generate:
  output: {bot_output}
layer_7_compliance:
  response_source: {response_source}
</layer_outputs>
</badcase_context>
请分析这个 Badcase 的根因，输出 JSON。
"""

_JUDGE_OUTPUT_SCHEMA = (
    '{"trace_id": "...", "root_cause_layer": "layer_1..layer_7|uncertain", '
    '"root_cause_category": "semantic|knowledge|process|coverage|uncertain", '
    '"evidence": "≤100字", "confidence": 0.0~1.0, '
    '"suggested_fix_table": "A_knowledge|B_intent|C_rule|D_model|none", '
    '"needs_human_review": true|false}'
)


@dataclass
class AttributionResult:
    """单条归因结果 (3 次自一致性投票后)"""

    trace_id: str
    root_cause_layer: str
    root_cause_category: str
    evidence: str
    confidence: float
    fix_table: str
    needs_human_review: bool
    majority_ratio: float


def _parse_judge_json(raw: str) -> dict[str, Any] | None:
    """解析 judge 输出 JSON (容忍 ```json 包裹)"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        return json.loads(text[start:end])
    except (ValueError, json.JSONDecodeError):
        return None


class BadcaseJudge:
    """模块 A · Badcase 根因自动归因 (LLM-as-Judge)

    - 自一致性采样 n=3 多数票 (方案 §9.3)
    - 三道人工兜底闸门: 多数票不齐 / confidence<0.7 / uncertain (方案 §9.5)
    - 分流表建议: 归因层 → 默认表映射 (可人工覆盖)
    """

    def __init__(self, llm: Any, model: str = "", *, min_confidence: float = 0.7, samples: int = 3) -> None:
        self._llm = llm
        self._model = model or "unknown"
        self._min_conf = min_confidence
        self._samples = max(samples, 1)

    async def _one_sample(self, user_prompt: str) -> dict[str, Any] | None:
        # 优先 chat_json (结构化输出, Ollama format=json), 不可用时回落 chat+解析
        try:
            raw = await self._llm.chat_json(
                [
                    {"role": "system", "content": _JUDGE_SYSTEM_PROMPT + "\n输出 JSON schema: " + _JUDGE_OUTPUT_SCHEMA},
                    {"role": "user", "content": user_prompt},
                ],
                timeout=60,
            )
            return dict(raw) if isinstance(raw, dict) else None
        except AttributeError:
            resp = await self._llm.chat(
                [
                    {"role": "system", "content": _JUDGE_SYSTEM_PROMPT + "\n输出 JSON schema: " + _JUDGE_OUTPUT_SCHEMA},
                    {"role": "user", "content": user_prompt},
                ],
                timeout=60,
            )
            return _parse_judge_json(resp)

    async def attribute(self, context: dict[str, Any]) -> AttributionResult | None:
        """对单条 Badcase 归因

        context: {trace_id, user_input, user_feedback, intent, confidence,
                  traffic_class, rag_hit, context_len, bot_output, response_source}
        """
        user_prompt = _JUDGE_USER_TEMPLATE.format(
            user_input=context.get("user_input", ""),
            user_feedback=context.get("user_feedback", "无"),
            intent=context.get("intent", ""),
            confidence=context.get("confidence", ""),
            traffic_class=context.get("traffic_class", ""),
            rag_hit=context.get("rag_hit", ""),
            context_len=context.get("context_len", 0),
            bot_output=(context.get("bot_output") or "")[:400],
            response_source=context.get("response_source", ""),
        )

        votes: list[dict[str, Any]] = []
        for attempt in range(self._samples):
            try:
                parsed = await self._one_sample(user_prompt)
            except Exception as exc:
                logger.warning("归因采样 %s 失败: %s", attempt + 1, exc)
                continue
            if parsed:
                votes.append(parsed)
        if not votes:
            return None

        # 多数票归因 (按 root_cause_layer 聚合)
        from collections import Counter

        layer_counts = Counter(v.get("root_cause_layer", "uncertain") for v in votes)
        majority_layer, majority_n = layer_counts.most_common(1)[0]
        majority_ratio = majority_n / len(votes)
        same_vote = next(v for v in votes if v.get("root_cause_layer") == majority_layer)

        layer = majority_layer if majority_layer in ROOT_CAUSE_LAYERS else "uncertain"
        category = same_vote.get("root_cause_category", "uncertain")
        if category not in ROOT_CAUSE_CATEGORIES:
            category = "uncertain"
        try:
            conf = float(same_vote.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0

        needs_review = majority_ratio < 1.0 or conf < self._min_conf or layer == "uncertain"
        fix_table = same_vote.get("suggested_fix_table", "")
        if fix_table not in FIX_TABLES:
            fix_table = fix_table_for_layer(layer)

        return AttributionResult(
            trace_id=context.get("trace_id", ""),
            root_cause_layer=layer,
            root_cause_category=category,
            evidence=str(same_vote.get("evidence", ""))[:300],
            confidence=conf,
            fix_table=fix_table,
            needs_human_review=needs_review,
            majority_ratio=majority_ratio,
        )


# ── 模块 B: 金标评测集扩充 (引擎二 规则模板 + 四层过滤 v1) ──

_SYNONYM_MAP: dict[str, list[str]] = {
    "查询": ["查", "看看", "查一下", "帮我查"],
    "多少": ["几何", "数额"],
    "怎么": ["如何", "怎样"],
    "办理": ["开通", "申请"],
    "修改": ["更改", "变更"],
}
_PREFIX_SUFFIX = ("那个，", "请问", "我想问下，", "，谢谢", "？", "。")


def rule_augment(seed: str) -> list[str]:
    """引擎二·规则模板层: 同义替换 + 口语化前后缀 (零成本, 产出约 40% 变体)"""
    variants: set[str] = set()
    variants.add(seed + "，谢谢")
    variants.add("请问，" + seed)
    variants.add("那个，" + seed)
    for word, syns in _SYNONYM_MAP.items():
        if word in seed:
            for syn in syns:
                variants.add(seed.replace(word, syn, 1))
    # 口语化: 去句尾加疑问
    if not seed.endswith("？") and not seed.endswith("?"):
        variants.add(seed + "？")
    variants.discard(seed)
    return [v.strip() for v in variants if 4 <= len(v.strip()) <= 40]


def filter_variants(
    variants: list[str],
    *,
    min_len: int = 4,
    max_len: int = 40,
    existing: set[str] | None = None,
    compliance_words: set[str] | None = None,
) -> tuple[list[str], list[str]]:
    """四层过滤 v1 (方案 §10.4): 长度/合规/去重; PPL 与向量锚定留待嵌入服务可用时启用

    Returns: (passed, rejected)
    """
    existing = existing or set()
    passed, rejected = [], []
    for v in variants:
        v = v.strip()
        if not (min_len <= len(v) <= max_len):
            rejected.append(v)
            continue
        if compliance_words and any(w in v for w in compliance_words):
            rejected.append(v)
            continue
        if v in existing:
            rejected.append(v)
            continue
        passed.append(v)
        existing.add(v)
    return passed, rejected
