#!/usr/bin/env python3
"""intent_classifier_spike.py — 意图分类"轻量 BERT vs 规则分类器"对比探针 (Spike)

纯探针脚本: 不触碰线上服务 (bot :8000 / assist :8001)。系统 python3 独立运行，
需要 torch/transformers/sklearn (poetry venv 无需)。

产出:
  1. 用种子集 (129 例) + 规则模板弱标签合成样本, 微调足量小 BERT
     (uer/chinese_roberta_L-4_H-512, 24M) 的 10 类意图分类头;
  2. 同一留出集 + 12 组易混淆对上对比 小BERT vs RuleClassifier(规则复刻版)
     的 准确率 / 宏F1 / 平均推理延迟;
  3. 打印对比结论, 供决定是否替换线上慢路径。

环境变量:
  HF_ENDPOINT=https://hf-mirror.com  # 国内镜像, 下载失败则退到 CPU/降档
  未设置时默认 CPU; 有 MPS 自动用 MPS。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import time
from pathlib import Path

random.seed(0)

# ---------------------------------------------------------------- 常量/路径
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data" / "intent_classification"
SEED_JSON = DATA / "seed_dataset.json"
OUT_DIR = ROOT / "data" / "intent_classification" / "out_intent_clf"
MODEL_NAME = os.environ.get("SPIKE_MODEL", "uer/chinese_roberta_L-4_H-512")

# 标签顺序固定 (与 IntentLabel 对齐)
CLASSES = [
    "faq",
    "bill_query",
    "transaction_query",
    "limit_query",
    "installment_inquiry",
    "reward_query",
    "card_loss",
    "complaint",
    "transfer_agent",
    "chitchat",
]
IDX = {c: i for i, c in enumerate(CLASSES)}

# ---------------------------------------------------------- RuleClassifier 复刻
# 与 lumio/services/common/classifier.py 的 _RULES 保持一致 (探针内自包含, 避免
# 在无 torch 的 poetry venv 里 import 整个 lumio)。
_RULES: list[dict] = [
    {
        "intent": "bill_query",
        "patterns": ["账单", "消费记录", "还款金额", "本期账单", r"上个?月.?花了多少"],
        "keywords": ["账单", "消费", "还款", "欠款", "应还", "最低还款"],
        "confidence": 0.84,
    },
    {
        "intent": "transaction_query",
        "patterns": ["交易记录", "明细", "流水", "扣款"],
        "keywords": ["交易", "明细", "流水", "扣款", "刷卡"],
        "confidence": 0.56,
    },
    {
        "intent": "limit_query",
        "patterns": ["额度", "可用额度", "信用额度", "提额", "降额"],
        "keywords": ["额度", "可用", "提额", "临时额度", "授信"],
        "confidence": 0.95,
    },
    {
        "intent": "installment_inquiry",
        "patterns": ["分期", "期数", "手续费率", "账单分期", "消费分期"],
        "keywords": ["分期", "期数", "手续费", "分期费率"],
        "confidence": 0.71,
    },
    {
        "intent": "reward_query",
        "patterns": ["积分", "积分兑换", "积分过期", "积分余额"],
        "keywords": ["积分", "兑换", "过期", "积分商城"],
        "confidence": 0.94,
    },
    {"intent": "faq", "patterns": ["什么是", "怎么办理", "如何操作", "流程是什么"], "keywords": [], "confidence": 0.47},
    {
        "intent": "card_loss",
        "patterns": ["挂失", "补卡", "换卡", "卡片丢失"],
        "keywords": ["挂失", "丢失", "补卡", "换卡"],
        "confidence": 0.56,
    },
    {
        "intent": "complaint",
        "patterns": ["投诉", "不满意", "举报", "投诉你们"],
        "keywords": ["投诉", "不满", "举报"],
        "confidence": 0.62,
    },
    {
        "intent": "transfer_agent",
        "patterns": ["转人工", "人工客服", "找人工", r"我要找.*人"],
        "keywords": ["人工", "转人工", "真人"],
        "confidence": 0.9,
    },
    {
        "intent": "chitchat",
        "patterns": ["你好", "嗨", "在吗", "你是谁", "谢谢", "再见"],
        "keywords": [],
        "confidence": 0.35,
    },
]


class RuleClassifierReplica:
    """复刻线上 RuleClassifier.classify 的最长匹配 / 最高置信度逻辑。"""

    _FAST_PATH_THRESHOLD = 0.7

    def __init__(self, rules: list[dict] | None = None) -> None:
        self._rules = rules or _RULES
        self._compiled = []
        for r in self._rules:
            self._compiled.append(
                {
                    "intent": r["intent"],
                    "patterns": [re.compile(p) for p in r["patterns"]],
                    "keywords": r.get("keywords", []),
                    "confidence": r.get("confidence", 0.7),
                }
            )

    def classify(self, text: str) -> str:
        best = "faq"
        best_c = 0.0
        # 与线上一致的平局裁决: 同置信时敏感意图(挂失/投诉/转人工)优先为主意图
        sensitive = {"card_loss", "complaint", "transfer_agent"}
        for rule in self._compiled:
            hit = 0.0
            for p in rule["patterns"]:
                if p.search(text):
                    hit = rule["confidence"]
                    break
            if not hit and rule["keywords"]:
                found = sum(1 for kw in rule["keywords"] if kw in text)
                if found:
                    hit = rule["confidence"] * (0.8 if found >= 2 else 0.65)
            if hit > best_c or (hit == best_c and rule["intent"] in sensitive and best not in sensitive):
                best_c, best = hit, rule["intent"]
        return best


# ------------------------------------------------- 规则模板弱标签合成 (Weak labels)
# 用规则的关键词/短语填进自然问法, 生成带弱标签的合成训练样本。重点补齐易混淆对
# ("总额 vs 逐笔"、"积分兑换 vs 通用FAQ" 等), 增强小模型区分度。
_TPL: dict[str, list[str]] = {
    "bill_query": ["这个月{总额}是多少", "帮我查一下{账单}", "上个月{还款}多少", "本期{应还}是多少", "我还欠{多少钱}"],
    "transaction_query": [
        "最近有{交易}吗",
        "帮我看看{明细}",
        "这顿{刷卡}的{流水}",
        "把我{消费}的{记录}列出来",
        "昨天晚上那笔{扣款}是什么",
    ],
    "limit_query": ["我现在{额度}多少", "{可用额度}还有多少", "怎么{提额}", "能{临时额度}吗", "为什么{降额}"],
    "installment_inquiry": [
        "账单能不能{分期}",
        "分12{期数}手续费怎么算",
        "消费{分期}怎么办",
        "{分期费率}是多少",
        "年费能{分期}吗",
    ],
    "reward_query": ["我有多少{积分}", "{积分兑换}怎么换", "{积分}会{过期}吗", "{积分商城}在哪里", "{积分余额}多少"],
    "card_loss": ["{挂失}我的卡", "卡丢了要{补卡}", "{换卡}怎么操作", "怎么{挂失}", "我的卡{丢失}了"],
    "complaint": ["我要{投诉}", "对服务很{不满}", "{举报}这个扣费", "我要{投诉你们}", "客服态度让我{不满}"],
    "transfer_agent": ["帮我{转人工}", "转{人工客服}", "我想找{真人}", "{人工}在吗", "帮我接{人工}"],
    # chitchat 只收"前置门拦不到、必须分类器判"的输入: 整句精确问候/道别(你好/谢谢/再见)
    # 在运行期被 _is_greeting/_is_farewell 拦截, 永远到不了分类器, 训练它们=把容量花在
    # 不存在的流量上(训练/推理分布不一致)。这里全部用硬规则吃不下的变体与真闲聊。
    "chitchat": [
        "你好呀",
        "早上好",
        "哈喽",
        "你吃饭了吗",
        "今天天气不错啊",
        "你是机器人吗",
        "讲个笑话吧",
        "聊点别的",
        "随便聊聊",
        "你真贴心",
        "周末有什么安排",
        "你多大了",
        "谢谢你的回答",
        "麻烦你了",
    ],
    "faq": [
        "什么是{年费}",
        "怎么{办理}信用卡",
        "激活{如何操作}",
        "免息期{流程是什么}",
        "汇率{什么是}",
        "是不是所有卡都能免年费",
        "你们有没有新出的联名卡",
        "我的卡是不是支持境外免费",
        "白金卡有哪些专属权益",
        "信用卡刷满几笔能免年费吗",
        "听说你们有张隐藏黑金卡是真的吗",
        "卡片赠送的接送机权益怎么用",
        "这张卡是不是免息期最长",
        "这款卡真的送礼品吗",
        "我的卡是不是终身免年费还带接送机",
        "你们是不是有海洋主题的联名卡",
    ],
}
# {总额} 槽位 -> 若干可选填词, 增加多样性
_SLOTS = {
    "总额": ["总共消费了多少", "一共花了多少", "合计金额", "账单总额"],
    "账单": ["本期账单", "上个账单", "这个月的账"],
    "还款": ["还款金额", "最低还款", "欠款总额"],
    "应还": ["账单应还", "应还款项"],
    "多少钱": ["多少钱", "多少金额", "欠多少钱"],
}


def _fill(tpl: str) -> str:
    """展开模板 {词} 槽位; 未定义槽位则随机选。"""
    out = tpl
    for key, choices in _SLOTS.items():
        token = "{" + key + "}"
        if token in out:
            out = out.replace(token, random.choice(choices))
    # 剩余裸 {非槽} 直接去掉括号, 保留内部词, 作为弱信号
    for key in set(re.findall(r"\{([^}]+)\}", tpl)):
        token = "{" + key + "}"
        if token in out:
            out = out.replace(token, random.choice(_SLOT_FILL.get(key, [key])))
    return out


_SLOT_FILL = {
    "交易": "交易记录",
    "刷卡": "刷卡记录",
    "流水": "交易流水",
    "扣款": "扣款记录",
    "记录": "明细",
    "明细": "消费明细",
    "积分": "积分",
    "账单": "账单",
    "消费": "消费",
    "分期": "分期",
    "期数": "期数",
    "挂失": "挂失",
    "补卡": "补卡",
    "换卡": "换卡",
    "投诉": "投诉",
    "不满": "不满意",
    "举报": "举报",
    "投诉你们": "投诉你们",
    "转人工": "转人工",
    "人工客服": "人工客服",
    "真人": "真人",
    "人工": "人工",
    "年费": "年费",
    "办理": "办理",
    "如何操作": "如何操作",
    "流程是什么": "流程是什么",
    "提额": "提额",
    "可用额度": "可用额度",
    "额度": "额度",
    "临时额度": "临时额度",
    "降额": "降额",
    "积分兑换": "积分兑换",
    "过期": "过期",
    "积分商城": "积分商城",
    "积分余额": "积分余额",
}


def gen_weak_labels(per_class: int = 30, rng: random.Random | None = None) -> list[dict]:
    """用规则模板生成带弱标签的合成训练样本 (weak labels)。"""
    if rng is None:
        rng = random.Random(7)
    out: list[dict] = []
    for label, tmpls in _TPL.items():
        for _ in range(per_class):
            tpl = rng.choice(tmpls)
            text = _fill(tpl)
            # 保证合成文本非空且不重复
            if text and text not in [e["text"] for e in out]:
                out.append({"text": text, "intent": label})
    return out


# ── 多轮上下文合成样本 (训练/推理分布一致性) ───────────────────────────────────
# 运行时 bert_classifier._build_dialog_input 以角色标记把历史拼进输入 (老→新, 段间
# 无分隔, 换行接当前句), 而旧训练只有单句 — 模型从未见过它线上实际吃到的格式。
# 这里按同一格式合成少量多轮样本进训练集, 让模型学会"结合上文判当前句"。
# 格式契约由 tests/test_classifier.py::test_bert_build_dialog_input_format_contract
# 冻结: 改运行时格式必须同步此处生成格式, 否则训练/推理输入格式再次漂移。
_HISTORY_TPL: list[tuple[str, str, str, str]] = [
    # (上轮 customer, 上轮 bot, 当前句, 标签) — bot 话术取 slot_tracker 同款追问
    ("我想分期", "请问您想分期的金额是多少?", "分一万二", "installment_inquiry"),
    ("帮我查一下账单", "请问您要查哪个月的账单?", "上个月的", "bill_query"),
    ("查下最近的消费记录", "请问您要查哪个时间段的交易?", "昨天的", "transaction_query"),
    ("我卡丢了", "请提供您信用卡的后四位以便验证身份", "后四位是1234", "card_loss"),
    ("我要投诉", "请详细描述您遇到的问题", "乱扣了我一笔年费", "complaint"),
    ("帮我转人工", "正在为您转接人工客服", "好的谢谢", "transfer_agent"),
    ("积分有多少", "请问您持有的是哪种卡?", "白金卡", "reward_query"),
    ("额度不够用", "请问您持有的是哪种卡?", "金卡", "limit_query"),
]


def gen_history_samples(per_tpl: int = 6) -> list[dict]:
    """按运行时 _build_dialog_input 的角色标记格式合成多轮样本 (不引入随机, 可复现)。"""
    out: list[dict] = []
    for prev, bot, cur, label in _HISTORY_TPL:
        for _ in range(per_tpl):
            out.append({"text": f"用户:{prev}客服:{bot}\n{cur}", "intent": label})
    return out


# ---------------------------------------------------------------- load seeds
def load_data() -> tuple[list[dict], list[dict]]:
    d = json.loads(SEED_JSON.read_text(encoding="utf-8"))
    examples: list[dict] = [{"text": e["text"], "intent": e["intent"]} for e in d["examples"]]
    confusable: list[dict] = d["confusable_pairs"]
    return examples, confusable


def stratified_split(rows: list[dict], test_frac: float = 0.2, seed: int = 0) -> tuple[list[dict], list[dict]]:
    rng = random.Random(seed)
    table: dict[str, list[dict]] = {}
    for r in rows:
        table.setdefault(r["intent"], []).append(r)
    train, test = [], []
    for _lbl, items in table.items():
        rng.shuffle(items)
        n_test = max(1, round(len(items) * test_frac))
        test += items[:n_test]
        train += items[n_test:]
    return train, test


# ------------------------------------------------------------------ training
def train_model(
    train_rows: list[dict],
    dev_rows: list[dict],
    save_dir: Path,
    model_name: str = MODEL_NAME,
    epochs: int = 6,
    lr: float = 2e-5,
    batch_size: int = 16,
    patience: int = 2,
) -> tuple:
    """微调 + dev 早停 + 温度校准。

    - dev 集两用: (1) 每 epoch 测 dev 精度做早停 (固定 3 epochs 曾把精度训到回落,
      且无验证集无从知道); (2) 温度校准 — 最小化 dev NLL 的一维标定, 压掉小 BERT
      的虚高置信 (下游 0.7 快路径/0.3 噪声下限都信任置信度数值, 校准是这些门的前提)。
    - 校准结果双写: calibration.json (人类可读记录) + config.temperature (推理侧
      bert_classifier 加载时自动采用)。
    """
    import torch
    from torch.utils.data import DataLoader, Dataset
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    device = "mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu"
    print(f"[train] device={device} model={model_name} train={len(train_rows)} dev={len(dev_rows)}")

    tokenizer = AutoTokenizer.from_pretrained(model_name, trust_remote_code=True)
    model = AutoModelForSequenceClassification.from_pretrained(
        model_name, num_labels=len(CLASSES), trust_remote_code=True
    ).to(device)

    def make_ds(rows: list[dict]) -> Dataset:
        labels = [r["intent"] for r in rows]
        texts = [r["text"] for r in rows]

        class _DS(Dataset):
            def __len__(self) -> int:
                return len(texts)

            def __getitem__(self, i: int) -> dict:
                enc = tokenizer(texts[i], truncation=True, max_length=32, padding="max_length")
                return {
                    "input_ids": torch.tensor(enc["input_ids"]),
                    "attention_mask": torch.tensor(enc["attention_mask"]),
                    "labels": torch.tensor(IDX[labels[i]]),
                }

        return _DS()

    def logits_of(rows: list[dict]) -> torch.Tensor:
        model.eval()
        dl = DataLoader(make_ds(rows), batch_size=batch_size)
        outs = []
        with torch.no_grad():
            for b in dl:
                outs.append(
                    model(input_ids=b["input_ids"].to(device), attention_mask=b["attention_mask"].to(device)).logits
                )
        return torch.cat(outs, dim=0)

    def acc_of(rows: list[dict]) -> float:
        if not rows:
            return 0.0
        logits = logits_of(rows)
        preds = logits.argmax(-1).tolist()
        return sum(1 for p, r in zip(preds, rows, strict=False) if p == IDX[r["intent"]]) / len(rows)

    dl = DataLoader(make_ds(train_rows), batch_size=batch_size, shuffle=True)
    opt = torch.optim.AdamW(model.parameters(), lr=lr)
    best_state_path = save_dir / ".best_state.pt"
    best_dev_acc = -1.0
    stale = 0
    for ep in range(epochs):
        model.train()
        tot, correct = 0, 0
        t0 = time.time()
        for b in dl:
            bid, am, lab = b["input_ids"].to(device), b["attention_mask"].to(device), b["labels"].to(device)
            opt.zero_grad()
            out = model(input_ids=bid, attention_mask=am, labels=lab)
            out.loss.backward()
            opt.step()
            correct += (out.logits.argmax(-1) == lab).sum().item()
            tot += lab.numel()
        dev_acc = acc_of(dev_rows)
        improved = dev_acc > best_dev_acc
        print(
            f"  epoch {ep+1}/{epochs} train_acc={correct/tot:.3f} dev_acc={dev_acc:.3f}"
            f" ({'best+' if improved else ''}{time.time()-t0:.1f}s)"
        )
        if improved:
            best_dev_acc = dev_acc
            save_dir.mkdir(parents=True, exist_ok=True)
            torch.save(model.state_dict(), best_state_path)
            stale = 0
        else:
            stale += 1
            if stale >= patience:
                print(f"  early stop: dev 连续 {patience} 轮未提升, 回载最优权重")
                break

    model.load_state_dict(torch.load(best_state_path))
    model.eval()

    # ── 温度校准: 在 dev logits 上最小化 NLL 的一维网格搜索 ──
    def nll(logits: torch.Tensor, labels: list[int], t: float) -> float:
        scaled = logits / t
        logp = torch.log_softmax(scaled, dim=-1)
        return float(-logp[torch.arange(len(labels)), torch.tensor(labels)].mean())

    dev_labels = [IDX[r["intent"]] for r in dev_rows]
    dev_logits = logits_of(dev_rows)
    best_t, best_nll = 1.0, nll(dev_logits, dev_labels, 1.0)
    for t in (x / 20 for x in range(10, 61)):  # 0.5 .. 3.0, 步长 0.05
        v = nll(dev_logits, dev_labels, t)
        if v < best_nll:
            best_t, best_nll = t, v
    print(f"  calibrate: temperature={best_t:.2f} dev_nll={nll(dev_logits, dev_labels, 1.0):.4f} -> {best_nll:.4f}")

    # 标签映射 + 校准温度随权重落盘 (推理侧从训练产物读取, 不再手工同步)
    model.config.id2label = {i: c for i, c in enumerate(CLASSES)}
    model.config.label2id = {c: i for i, c in enumerate(CLASSES)}
    model.config.temperature = best_t
    save_dir.mkdir(parents=True, exist_ok=True)
    tokenizer.save_pretrained(save_dir)
    model.save_pretrained(save_dir)
    (save_dir / "calibration.json").write_text(
        json.dumps(
            {
                "temperature": best_t,
                "dev_accuracy": dev_acc_of_final(model, tokenizer, device, dev_rows),
                "dev_nll_temperature_1": nll(dev_logits, dev_labels, 1.0),
                "dev_nll_calibrated": best_nll,
                "dev_size": len(dev_rows),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    best_state_path.unlink(missing_ok=True)
    print(f"[train] saved -> {save_dir}")
    return model, tokenizer, device


def dev_acc_of_final(model, tokenizer, device, dev_rows: list[dict]) -> float:
    """校准后模型的 dev 精度 (写 calibration.json 用, 与 evaluate() 同口径)."""
    try:
        return evaluate(model, tokenizer, device, dev_rows)[0]
    except Exception:
        return 0.0


# ------------------------------------------------------------------ eval
def load_trained(save_dir: Path, model_name: str = MODEL_NAME):
    import torch
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    device = "mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu"
    tok = AutoTokenizer.from_pretrained(save_dir, trust_remote_code=True)
    m = AutoModelForSequenceClassification.from_pretrained(save_dir, trust_remote_code=True).to(device).eval()
    return m, tok, device


def predict_model(model, tokenizer, device, text: str) -> str:
    import torch

    enc = tokenizer(text, truncation=True, max_length=32, padding="max_length", return_tensors="pt")
    enc = {k: v.to(device) for k, v in enc.items()}
    with torch.no_grad():
        logits = model(**enc).logits
    return CLASSES[logits.argmax(-1).item()]


def evaluate(model, tokenizer, device, rows: list[dict]) -> tuple[float, float, list[str]]:
    try:
        from sklearn.metrics import accuracy_score, f1_score
    except Exception:

        def accuracy_score(g, p):
            return _vanilla_metrics(g, p)[0]

        def f1_score(g, p, average="macro", zero_division=0):
            return _vanilla_metrics(g, p, average, zero_division)[1]

    texts = [r["text"] for r in rows]
    preds = [predict_model(model, tokenizer, device, t) for t in texts]
    gts = [r["intent"] for r in rows]
    acc = accuracy_score(gts, preds)
    f1 = f1_score(gts, preds, average="macro", zero_division=0)
    return acc, f1, preds


def _vanilla_metrics(gts, preds, average="macro", zero_division=0):
    acc = sum(1 for g, p in zip(gts, preds, strict=False) if g == p) / len(gts)
    classes = set(gts) | set(preds)
    scores = []
    for c in classes:
        tp = sum(1 for g, p in zip(gts, preds, strict=False) if g == c and p == c)
        fp = sum(1 for g, p in zip(gts, preds, strict=False) if g != c and p == c)
        fn = sum(1 for g, p in zip(gts, preds, strict=False) if g == c and p != c)
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        scores.append(2 * p * r / (p + r) if p + r else 0.0)
    return acc, sum(scores) / len(scores)


def latency(model, tokenizer, device, texts: list[str], n: int = 200) -> tuple[float, float]:
    """返回 (单次推理均值 ms, 标准差 ms), 含 tokenize。"""
    # warmup
    for _ in range(10):
        predict_model(model, tokenizer, device, texts[0])
    times = []
    for i in range(n):
        t = texts[i % len(texts)]
        t0 = time.time()
        predict_model(model, tokenizer, device, t)
        times.append((time.time() - t0) * 1000)
    mean = sum(times) / len(times)
    var = sum((x - mean) ** 2 for x in times) / len(times)
    return mean, var**0.5


def rule_per_intent_report(rows: list[dict]) -> dict[str, tuple[int, int]]:
    """规则分类器在种子集上的逐类命中/总数。

    用途: _RULES 的 confidence 目前是拍脑袋常数 (0.85/0.9/0.95), 这里给出实测命中率,
    供人工决定是否回写 (命中率低于快路径阈值 0.7 的类在规则通道会被自然拦下走 BERT/LLM,
    虚高的类则会把误判直接放行 — 置信度应以实测为准)。
    """
    from collections import defaultdict

    clf = RuleClassifierReplica()
    hit: dict[str, int] = defaultdict(int)
    tot: dict[str, int] = defaultdict(int)
    for r in rows:
        tot[r["intent"]] += 1
        if clf.classify(r["text"]) == r["intent"]:
            hit[r["intent"]] += 1
    return {k: (hit[k], tot[k]) for k in tot}


# ------------------------------------------------------------------ main
def rule_metrics(rows: list[dict]) -> tuple[float, float, list[str]]:
    clf = RuleClassifierReplica()
    preds = [clf.classify(r["text"]) for r in rows]
    gts = [r["intent"] for r in rows]
    try:
        from sklearn.metrics import accuracy_score, f1_score

        acc = accuracy_score(gts, preds)
        f1 = f1_score(gts, preds, average="macro", zero_division=0)
    except Exception:
        acc, f1 = _vanilla_metrics(gts, preds)
    return acc, f1, preds


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-train", action="store_true", help="跳过训练, 直接加载已保存模型评估")
    ap.add_argument("--epochs", type=int, default=6)
    ap.add_argument("--patience", type=int, default=2, help="dev 精度连续 N 轮未提升即早停")
    ap.add_argument("--test-frac", type=float, default=0.2)
    ap.add_argument("--dev-frac", type=float, default=0.15)
    ap.add_argument("--weak-per-class", type=int, default=30)
    args = ap.parse_args()

    examples, confusable = load_data()
    train_rows, test_rows = stratified_split(examples, args.test_frac, seed=0)
    train_rows, dev_rows = stratified_split(train_rows, args.dev_frac, seed=0)

    # 规则模板弱标签合成 (仅进训练集, 不进 dev/test — 合成样本与规则同分布,
    # 进验证集会虚抬早停/校准指标); 多轮上下文样本同理只进训练集。
    synth = gen_weak_labels(per_class=args.weak_per_class)
    hist = gen_history_samples()
    train_rows = train_rows + synth + hist
    print(
        f"[data] seeds={len(examples)} train={len(train_rows)}(+{len(synth)} weak +{len(hist)} multi-turn) "
        f"dev={len(dev_rows)} test={len(test_rows)} confusable={len(confusable)}"
    )

    # 规则逐类命中率报告: 供人工回写 _RULES 置信度 (替代拍脑袋常数)
    print("\n==================== 规则逐类命中率 (建议置信度) ====================")
    for intent, (hit, tot) in sorted(rule_per_intent_report(examples).items()):
        acc = hit / tot
        print(f"  {intent:<22} {hit:>3}/{tot:<3} acc={acc:.2f} 建议置信度={max(0.5, min(0.95, round(acc, 2))):.2f}")

    if args.skip_train:
        model, tokenizer, device = load_trained(OUT_DIR)
    else:
        model, tokenizer, device = train_model(
            train_rows, dev_rows, OUT_DIR, epochs=args.epochs, patience=args.patience
        )

    print("\n==================== 留出集 (test) ====================")
    t0 = time.time()
    model_test_acc, model_test_f1, model_preds = evaluate(model, tokenizer, device, test_rows)
    model_test_time = time.time() - t0
    r_acc, r_f1, r_preds = rule_metrics(test_rows)
    print(
        f" 小BERT : acc={model_test_acc:.3f} macroF1={model_test_f1:.3f}  实际上推理{model_test_time:.1f}s(n={len(test_rows)})"
    )
    print(f" Rule   : acc={r_acc:.3f} macroF1={r_f1:.3f}")
    for i, (gt, mp, rp) in enumerate(zip([r["intent"] for r in test_rows], model_preds, r_preds, strict=False)):
        mark = "  <" if (mp != gt or rp != gt) else ""
        if mp != gt or rp != gt:
            print(f"    [{test_rows[i]['text']}] gt={gt} | bert={mp} rule={rp}{mark}")

    print("\n==================== 易混淆对 (confusable) ====================")
    cc = [{"text": c["text"], "intent": c["correct"]} for c in confusable]
    b_acc, b_f1, _ = evaluate(model, tokenizer, device, cc)
    r_acc2, r_f1_2, _ = rule_metrics(cc)
    print(
        f" 小BERT : acc={b_acc:.3f} macroF1={b_f1:.3f}  (命中 {sum(1 for c,i in zip(cc,b'', strict=False) if True) })"
    )
    for c in confusable:
        mp = predict_model(model, tokenizer, device, c["text"])
        rp = RuleClassifierReplica().classify(c["text"])
        okb = "OK" if mp == c["correct"] else "X "
        okr = "OK" if rp == c["correct"] else "X "
        print(f"    [{okb}|{okr}] '{c['text']}' -> bert={mp:<16} rule={rp:<16} (期望={c['correct']})")

    print("\n==================== 推理延迟 (均值 ms, n=200) ====================")
    texts = [r["text"] for r in train_rows]
    mb = latency(model, tokenizer, device, texts)
    print(f" 小BERT : {mb[0]:.2f} ms ± {mb[1]:.2f}")
    clf_rule = RuleClassifierReplica()
    t0 = time.time()
    for _ in range(2000):
        clf_rule.classify(texts[_ % len(texts)])
    r_lat = (time.time() - t0) / 2000 * 1000
    print(f" Rule   : {r_lat:.4f} ms (纯正则, 快约 {mb[0]/r_lat:.0f}x)")


if __name__ == "__main__":
    main()
