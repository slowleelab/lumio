"""客户端模拟 Agent (管理端可控的对话流量发生器)

模拟真实客户走完整黑盒链路: POST /api/chat/send → GET /api/chat/poll 轮询
→ (可选) POST /api/chat/feedback 差评。场景覆盖八层管道的关键分支, 差评/
转人工/合规信号自动喂入智能质检闭环 (Badcase 采集)。

剧本随机化 (v2 — 告别固定台词):
- 话术变体池: 每轮 {"variants": [...]}, 每次运行随机抽一条 (同意图不同问法,
  专测分类器对措辞变化的鲁棒性); 兼容旧的 {"text": "..."} 单条形式。
- 槽位随机: {card_no}/{month}/{amount} 占位符从取值池随机填充 (含尾号等模糊指代)。
- 口语装饰: 低概率加前缀 ("那个 "/"请问一下 ")/语气后缀/相邻字打错 — 模拟真人输入。
- 噪声场景: 程序化生成乱码 (每次不同), 不再用固定两条。
- 会话级行为: 概率性中途挂断 / 开场先寒暄一句 / 顺带再问第二个主题 (多主题会话)。
- expect 支持字符串或列表 (任一命中即算 ✓)。

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

# ── 会话级行为概率 (真实客户不总是走完剧本) ──
ABANDON_RATE = 0.08  # 每轮后中途挂断概率
GREETING_RATE = 0.15  # 开场先寒暄一句概率
MULTI_TOPIC_RATE = 0.20  # 顺带再问第二个主题概率 (转人工后不再追加)

# ── 槽位取值池 (含规范形态与口语/模糊形态, 专测槽位抽取鲁棒性) ──

_SLOT_POOLS: dict[str, list[str]] = {
    "{card_no}": [
        "6222 1234 5678 9012",
        "6222 0000 1111 2222",
        "6225 8801 2345 6789",
        "尾号 9012",
        "尾号 8888 那张",
        "就是登记的手机号那个账户",
    ],
    "{month}": ["这个月", "上个月", "上月", "本期", "最近一期", "9 月"],
    "{amount}": ["5000", "8000", "12000", "3000"],
}

_PREFIXES = ["", "", "", "", "那个 ", "请问一下 ", "麻烦 ", "我想问下 "]
_SUFFIXES = ["", "", "", "", "？", "呢", "啊", "，谢谢"]
_TYPO_RATE = 0.06
_NOISE_CHARS = "asdfghjklzxcv1234567890!@#$%…；、。？?"


def fill_slots(text: str, rng: random.Random) -> str:
    for slot, pool in _SLOT_POOLS.items():
        if slot in text:
            text = text.replace(slot, rng.choice(pool))
    return text


def decorate(text: str, rng: random.Random) -> str:
    """口语装饰: 低概率前缀/后缀/相邻字打错。空串安全, 永不返回空。"""
    if not text:
        return text
    out = rng.choice(_PREFIXES) + text
    suffix = rng.choice(_SUFFIXES)
    if suffix and not out.endswith(("？", "?", "。", "，", "谢谢")):
        out += suffix
    if len(out) >= 4 and rng.random() < _TYPO_RATE:
        i = rng.randrange(1, len(out) - 2)
        out = out[:i] + out[i + 1] + out[i] + out[i + 2 :]
    return out


def gen_noise(rng: random.Random) -> str:
    """程序化乱码: 混合随机字符流/重复标点/键盘滚按, 每次不同"""
    kind = rng.random()
    if kind < 0.4:
        return "".join(rng.choice(_NOISE_CHARS) for _ in range(rng.randint(8, 20)))
    if kind < 0.7:
        return rng.choice("？。！?") * rng.randint(5, 12)
    return "".join(rng.choice("qwertyuiop") for _ in range(rng.randint(4, 7))) * 2


def pick_turn_text(turn: dict[str, Any], rng: random.Random, *, raw: bool = False) -> str:
    """抽取本轮输入: variants 池随机 → 槽位填充 → (可选)口语装饰; 噪声轮程序化生成"""
    if turn.get("noise"):
        return gen_noise(rng)
    variants = turn.get("variants") or [turn.get("text") or ""]
    text = rng.choice(variants)
    if not text:
        return text
    text = fill_slots(text, rng)
    return text if raw else decorate(text, rng)


def expect_hit(expect: Any, reply: str) -> bool | None:
    """期望命中判定: expect 为 str 或 list[str], 任一命中即 ✓; 空为 None (不统计)"""
    if not expect:
        return None
    keys = [expect] if isinstance(expect, str) else list(expect)
    return any(k in (reply or "") for k in keys if k)


# ── 场景剧本 ───────────────────────────────────────────────────────────


@dataclass
class Scenario:
    key: str
    name_zh: str
    turns: list[dict[str, Any]]
    final_feedback: str = ""  # "" / "down"
    tags: list[str] = field(default_factory=list)

    def variant_count(self) -> int:
        return sum(len(t.get("variants") or [1]) for t in self.turns)


SCENARIOS: list[Scenario] = [
    Scenario(
        key="bill_query",
        name_zh="账单查询 (链 B 槽位补齐)",
        turns=[
            {
                "variants": [
                    "帮我查一下信用卡账单",
                    "查下我{month}账单",
                    "{month}要还多少钱",
                    "我的账单出来了吗",
                    "看下最近一期账单",
                ],
                "expect": ["卡号", "账单"],
            },
            {"variants": ["卡号是 {card_no}", "{card_no}", "用 {card_no} 这张查"], "expect": ""},
        ],
        tags=["chain_b", "slot"],
    ),
    Scenario(
        key="balance_query",
        name_zh="余额/额度查询",
        turns=[
            {
                "variants": [
                    "我的信用卡可用额度还有多少",
                    "看看我还剩多少额度",
                    "额度还剩多少",
                    "现在卡里还能刷多少",
                ],
                "expect": "",
            }
        ],
        tags=["chain_b"],
    ),
    Scenario(
        key="card_loss",
        name_zh="挂失确认链 (敏感工具)",
        turns=[
            {
                "variants": [
                    "我的卡丢了, 要挂失",
                    "信用卡找不到了, 怎么办",
                    "卡好像被盗了, 赶紧给我停了",
                    "钱包被偷了, 卡也在里面",
                ],
                "expect": ["确认", "挂失"],
            },
            {"variants": ["确认挂失", "对, 挂吧", "是的, 帮我办"], "expect": ""},
        ],
        tags=["chain_a", "sensitive"],
    ),
    Scenario(
        key="installment",
        name_zh="分期咨询 (RAG 知识链)",
        turns=[
            {
                "variants": [
                    "账单分期手续费怎么算的",
                    "分期利率是多少",
                    "办分期要多少手续费",
                    "分期付款划算吗, 怎么收费",
                ],
                "expect": "",
            }
        ],
        tags=["rag"],
    ),
    Scenario(
        key="definition",
        name_zh="定义句式 (强制咨询域)",
        turns=[
            {
                "variants": [
                    "什么是临时额度",
                    "临时额度是什么意思",
                    "什么叫临时额度",
                    "容时容差是什么",
                    "什么是溢缴款",
                ],
                "expect": "",
            }
        ],
        tags=["definition"],
    ),
    Scenario(
        key="composite",
        name_zh="复合意图 (取数+解释)",
        turns=[
            {
                "variants": [
                    "帮我看下{month}账单, 为什么最低还款这么多",
                    "查下账单, 最低还款额怎么这么高",
                    "{month}账单给我看看, 最低还款是怎么算的",
                ],
                "expect": "",
            }
        ],
        tags=["chain_c"],
    ),
    Scenario(
        key="complaint",
        name_zh="投诉 (高风险直转人工)",
        turns=[
            {
                "variants": [
                    "我要投诉你们银行乱扣费, 这个问题反映三次了都没人处理",
                    "扣费有问题, 我要投诉",
                    "你们这服务太差了, 我要正式投诉",
                    "再不解决我就打监管电话投诉了",
                ],
                "expect": ["转", "专员", "投诉"],
            }
        ],
        tags=["high_risk", "transfer"],
    ),
    Scenario(
        key="transfer_direct",
        name_zh="主动转人工 (信号采集)",
        turns=[
            {
                "variants": [
                    "请帮我转接人工客服",
                    "转人工",
                    "我要找真人",
                    "给我接人工",
                    "人工客服在哪里, 我不想跟机器人说了",
                ],
                "expect": ["转", "专员", "人工"],
            }
        ],
        tags=["transfer"],
    ),
    Scenario(
        key="chitchat",
        name_zh="闲聊",
        turns=[
            {
                "variants": ["今天天气不错呀", "你好呀", "在吗", "你是机器人吗", "你会说英语吗"],
                "expect": "",
            }
        ],
        tags=["chitchat"],
    ),
    Scenario(
        key="noise",
        name_zh="乱码噪声 (噪声门, 程序化生成)",
        turns=[{"noise": True}, {"noise": True}],
        tags=["noise"],
    ),
    Scenario(
        key="faq_points",
        name_zh="FAQ 直出 (积分兑换)",
        turns=[
            {
                "variants": [
                    "积分怎么兑换礼品",
                    "积分能换什么",
                    "我的积分怎么用",
                    "积分商城有什么好东西",
                ],
                "expect": "",
            }
        ],
        tags=["faq"],
    ),
    Scenario(
        key="knowledge_gap",
        name_zh="知识缺口 + 差评 (喂闭环)",
        turns=[
            {
                "variants": [
                    "数字人民币硬钱包怎么充值",
                    "数币硬钱包如何充钱",
                    "怎么给数字人民币硬钱包充值",
                    "硬钱包没电了里面钱怎么办",
                ],
                "expect": "",
            }
        ],
        final_feedback="down",
        tags=["rag", "feedback", "closed_loop"],
    ),
]

SCENARIO_MAP = {s.key: s for s in SCENARIOS}

_GREETING_VARIANTS = ["你好", "在吗", "hi", "你好呀，请教个问题"]
# 多主题连问只串轻量场景 (不串敏感/转人工类)
_CHAINABLE_KEYS = ("balance_query", "installment", "definition", "faq_points", "chitchat")


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
    abandoned: int = 0
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
            "abandoned": self.abandoned,
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
    """一个虚拟客户: 独立 session, 随机化跑完场景 (可能中途挂断/多主题)"""

    def __init__(self, base_url: str, customer_id: str, rng: random.Random | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._customer_id = customer_id
        self._session_id = ""
        self._rng = rng or random.Random(random.random())

    async def run_scenario(self, sc: Scenario, *, chained: bool = False) -> list[TurnRecord]:
        """跑一个场景。chained=True 表示同一会话顺带追问的第二个主题。"""
        records: list[TurnRecord] = []
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            if not chained:
                self._session_id = f"sim-{sc.key}-{int(time.time() * 1000)}-{self._rng.randint(100, 999)}"
                if self._rng.random() < GREETING_RATE:
                    records.append(await self._one_turn(client, sc, 0, self._rng.choice(_GREETING_VARIANTS), None))
            for i, turn in enumerate(sc.turns, start=1):
                records.append(await self._one_turn(client, sc, i, pick_turn_text(turn, self._rng), turn.get("expect")))
                # 中途挂断: 剧本没走完就消失 (真实客户行为; 最后一轮之后不算挂断)
                if i < len(sc.turns) and self._rng.random() < ABANDON_RATE:
                    state.stats.abandoned += 1
                    logger.debug("模拟客户中途挂断: session=%s 场景=%s 第 %d 轮", self._session_id, sc.key, i)
                    state.stats.sessions += 1
                    return records
            if sc.final_feedback == "down" and records and await self._send_feedback(client, records[-1]):
                state.stats.feedbacks += 1
            # 多主题连问: 本主题没转人工时, 概率性在同一会话再问一个轻量主题
            transferred = any(k in (rec.reply or "") for rec in records for k in ("转接", "人工", "专员"))
            if not chained and self._rng.random() < MULTI_TOPIC_RATE and not transferred:
                keys = [k for k in _CHAINABLE_KEYS if k in SCENARIO_MAP]
                if keys:
                    second = SCENARIO_MAP[self._rng.choice(keys)]
                    records += await self.run_scenario(second, chained=True)
                    state.stats.sessions -= 1  # 连问不重复计会话
        if not chained:
            state.stats.sessions += 1
        return records

    async def _one_turn(
        self,
        client: httpx.AsyncClient,
        sc: Scenario,
        turn_no: int,
        text: str,
        expect: Any,
    ) -> TurnRecord:
        t0 = time.monotonic()
        reply = await self._send_and_poll(client, text)
        latency = time.monotonic() - t0
        ok = expect_hit(expect, reply)
        rec = TurnRecord(sc.key, turn_no, text, reply, latency, ok)
        state.stats.turns += 1
        state.stats.latencies.append(latency)
        if ok is not None:
            state.stats.expect_checks += 1
            state.stats.expect_hits += int(ok)
        state.push_recent(rec)
        await asyncio.sleep(TURN_GAP)
        return rec

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
                    "message_id": f"sim-{int(time.time() * 1000)}",
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
            customer = SimCustomer(base_url, customer_id, rng=rng)
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
    total_variants = sum(SCENARIO_MAP[k].variant_count() for k in valid)
    logger.info(
        "对话模拟器启动: %d 用户 x %d 场景 (%d 条话术变体) @ %.0fs 间隔",
        state.users,
        len(valid),
        total_variants,
        state.interval,
    )
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
