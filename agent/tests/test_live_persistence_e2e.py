"""真实客户端路径 · 对话持久化端到端回归测试.

背景: 早期用临时 curl + 任意 session_id 探测接口, 无法覆盖"真实客户端先建会话、
再复用同一 session_id 多轮"的完整链路, 导致对话落库静默丢失 (get_or_create 在无 meta
时新造随机 id 丢弃入参 session_id, add_turn 因查不到 meta 整轮丢失, 且 router 静默吞掉) 未及时暴露.

本条测试严格复刻 web 前端 useCustomerChat 的调用路径:
  1. 首条消息不带 session_id → /chat/send 服务端生成并返回 session_id
  2. 复用该 session_id 继续 /chat/poll 取回复
  3. 用同一 session_id 发第二条消息 (多轮), 再 poll
断言:
  - 每轮都返回 done + 非空回复
  - Redis 里该 session_id 的 meta 存在 (首次消息即建立, 不再丢)
  - PG dialogue_log 为该 session_id 落库 (customer + bot 轮次)

前置: 运行中的 bot (:8000 或 LUMIO_TEST_BOT_URL) + docker 中间件。
无可用 bot 时自动 skip, 不破坏普通单元测试。标记 slow, CI 单独跑。
"""

from __future__ import annotations

import httpx
import pytest
import redis.asyncio as aioredis

pytestmark = pytest.mark.slow

# 默认打真实开发栈; 也可用 LUMIO_TEST_BOT_URL 指定 (CI 里指向独立 bot 实例)
_BOT_BASE = "http://127.0.0.1:8000"


def _resolve_base() -> str:
    import os

    return os.environ.get("LUMIO_TEST_BOT_URL", _BOT_BASE)


async def _bot_ready(base: str) -> bool:
    try:
        async with httpx.AsyncClient(base_url=base, timeout=5.0) as c:
            r = await c.get("/api/health")
            return r.status_code == 200
    except Exception:
        return False


async def _send_and_reply(client: httpx.AsyncClient, *, session_id: str | None, message: str):
    """真实客户端: send → 轮询 poll 直到拿到回复; 返回 (reply_dict, resolved_session_id)."""
    send = await client.post(
        "/api/chat/send",
        json={"session_id": session_id or None, "message": message},
    )
    assert send.status_code == 200, f"chat/send failed: {send.status_code} {send.text}"
    resolved = send.json().get("session_id")

    reply = None
    for _ in range(3):  # 最多轮询 3×25s (timeout 单位为秒, 端点约束 ge=1,le=60)
        poll = await client.get(f"/api/chat/poll?session_id={resolved}&timeout=25")
        assert poll.status_code == 200
        data = poll.json()
        if data.get("status") == "done":
            reply = data
            break
    return reply, resolved


async def _get_redis_meta(session_id: str) -> bool:
    r = aioredis.Redis(host="localhost", port=6379, db=0)
    try:
        return await r.exists(f"lumio:session:{session_id}:meta") == 1
    finally:
        await r.aclose()


async def _get_dialogue_log(session_id: str) -> list[dict]:
    """复用应用自身的 session factory 读 PG dialogue_log, 不硬编码连接串."""
    from sqlalchemy import text

    from lumio.services.common.database import get_async_session_factory

    factory = get_async_session_factory()
    rows: list[dict] = []
    async with factory() as db:
        res = await db.execute(
            text("SELECT speaker, content FROM dialogue_log " "WHERE session_id = :sid ORDER BY id"),
            {"sid": session_id},
        )
        for row in res.mappings():
            rows.append({"speaker": row["speaker"], "content": row["content"]})
    return rows


@pytest.mark.asyncio
async def test_real_client_path_persists_multi_turn_dialogue():
    base = _resolve_base()
    if not await _bot_ready(base):
        pytest.skip(f"无可用 bot 于 {base}, 跳过 (需要 make dev / make up)")

    from lumio.shared.auth import create_access_token

    token = create_access_token("live-e2e-user", "customer")
    async with httpx.AsyncClient(base_url=base, timeout=60.0, headers={"Authorization": f"Bearer {token}"}) as client:
        # 第 1 轮: 首条消息不带 session_id → 服务端建会话并返回
        r1, sid1 = await _send_and_reply(client, session_id=None, message="我想查询信用卡账单和还款日")
        assert r1 is not None, "第1轮未在超时内拿到回复"
        assert r1.get("has_message") is True and r1.get("reply")

        # 第 2 轮: 复用同一 session_id (真实多轮)
        r2, sid2 = await _send_and_reply(client, session_id=sid1, message="那还款日具体是哪一天")
        assert r2 is not None, "第2轮未在超时内拿到回复"
        assert sid2 == sid1, "多轮会话 session_id 应保持不变"

        # 核心断言: 首次消息即建立 meta (get_or_create 不再丢 id)
        assert await _get_redis_meta(sid1), f"Redis meta 缺失 (首条消息未按 session_id 落库): {sid1}"

        # 核心断言: 两轮落在同一个 session_id 下的 dialogue_log (customer+bot)
        rows = await _get_dialogue_log(sid1)
        speakers = [r["speaker"] for r in rows]
        assert len(rows) >= 2, f"dialogue_log 未落库或轮次过少: {rows}"
        assert "customer" in speakers and "bot" in speakers, f"对话应同时含 customer 与 bot 轮次: {rows}"
        assert rows[-1]["content"], "最后一条回复不应为空"
