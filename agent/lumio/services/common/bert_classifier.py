"""轻量 BERT 意图分类器 (torch 推理快路径)。

微调自 uer/chinese_roberta_L-4_H-512 (24M), 对 10 类封闭意图做单标签分类。
本模块**不在模块级 import torch/transformers** —— 这是刻意为之:

- 主 venv / CI / 大量单元测试不加载 torch, 避免引入几百 MB 依赖与慢导入;
- 模型加载与推理都推迟到首次 classify() 用时进行, 且推理经 asyncio.to_thread
  扔出事件循环, 防止阻塞 bot 的 async 处理。

设计为 IntentClassifier 的快路径 (取代规则): 置信度 (softmax 概率) >= 阈值直接用,
不足由上层回退 LLM 慢路径。加载/推理任何异常向上抛, 由上层兜底回退规则, 打不挂线上。

多轮与多意图 (draft-0.2 第一批):
- classify 接受可选 history (最近对话轮次), 以角色标记拼接进输入, 让模型在"上轮再说分期、
  这轮"手续费怎么收""时能借上下文落在分期域, 而非当全新单句判成 FAQ。
- 输出 top-K (softmax 次高分别作 alternatives) 填回 IntentResult.alternatives, 支撑"一句多诉求"
  的次意图优先路由 (如"卡丢了顺便查最后一笔消费"挂失主 + 交易次)。与训练脚本保持 top-K 一致。
"""

from __future__ import annotations

import asyncio
import logging
import math
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

# 单个 softmax 解码时作为"次意图"带出的 top-K (多意图). 1 即只出主意图(旧行为).
_TOP_K = 3

# 多轮上下文拼接上限 (字符). 超出按最新优先裁, 防止超 max_length 巨量截断吃掉当前句.
_MAX_HISTORY_CHARS = 256

# 角色标记: 让 BERT 区分"用户这句"与"机器人上句", 提升对话级指代消解表现.
_ROLE_TAG = {"customer": "用户", "bot": "客服"}

# 本文件位于 lumio/services/common/ 下, 上溯 3 级即 agent/ 根
_AGENT_ROOT = Path(__file__).resolve().parents[3]


def _resolve_model_path(model_path: str) -> str:
    """相对路径以 agent/ 为基准解析为绝对路径。"""
    p = Path(model_path)
    return str(p if p.is_absolute() else _AGENT_ROOT / p)


# ── P1 提纯"认不认": energy-based OOD + 温度校准 (纯函数, 无 torch 依赖可单测) ──
def logit_energy(logits: list[float]) -> float:
    """负 log-sum-exp: energy = -log Σ exp(z_i).

    封闭意图集内的强判定 → 某个 logit 明显最大 → energy 显著低(趋近 -max logit);
    OOD/乱码 → logits 分散、互相抵消 → energy 偏高(数值更接近 0)。
    保留 max 减缩避免 exp 上溢; 数值上等同 torch.logsumexp 的相反数。
    """
    if not logits:
        return 0.0
    m = max(logits)
    return -(m + math.log(sum(math.exp(x - m) for x in logits)))


def calibrate_logits(logits: list[float], temperature: float) -> list[float]:
    """温度缩放: logits / temperature 后再 softmax. temperature=1.0 时原样返回(不缩放).

    用验证集一维标定 temperature>1 压低过拟合小 BERT 的虚高置信, 使其与准确率对齐。
    该函数只作用于置信度路径; energy(不认通道)仍取原始 logits, 与校准解耦。
    """
    if temperature is None or temperature <= 0.0 or temperature == 1.0:
        return list(logits)
    return [x / temperature for x in logits]


def ood_verdict(
    energy: float,
    threshold: float,
    ambiguous_band: float | None = 0.0,
) -> str:
    """把 energy 映射到三态"认不认": "known" | "ambiguous" | "unknown".

    约定(energy-based OOD, Liu 2020): energy 低 → 自信(封闭集内可信 known);
    energy 高 → 倾向 OOD/噪声(unknown)。threshold 即"认/不认"分界(需按部署集标定):
    - energy < threshold - band → known (自信, BERT 可信)
    - threshold - band <= energy <= threshold + band → ambiguous (模糊带宽, 交 LLM 仲裁)
    - energy > threshold + band → unknown (OOD/噪声)
    band 用于"模糊带宽仲裁": 在阈值两侧各留一段给"吃不准"的样本。
    """
    if energy < threshold - ambiguous_band:
        return "known"
    if energy <= threshold + ambiguous_band:
        return "ambiguous"
    return "unknown"


