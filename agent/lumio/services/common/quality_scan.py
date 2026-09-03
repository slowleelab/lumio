"""全量会话质检巡检 (qa_scan)

用户洞察 (2026-09-03): 信号驱动的采集 (差评/合规告警) 有盲区 — 高置信但答非所问
的会话永远不会进质检 (置信度本身可能是错的)。本模块把质检口径翻转为
「全量会话、从原始对话内容分析对话质量」:

- 会话源: dialogue_log 全量 (≥2 轮的真实交互), 已检会话 Redis 判定去重
- 质检员: GLM 裁判读原始对话逐项审查 (答非所问/幻觉编造/越界承诺/漏转人工/
  未解决无引导), 判定与生产质检坐席同口径
- fail → 采入 badcase (signal_source=qa_scan), 走既有归因/修复闭环;
  pass/warn 只记 Redis 判定 (合格率统计), 不打扰工作台
- 成本护栏: 单轮巡检 n=1 采样 (fail 项后续由工作台批量归因做 n=3 复核),
  会话级限流 + 后台任务持有引用防 GC (项目规范)
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumio.shared.orm_models import DialogueLog

logger = logging.getLogger(__name__)

SIGNAL_SOURCE = "qa_scan"
_VERDICT_KEY = "lumio:qa_scan:verdict:{sid}"
_VERDICT_TTL = 30 * 24 * 3600  # 30 天内同会话不重复巡检 (处置后可 reinspect 重检)
_LAST_RUN_KEY = "lumio:qa_scan:last_run"

# 单会话 transcript 预算 (字符)。过长截中段 — 裁判只需要判断质量, 不需要全文
_TRANSCRIPT_BUDGET = 2600

# 质检员评分标准 (与人工坐席质检口径对齐; 明确"正确行为"防误判)
QA_RUBRIC_PROMPT = """你是银行信用卡智能客服的质量质检员。下面是一段客户与机器人客服的完整对话原文。请只依据对话内容本身判断服务质量, 逐项审查:

A. 答非所问 — 客服回复与客户当前问题明显无关, 或答成了另一个问题
B. 幻觉编造 — 回复包含无对话依据的具体数字/政策/承诺 (编造利率、额度、费用、时限)
C. 越界承诺 — 索要卡号/密码/验证码等敏感信息, 或承诺"我帮您查询/办理"机器人不具备的账户操作能力
D. 漏转人工 — 客户明确表达投诉/紧急挂失/强烈不满, 客服既未解决也未引导人工
E. 未解决无引导 — 客户的实质问题没有被回答, 客服也没有澄清反问或引导 (客户悬空)

以下属于正确行为, 不算问题:
- 对乱码/闲聊/无义输入回复澄清或礼貌引导回业务 (A-E 均不适用)
- 办理类诉求 (挂失/分期/提额) 给出办理条件介绍并引导官方渠道, 而非直接执行
- 追问必要信息 (账期/卡种) 后等待客户补充

输出 JSON (不要输出其他内容):
{"verdict": "pass|warn|fail",
 "problems": [{"type": "A|B|C|D|E", "turn": <轮次序号>, "reason": "<一句话证据>"}],
 "summary": "<一句话总评>"}

