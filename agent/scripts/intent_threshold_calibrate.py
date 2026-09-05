"""意图快路径按类阈值校准 (架构整改 Phase 3)

数据驱动替代全局常数 _FAST_PATH_THRESHOLD=0.7: 各意图类的 softmax 分布本就
不同 (chitchat 与 bill_query 的可分性差异), 一刀切必然两头误伤。脚本用种子
金标集跑 BERT 快路径, 按"预测类"统计 正确采纳/错误采纳 的置信分布, 取满足
精度目标的最低阈值 (采纳率最大), 写入 fast_path_thresholds.json 供运行时加载。

用法:
    poetry run python scripts/intent_threshold_calibrate.py \
        [--precision 0.97] [--floor 0.5] [--out data/intent_classification/fast_path_thresholds.json]

口径:
- 只校准 BERT 快路径 (rule/vector/LLM 各有自己的采纳条件);
- 阈值作用于"预测 top-1 类": conf >= T[top1] 才快路径短路, 否则落慢路径;
- 样本 = 种子集全量 (191 条, 10 类均衡); 样本 <8 条的类不单独校准 (沿用全局)。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path

_AGENT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_AGENT_ROOT))

from lumio.shared.config import get_settings  # noqa: E402


async def main() -> int:
    parser = argparse.ArgumentParser(description="意图快路径按类阈值校准")
    parser.add_argument("--precision", type=float, default=0.97, help="目标精度 (正确采纳/总采纳)")
    parser.add_argument("--floor", type=float, default=0.5, help="阈值下限 (低于此不如直接走慢路径)")
    parser.add_argument("--min-samples", type=int, default=8, help="类样本低于此数不单独校准")
    parser.add_argument("--out", type=str, default="data/intent_classification/fast_path_thresholds.json")
    args = parser.parse_args()

    seed_path = _AGENT_ROOT / "data/intent_classification/seed_dataset.json"
    seed = json.loads(seed_path.read_text())
    examples = [(e["text"], e["intent"]) for e in seed["examples"] if e.get("text") and e.get("intent")]
    print(f"种子集: {len(examples)} 条, 类别 {sorted({lb for _, lb in examples})}")

    # ── 跑 BERT 快路径 (逐条, 与线上一致的前向口径) ──
    from lumio.services.common.bert_classifier import BertIntentClassifier

    settings = get_settings()
    bert = BertIntentClassifier(settings.classification.bert_model_path)
    # _predict 是同步纯函数; 逐条调用避免并发加载竞态
    results: list[tuple[str, str, float]] = []  # (true, pred, conf)
    for text, label in examples:
        intent = await bert.classify(text)
        results.append((label, intent.primary_intent.value, intent.primary_confidence))

    # ── 按"预测类"统计: 正确/错误采纳的置信样本 ──
    by_pred: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    for true_label, pred_label, conf in results:
        by_pred[pred_label].append((conf, pred_label == true_label))

    # ── 逐类扫阈值: 满足精度的最低值 (floor 封底, 精度不可达则收紧到 0.95 封顶) ──
    global_default = 0.7
    thresholds: dict[str, float] = {}
    report_rows: list[dict] = []
    for pred_label, samples in sorted(by_pred.items()):
        correct = sorted(c for c, ok in samples if ok)
        wrong = sorted(c for c, ok in samples if not ok)
        n = len(samples)
        if n < args.min_samples:
            report_rows.append({"label": pred_label, "n": n, "correct": len(correct), "wrong": len(wrong),
                                "threshold": None, "reason": f"样本<{args.min_samples}, 沿用全局 {global_default}"})
            continue
        best: float | None = None
        for t in [round(0.30 + 0.01 * i, 2) for i in range(66)]:  # 0.30 .. 0.95
            accepts = [(c, ok) for c, ok in samples if c >= t]
            if not accepts:
                continue
            prec = sum(1 for _, ok in accepts if ok) / len(accepts)
            if prec >= args.precision:
                best = max(t, args.floor)
                break
        if best is None:
            best = 0.95  # 精度不可达: 高阈值交给慢路径, 宁可多花一次 LLM
        thresholds[pred_label] = best
        acc_any = sum(1 for _, ok in samples if ok) / n
        report_rows.append({"label": pred_label, "n": n, "correct": len(correct), "wrong": len(wrong),
                            "threshold": best, "any_acc": round(acc_any, 3)})

    # ── 输出 ──
    out_path = _AGENT_ROOT / args.out
    payload = {
        "meta": {
            "purpose": "BERT 快路径按类采纳阈值 (校准产物, 运行时加载; 缺类沿用全局 0.7)",
            "precision_target": args.precision,
            "floor": args.floor,
            "seed_version": seed.get("meta", {}).get("version"),
            "examples": len(examples),
            "generated_by": "scripts/intent_threshold_calibrate.py",
        },
        "global_default": global_default,
        "thresholds": thresholds,
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"\n写入 {out_path}")
    for row in report_rows:
        t = row.get("threshold")
        print(f"  {row['label']:20s} n={row['n']:3d} 对/错={row['correct']}/{row['wrong']} "
              f"阈值={t if t is not None else '全局'} {row.get('reason', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
