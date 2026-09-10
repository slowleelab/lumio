"""对话模拟器路由: 场景清单 / 状态 / 停止 (无副作用端点; start 会拉真实 worker 不在单测触发)"""

from __future__ import annotations

import httpx
import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def admin_client(db_schema: None, bot_server: str):
    from lumio.shared.auth import create_access_token

    token = create_access_token("test-admin", "admin")
    async with httpx.AsyncClient(
        base_url=bot_server, timeout=30.0, headers={"Authorization": f"Bearer {token}"}
    ) as client:
        yield client


@pytest.mark.asyncio
async def test_scenarios_list(admin_client: httpx.AsyncClient) -> None:
    r = await admin_client.get("/api/admin/simulator/scenarios")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 1
    s = body["scenarios"][0]
    assert {"key", "name_zh", "turns", "variants", "final_feedback", "tags"} <= set(s)


@pytest.mark.asyncio
async def test_status_shape(admin_client: httpx.AsyncClient) -> None:
    r = await admin_client.get("/api/admin/simulator/status")
    assert r.status_code == 200
    assert "running" in r.json()


@pytest.mark.asyncio
async def test_stop_idempotent(admin_client: httpx.AsyncClient) -> None:
    """无 worker 在跑时 stop 立即返回 running=false, 幂等可重入"""
    r1 = await admin_client.post("/api/admin/simulator/stop")
    assert r1.status_code == 200 and r1.json()["running"] is False
    r2 = await admin_client.post("/api/admin/simulator/stop")
    assert r2.status_code == 200 and r2.json()["running"] is False
