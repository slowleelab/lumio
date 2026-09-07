#!/usr/bin/env python
"""energy-OOD 阈值校准 (P0 整改: "有枪没子弹" → 部署环境实测标定)

背景: classification.ood_energy_threshold 长期为保守初值 0.0 ("假开关"注释自述)。
能量数值尺度取决于 logits 绝对值, 必须用部署环境的真实模型标定。

方法:
- 分布内 (ID): seed_dataset.json 全量样例 (训练分布代表)
- 分布外 (OOD): 乱码 / 成语古诗闲聊 / 无关领域文本 (历史 badcase 会话实录)
- 对两集合跑 BertIntentClassifier.ood_score, 输出能量分布分位数,
  推荐 threshold = ID.p99 与 OOD.p1 的中点 (偏向少误杀 ID; 分离不足时报警)

用法: poetry run python scripts/calibrate_ood_threshold.py
输出: 推荐的 CLS_OOD_ENERGY_THRESHOLD (写入 .env / config 默认值)
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lumio.services.common.bert_classifier import BertIntentClassifier
from lumio.shared.config import get_settings

# ── 分布外样本 (历史 badcase/模拟器噪声实录; 新坏例类型持续追加) ──
OOD_SAMPLES: list[str] = [
    # 乱码 / 按键误触 (会话 e33d1fa8, f08227d4 实录)
    "额佛呢份",
    "hjfw",
    "sncjao",
    "889",
    "4444",
    "YRNN",
    "HWS46",
    "jdfkl",
    "asdfgh",
    # 成语 / 古诗 / 俗语半截 (会话 8700a2ea, 22ad 实录)
    "锄禾日当午",
    "床前明月光",
    "丈二和尚",
    "不管三七二十一",
    "姜太公钓鱼",
    "醉翁之意不在酒",
    # 纯闲聊 / 寒暄外的无业务闲谈
    "今天天气不错",
    "你们下班了吗",
    "中午吃什么好",
    "我有点无聊",
    "讲个笑话听听",
    "你会下棋吗",
    "世界杯谁赢了",
    # 无关领域 (非信用卡客服分布)
    "帮我订一张去北京的机票",
    "附近有什么好吃的餐厅",
    "这首歌叫什么名字",
    "今天股票涨了吗",
    "怎么退货",
]


def percentile(sorted_vals: list[float], p: float) -> float:
    if not sorted_vals:
        return float("nan")
    idx = min(int(len(sorted_vals) * p), len(sorted_vals) - 1)
    return sorted_vals[idx]


async def _score_all(clf: BertIntentClassifier, texts: list[str]) -> list[float]:
    """顺序跑 ood_score — 并发 to_thread 的 torch 前向既会段错误 (SIGSEGV),
    又与线上服务线程池超订阅互相拖垮 (实测 4 并发 × 默认全核线程 = 6.5s/条)。
    先压 torch 线程数为 2, 再逐条推理。"""
    try:
        import torch

        torch.set_num_threads(2)
    except Exception:
        pass
    out: list[float] = []
    for i, t in enumerate(texts):
        out.append(await clf.ood_score(t))
        if (i + 1) % 25 == 0:
            print(f"  … {i + 1}/{len(texts)}", flush=True)
    return out


async def main() -> None:
    settings = get_settings()
    registry_path = settings.classification.model_registry_path
    model_path = settings.classification.bert_model_path
    try:
        from lumio.services.common.model_registry import ModelRegistry

        model_path = ModelRegistry(state_path=registry_path).compose_classifier_path(model_path)
    except Exception:
        pass

    import json

    seed_path = Path(__file__).resolve().parent.parent / "data" / "intent_classification" / "seed_dataset.json"
    seed = json.loads(seed_path.read_text())
    id_texts = [e["text"] for e in seed["examples"] if e.get("text")]

    clf = BertIntentClassifier(model_path=model_path, temperature=settings.classification.ood_temperature)
    print(f"模型: {model_path}")
    print(f"分布内样本: {len(id_texts)} (seed_dataset) | 分布外样本: {len(OOD_SAMPLES)} (badcase 实录)")

    id_energy = sorted(await _score_all(clf, id_texts))
    ood_energy = sorted(await _score_all(clf, OOD_SAMPLES))

    def stats(name: str, vals: list[float]) -> None:
        print(
            f"{name}: p1={percentile(vals, 0.01):8.3f} p5={percentile(vals, 0.05):8.3f} "
            f"p50={percentile(vals, 0.50):8.3f} p95={percentile(vals, 0.95):8.3f} p99={percentile(vals, 0.99):8.3f}"
        )

    stats("ID  ", id_energy)
    stats("OOD ", ood_energy)

    id_p99 = percentile(id_energy, 0.99)
    ood_p1 = percentile(ood_energy, 0.01)
    ood_p5 = percentile(ood_energy, 0.05)
    print(f"\n分离度: ID.p99={id_p99:.3f}  OOD.p1={ood_p1:.3f}  OOD.p5={ood_p5:.3f}")

    if ood_p1 <= id_p99:
        print("⚠️ 两分布重叠 (OOD.p1 ≤ ID.p99): energy 门单独不可用, 建议保持短路双信号 (intent+energy)")
    recommended = round((id_p99 + ood_p5) / 2.0, 3)
    # band 建议: 模糊带宽取分离区间的一半, 上限 1.0 (默认)
    band = round(min(max((ood_p5 - id_p99) / 4.0, 0.0), 1.0), 3)
    print("\n推荐配置 (写入 .env):")
    print("  CLS_OOD_ENABLED=true")
    print(f"  CLS_OOD_ENERGY_THRESHOLD={recommended}")
    print(f"  CLS_OOD_AMBIGUOUS_BAND={band}")
    print(f"\n校验: threshold 下 ID 误杀率 = {sum(1 for e in id_energy if e > recommended) / len(id_energy):.1%}, "
          f"OOD 捕获率 (energy > threshold) = {sum(1 for e in ood_energy if e > recommended) / len(ood_energy):.1%}")


if __name__ == "__main__":
    asyncio.run(main())
