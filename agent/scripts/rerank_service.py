"""本地 cross-encoder 重排服务 (TEI 兼容契约)

生产级重排应为专门的 cross-encoder 通过批量 /rerank 接口打分，而非文本模型+正则抠分。
本服务用 sentence-transformers 加载 bge-reranker-v2-m3，暴露与 TEI /rerank 相同的一等契约，
这样应用的 TEIReranker 无需改接口即可接入。

接口:
    POST /rerank   body={"query": str, "texts": [str], "top_n": int, "raw_scores": bool}
                   响应 [{"index": int, "score": float, "text": str}, ...] 按 score 降序
    GET  /health   {"status": "ok", "model": str}

打分语义: cross-encoder 对 (query, doc) 输出原始 logit（无界）。默认将 top_n 候选做
min-max 归一到 0~1（最优候选≈1），使应用侧的 `score >= confidence_threshold` (默认 0.5)
阈值可比较且不会因候选数缩放而自动置空。需原始分时置 raw_scores=true。

模型来源: bge-reranker-v2-m3 的 safetensors 经 hf-mirror 拉取（本机 HF 直连被墙）；
通过环境变量 HF_ENDPOINT=https://hf-mirror.com 启用。

用法 (独立 venv, 本脚本不依赖 lumio 包):
    HF_ENDPOINT=https://hf-mirror.com .rerank-venv/bin/python scripts/rerank_service.py \
        --model BAAI/bge-reranker-v2-m3 --port 8080
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import threading

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

logger = logging.getLogger("rerank_service")

_model_lock = threading.Lock()
_model = None
_model_name = ""
_model_id = ""


class RerankRequest(BaseModel):
    query: str
    texts: list[str]
    top_n: int | None = None
    raw_scores: bool = False


class HealthResponse(BaseModel):
    status: str
    model: str


app = FastAPI(title="Lumio Rerank Service", version="1.0")


def _load_model() -> tuple:
    """懒加载 cross-encoder（加锁避免并发重复加载）"""
    global _model, _model_name, _model_id
    with _model_lock:
        if _model is None:
            from sentence_transformers import CrossEncoder

            logger.info("加载 cross-encoder: %s", _model_id)
            _model = CrossEncoder(_model_id)
            _model_name = _model_id
        return _model, _model_name


def _minmax(values: list[float]) -> list[float]:
    """将一批原始分 min-max 归一到 0~1（最优候选≈1，最差≈0）

    cross-encoder 原始分无界，且会随候选数缩放射（softmax 下最优分随 N 增大而变小，
    与应用侧固定 0.5 阈值冲突）。min-max 对最优候选恒为 1，阈值过滤更稳定。
    """
    lo, hi = min(values), max(values)
    if hi <= lo:  # 全部相等 → 无法区分，统一置 1 避免除零
        return [1.0] * len(values)
    return [(v - lo) / (hi - lo) for v in values]


@app.post("/rerank")
async def rerank(req: RerankRequest) -> list[dict]:
    if not req.texts:
        return []
    model, _ = _load_model()
    top_n = req.top_n if req.top_n is not None else len(req.texts)
    top_n = max(1, min(top_n, len(req.texts)))

    # cross-encoder 打分是 CPU 密集操作，用线程池避免阻塞事件循环
    pairs = [(req.query, text) for text in req.texts]
    try:
        scores = await asyncio.to_thread(model.predict, pairs)
    except Exception as exc:  # pragma: no cover - 上游推理异常
        logger.exception("cross-encoder 推理失败")
        raise HTTPException(status_code=500, detail=f"rerank 推理失败: {exc}") from exc

    scores = [float(s) for s in scores]
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)[:top_n]
    out_scores = [scores[i] for i in order] if req.raw_scores else _minmax([scores[i] for i in order])
    return [{"index": i, "score": round(s, 6), "text": req.texts[i]} for i, s in zip(order, out_scores, strict=True)]


@app.get("/health")
async def health() -> HealthResponse:
    global _model_name
    return HealthResponse(status="ok", model=_model_name or "not_loaded")


def main() -> None:
    global _model_id
    parser = argparse.ArgumentParser(description="Lumio cross-encoder rerank service")
    parser.add_argument("--model", default="BAAI/bge-reranker-v2-m3", help="模型名")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    _model_id = args.model
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(app, host=args.host, port=args.port, workers=args.workers, log_level="info")


if __name__ == "__main__":
    main()
