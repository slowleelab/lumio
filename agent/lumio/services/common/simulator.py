"""客户端模拟 Agent (管理端可控的对话流量发生器)

模拟真实客户走完整黑盒链路: POST /api/chat/send → GET /api/chat/poll 轮询
→ (可选) POST /api/chat/feedback 差评。场景覆盖八层管道的关键分支, 差评/
转人工/合规信号自动喂入智能质检闭环 (Badcase 采集)。

场景剧本 = 一串轮次; 每轮 {text, expect?}: expect 为期望回复包含的提示词
(仅统计用, 不作断言失败)。final_feedback="down" 时最后一轮回复后发差评。

启停由 /admin/simulator/* 控制; runner 为后台 asyncio 任务集 (引用防 GC)。
并发模型: N 个虚拟用户 loop, 每个用户独立 session 顺序跑完一个场景 (随机
抽取), 跑完歇 interval 秒再抽下一个 —— 稳态低压力, 不打爆本地 LLM。
"""

from __future__ import annotations

import asyncio
import contextlib
import random
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from lumio.shared.logger import get_logger

logger = get_logger(__name__)

POLL_TIMEOUT = 30.0  # 单轮等待回复上限 (本地 LLM 慢)
TURN_GAP = 2.0  # 轮间隔 (秒), 模拟人打字


# ── 场景剧本 ───────────────────────────────────────────────────────────


@dataclass
class Scenario:
    key: str
    name_zh: str
    turns: list[dict[str, str]]
    final_feedback: str = ""  # "" / "down"
    tags: list[str] = field(default_factory=list)


SCENARIOS: list[Scenario] = [
    Scenario(
        key="bill_query",
        name_zh="账单查询 (链 B 槽位补齐)",
        turns=[
            {"text": "帮我查一下信用卡账单", "expect": "卡号"},
            {"text": "卡号是 6222 1234 5678 9012", "expect": ""},
        ],
        tags=["chain_b", "slot"],
    ),
    Scenario(
        key="balance_query",
        name_zh="余额/额度查询",
        turns=[{"text": "我的信用卡可用额度还有多少", "expect": ""}],
        tags=["chain_b"],
    ),
    Scenario(
        key="card_loss",
        name_zh="挂失确认链 (敏感工具)",
        turns=[
            {"text": "我的卡丢了, 要挂失", "expect": "确认"},
            {"text": "确认挂失", "expect": ""},
        ],
        tags=["chain_a", "sensitive"],
    ),
    Scenario(
        key="installment",
        name_zh="分期咨询 (RAG 知识链)",
        turns=[{"text": "账单分期手续费怎么算的", "expect": ""}],
        tags=["rag"],
    ),
    Scenario(
        key="definition",
        name_zh="定义句式 (强制咨询域)",
        turns=[{"text": "什么是临时额度", "expect": ""}],
        tags=["definition"],
    ),
    Scenario(
        key="composite",
        name_zh="复合意图 (取数+解释)",
        turns=[{"text": "帮我看下这个月账单, 为什么最低还款这么多", "expect": ""}],
        tags=["chain_c"],
    ),
    Scenario(
        key="complaint",
        name_zh="投诉 (高风险直转人工)",
        turns=[{"text": "我要投诉你们银行乱扣费, 这个问题反映三次了都没人处理", "expect": "转"}],
        tags=["high_risk", "transfer"],
    ),
    Scenario(
        key="transfer_direct",
        name_zh="主动转人工 (信号采集)",
        turns=[{"text": "请帮我转接人工客服", "expect": "转"}],
        tags=["transfer"],
    ),
    Scenario(
        key="chitchat",
        name_zh="闲聊",
        turns=[{"text": "今天天气不错呀", "expect": ""}],
        tags=["chitchat"],
    ),
    Scenario(
        key="noise",
        name_zh="乱码噪声 (噪声门)",
        turns=[{"text": "asdfjkl;;;12345;;;zzz"}, {"text": "？？？？？？"}],
        tags=["noise"],
    ),
    Scenario(
        key="faq_points",
        name_zh="FAQ 直出 (积分兑换)",
        turns=[{"text": "积分怎么兑换礼品", "expect": ""}],
        tags=["faq"],
    ),
    Scenario(
        key="knowledge_gap",
        name_zh="知识缺口 + 差评 (喂闭环)",
        turns=[{"text": "数字人民币硬钱包怎么充值", "expect": ""}],
        final_feedback="down",
        tags=["rag", "feedback", "closed_loop"],
    ),
]