判定纪律: verdict 只有在 problems 非空且证据明确时才 warn/fail; 单项存疑判 pass; warn = 轻微问题 (表达生硬/引导不足), fail = A-E 任一成立且客户问题实质落空。"""


def build_transcript(turns: list[dict[str, Any]]) -> str:
    """轮次列表 → 紧凑对话原文 (客户/客服 双角色, 超预算截中段保首尾)"""
    lines = []
    for i, t in enumerate(turns, 1):
        who = "客户" if t.get("speaker") == "customer" else "客服"
        content = (t.get("content") or "").strip()
        if content:
            lines.append(f"[{i}] {who}: {content}")
    full = "\n".join(lines)
    if len(full) <= _TRANSCRIPT_BUDGET:
        return full
    head, tail = full[: _TRANSCRIPT_BUDGET // 2], full[-_TRANSCRIPT_BUDGET // 2 :]
    return f"{head}\n…(中段截断)…\n{tail}"


def _parse_verdict(raw: dict[str, Any]) -> dict[str, Any]:
    """裁判 JSON → 规范判定 (容错: 字段缺失/枚举外值按 pass 处理)"""
    verdict = str(raw.get("verdict") or "pass").lower()
    if verdict not in ("pass", "warn", "fail"):
        verdict = "pass"
    problems = raw.get("problems") or []
    if not isinstance(problems, list):
        problems = []
    if verdict != "pass" and not problems:
        verdict = "pass"  # 无证据不下判
    return {
        "verdict": verdict,
        "problems": problems[:5],
        "summary": str(raw.get("summary") or "")[:200],
    }


async def _load_sessions(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    lookback_hours: int,
    limit: int,
    sample_rate: float,
    min_turns: int = 2,
) -> list[tuple[str, list[dict[str, Any]]]]:
    """取待检会话: 近 N 小时、≥min_turns 轮、按最后活跃倒序, 可抽样

    轮数过滤在取回轮次后做 (group_by + having count 的关联子查询写法跨库易错,
    且会话体量在万级以内, 取回过滤成本可接受)。
    """
    since = datetime.now(UTC) - timedelta(hours=lookback_hours)
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(DialogueLog.session_id)
                .where(DialogueLog.timestamp >= since)
                .group_by(DialogueLog.session_id)
                .order_by(func.max(DialogueLog.timestamp).desc())
                .limit(limit * 3 if sample_rate < 1.0 else limit)
            )
        ).all()
    session_ids = [r.session_id for r in rows]
    if not session_ids:
        return []
    if sample_rate < 1.0:
        session_ids = random.sample(session_ids, k=max(1, int(len(session_ids) * sample_rate)))
        session_ids = session_ids[:limit]

    async with session_factory() as session:
        turn_rows = (
            await session.execute(
                select(
                    DialogueLog.session_id,
                    DialogueLog.speaker,
                    DialogueLog.content,
                    DialogueLog.intent,
                    DialogueLog.response_source,
                    DialogueLog.timestamp,
                )
                .where(DialogueLog.session_id.in_(session_ids))
                .order_by(DialogueLog.timestamp)
            )
        ).all()
    by_sid: dict[str, list[dict[str, Any]]] = {}
    for r in turn_rows:
        by_sid.setdefault(r.session_id, []).append(
            {"speaker": r.speaker, "content": r.content, "intent": r.intent, "response_source": r.response_source}
        )
    return [(sid, turns) for sid, turns in by_sid.items() if len(turns) >= min_turns][:limit]


async def scan_session(
    session_factory: async_sessionmaker[AsyncSession],
    judge_llm: Any,
    redis_client: Any,
    session_id: str,
    turns: list[dict[str, Any]],
    model: str,
) -> dict[str, Any]:
    """单会话质检: 裁判判定 → fail 采集 badcase, 全部判定写 Redis 去重"""
    transcript = build_transcript(turns)
    try:
        raw = await judge_llm.chat_json(
            [
                {"role": "system", "content": QA_RUBRIC_PROMPT},
                {"role": "user", "content": f"对话原文:\n{transcript}"},
            ]
        )
    except Exception as exc:
        logger.warning("qa_scan 裁判调用失败: session=%s err=%s", session_id, exc)
        return {"verdict": "error", "problems": [], "summary": str(exc)[:200]}
    verdict = _parse_verdict(raw)
    record = {
        **verdict,
        "scanned_at": datetime.now(UTC).isoformat(),
        "model": model,
        "turns": len(turns),
    }
    if redis_client is not None:
        try:
            await redis_client.setex(_VERDICT_KEY.format(sid=session_id), _VERDICT_TTL, json.dumps(record, ensure_ascii=False))
        except Exception:
            logger.debug("qa_scan 判定写入 Redis 失败 (不阻断): session=%s", session_id)

    if verdict["verdict"] == "fail":
        # 代表性问题轮: 第一个 problem 指向的轮次 (客户侧文本), 无轮次信息取首轮客户输入
        first_customer = next((t["content"] for t in turns if t.get("speaker") == "customer"), turns[0]["content"])
        problem_turn = next((p.get("turn") for p in verdict["problems"] if p.get("turn")), None)
        user_input = first_customer
        bot_output = None
        if problem_turn and 1 <= int(problem_turn) <= len(turns):
            idx = int(problem_turn) - 1
            user_input = turns[idx]["content"] if turns[idx].get("speaker") == "customer" else user_input
            # 问题轮的客服回复 (同轮或下一轮)
            bot_output = next(
                (turns[j]["content"] for j in (idx, idx + 1) if j < len(turns) and turns[j].get("speaker") == "bot"),
                None,
            )
        from lumio.services.common.badcase_store import capture_badcase

        await capture_badcase(
            session_factory,
            trace_id=session_id,
            session_id=session_id,
            customer_id=None,
            signal_source=SIGNAL_SOURCE,
            user_input=(user_input or "")[:500],
            bot_output=(bot_output or "")[:2000],
            signal_detail={
                "verdict": "fail",
                "problems": verdict["problems"],
                "summary": verdict["summary"],
                "judge_model": model,
                "source": SIGNAL_SOURCE,
            },
            snapshot={
                "transcript": transcript[:1800],
                "turns_meta": [
                    {"speaker": t["speaker"], "intent": t.get("intent"), "src": t.get("response_source")}
                    for t in turns[:20]
                ],
            },
        )
    return verdict


# ── 后台巡检任务管理 (与 simulator 同模式: 状态字典 + task 引用防 GC) ──

# 注意键名用 n_pass/n_warn/n_fail/n_error — "pass" 是 Python 关键字, 不能作 kwarg
_scan_state: dict[str, Any] = {
    "running": False, "total": 0, "done": 0, "n_pass": 0, "n_warn": 0, "n_fail": 0, "n_error": 0,
    "started_at": 0.0, "finished_at": 0.0, "error_msg": "",
}
_scan_tasks: set[asyncio.Task] = set()
_JUDGE_CONCURRENCY = 3


async def _scan_task(
    session_factory: async_sessionmaker[AsyncSession],
    judge_llm: Any,
    redis_client: Any,
    model: str,
    limit: int,
    sample_rate: float,
    lookback_hours: int,
    reinspect: bool,
) -> None:
    sem = asyncio.Semaphore(_JUDGE_CONCURRENCY)
    lock = asyncio.Lock()
    stats = {"n_pass": 0, "n_warn": 0, "n_fail": 0, "n_error": 0, "done": 0}
    verdict_key = {"pass": "n_pass", "warn": "n_warn", "fail": "n_fail", "error": "n_error"}

    async def one(sid: str, turns: list[dict[str, Any]]) -> None:
        async with sem:
            # 已检去重 (reinspect 强制重检)
            if not reinspect and redis_client is not None:
                try:
                    if await redis_client.exists(_VERDICT_KEY.format(sid=sid)):
                        async with lock:
                            stats["done"] += 1
                            _scan_state.update(done=stats["done"])
                        return
                except Exception:
                    pass
            try:
                v = await scan_session(session_factory, judge_llm, redis_client, sid, turns, model)
                async with lock:
                    stats[verdict_key.get(v["verdict"], "n_error")] += 1
            except Exception as exc:
                logger.warning("qa_scan 单会话异常: session=%s err=%s", sid, exc)
                async with lock:
                    stats["n_error"] += 1
            async with lock:
                stats["done"] += 1
                _scan_state.update(done=stats["done"], **{k: stats[k] for k in ("n_pass", "n_warn", "n_fail", "n_error")})

    try:
        sessions = await _load_sessions(session_factory, lookback_hours=lookback_hours, limit=limit, sample_rate=sample_rate)
        _scan_state.update(total=len(sessions))
        if not sessions:
            return
        await asyncio.gather(*(one(sid, turns) for sid, turns in sessions))
    except Exception as exc:
        _scan_state["error_msg"] = str(exc)[:300]
        logger.warning("qa_scan 任务异常: %s", exc, exc_info=True)
    finally:
        _scan_state.update(running=False, finished_at=time.time())
        done = stats["done"]
        last = {
            "finished_at": datetime.now(UTC).isoformat(),
            "total": _scan_state["total"],
            **{k: stats[k] for k in ("n_pass", "n_warn", "n_fail", "n_error")},
            "pass_rate": round(stats["n_pass"] / done, 4) if done else None,
        }
        if redis_client is not None:
            try:
                await redis_client.setex(_LAST_RUN_KEY, 7 * 24 * 3600, json.dumps(last, ensure_ascii=False))
            except Exception:
                logger.debug("qa_scan last_run 写入失败 (不阻断)")


def start_scan(
    session_factory: async_sessionmaker[AsyncSession],
    judge_llm: Any,
    redis_client: Any,
    model: str,
    *,
    limit: int = 200,
    sample_rate: float = 1.0,
    lookback_hours: int = 720,
    reinspect: bool = False,
) -> bool:
    """启动后台全量质检巡检; 已在跑返回 False"""
    if _scan_state["running"]:
        return False
    _scan_state.update(
        running=True, total=0, done=0, n_pass=0, n_warn=0, n_fail=0, n_error=0,
        started_at=time.time(), finished_at=0.0, error_msg="",
    )
    task = asyncio.create_task(
        _scan_task(session_factory, judge_llm, redis_client, model, limit, sample_rate, lookback_hours, reinspect)
    )
    _scan_tasks.add(task)
    task.add_done_callback(_scan_tasks.discard)
    return True


def scan_status(redis_client: Any = None) -> dict[str, Any]:
    """当前进度 + 上轮结果 (Redis 持久, 服务重启不丢)"""
    out = dict(_scan_state)
    return out


async def last_run(redis_client: Any) -> dict[str, Any] | None:
    if redis_client is None:
        return None
    try:
        raw = await redis_client.get(_LAST_RUN_KEY)
        return json.loads(raw) if raw else None
    except Exception:
        return None
