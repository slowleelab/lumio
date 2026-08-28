#!/usr/bin/env python3
"""closed_loop_dryrun.py — 意图识别四步闭环干跑验证（无 PG/GPU 环境可执行）

在临时工作目录里端到端跑一遍闭环各段代码路径（真实 PG trap 采集与 GPU 重训不在
本脚本范围, 需中间件/训练机, 见 docs/意图识别_闭环runbook.md）:

  感知样本(合成) → 归因(AttributeEngine, 纯逻辑) → 精选(select_candidates)
  → 人审 staging(JSONL) → 并入 seed(临时副本, 版本/counts 校验) → 四门评估(规则预测器)
  → 注册表 staging→canary→promote(四门全过才转 active)

人审步骤用 --auto-approve 模拟批量批准 — **仅干跑验证用**; 真实回流必须人工逐条批,
不允许自动 approve 写进训练集(监管底线, 见 sample_backflow.finalize_confirmed)。

用法:
  poetry run python scripts/closed_loop_dryrun.py [--auto-approve] [--work-dir DIR]
退出码: 0 = 链路各段执行成功(四门结果如实打印, 规则预测器不保证 PASS); 1 = 某段异常。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

_AGENT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_AGENT_ROOT))  # 脚本直跑时把 agent/ 挂进 import 路径 (lumio 包)
_SEED_SRC = _AGENT_ROOT / "data" / "intent_classification" / "seed_dataset.json"

# 合成失败样本: 快路径(BERT)与规则分歧 + 低置信 → 归因判 classification FAILURE
_SYNTH_FAILURES: list[dict[str, Any]] = [
    {"text": "干跑样本: 怎么申请分期", "fast_intent": "bill_query", "rule_intent": "installment_inquiry"},
    {"text": "干跑样本: 上个月账单总额", "fast_intent": "faq", "rule_intent": "bill_query"},
    {"text": "干跑样本: 这笔退款进度", "fast_intent": "transaction_query", "rule_intent": "reward_query"},
]


def _synth_pairs() -> list[tuple[Any, Any]]:
    """合成 (AttribSample, AttributionVerdict) 对, 模拟 trap 采到的分类层失败样本。"""
    from lumio.services.common.trap_eval import AttribSample, AttributeEngine

    engine = AttributeEngine()
    pairs = []
    for i, row in enumerate(_SYNTH_FAILURES):
        s = AttribSample(
            sample_id=f"dryrun-{i}",
            text=row["text"],
            final_source="llm",
            final_intent=row["fast_intent"],
            final_confidence=0.2,
            margin=0.05,
            divergence=True,
            fast_source="bert",
            fast_intent=row["fast_intent"],
            fast_confidence=0.2,
            rule_intent=row["rule_intent"],
        )
        pairs.append((s, engine.attribute(s)))
    return pairs


def run_chain(work_dir: Path, auto_approve: bool) -> dict[str, Any]:
    """执行闭环各段, 返回逐段结果摘要 (任何一段抛异常即中断, 由 main 转退出码)。"""
    from lumio.services.common.eval_gates import EvalGates
    from lumio.services.common.model_registry import ModelRegistry
    from lumio.services.common.sample_backflow import (
        finalize_confirmed,
        select_candidates,
        write_staging,
    )

    summary: dict[str, Any] = {}
    work_dir.mkdir(parents=True, exist_ok=True)
    seed_path = work_dir / "seed_dataset.json"
    review_path = work_dir / "backflow_review.jsonl"
    registry_path = work_dir / "model_registry.json"
    shutil.copy(_SEED_SRC, seed_path)

    # 1. 感知 + 归因 (合成样本)
    pairs = _synth_pairs()
    summary["attributed"] = [(p[0].text, p[1].layer.value, p[1].verdict.value) for p in pairs]

    # 2. 精选
    candidates = select_candidates(pairs, max_n=50)
    summary["candidates"] = len(candidates)
    if not candidates:
        raise RuntimeError("精选结果为空: 归因样本未触发 classification 层失败 (检查 _SYNTH_FAILURES 字段)")

    # 3. 人审 staging
    write_staging(candidates, str(review_path))
    if auto_approve:
        rows = [json.loads(line) for line in review_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        for row in rows:
            row["approved"] = True
        review_path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n", encoding="utf-8")
        summary["approved"] = len(rows)
    else:
        summary["approved"] = 0  # 未批准 → finalize 应并入 0 条, 链路仍走通

    # 4. 并入 (临时 seed 副本)
    before_version = json.loads(seed_path.read_text(encoding="utf-8"))["meta"]["version"]
    added, new_version = finalize_confirmed(str(review_path), str(seed_path))
    merged = json.loads(seed_path.read_text(encoding="utf-8"))
    counts_consistent = merged["meta"]["counts"]["examples"] == len(merged["examples"])
    summary["merge"] = {
        "before_version": before_version,
        "after_version": new_version,
        "added": added,
        "counts_consistent": counts_consistent,
    }

    # 5. 四门评估 (规则预测器; 门如实报 PASS/FAIL, 不保证过)
    from lumio.services.common.classifier import RuleClassifier

    rule = RuleClassifier()

    def predict(text: str) -> tuple[str, float]:
        r = rule.classify(text)
        return r.primary_intent.value, r.primary_confidence

    gate_results = EvalGates().run(predict)
    summary["gates"] = [{"name": r.name, "passed": r.passed, "detail": r.detail} for r in gate_results]

    # 6. 注册表 promote (四门全过才转 active)
    registry = ModelRegistry(state_path=str(registry_path), allow_ungated=False)
    registry.register("dryrun-v0.0.1", str(work_dir / "weights_dryrun"), notes="closed-loop dry run")
    registry.set_canary("dryrun-v0.0.1")
    all_pass = all(r.passed for r in gate_results)
    active = registry.promote("dryrun-v0.0.1", [r.to_dict() for r in gate_results]) if all_pass else None
    summary["promote"] = {"all_gates_pass": all_pass, "active": active}
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--auto-approve", action="store_true", help="干跑模拟人审批量批准 (真实回流禁止)")
    ap.add_argument("--work-dir", default=None, help="工作目录 (默认系统临时目录)")
    args = ap.parse_args()

    work_dir = Path(args.work_dir) if args.work_dir else Path(tempfile.mkdtemp(prefix="closed_loop_dryrun_"))
    try:
        summary = run_chain(work_dir, args.auto_approve)
    except Exception as exc:
        print(f"闭环干跑失败: {exc}")
        return 1
    print(f"工作目录: {work_dir}")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("闭环各段执行完成 (四门结果见上; 规则预测器不保证 PASS, promote 仅在四门全过时执行)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