class BertIntentClassifier:
    """基于微调小 BERT 的意图分类器, 懒加载 + 线程化推理。"""

    def __init__(self, model_path: str, temperature: float | None = None) -> None:
        self._model_path: str = _resolve_model_path(model_path)
        self._temperature: float | None = temperature
        self._model: Any = None
        self._tokenizer: Any = None
        self._device: str = "cpu"
        self._lock = asyncio.Lock()

    async def classify(self, text: str, history: list[dict[str, str]] | None = None) -> IntentResult:
        """对单条文本分类, 返回含 softmax 置信度的 IntentResult + 次意图 alternatives。

        Args:
            text: 用户当前输入 (单句或用户消息)
            history: 可选多轮上下文, 元素为 {"speaker": "customer"|"bot", "content": "..."},
                按时间升序。提供时以角色标记拼接进输入, 供模型借上下文判定意图/指代。

        Returns:
            IntentResult, 其中 primary_intent 为主意图, alternatives 为次意图 (top-K 含主)。
        """
        if self._model is None:
            await self._load()
        dialog_input = self._build_dialog_input(text, history)
        return await asyncio.to_thread(self._predict, dialog_input)

    async def ood_score(self, text: str, history: list[dict[str, str]] | None = None) -> float:
        """energy-based OOD 分 (P1): -logsumexp(logits), 供上层用作"认不认"闸。

        约定(Liu 2020): energy 越低(越负) = 封闭集内强判定越"认"known; energy 越高
        (向 0 收敛) = logits 分散趋 OOD/乱码。上层以 ood_verdict 的 threshold 分界裁
        known/ambiguous/unknown, 不是"值大小"直接对应认不认。
        与 classify 共享一次前向, 复用 dialog_input 拼接, 不重复 tokenize。
        """
        if self._model is None:
            await self._load()
        dialog_input = self._build_dialog_input(text, history)
        return await asyncio.to_thread(self._predict_energy, dialog_input)

    def _predict_energy(self, dialog_input: str) -> float:
        import torch

        enc = self._tokenizer(
            dialog_input, truncation=True, max_length=128, padding="max_length", return_tensors="pt"
        ).to(self._device)
        with torch.no_grad():
            logits = self._model(**enc).logits[0]
        return logit_energy(logits.tolist())

    @staticmethod
    def _build_dialog_input(text: str, history: list[dict[str, Any]] | None) -> str:
        """把历史轮次拼成 BERT 输入串 (方案: 文本上下文拼接)。

        仅取 customer/bot 双方的消息, 逆时间拼接 (最新轮在最前, 越近越关键),
        总长受 _MAX_HISTORY_CHARS 约束, 保证当前句 text 不被挤压。

        乱码轮过滤: bot 以 clarify 收尾的轮对 (上一句 customer 是本轮 clarify 的对象)
        不进上下文 —— 乱码历史会把真实意图的置信度拖低 (实测同句"分期"背着乱码历史
        conf 0.30, 背着自己上一轮 0.62), 且澄清轮没有任何语义信息可借。
        """
        if not history:
            return text
        # 正序遍历标记被澄清的轮对 (customer 乱码句 + 其后 clarify 的 bot 句)
        excluded: set[int] = set()
        for i in range(1, len(history)):
            prev, cur = history[i - 1], history[i]
            if (
                cur.get("speaker") == "bot"
                and cur.get("response_source") == "clarify"
                and prev.get("speaker") == "customer"
            ):
                excluded.add(i - 1)
                excluded.add(i)
        buf: list[str] = []
        used = 0
        # 从最近往回扫, 只带最关键的历史
        for idx in range(len(history) - 1, -1, -1):
            if idx in excluded:
                continue
            turn = history[idx]
            speaker = _ROLE_TAG.get(turn.get("speaker", ""))
            content = (turn.get("content") or "").strip()
            if not speaker or not content:
                continue
            seg = f"{speaker}:{content}"
            if used + len(seg) > _MAX_HISTORY_CHARS:
                break
            buf.append(seg)
            used += len(seg)
            if len(buf) >= 6:
                break
        if not buf:
            return text
        return "".join(reversed(buf)) + "\n" + text

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

    async def preload(self) -> None:
        """启动期主动预热: 加载模型, 避免首个需 BERT 的请求落在事件循环里冷加载
        (冷加载 + 慢分类时可达 9s+, 见延迟治理)。幂等, 加载失败只记日志不阻断启动。"""
        try:
            await self._load()
        except Exception:
            logger.warning("BERT 预加载失败, 首个请求将惰性加载")

    def _load_sync(self) -> tuple:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        device = "mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu"
        tokenizer = AutoTokenizer.from_pretrained(self._model_path)
        model = AutoModelForSequenceClassification.from_pretrained(self._model_path).to(device).eval()
        return model, tokenizer, device

    def _predict(self, dialog_input: str) -> IntentResult:
        import torch

        enc = self._tokenizer(
            dialog_input, truncation=True, max_length=128, padding="max_length", return_tensors="pt"
        ).to(self._device)
        with torch.no_grad():
            logits = self._model(**enc).logits[0]
        # P1 温度校准: 仅在置信度路径应用, energy 通道(_predict_energy)仍取原始 logits.
        if self._temperature is not None and self._temperature > 0.0 and self._temperature != 1.0:
            logits = logits / self._temperature
        probs = torch.softmax(logits, dim=-1)
        top_idx = probs.topk(min(_TOP_K, len(probs))).indices.tolist()
        labels = [_IDX_TO_LABEL[int(i)] for i in top_idx]
        conf = float(probs[top_idx[0]])
        return IntentResult(
            primary_intent=labels[0],
            primary_confidence=conf,
            alternatives=[lbl for lbl in labels if lbl != labels[0]],
        )