SCENARIO_MAP = {s.key: s for s in SCENARIOS}


# ── 运行状态 ───────────────────────────────────────────────────────────


@dataclass
class TurnRecord:
    scenario: str
    turn: int
    text: str
    reply: str
    latency: float
    expect_ok: bool | None


@dataclass
class SimulatorStats:
    started_at: float = 0.0
    sessions: int = 0
    turns: int = 0
    expect_hits: int = 0
    expect_checks: int = 0
    feedbacks: int = 0
    errors: int = 0
    latencies: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        lat = sorted(self.latencies)
        n = len(lat)
        return {
            "started_at": self.started_at,
            "sessions": self.sessions,
            "turns": self.turns,
            "expect_hits": self.expect_hits,
            "expect_checks": self.expect_checks,
            "feedbacks": self.feedbacks,
            "errors": self.errors,
            "latency_avg_ms": round(sum(lat) / n * 1000, 1) if n else 0,
            "latency_p95_ms": round(lat[int(n * 0.95)] * 1000, 1) if n else 0,
        }


class SimulatorState:
    """进程内单例状态 (启停/统计/最近运行记录)"""

    def __init__(self) -> None:
        self.running = False
        self.scenario_keys: list[str] = [s.key for s in SCENARIOS]
        self.users = 2
        self.interval = 8.0
        self.stats = SimulatorStats()
        self.recent: list[dict[str, Any]] = []
        self.tasks: set[asyncio.Task] = set()
        self._stop_event = asyncio.Event()
        self.auth_token: str = ""  # 虚拟客户 JWT (feedback 需要)

    def push_recent(self, rec: TurnRecord) -> None:
        self.recent.append(
            {
                "ts": time.time(),
                "scenario": rec.scenario,
                "turn": rec.turn,
                "text": rec.text[:60],
                "reply": (rec.reply or "")[:80],
                "latency_ms": round(rec.latency * 1000, 1),
                "expect_ok": rec.expect_ok,
            }
        )
        del self.recent[:-100]  # 环形上限


state = SimulatorState()


# ── 虚拟客户 runner ────────────────────────────────────────────────────


class SimCustomer:
    """一个虚拟客户: 独立 session, 顺序跑完一个场景的所有轮次"""

    def __init__(self, base_url: str, customer_id: str) -> None:
        self._base = base_url.rstrip("/")
        self._customer_id = customer_id
        self._session_id = ""

    async def run_scenario(self, sc: Scenario) -> list[TurnRecord]:
        records: list[TurnRecord] = []
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            self._session_id = f"sim-{sc.key}-{int(time.time()*1000)}-{random.randint(100, 999)}"
            for i, turn in enumerate(sc.turns):
                t0 = time.monotonic()
                reply = await self._send_and_poll(client, turn["text"])
                latency = time.monotonic() - t0
                expect = turn.get("expect") or ""
                ok = (expect in reply) if expect else None
                records.append(TurnRecord(sc.key, i + 1, turn["text"], reply, latency, ok))
                state.stats.turns += 1
                state.stats.latencies.append(latency)
                if ok is not None:
                    state.stats.expect_checks += 1
                    state.stats.expect_hits += int(ok)
                state.push_recent(records[-1])
                if i < len(sc.turns) - 1:
                    await asyncio.sleep(TURN_GAP)
            if sc.final_feedback == "down" and records and await self._send_feedback(client, records[-1]):
                state.stats.feedbacks += 1
        state.stats.sessions += 1
        return records

    async def _send_and_poll(self, client: httpx.AsyncClient, text: str) -> str:
        send = await client.post(
            f"{self._base}/api/chat/send",
            json={
                "session_id": self._session_id,
                "customer_id": self._customer_id,
                "message": text,
                "channel": "web",
            },
        )
        send.raise_for_status()
        deadline = time.monotonic() + POLL_TIMEOUT
        while time.monotonic() < deadline:
            if state._stop_event.is_set():
                return ""
            poll = await client.get(
                f"{self._base}/api/chat/poll",
                params={"session_id": self._session_id, "timeout": 5},
            )
            data = poll.json()
            if data.get("has_message"):
                return str(data.get("reply") or "")
            await asyncio.sleep(1.0)
        return "(超时未收到回复)"

    async def _send_feedback(self, client: httpx.AsyncClient, last: TurnRecord) -> bool:
        headers = {"Authorization": f"Bearer {state.auth_token}"} if state.auth_token else {}
        try:
            r = await client.post(
                f"{self._base}/api/chat/feedback",
                json={
                    "session_id": self._session_id,
                    "message_id": f"sim-{int(time.time()*1000)}",
                    "rating": "down",
                    "comment": f"[模拟客户差评] 场景={last.scenario}",
                },
                headers=headers,
            )
            return r.status_code == 200
        except Exception as exc:
            logger.debug("模拟差评发送失败(不阻断): %s", exc)
            return False


