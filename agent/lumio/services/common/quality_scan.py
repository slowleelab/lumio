"""全量会话质检巡检 (qa_scan)

用户洞察 (2026-09-03): 信号驱动的采集 (差评/合规告警) 有盲区 — 高置信但答非所问
的会话永远不会进质检 (置信度本身可能是错的)。本模块把质检口径翻转为
「全量会话、从原始对话内容分析对话质量」:

- 会话源: dialogue_log 全量 (≥2 轮的真实交互), 已检会话 Redis 判定去重
- 质检员: GLM 裁判读原始对话逐项审查 (答非所问/幻觉编造/越界承诺/漏转人工/
  未解决无引导), 判定与生产质检坐席同口径
- 全量判定 (pass/warn/fail) 落 quality_record 表 — 每一个会话都进工作台
  质检记录列表 (按会话时间倒序); 此前 pass/warn 只写 Redis (30 天 TTL),
  会话清单不可见, "全量纳入"无从谈起
- fail → 采入 badcase (signal_source=qa_scan), 走既有归因/修复闭环
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

from lumio.shared.orm_models import DialogueLog, QualityRecord

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
B. 幻觉编造 — 回复包含无对话依据的具体数字/政策/承诺。仅限"客户个人账户数据"
   (余额/账单金额/积分/额度): 客服报出客户账户的具体数字而无查询过程才算;
   金融产品的公开参数 (费率/期数/免息期/额度范围等) 是行业常识性信息, 客服
   基于知识回答属正常, 不得以"看不到依据"判编造 (闭环第十轮: 检索上下文含
   费率表 0.75%/月 被误判)
C. 越界承诺 — 索要卡号/密码/验证码等敏感信息, 或承诺"我帮您查询/办理"机器人不具备的账户操作能力
D. 漏转人工 — 客户明确表达投诉/紧急挂失/强烈不满, 客服既未解决也未引导人工
E. 未解决无引导 — 客户的实质问题没有被回答, 客服也没有澄清反问或引导 (客户悬空)

以下属于正确行为, 不算问题:
- 声明"已为您转接人工/专员将为您服务"是转接流程的标准话术 (后端会完成坐席
  派发), 不构成越界承诺 — 客服机器人有权发起转接
- 机器人具备账户查询能力 (经工具链路): 直接返回账单金额/额度/积分余额等
  具体账户数据属正常服务, 不构成编造或越界 — 产品口径: 客户经渠道登录即
  视为身份已核实, 无需在对话中重复核验 (2026-09-03 产品决策)
- 机器人具备挂失等敏感操作办理能力 (经工具直连): "挂失已受理, 受理编号
  LS-xxx" 是工具回执的真实执行结果, 不是虚构 — 产品口径: 审核核实默认
  视为已通过, 紧急保护性操作 (挂失/停卡) 直接办理无需客户二次确认
  (2026-09-03 产品决策)
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
    exclude_checked: bool = False,
    redis_client: Any = None,
    offset: int = 0,
) -> tuple[list[tuple[str, list[dict[str, Any]], datetime | None]], int, int]:
    """取待检会话: 近 N 小时、≥min_turns 轮、按最后活跃倒序, 可抽样, 排除已检

    轮数过滤在取回轮次后做 (group_by + having count 的关联子查询写法跨库易错,
    且会话体量在万级以内, 取回过滤成本可接受)。
    exclude_checked: 跳过已有 30 天检定 verdict 的会话 (全量补扫模式);
    返回 (会话列表, 本批因已检而跳过的个数) — 跳过数供调用方累计翻页偏移。
    会话列表元素为 (session_id, 轮次列表, 会话时间=最后一轮 timestamp)。
    """
    since = datetime.now(UTC) - timedelta(hours=lookback_hours)
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(DialogueLog.session_id)
                .where(DialogueLog.timestamp >= since)
                .group_by(DialogueLog.session_id)
                .order_by(func.max(DialogueLog.timestamp).desc())
                .limit(offset + limit * 3 if sample_rate < 1.0 else offset + limit)
                .offset(offset)
            )
        ).all()
    session_ids = [r.session_id for r in rows]
    raw_n = len(session_ids)
    if not session_ids:
        return [], offset, raw_n
    if exclude_checked and redis_client is not None:
        try:
            keys = [_VERDICT_KEY.format(sid=sid) for sid in session_ids]
            verdicts = await redis_client.mget(keys)
            skipped = sum(1 for v in verdicts if v)
            session_ids = [sid for sid, v in zip(session_ids, verdicts, strict=False) if not v]
        except Exception:
            skipped = 0
    else:
        skipped = 0
    if not session_ids:
        return [], offset + skipped, raw_n
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
    last_ts: dict[str, datetime] = {}
    for r in turn_rows:
        by_sid.setdefault(r.session_id, []).append(
            {"speaker": r.speaker, "content": r.content, "intent": r.intent, "response_source": r.response_source}
        )
        last_ts[r.session_id] = r.timestamp
    return (
        [(sid, turns, last_ts.get(sid)) for sid, turns in by_sid.items() if len(turns) >= min_turns][:limit],
        skipped,
        raw_n,
    )


async def scan_session(
    session_factory: async_sessionmaker[AsyncSession],
    judge_llm: Any,
    redis_client: Any,
    session_id: str,
    turns: list[dict[str, Any]],
    model: str,
    session_time: datetime | None = None,
) -> dict[str, Any]:
    """单会话质检: 裁判判定 → 判定落 quality_record (全量) → fail 采集 badcase

    session_time: 会话最后一轮对话时间 (质检记录按对话发生顺序排列的锚点)。
    """
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

        bc = await capture_badcase(
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
            session_time=session_time,
        )
        badcase_id = str(bc.id)
    else:
        badcase_id = None
        first_customer = next((t["content"] for t in turns if t.get("speaker") == "customer"), "")

    # 全量判定落库 (质检记录列表的每会话一条) — 失败不阻断巡检:
    # Redis 判定已写, 重启/补扫时可由 backfill_redis_verdicts 重新入库
    if session_factory is not None:
        try:
            from lumio.services.common.badcase_store import record_quality

            await record_quality(
                session_factory,
                session_id=session_id,
                verdict=verdict["verdict"],
                problems=verdict["problems"],
                summary=verdict["summary"],
                preview=first_customer,
                judge_model=model,
                turns=len(turns),
                session_time=session_time,
                badcase_id=badcase_id,
            )
        except Exception as exc:
            logger.warning("qa_scan 判定落库失败 (不阻断): session=%s err=%s", session_id, exc)
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

    async def one(sid: str, turns: list[dict[str, Any]], session_time: datetime | None) -> None:
        async with sem:
            try:
                v = await scan_session(session_factory, judge_llm, redis_client, sid, turns, model, session_time=session_time)
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
        # 全量补扫 (用户反馈: 没把所有会话纳入 — 旧实现单批 limit 扫完即停,
        # 已检去重让后续轮次永远跳过更早的存量): 批次循环, 每批排除已检会话,
        # 直到无未检会话或达总上限; limit 语义从"单批"变为"本轮总上限"。
        batch_size = 100
        scanned = 0
        short_batches = 0
        window = 0  # 已翻过的排序窗口位置 (含已检跳过)
        _scan_state.update(total=0)
        while scanned < limit:
            batch_cap = min(batch_size, limit - scanned)
            sessions, _skipped, raw_n = await _load_sessions(
                session_factory,
                lookback_hours=lookback_hours,
                limit=batch_cap,
                sample_rate=sample_rate,
                exclude_checked=not reinspect and redis_client is not None,
                redis_client=redis_client,
                offset=window,
            )
            window += batch_cap
            # 翻尽判据是 SQL 原始行数 (窗口内已检占位是常态 — 最近会话大多被
            # 前几轮扫过, fresh 为 0 或 < batch_cap 都不代表窗口耗尽)。
            # 连续 2 个短批才判翻尽: SQL 偶发慢查询返回少行会误 break
            # (实测首轮补扫因此在 1997/3255 处提前停, 二次续扫补完)
            short_batches = short_batches + 1 if raw_n < batch_cap else 0
            if short_batches >= 2:
                break
            if not sessions:
                continue  # 本批全为已检占位, 翻下一页
            _scan_state.update(total=_scan_state["total"] + len(sessions))
            await asyncio.gather(*(one(sid, turns, session_time) for sid, turns, session_time in sessions))
            scanned += len(sessions)
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


# ── 裁判构造 / 单会话钩子 / 存量回填 ──


def build_judge_llm(llm_client: Any, settings: Any) -> Any:
    """质检裁判 LLM: 配置了跨家族远程裁判则 RemoteJudgeClient (失败自动回退
    本地), 否则本地; llm_client 未就绪返回 None (调用方静默跳过)"""
    if llm_client is None:
        return None
    if settings.llm.judge_base_url and settings.llm.judge_api_key:
        from lumio.services.common.judge_client import RemoteJudgeClient

        return RemoteJudgeClient(fallback_llm=llm_client)
    return llm_client


def judge_model_name(settings: Any) -> str:
    """裁判模型名 (审计记录用): 远程裁判记 judge_model, 本地兜底记 primary_model"""
    if settings.llm.judge_base_url and settings.llm.judge_api_key:
        return str(settings.llm.judge_model)
    return str(settings.llm.primary_model)


async def _load_session_turns(
    session_factory: async_sessionmaker[AsyncSession],
    session_id: str,
) -> tuple[list[dict[str, Any]], datetime | None]:
    """取单会话全部对话轮 + 会话时间锚点 (最后一轮 timestamp)"""
    async with session_factory() as session:
        rows = (
            await session.execute(
                select(
                    DialogueLog.speaker,
                    DialogueLog.content,
                    DialogueLog.intent,
                    DialogueLog.response_source,
                    DialogueLog.timestamp,
                )
                .where(DialogueLog.session_id == session_id)
                .order_by(DialogueLog.timestamp)
            )
        ).all()
    turns = [
        {"speaker": r.speaker, "content": r.content, "intent": r.intent, "response_source": r.response_source}
        for r in rows
    ]
    return turns, (rows[-1].timestamp if rows else None)


async def scan_session_by_id(
    session_factory: async_sessionmaker[AsyncSession],
    judge_llm: Any,
    redis_client: Any,
    session_id: str,
    model: str,
) -> dict[str, Any] | None:
    """按需单会话巡检 (chat_end 自动质检钩子)。

    已检 (Redis 30 天去重) 或对话不足 2 轮 (问候/噪声会话) 返回 None 跳过。
    """
    if redis_client is not None:
        try:
            if await redis_client.get(_VERDICT_KEY.format(sid=session_id)):
                return None
        except Exception:
            pass
    turns, session_time = await _load_session_turns(session_factory, session_id)
    if len(turns) < 2:
        return None
    return await scan_session(session_factory, judge_llm, redis_client, session_id, turns, model, session_time=session_time)


def _parse_redis_verdict(session_id: str, raw: str | bytes) -> dict[str, Any] | None:
    """Redis 判定 JSON → quality_record 入库参数 (scanned_at 缺失/解析失败返回
    None — scanned_at 是幂等去重键, 缺了宁可不回填也不重复插入)"""
    try:
        d = json.loads(raw)
        verdict = str(d.get("verdict") or "")
        if verdict not in ("pass", "warn", "fail") or not d.get("scanned_at"):
            return None
        turns = d.get("turns")
        return {
            "session_id": session_id,
            "verdict": verdict,
            "problems": d.get("problems") or [],
            "summary": (str(d.get("summary") or "")[:200]) or None,
            "judge_model": d.get("model"),
            "turns": int(turns) if isinstance(turns, int | float) else None,            "scanned_at": datetime.fromisoformat(str(d["scanned_at"])),
        }
    except Exception:
        return None


async def backfill_redis_verdicts(
    session_factory: async_sessionmaker[AsyncSession] | None,
    redis_client: Any,
) -> int:
    """存量 Redis 判定 → quality_record 一次性回填 (幂等, 服务启动时跑)

    判定此前只存 Redis (30 天 TTL), "每一个会话都纳入质检列表"要求判定
    成为可查询资产; 已入库 (同 session_id + scanned_at) 的跳过。
    """
    if session_factory is None or redis_client is None:
        return 0
    try:
        pairs: list[tuple[str, Any]] = []
        async for key in redis_client.scan_iter(match=_VERDICT_KEY.format(sid="*")):
            sid = str(key).rsplit(":", 1)[-1]
            raw = await redis_client.get(key)
            if raw:
                pairs.append((sid, raw))
        rows = [p for p in (_parse_redis_verdict(sid, raw) for sid, raw in pairs) if p]
        if not rows:
            return 0
        inserted = 0
        async with session_factory() as session:
            sids = list({r["session_id"] for r in rows})
            # 会话时间锚点 + 首轮客户输入预览 (批量回查 dialogue_log)
            ts_map: dict[str, datetime] = {}
            preview_map: dict[str, str] = {}
            turn_rows = (
                await session.execute(
                    select(DialogueLog.session_id, DialogueLog.speaker, DialogueLog.content, DialogueLog.timestamp)
                    .where(DialogueLog.session_id.in_(sids))
                    .order_by(DialogueLog.timestamp)
                )
            ).all()
            for r in turn_rows:
                ts_map[r.session_id] = r.timestamp
                if r.session_id not in preview_map and r.speaker == "customer" and (r.content or "").strip():
                    preview_map[r.session_id] = r.content.strip()[:160]
            # 存量 fail 的闭环关联: 同会话已采集的 qa_scan badcase (最新一条)
            fail_sids = [r["session_id"] for r in rows if r["verdict"] == "fail"]
            badcase_map: dict[str, str] = {}
            if fail_sids:
                from lumio.shared.orm_models import Badcase

                bc_rows = (
                    await session.execute(
                        select(Badcase.id, Badcase.session_id)
                        .where(Badcase.signal_source == "qa_scan", Badcase.session_id.in_(set(fail_sids)))
                        .order_by(Badcase.created_at.desc())
                    )
                ).all()
                for r in bc_rows:
                    badcase_map.setdefault(r.session_id, str(r.id))
            existing = {
                (r[0], r[1])
                for r in (
                    await session.execute(
                        select(QualityRecord.session_id, QualityRecord.scanned_at).where(
                            QualityRecord.session_id.in_(sids)
                        )
                    )
                ).all()
            }
            for r in rows:
                key = (r["session_id"], r["scanned_at"])
                if key in existing:
                    continue
                sid = r["session_id"]
                session.add(
                    QualityRecord(
                        session_id=sid,
                        verdict=r["verdict"],
                        problems=r["problems"] or None,
                        summary=r["summary"],
                        preview=preview_map.get(sid),
                        judge_model=r["judge_model"],
                        turns=r["turns"],
                        session_time=ts_map.get(sid),
                        scanned_at=r["scanned_at"],
                        badcase_id=badcase_map.get(sid) if r["verdict"] == "fail" else None,
                    )
                )
                inserted += 1
            await session.commit()
        if inserted:
            logger.info("qa_scan Redis 判定回填: %d 条 → quality_record", inserted)
        return inserted
    except Exception as exc:
        logger.warning("qa_scan Redis 判定回填失败 (不阻断): %s", exc)
        return 0
