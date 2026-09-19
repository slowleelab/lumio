"""查询工程 (检索前处理, 三层 — 全部规则层零 LLM 成本)

对话理解升级的补全面: 上下文改写 (classifier 慢路径) 只做"接话翻译"
(指代消解+省略恢复), 对检索的表达适配没做 — 口语错序 ("就是费年？"),
黑话与知识库书面语的词汇鸿沟 ("提临时额度" vs 文档的"临时额度调整"),
单一查询表达的召回局限, 全部原样进 BM25/向量。

三层分工 (成本精准分配):
1. normalize_query  口语转书面: 错序修正 + 语气清理 + 黑话归一 (map 词表) —
   主查询, BM25 与向量共用; 实体保全 (数字原样保留)
2. synonym_terms    同义扩展: 命中同义组的词以 OR should 注入 BM25 —
   只作用词法路 (词法对表达最敏感), 向量天生容忍变体不扩展
3. alt_queries      多查询展开: 原句/归一句两路词法并查 —
   RRF 融合天然支持多路; 向量只走归一主句 (embed 成本考虑)

词表纪律: colloquial_map / synonym_groups 收口 data/policy/lexicon.json,
改词表必须过金标回归集 (tests/test_golden_regression.py)。
"""

from __future__ import annotations

import re

from lumio.shared.lexicon import lexicon_map, lexicon_values

# 句尾语气词 (单字, 剥离后不影响语义; 与 FAQ exact 归一不同 — 这里面向检索)
_TAIL_PARTICLES = "呢啊呀哈呗嘛啦哦噢咯"
# 口语填充前缀 (剥离; 出现在句首才剥)
_FILLER_PREFIXES = ("那个", "麻烦", "我想问下", "请问一下", "问一下", "帮忙", "帮", "给我")

_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _strip_fillers(text: str) -> str:
    t = text.strip()
    changed = True
    while changed:
        changed = False
        for p in _FILLER_PREFIXES:
            if t.startswith(p) and len(t) > len(p):
                t = t[len(p) :].lstrip()
                changed = True
    return t.strip()


def _strip_tail_particle(text: str) -> str:
    t = text.strip()
    while t and t[-1] in _TAIL_PARTICLES and len(t) > 2:
        t = t[:-1].rstrip()
    return t


def normalize_query(text: str) -> str:
    """口语转书面: 填充词剥离 + 语气词清理 + 黑话/错序归一 (词表驱动)。

    实体保全: 原句数字必须原样保留 (归一 map 不允许碰数字 — 防卡号/金额被改)。
    归一是保序替换, 不改句式; 无法归一的口语原样返回 (宁可不改不可改错)。
    """
    if not text or not text.strip():
        return text or ""
    t = _strip_tail_particle(_strip_fillers(text))
    if not t:
        return text.strip()
    colloquial = lexicon_map("colloquial_map")
    if colloquial:
        for slang, formal in colloquial.items():
            if slang in t:
                t = t.replace(slang, formal)
    # 实体保全兜底: 归一过程意外吞数字则回退原句 (理论不可达, 防词表误配)
    if set(_NUM_RE.findall(text)) - set(_NUM_RE.findall(t)):
        return _strip_tail_particle(_strip_fillers(text))
    return t


def synonym_terms(text: str) -> list[str]:
    """同义扩展词: 命中同义组时返回该组其余词 (OR 注入 BM25, 不改主查询)。"""
    hits: list[str] = []
    for group in lexicon_values("synonym_groups"):
        members = [m for m in (group if isinstance(group, list) else []) if m]
        if not members:
            continue
        if any(m in text for m in members):
            hits.extend(m for m in members if m not in text)
    # 去重保序, 上限防词表膨胀打爆 ES 查询
    seen: set[str] = set()
    out: list[str] = []
    for w in hits:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out[:8]


def build_retrieval_queries(original: str, rewritten: str | None = None) -> dict:
    """检索查询组装。

    - main: 改写句(如有, 已自包含) > 归一句 — BM25 与向量共用主查询
    - synonym_terms: main 命中同义组的 OR 扩展词 (仅 BM25)
    - alt_queries: 词法多路 (main + 归一原句), 不同才多路; 向量只走 main
    """
    rw = (rewritten or "").strip()
    normalized = normalize_query(original)
    main = rw or normalized or original.strip()
    alts: list[str] = []
    if normalized and normalized != main and len(normalized) >= 4:
        alts.append(normalized)
    return {"main": main, "synonym_terms": synonym_terms(main), "alt_queries": alts}
