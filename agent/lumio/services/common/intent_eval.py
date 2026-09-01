"""意图发布评测闸门 (流派二 · 变更前/发布时门禁)

新意图从 pending_review 通过评审后, 必须先过两道门才能进影子观察:

1. overlap  重叠检测 —— 候选种子与存量语料 (出厂 seed_dataset.json + 其他已生效
           注册表意图) 逐一余弦比对; 近重复 (≥0.92) 硬失败, 高相似 (≥0.85) 告警。
           防止"新意图其实是旧意图的换皮"造成边界混淆。
2. golden  金标域回归 —— 用"出厂种子 + 已生效注册表种子 [+ 候选种子]"构建虚拟
           最近邻域分类器, 在金标集上跑域判定; 加入候选种子后准确率不得比基线
           (不含候选) 下跌超过容差。防止新种子把既有样本的域判定带偏。

全流程只用本地嵌入 (CPU), 无 LLM 调用 —— 评测门必须在无外部依赖时可重复执行。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lumio.shared.intent_registry import RegistryEntry
from lumio.shared.logger import get_logger

logger = get_logger(__name__)

SEED_PATH = Path(__file__).resolve().parents[3] / "data" / "intent_classification" / "seed_dataset.json"

# 闸门阈值 (治理策略常量)
# 实测校准 (mxbai-embed-large, 中文短句): 语义无关句对余弦底噪普遍 0.85~0.92
# (如"英镑汇率"vs"我要投诉"实测 0.9178), 同文本=1.0、同义改写≈0.95+。
# 故 warn=0.94 / hard=0.97; 若换嵌入模型需重新标定这两档。
OVERLAP_HARD_FAIL = 0.97  # 近重复: 疑似同一意图换皮 → 硬失败
OVERLAP_WARN = 0.94  # 高相似: 建议人工复核边界
GOLDEN_DROP_TOLERANCE = 0.02  # 域判定准确率最大可容忍跌幅
MAX_CORPUS_ROWS = 600  # 重叠检测的存量语料上限 (够判且省嵌入开销)


@dataclass
class GateReport:
    """评测闸门报告 (持久化进注册表条目 eval_report)"""

    passed: bool = False
    overlap: dict[str, Any] = field(default_factory=dict)
    golden: dict[str, Any] = field(default_factory=dict)
    checked_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"passed": self.passed, "overlap": self.overlap, "golden": self.golden, "checked_at": self.checked_at}


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _load_factory_seeds() -> list[dict[str, str]]:
    """出厂种子语料 [{text, intent(叶子 slug)}]"""
    try:
        data = json.loads(SEED_PATH.read_text(encoding="utf-8"))
        return [
            {"text": e["text"], "intent": e["intent"]}
            for e in data.get("examples", [])
            if e.get("text") and e.get("intent")
        ]
    except Exception as exc:
        logger.warning("出厂种子语料加载失败(重叠检测仅用注册表语料): %s", exc)
        return []


def _domain_of_any(intent_slug: str) -> str:
    from lumio.shared.intent_taxonomy import domain_of

    return domain_of(intent_slug).value


async def run_publish_gates(entry: RegistryEntry, embed: Any) -> GateReport:
    """执行两道发布闸门。

    Args:
        entry: 待评测的注册表条目 (评审已通过)
        embed: 嵌入服务 (provider 对象或 provider.embed 可调用)
    """
    import time

    from lumio.shared.intent_registry import RegistryState, get_registry

    embed_fn = embed.embed if hasattr(embed, "embed") else embed
    report = GateReport(checked_at=time.time())

    # ── 语料装配 ──
    factory = _load_factory_seeds()
    registry = get_registry()
    other_active: list[dict[str, str]] = []
    for e in registry.list_entries():
        if e.slug != entry.slug and e.state == RegistryState.ACTIVE:
            other_active.extend({"text": s, "intent": e.slug} for s in e.seeds)

    existing = factory + other_active
    if len(existing) > MAX_CORPUS_ROWS:
        existing = existing[:MAX_CORPUS_ROWS]

    # ── 一次性嵌入 (重叠检测与金标回归共用, 本地 CPU 下嵌入是主要开销) ──
    cand_vecs = await embed_fn(entry.seeds) if entry.seeds else []
    exist_vecs = await embed_fn([r["text"] for r in existing]) if existing else []

    # ── 门 1: 重叠检测 ──
    overlaps: list[dict[str, Any]] = []
    max_sim = 0.0
    if existing and entry.seeds:
        for i, cvec in enumerate(cand_vecs):
            best_sim, best_j = 0.0, -1
            for j, evec in enumerate(exist_vecs):
                sim = _cosine(cvec, evec)
                if sim > best_sim:
                    best_sim, best_j = sim, j
            if best_sim >= OVERLAP_WARN:
                overlaps.append(
                    {
                        "seed": entry.seeds[i],
                        "similar_to": existing[best_j]["text"],
                        "existing_intent": existing[best_j]["intent"],
                        "similarity": round(best_sim, 4),
                    }
                )
            max_sim = max(max_sim, best_sim)
    hard_conflicts = [o for o in overlaps if o["similarity"] >= OVERLAP_HARD_FAIL]
    report.overlap = {
        "max_similarity": round(max_sim, 4),
        "warn_count": len(overlaps),
        "hard_conflicts": hard_conflicts,
        "details": overlaps[:20],
        "passed": not hard_conflicts,
    }

    # ── 门 2: 金标域回归 ──
    from lumio.services.common.eval_gates import GOLDEN_CASES

    golden_rows: list[tuple[str, str]] = []
    for text, expected in GOLDEN_CASES:
        try:
            golden_rows.append((text, _domain_of_any(expected)))
        except Exception:
            continue  # 期望意图无法归域的用例跳过

    baseline_rows = [{"text": r["text"], "domain": _domain_of_any(r["intent"])} for r in existing]
    if golden_rows and baseline_rows and exist_vecs:
        cand_domain = entry.domain
        golden_texts = [t for t, _ in golden_rows]
        golden_vecs = await embed_fn(golden_texts)
        base_vecs = exist_vecs  # 复用重叠检测已嵌入的存量语料向量

        def _accuracy(with_candidate: bool) -> float:
            ok = 0
            for gvec, (_, expected_domain) in zip(golden_vecs, golden_rows, strict=True):
                best_sim, best_domain = 0.0, ""
                for bvec, row in zip(base_vecs, baseline_rows, strict=True):
                    sim = _cosine(gvec, bvec)
                    if sim > best_sim:
                        best_sim, best_domain = sim, row["domain"]
                if with_candidate:
                    for cvec in cand_vecs:
                        sim = _cosine(gvec, cvec)
                        if sim > best_sim:
                            best_sim, best_domain = sim, cand_domain
                ok += best_domain == expected_domain
            return ok / len(golden_rows)

        baseline_acc = _accuracy(False)
        with_cand_acc = _accuracy(True)
        drop = round(baseline_acc - with_cand_acc, 4)
        report.golden = {
            "cases": len(golden_rows),
            "baseline_accuracy": round(baseline_acc, 4),
            "with_candidate_accuracy": round(with_cand_acc, 4),
            "drop": drop,
            "tolerance": GOLDEN_DROP_TOLERANCE,
            "passed": drop <= GOLDEN_DROP_TOLERANCE,
        }
    else:
        report.golden = {"cases": len(golden_rows), "skipped": True, "passed": True}

    report.passed = report.overlap.get("passed", False) and report.golden.get("passed", False)
    logger.info(
        "意图发布闸门: %s passed=%s overlap_max=%.3f golden_drop=%s",
        entry.slug,
        report.passed,
        report.overlap.get("max_similarity", 0.0),
        report.golden.get("drop"),
    )
    return report
