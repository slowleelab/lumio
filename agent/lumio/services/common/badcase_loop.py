"""事后优化闭环核心 (目标架构 ⑧)

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
from dataclasses import dataclass, field
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

# ── 缺陷类型 (归因主维度; 领域专家视角: 质检运营面对"业务缺陷"而非系统分层) ──
# 层/性质/修复表全部由缺陷派生 (单一事实源): 归因器与人工只选缺陷,
# 技术投影 (layer/category/fix_table) 不再是并列的用户维度。
DEFECT_TYPES: dict[str, dict[str, str]] = {
    "knowledge_missing": {"label": "知识缺失", "layer": "layer_5", "category": "knowledge", "fix_table": "A_knowledge"},
    "knowledge_outdated": {
        "label": "知识过时",
        "layer": "layer_5",
        "category": "knowledge",
        "fix_table": "A_knowledge",
    },
    "intent_misread": {"label": "意图理解错", "layer": "layer_3", "category": "semantic", "fix_table": "B_intent"},
    "intent_uncovered": {"label": "说法未覆盖", "layer": "layer_3", "category": "coverage", "fix_table": "B_intent"},
    "rule_flaw": {"label": "流程/规则缺陷", "layer": "layer_4", "category": "process", "fix_table": "C_rule"},
    "reply_quality": {"label": "回复质量差", "layer": "layer_6", "category": "process", "fix_table": "D_model"},
    "fallback_poor": {"label": "兜底不当", "layer": "layer_4", "category": "process", "fix_table": "C_rule"},
    "compliance_risk": {"label": "合规风险", "layer": "layer_7", "category": "process", "fix_table": "C_rule"},
}


def defect_to_layer(defect: str | None) -> str:
    """缺陷 → 责任层 (派生; 未知名/uncertain 归 uncertain)"""
    d = DEFECT_TYPES.get(defect or "")
    return d["layer"] if d else "uncertain"


def defect_to_category(defect: str | None) -> str:
    d = DEFECT_TYPES.get(defect or "")
    return d["category"] if d else "uncertain"


def defect_to_fix_table(defect: str | None) -> str:
    d = DEFECT_TYPES.get(defect or "")
    return d["fix_table"] if d else "none"


# 旧粗分类 (semantic/knowledge/process/coverage) → 最近缺陷 (展示兼容:
# 存量案例归因升级前的 category 值, 人工确认时作为主缺陷默认值带出)
_LEGACY_CATEGORY_TO_DEFECT: dict[str, str] = {
    "semantic": "intent_misread",
    "knowledge": "knowledge_missing",
    "coverage": "intent_uncovered",
    "process": "rule_flaw",
}


def normalize_defect(value: str | None) -> str:
    """任意存量值 → 缺陷枚举 (旧粗分类取最近缺陷, 未知名归 uncertain)"""
    if value in DEFECT_TYPES:
        return value  # type: ignore[return-value]
    return _LEGACY_CATEGORY_TO_DEFECT.get(value or "", "uncertain")


# 根因层 → 允许的修复分流表 (首位 = 推荐默认; 方案 §5.1)
# 设计: A/B/D 各承接一个专属层 (知识/意图/生成), C·规则是工程配置族
# (预处理/会话/路由/合规) 的公共收容桶; 交叉次选项是现实中的替代修复路径 —
#   layer_3 意图误判 → 高频明确表述可加 L1 规则词 (C)
#   layer_5 检索不命中 → query 归一/同义词缺口改 lexicon 词表 (C)
#   layer_6 生成失效 → 上下文缺正确知识时补文档内容 (A)
# 允许集外组合无业务含义 (如 合规漏配 × 补意图语料), 归因器输出与人工
# 改判均受此约束 (update_fix_status 守门); uncertain 未定层不带修复路由
_LAYER_ALLOWED_FIX_TABLES: dict[str, tuple[str, ...]] = {
    "layer_1": ("C_rule",),
    "layer_2": ("C_rule",),
    "layer_3": ("B_intent", "C_rule"),
    "layer_4": ("C_rule",),
    "layer_5": ("A_knowledge", "C_rule"),
    "layer_6": ("D_model", "A_knowledge"),
    "layer_7": ("C_rule",),
}

# 根因层 → 默认分流表 (= 允许集首位; 人工可在允许集内覆盖)
_LAYER_TO_FIX_TABLE = {layer: tables[0] for layer, tables in _LAYER_ALLOWED_FIX_TABLES.items()}


def fix_table_for_layer(layer: str | None) -> str:
    """根因层 → 默认分流表"""
    return _LAYER_TO_FIX_TABLE.get(layer or "", "none")


def allowed_fix_tables(layer: str | None) -> tuple[str, ...]:
    """根因层 → 允许的修复分流表 (uncertain/未知层为空元组 = 无修复路由)"""
    return _LAYER_ALLOWED_FIX_TABLES.get(layer or "", ())


def is_valid_layer_table(layer: str | None, fix_table: str | None) -> bool:
    """层×表组合是否合法: 'none' (无需修复) 任意层可持有; 未定层不约束表"""
    if not fix_table or fix_table == "none":
        return True
    allowed = allowed_fix_tables(layer)
    return not allowed or fix_table in allowed


# ── 粗筛去重 key (方案 §4.1: 向量相似 > 0.95 合并, 文本哈希前缀粗分组) ──


def dedup_key(user_input: str) -> str:
    """粗去重分组键: 去标点小写前 32 字符哈希 (细粒度向量去重由归因阶段做)"""
    normalized = "".join(ch for ch in user_input.lower().strip() if ch.isalnum())
    return hashlib.sha256(normalized[:32].encode()).hexdigest()[:16]


# ── 模块 A: LLM-as-Judge 自动归因 ──

_JUDGE_SYSTEM_PROMPT = """你是银行信用卡智能客服的质量分析专家。给你一个服务坏例
（客户输入 + 机器人回复 + 各环节中间产物），判断属于哪类业务缺陷。
缺陷类型（只能从中选择一个作为主缺陷）：
- knowledge_missing 知识缺失: 该问题需要知识作答, 但知识库没有相关内容
- knowledge_outdated 知识过时: 检索到了内容但已过时/不准确, 导致答错
- intent_misread 意图理解错: 客户意思被理解错（问账单判成闲聊/faq 等）
- intent_uncovered 说法未覆盖: 意思理解了或置信很低, 但这类说法/场景没被意图库收录
- rule_flaw 流程/规则缺陷: 意图对了, 但编排/槽位/路由规则设计不当导致没办成事
- reply_quality 回复质量差: 检索内容是对的, 但生成的话术跑题/生硬/编造数字
- fallback_poor 兜底不当: 该查的诉求被"无法查询/请去官方渠道"打发、该转人工没转
- compliance_risk 合规风险: 回复含不合规承诺、索要敏感信息或编造办理结果
判定纪律：
1. 只依据给定的中间产物判断，不得推测未给出的信息
2. 证据不足以确定时 root_cause_defect 填 "uncertain"，不要强行归因
3. 输出严格的 JSON，不要输出任何 JSON 之外的文字
4. 判定锚点（中间产物满足以下模式时直接定缺陷，证据字段优先于直觉）：
   - intent 与用户输入语义明显不符 → intent_misread
   - intent 基本对但置信很低 / 被当闲聊处理, 客户其实在问业务 → intent_uncovered
   - rag_hit=false 且该问题需要知识作答（非纯操作/转人工诉求）→ knowledge_missing
   - rag_hit=true 但引用内容旧/答非所问的依据 → knowledge_outdated
   - rag_hit=true、意图也对, 但回复与输入主题无关或含编造数字 → reply_quality
   - 意图识别正确, 但回复以"无法查询/请去官方渠道"拒绝（该类诉求本有工具链可查）→ fallback_poor
   - 回复含不合规承诺、敏感信息泄露或编造办理话术 → compliance_risk
