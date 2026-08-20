"""轻量 BERT 意图分类器 (torch 推理快路径)。

微调自 uer/chinese_roberta_L-4_H-512 (24M), 对 10 类封闭意图做单标签分类。
本模块**不在模块级 import torch/transformers** —— 这是刻意为之:

- 主 venv / CI / 大量单元测试不加载 torch, 避免引入几百 MB 依赖与慢导入;
- 模型加载与推理都推迟到首次 classify() 用时进行, 且推理经 asyncio.to_thread
  扔出事件循环, 防止阻塞 bot 的 async 处理。

设计为 IntentClassifier 的快路径 (取代规则): 置信度 (softmax 概率) >= 阈值直接用,
不足由上层回退 LLM 慢路径。加载/推理任何异常向上抛, 由上层兜底回退规则, 打不挂线上。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from lumio.shared.models import IntentLabel, IntentResult

logger = logging.getLogger(__name__)

# 与训练脚本 intent_classifier_spike.py 的 IDX 顺序严格一致 —— 标签序号 -> IntentLabel
_CLASSES: list[IntentLabel] = [
    IntentLabel.FAQ,
    IntentLabel.BILL_QUERY,
    IntentLabel.TRANSACTION_QUERY,
    IntentLabel.LIMIT_QUERY,
    IntentLabel.INSTALLMENT_INQUIRY,
    IntentLabel.REWARD_QUERY,
    IntentLabel.CARD_LOSS,
    IntentLabel.COMPLAINT,
    IntentLabel.TRANSFER_AGENT,
    IntentLabel.CHITCHAT,
]
_IDX_TO_LABEL: dict[int, IntentLabel] = {i: lbl for i, lbl in enumerate(_CLASSES)}

# 本文件位于 lumio/services/common/ 下, 上溯 3 级即 agent/ 根
_AGENT_ROOT = Path(__file__).resolve().parents[3]


def _resolve_model_path(model_path: str) -> str:
    """相对路径以 agent/ 为基准解析为绝对路径。"""
    p = Path(model_path)
    return str(p if p.is_absolute() else _AGENT_ROOT / p)


class BertIntentClassifier:
    """基于微调小 BERT 的意图分类器, 懒加载 + 线程化推理。"""

    def __init__(self, model_path: str) -> None:
        self._model_path: str = _resolve_model_path(model_path)
        self._model: Any = None
        self._tokenizer: Any = None
        self._device: str = "cpu"
        self._lock = asyncio.Lock()

    async def classify(self, text: str) -> IntentResult:
        """对单条文本分类, 返回含 softmax 置信度的 IntentResult。"""
        if self._model is None:
            await self._load()
        return await asyncio.to_thread(self._predict, text)

    async def _load(self) -> None:
        """首次使用时加载模型与分词器 (在线程池中执行, 避免阻塞事件循环)。"""
        async with self._lock:
            if self._model is not None:
                return
            try:
                self._model, self._tokenizer, self._device = await asyncio.to_thread(self._load_sync)
                logger.info("BERT 意图分类器加载完成 model=%s device=%s", self._model_path, self._device)
            except Exception:
                logger.exception("BERT 意图分类器加载失败 model=%s (上层将回退规则)", self._model_path)
                raise

    def _load_sync(self) -> tuple:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        device = (
            "mps" if torch.backends.mps.is_available()
            else "cuda" if torch.cuda.is_available()
            else "cpu"
        )
        tokenizer = AutoTokenizer.from_pretrained(self._model_path)
        model = AutoModelForSequenceClassification.from_pretrained(self._model_path).to(device).eval()
        return model, tokenizer, device

    def _predict(self, text: str) -> IntentResult:
        import torch

        enc = self._tokenizer(text, truncation=True, max_length=32, padding="max_length", return_tensors="pt").to(
            self._device
        )
        with torch.no_grad():
            logits = self._model(**enc).logits[0]
        probs = torch.softmax(logits, dim=-1)
        idx = int(probs.argmax(dim=-1))
        conf = float(probs[idx])
        return IntentResult(primary_intent=_IDX_TO_LABEL[idx], primary_confidence=conf)
