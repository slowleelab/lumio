"""闭环 P3 样本精选回流 (SampleBackflow, human-in-loop)

把 P2 归因出的"可操作失败样本"精选为再训练候选, 但**不自动重训/不自动写训练集**
(受监管银行场景, 训练数据变更必须经人审).

流程:
1. select_candidates: 从 (AttribSample, AttributionVerdict) 对里筛出分类层失败样本,
   按 文本+意图 去重 (同一错误问法只回流一次), 限制上限.
2. write_staging: 写 review JSONL (human 逐条批: approved = true/false).
3. finalize_confirmed: 读回人工批准的条目 → 去重后并入 seed_dataset.json 的
   examples, 并递增 meta.version / counts.examples (真正的"写回训练集").

P1 落库文本已打码, 回流样本保留打码文本 (隐私前置, 恶意样本安全).
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from lumio.shared.logger import get_logger
from lumio.shared.models import IntentLabel

logger = get_logger(__name__)

_AGENT_ROOT = Path(__file__).resolve().parents[3]


def _agent_path(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else _AGENT_ROOT / p


@dataclass
class BackflowCandidate:
    sample_id: str
    text: str
    intent: str
    layer: str
    verdict: str
    confidence: float
    margin: float
    reason: str

    def to_review_row(self) -> dict[str, Any]:
        return {
            "sample_id": self.sample_id,
            "text": self.text,
            "intent": self.intent,
            "layer": self.layer,
            "verdict": self.verdict,
            "confidence": round(self.confidence, 3),
            "margin": round(self.margin, 3),
            "reason": self.reason,
            "approved": None,  # 待人工: true / false
        }


def select_candidates(
    pairs: Sequence[tuple[Any, Any]],
    *,
    max_n: int = 50,
    seen: set[str] | None = None,
) -> list[BackflowCandidate]:
    """从 (sample, verdict) 对中选出分类层失败样本, 做 {text|intent} 去重. """
    from lumio.services.common.trap_eval import EvalLayer, VerdictType

    seen = set() if seen is None else set(seen)
    out: list[BackflowCandidate] = []
    for sample, verdict in pairs:
        if len(out) >= max_n:
            break
        if verdict.layer != EvalLayer.CLASSIFICATION:
            continue  # retrieval/generation 为候选, 需 P3 下游信号定罪, 不入回流
        if verdict.verdict in (VerdictType.HEALTHY,):
            continue
        key = (sample.text, sample.final_intent)
        if key in seen:
            continue
        seen.add(key)
        reason = "; ".join(verdict.evidences[:3]) if verdict.evidences else verdict.verdict.value
        out.append(
            BackflowCandidate(
                sample_id=sample.sample_id or "",
                text=sample.text,
                intent=sample.final_intent,
                layer=verdict.layer.value,
                verdict=verdict.verdict.value,
                confidence=sample.final_confidence,
                margin=sample.margin,
                reason=reason,
            )
        )
    return out


def write_staging(candidates: Sequence[BackflowCandidate], path: str) -> int:
    """写入人审 review JSONL (逐行一条候选, approved 待批). """
    target = _agent_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with target.open("w", encoding="utf-8") as f:
        for c in candidates:
            f.write(json.dumps(c.to_review_row(), ensure_ascii=False) + "\n")
            n += 1
    logger.info("回流 staging 已写: %s (%d 条)", target, n)
    return n


def finalize_confirmed(
    review_path: str,
    seed_path: str,
) -> tuple[int, str]:
    """读取 review JSONL 中 approved==true 的条目, 去重后并入 seed_dataset.json.
    返回 (新增条数, 新版本号). 未批准自动跳过 → 不写训练集. """
    review = _agent_path(review_path)
    if not review.exists():
        logger.warning("回流 review 文件不存在: %s", review)
        return 0, ""
    approved: list[dict[str, Any]] = []
    for line in review.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("approved", False) and row.get("intent") and row.get("text"):
            # 只接受合法意图标签, 防脏数据污染训练集
            try:
                IntentLabel(row["intent"])
            except ValueError:
                logger.warning("回流条目意图非法, 跳过: %s", row.get("intent"))
                continue
            approved.append({"text": row["text"], "intent": row["intent"]})
    if not approved:
        return 0, ""

    seed = _agent_path(seed_path)
    if not seed.exists():
        logger.warning("seed 数据集不存在: %s", seed)
        return 0, ""
    data = json.loads(seed.read_text(encoding="utf-8"))
    existing = {e["text"] for e in data.get("examples", [])}
    new_rows = [r for r in approved if r["text"] not in existing]
    if not new_rows:
        return 0, str(data.get("meta", {}).get("version", ""))

    data.setdefault("examples", []).extend(new_rows)
    meta = data.setdefault("meta", {})
    meta["examples"] = len(data["examples"])
    meta["version"] = _bump_patch(str(meta.get("version", "0.0.0")))
    meta["source"] = "rule-aligned seeds + 人工撰写 + closed-loop 回流"
    seed.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(
        "回流已并入训练集: %s +%d 条 → version=%s",
        seed,
        len(new_rows),
        meta["version"],
    )
    return len(new_rows), str(meta["version"])


def _bump_patch(version: str) -> str:
    parts = version.split(".")
    if len(parts) == 3:
        return f"{parts[0]}.{parts[1]}.{int(parts[2]) + 1}"
    return f"{version}+backflow"