5. 伴随缺陷：主缺陷之外，若中间产物显示还存在其他独立缺陷（非同一问题的传导），
   填入 contributing_defects（至多 2 个，不与主缺陷相同；无则为空数组）。
   示例：主缺陷是意图理解错，同时 rag_hit=false 表明知识库也无兜底内容
   → root_cause_defect="intent_misread", contributing_defects=["knowledge_missing"]
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
请分析这个 Badcase 的缺陷，输出 JSON。
"""

_JUDGE_OUTPUT_SCHEMA = (
    '{"trace_id": "...", "root_cause_defect": "knowledge_missing|knowledge_outdated|'
    'intent_misread|intent_uncovered|rule_flaw|reply_quality|fallback_poor|compliance_risk|uncertain", '
    '"contributing_defects": ["defect_x", "..."], '
    '"evidence": "≤100字", "confidence": 0.0~1.0, '
    '"needs_human_review": true|false}'
)


@dataclass
class AttributionResult:
    """单条归因结果 (3 次自一致性投票后)

    主维度是缺陷 (defect); 责任层/性质/修复表全部由缺陷派生 (单一事实源)。
    root_cause_category 落库存缺陷枚举 (旧粗分类由 normalize_defect 兼容)。
    """

    trace_id: str
    root_cause_layer: str
    root_cause_category: str  # 缺陷枚举 (派生自主缺陷)
    evidence: str
    confidence: float
    fix_table: str
    needs_human_review: bool
    majority_ratio: float
    primary_defect: str = "uncertain"
    secondary_layers: list[str] = field(default_factory=list)  # 伴随缺陷枚举列表


def _parse_judge_json(raw: str) -> dict[str, Any] | None:
    """解析 judge 输出 JSON (容忍 ```json 包裹)"""
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    try:
        start, end = text.index("{"), text.rindex("}") + 1
        parsed: dict[str, Any] = json.loads(text[start:end])
        return parsed
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

        # n=3 自一致性采样并发执行 (原串行为本地 qwen 单卡时代的实现; 远程裁判
        # 3 路并发单条耗时降至 ~1/3, 采样独立性不受影响 — 每次请求独立采样)
        import asyncio

        results = await asyncio.gather(
            *[self._one_sample(user_prompt) for _ in range(self._samples)],
            return_exceptions=True,
        )
        votes: list[dict[str, Any]] = []
        for attempt, parsed in enumerate(results):
            if isinstance(parsed, BaseException):
                logger.warning("归因采样 %s 失败: %s", attempt + 1, parsed)
                continue
            if parsed:
                votes.append(parsed)
        if not votes:
            return None

        # 多数票归因 (按主缺陷聚合; 兼容升级前按层输出的旧裁判 — 层归一化为缺陷)
        from collections import Counter

        def vote_defect(v: dict[str, Any]) -> str:
            d = v.get("root_cause_defect")
            if isinstance(d, str) and d in DEFECT_TYPES:
                return d
            # 旧 schema (root_cause_layer): 层 → 主导缺陷归一 (保底兼容)
            layer_to_defect = {"layer_3": "intent_misread", "layer_5": "knowledge_missing", "layer_6": "reply_quality"}
            return layer_to_defect.get(str(v.get("root_cause_layer") or ""), "uncertain")

        defect_counts = Counter(vote_defect(v) for v in votes)
        primary_defect, majority_n = defect_counts.most_common(1)[0]
        majority_ratio = majority_n / len(votes)
        same_vote = next(v for v in votes if vote_defect(v) == primary_defect)

        # 技术投影全部由缺陷派生 (单一事实源, 不再取 LLM 输出 — 消除跨维度不一致)
        layer = defect_to_layer(primary_defect)
        category = primary_defect
        try:
            conf = float(same_vote.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0

        # 伴随缺陷聚合: ≥2 票独立提及的 (排除主缺陷, 防传导误计)
        contrib_counts: Counter[str] = Counter()
        for v in votes:
            for x in v.get("contributing_defects") or []:
                if isinstance(x, str) and x in DEFECT_TYPES and x != primary_defect:
                    contrib_counts[x] += 1
        secondary_layers = [x for x, n in contrib_counts.most_common(2) if n >= 2]

        needs_review = majority_ratio < 1.0 or conf < self._min_conf or primary_defect == "uncertain"
        fix_table = defect_to_fix_table(primary_defect)
        # 允许集兜底: 派生表必须落在层允许集 (映射表与允许集不一致时的保险)
        if fix_table not in allowed_fix_tables(layer):
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
            primary_defect=primary_defect,
            secondary_layers=secondary_layers,
        )


# ── 模块 B: 金标评测集扩充 (引擎二 规则模板 + 四层过滤) ──

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
    """四层过滤 (方案 §10.4): 长度/合规/去重; PPL 与向量锚定留待嵌入服务可用时启用

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
