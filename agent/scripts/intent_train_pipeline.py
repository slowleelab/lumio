#!/usr/bin/env python3
"""intent_train_pipeline.py — 意图分类 BERT 训练管线 (造数 + 微调 + 评测 + energy 标定)

与 intent_classifier_spike.py 不同: 它是"训练出可上线模型 + 标定 P1 energy-OOD 阈值"的
正式管线, 而非探针对比。产出:
  1. 数据: 读 seed_dataset.json (v0.3.0, 已含手工扩充), 训练时叠加
     (a) 确定性 paraphrase 扩充 (查→查/查询/帮我查…) → 域内多样性
     (b) 规则模板弱标签合成 (复用 spike) → 扩量
  2. 训练: 微调 uer/chinese_roberta_L-4_H-512 (24M) 10 类头, 落盘到版本目录 out_intent_clf_vMM.m
  3. 评测: 留出集 acc/macroF1 (vs Rule) + 易混淆对正确率
  4. energy 标定: 用独立 OOD/噪声池 (ood_pool.json, 只评测不训练) 计算 in vs OOD 的
     energy 分布, 挑一个"域内误伤 ≤1%"的最严操作点, 输出推荐 ood_energy_threshold/band
     到 ood_calibration.json (不改 closed_loop.json 的开关, 阈值先备着灰度用)。

环境变量 HF_ENDPOINT 可设镜像; 有 MPS/CUDA 自动用; 全部随机固定 seed。
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
DATA = ROOT / "data" / "intent_classification"

import intent_classifier_spike as spike  # noqa: E402  # 复用种子/弱标签/训练/评测/延迟函数

# ---------------------------------------------------------------- 常量/路径
CLASSES = spike.CLASSES
IDX = spike.IDX
SEED_JSON = DATA / "seed_dataset.json"
OOD_JSON = DATA / "ood_pool.json"
DEFAULT_OUT = DATA / "out_intent_clf_v030"
MODEL_NAME = spike.MODEL_NAME
MAX_LEN = 42  # 比 spike 的 32 略长, 兜住含"刷满几笔"这类较长真实问法

# ---------------------------------------------------------- paraphrase 扩充
# 关键词 -> 同义替换, 生成真实问法变体。只换不改类, 保持标签不变。
_SWAP: dict[str, list[str]] = {
    "查": ["查", "查询", "查一下", "看看", "帮我看"],
    "多少": ["多少", "有多少", "是多少", "一共多少"],
    "账单": ["账单", "本期账单", "本月账单", "账"],
    "额度": ["额度", "可用额度", "信用额度"],
    "积分": ["积分", "我的积分", "真实积分"],
}
_PRES = ["", "请问", "我想", "帮我"]  # 前缀语气多样性


def _augment(rows: list[dict], target_per: int = 6, seed: int = 7) -> list[dict]:
    """确定性 paraphrase 扩充: 每类至少 target_per 条(不足则生成变体凑齐), 保真标签."""
    rng = random.Random(seed)
    table: dict[str, list[str]] = {}
    for r in rows:
        table.setdefault(r["intent"], []).append(r["text"])
    out: list[dict] = []
    used: set[str] = set()
    for lbl, texts in table.items():
        pool = list(texts)
        while len(pool) < target_per and len(texts) > 0:
            src = rng.choice(texts)
            # 挑一个命中词做同义替换(若没有则只换前缀)
            tok = rng.choice(list(_SWAP))
            variant = src.replace(tok, rng.choice(_SWAP[tok]), 1) if tok in src else rng.choice(_PRES) + src
            if variant != src and variant not in used:
                pool.append(variant)
            # 防无限循环: 尝试次数上限
            if len(pool) - len(texts) > target_per * 4:
                break
        for t in pool:
            if t not in used:
                used.add(t)
                out.append({"text": t, "intent": lbl})
    return out


# ------------------------------------------------------------------ energy
def _energy(model, tokenizer, device, text: str) -> float:
    import torch

    enc = tokenizer(text, truncation=True, max_length=MAX_LEN, return_tensors="pt")
    enc = {k: v.to(device) for k, v in enc.items()}
    with torch.no_grad():
        logits = model(**enc).logits[0].tolist()
    m = max(logits)
    return -(m + math.log(sum(math.exp(x - m) for x in logits)))


def calibrate_energy(model, tokenizer, device, in_rows: list[dict], ood_texts: list[str],
                     band: float = 0.5) -> dict:
    """in vs OOD 能量分布; 选"域内误伤≤1%"的最严 block 线, 反推阈值.

    约定(Liu 2020): energy 低=known, 高=OOD。unknown 判定: energy > threshold + band。
    故 block_line = threshold + band。返回推荐 threshold 及各档位的对应命中率。
    """

    in_e = sorted(_energy(model, tokenizer, device, r["text"]) for r in in_rows)
    ood_e = sorted(_energy(model, tokenizer, device, t) for t in ood_texts)
    n = len(in_e)
    # 候选 block 线: 域内 p90 起, 逐点评估, 取首个满足"域内 unknown ≤1%"且 OOD 命中尽量高
    lines: list[tuple[float, float, float]] = []  # (line, in_unknown_rate, ood_catch_rate)
    cand = [in_e[min(n - 1, int(n * p / 100))] for p in range(90, 101)] + list(ood_e)
    for line in set(cand):
        in_unknown = sum(1 for x in in_e if x > line) / n
        ood_catch = sum(1 for x in ood_e if x > line) / len(ood_e)
        lines.append((line, in_unknown, ood_catch))
    lines.sort(key=lambda t: (t[1], -t[2]))  # 域内误伤优先小, 再 OOD 命中大
    # 域内误伤≤1% 里取 OOD 命中最高
    ok = [t for t in lines if t[1] <= 0.01]
    best = max(ok, key=lambda t: t[2]) if ok else lines[0]
    block_line, in_unk, ood_catch = best
    threshold = block_line - band
    return {
        "in_domain_n": n,
        "in_domain_energy": {"min": in_e[0], "median": in_e[n // 2], "p90": in_e[int(n * 0.9)], "max": in_e[-1]},
        "ood_n": len(ood_e),
        "ood_energy": {"min": ood_e[0], "median": ood_e[len(ood_e) // 2], "p90": ood_e[int(len(ood_e) * 0.9)], "max": ood_e[-1]},
        "recommended": {
            "block_line": round(block_line, 4),
            "ood_ambiguous_band": band,
            "ood_energy_threshold": round(threshold, 4),
            "in_domain_unknown_rate": round(in_unk, 4),
            "ood_catch_rate": round(ood_catch, 4),
        },
        "operating_points": [
            {"block_line": round(ln, 3), "in_unknown": round(u, 3), "ood_catch": round(c, 3)}
            for ln, u, c in sorted(lines, key=lambda t: -t[2])[:12]
        ],
    }


# ------------------------------------------------------------------ main
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=str, default=str(DEFAULT_OUT))
    ap.add_argument("--epochs", type=int, default=6)
    ap.add_argument("--test-frac", type=float, default=0.2)
    ap.add_argument("--weak-per-class", type=int, default=60)
    ap.add_argument("--target-per-class", type=int, default=12)
    ap.add_argument("--val-only", action="store_true", help="只加载已保存模型做评测/标定, 不训练")
    args = ap.parse_args()
    out_dir = Path(args.out)

    examples = [{"text": e["text"], "intent": e["intent"]} for e in json.loads(SEED_JSON.read_text(encoding="utf-8"))["examples"]]
    confusable = json.loads(SEED_JSON.read_text(encoding="utf-8"))["confusable_pairs"]
    ood = json.loads(OOD_JSON.read_text(encoding="utf-8"))["examples"]
    ood_texts = [e["text"] for e in ood]

    train_rows, test_rows = spike.stratified_split(examples, args.test_frac, seed=0)
    synth = spike.gen_weak_labels(per_class=args.weak_per_class)
    aug = _augment(train_rows, target_per=args.target_per_class)
    train_rows = train_rows + synth + aug
    print(f"[data] seeds={len(examples)} train={len(train_rows)} "
          f"(weak{len(synth)} + paraphrase{len(aug)}) test={len(test_rows)} confusable={len(confusable)} ood={len(ood_texts)}")

    import torch  # 训练/评测需要(延迟到 main, 避免无 torch 环境 import 报错)
    device = "mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu"

    if args.val_only:
        from transformers import AutoModelForSequenceClassification, AutoTokenizer
        tokenizer = AutoTokenizer.from_pretrained(out_dir)
        model = AutoModelForSequenceClassification.from_pretrained(out_dir).to(device).eval()
    else:
        model, tokenizer, _ = spike.train_model(train_rows, out_dir, epochs=args.epochs, batch_size=16)
        model.to(device).eval()

    print("\n=========== 留出集 (test) vs Rule ===========")
    m_acc, m_f1, m_preds = spike.evaluate(model, tokenizer, device, test_rows)
    r_acc, r_f1, _ = spike.rule_metrics(test_rows)
    print(f" 小BERT: acc={m_acc:.3f} macroF1={m_f1:.3f} | Rule: acc={r_acc:.3f} macroF1={r_f1:.3f}")

    print("\n=========== 易混淆对 ===========")
    cc = [{"text": c["text"], "intent": c["correct"]} for c in confusable]
    cc_acc, cc_f1, _ = spike.evaluate(model, tokenizer, device, cc)
    print(f" BERT: acc={cc_acc:.3f} (命中 {cc_acc * len(cc):.0f}/{len(cc)})")
    for c in confusable:
        pred = spike.predict_model(model, tokenizer, device, c["text"])
        print(f"   [{'OK' if pred == c['correct'] else 'X '}] '{c['text']}' -> {pred:<16} (期望={c['correct']})")

    print("\n=========== energy 标定 (in vs OOD, block线=域内误伤≤1%) ===========")
    cal = calibrate_energy(model, tokenizer, device, examples, ood_texts)
    rec = cal["recommended"]
    print(f" 域内 energy: min={cal['in_domain_energy']['min']:.2f} "
          f"med={cal['in_domain_energy']['median']:.2f} p90={cal['in_domain_energy']['p90']:.2f} "
          f"max={cal['in_domain_energy']['max']:.2f}")
    print(f" OOD   energy: min={cal['ood_energy']['min']:.2f} "
          f"med={cal['ood_energy']['median']:.2f} p90={cal['ood_energy']['p90']:.2f} "
          f"max={cal['ood_energy']['max']:.2f}")
    print(f" 推荐: ood_energy_threshold={rec['ood_energy_threshold']} "
          f"band={rec['ood_ambiguous_band']} (block_line={rec['block_line']}, "
          f"域内误伤={rec['in_domain_unknown_rate']}, OOD命中={rec['ood_catch_rate']})")

    cal_path = out_dir / "ood_calibration.json"
    cal_path.write_text(json.dumps(cal, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[calibration] saved -> {cal_path}")


if __name__ == "__main__":
    main()