async def _ensure_sim_customer_token(base_url: str) -> str:
    """登录虚拟客户拿 JWT (feedback 接口需要); 账号不存在时静默降级为不发反馈"""
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(
                f"{base_url.rstrip('/')}/api/auth/login",
                json={"username": "sim_customer", "password": "SimCustomer@2026"},
            )
            if r.status_code == 200:
                return r.json().get("access_token", "")
    except Exception as exc:
        logger.debug("模拟客户登录失败(差评场景降级): %s", exc)
    return ""


async def _user_loop(base_url: str, customer_id: str) -> None:
    """单个虚拟用户主循环: 随机抽场景 → 跑完 → 歇 interval"""
    rng = random.Random(random.random())
    while not state._stop_event.is_set():
        keys = state.scenario_keys or [s.key for s in SCENARIOS]
        sc = SCENARIO_MAP[rng.choice(keys)]
        try:
            customer = SimCustomer(base_url, customer_id)
            await customer.run_scenario(sc)
        except Exception as exc:
            state.stats.errors += 1
            logger.warning("模拟场景 %s 失败: %s", sc.key, exc)
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(state._stop_event.wait(), timeout=state.interval)


def start_simulator(base_url: str, *, scenario_keys: list[str], users: int, interval: float) -> dict[str, Any]:
    """启动模拟 (幂等保护: 已运行则拒绝)"""
    if state.running:
        raise RuntimeError("模拟器已在运行")
    valid = [k for k in scenario_keys if k in SCENARIO_MAP]
    if not valid:
        raise RuntimeError("未选择任何有效场景")
    state.scenario_keys = valid
    state.users = max(1, min(users, 10))
    state.interval = max(1.0, interval)
    state.stats = SimulatorStats(started_at=time.time())
    state.recent.clear()
    state._stop_event = asyncio.Event()
    state.running = True

    async def _bootstrap() -> None:
        state.auth_token = await _ensure_sim_customer_token(base_url)
        for i in range(state.users):
            task = asyncio.create_task(_user_loop(base_url, f"sim-customer-{i + 1}"))
            state.tasks.add(task)
            task.add_done_callback(state.tasks.discard)

    asyncio.get_running_loop().create_task(_bootstrap())
    logger.info("对话模拟器启动: %d 用户 x 场景 %s @ %.0fs 间隔", state.users, valid, state.interval)
    return status_dict()


def stop_simulator() -> dict[str, Any]:
    state._stop_event.set()
    for t in state.tasks:
        t.cancel()
    state.tasks.clear()
    state.running = False
    state.auth_token = ""
    logger.info("对话模拟器停止: %s", state.stats.to_dict())
    return status_dict()


def status_dict() -> dict[str, Any]:
    return {
        "running": state.running,
        "config": {
            "scenario_keys": state.scenario_keys,
            "users": state.users,
            "interval": state.interval,
        },
        "stats": state.stats.to_dict(),
        "recent": state.recent[-30:],
    }
