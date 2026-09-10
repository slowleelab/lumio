"""闭环管理路由端点测试 (admin 鉴权 + 参数校验 + 读写回路)

复用 bot_server 子进程夹具 (CI 干净环境真实启动); 本地 dev 服务占用 8000 时
该组自动跳过 — 与 test_bot_api 同一套约束。
"""

from __future__ import annotations

import httpx
import pytest_asyncio


@pytest_asyncio.fixture
async def admin_client(db_schema: None, bot_server: str):
    from lumio.shared.auth import create_access_token

    token = create_access_token("test-admin", "admin")
    async with httpx.AsyncClient(
        base_url=bot_server, timeout=30.0, headers={"Authorization": f"Bearer {token}"}
    ) as client:
        yield client


@pytest_asyncio.fixture
async def customer_client(bot_server: str):
    from lumio.shared.auth import create_access_token

    token = create_access_token("test-customer", "customer")
    async with httpx.AsyncClient(
        base_url=bot_server, timeout=30.0, headers={"Authorization": f"Bearer {token}"}
    ) as client:
        yield client


def _err_code(resp: httpx.Response) -> int:
    return int(resp.json().get("error", {}).get("code", 0))


class TestBadcaseEndpoints:
    async def test_list_and_stats(self, admin_client: httpx.AsyncClient) -> None:
        r = await admin_client.get("/api/admin/closed-loop/badcases", params={"limit": 5})
        assert r.status_code == 200
        body = r.json()
        assert "badcases" in body and isinstance(body["total"], int)

        r2 = await admin_client.get("/api/admin/closed-loop/badcases/stats")
        assert r2.status_code == 200
        stats = r2.json()
        for k in ("total", "today_new", "pending_review", "confirmed", "deployed"):
            assert k in stats

    async def test_get_missing_badcase_raises_input_error(self, admin_client: httpx.AsyncClient) -> None:
        r = await admin_client.get("/api/admin/closed-loop/badcases/00000000-0000-0000-0000-000000000000")
        assert 400 <= r.status_code < 500
        assert _err_code(r) == 2001

    async def test_customer_role_forbidden(self, customer_client: httpx.AsyncClient) -> None:
        r = await customer_client.get("/api/admin/closed-loop/badcases")
        assert r.status_code in (401, 403)


class TestQualityEndpoints:
    async def test_records_and_sessions(self, admin_client: httpx.AsyncClient) -> None:
        r = await admin_client.get("/api/admin/closed-loop/quality/records", params={"limit": 5})
        assert r.status_code == 200 and "records" in r.json()

        r2 = await admin_client.get("/api/admin/closed-loop/quality/sessions", params={"limit": 5})
        assert r2.status_code == 200 and "sessions" in r2.json()

        # 非法分类 → 2001
        r3 = await admin_client.get("/api/admin/closed-loop/quality/sessions", params={"category": "bogus"})
        assert 400 <= r3.status_code < 500 and _err_code(r3) == 2001

    async def test_coverage_and_trend(self, admin_client: httpx.AsyncClient) -> None:
        r = await admin_client.get("/api/admin/closed-loop/quality/coverage")
        assert r.status_code == 200 and isinstance(r.json().get("total_sessions"), int)

        r2 = await admin_client.get("/api/admin/closed-loop/quality/trend", params={"days": 2})
        assert r2.status_code == 200
        days = r2.json()["days"]
        assert len(days) == 2 and {"date", "pass", "warn", "fail", "new_cases"} <= set(days[0])

        r3 = await admin_client.get("/api/admin/closed-loop/quality/trend", params={"days": 0})
        assert r3.status_code == 422

    async def test_health_metrics(self, admin_client: httpx.AsyncClient) -> None:
        r = await admin_client.get("/api/admin/closed-loop/health-metrics")
        assert r.status_code == 200


class TestHumanVerdictLoop:
    async def test_write_then_read_back(self, admin_client: httpx.AsyncClient) -> None:
        sid = "cov-router-human-verdict"
        # 缺参 / 非法 verdict → 2001
        r0 = await admin_client.post("/api/admin/closed-loop/quality/human-verdict", json={})
        assert 400 <= r0.status_code < 500 and _err_code(r0) == 2001
        r1 = await admin_client.post(
            "/api/admin/closed-loop/quality/human-verdict", json={"session_id": sid, "verdict": "也许"}
        )
        assert 400 <= r1.status_code < 500

        # 写入人工判定 → 会话列表按最新判定读回 (qc_status=human)
        r2 = await admin_client.post(
            "/api/admin/closed-loop/quality/human-verdict", json={"session_id": sid, "verdict": "pass"}
        )
        assert r2.status_code == 200
        assert r2.json()["judge_model"] == "人工判定"

        r3 = await admin_client.get("/api/admin/closed-loop/quality/sessions", params={"keyword": sid})
        rows = r3.json()["sessions"]
        assert rows and rows[0]["session_id"] == sid
        assert rows[0]["qc_status"] == "human" and rows[0]["verdict"] == "pass"


class TestRescanAndReplay:
    async def test_rescan_missing_sid_and_skipped(self, admin_client: httpx.AsyncClient) -> None:
        r0 = await admin_client.post("/api/admin/closed-loop/quality/rescan", json={})
        assert 400 <= r0.status_code < 500 and _err_code(r0) == 2001

        r1 = await admin_client.post(
            "/api/admin/closed-loop/quality/rescan", json={"session_id": "cov-no-such-session"}
        )
        assert r1.status_code == 200
        assert r1.json()["status"] == "skipped"  # 无对话轮, 判定跳过

    async def test_replay_missing_msgs_and_status_unknown(self, admin_client: httpx.AsyncClient) -> None:
        r0 = await admin_client.post(
            "/api/admin/closed-loop/quality/replay", json={"session_id": "cov-no-such-session"}
        )
        assert 400 <= r0.status_code < 500  # 原会话无客户消息

        r1 = await admin_client.get(
            "/api/admin/closed-loop/quality/replay/status", params={"session_id": "cov-unknown"}
        )
        assert r1.status_code == 200 and r1.json()["status"] == "unknown"
