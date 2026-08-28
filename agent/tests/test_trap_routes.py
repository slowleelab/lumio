"""闭环 P1 感知缝管理端点 ASGI 单元测试 (router.py: /admin/classifier-sample/*)

全部 mock, 无中间件 / 无 DB: 用 fake collector 替换 app.state.trap_collector.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lumio.services.bot.router import router as bot_router
from lumio.shared.auth import AuthUser, get_current_user
from lumio.shared.middleware import register_exception_handlers


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.include_router(bot_router, prefix="/api")
    register_exception_handlers(app)  # LumioError(如 AuthorizationError) → 统一 JSON 错误码

    fake = AsyncMock()
    fake.aggregate.return_value = [{"intent": "limit_query", "count": 42, "avg_confidence": 0.81, "window_days": 7}]
    fake.purge_older_than.return_value = 5
    app.state.trap_collector = fake

    # 固定 admin 登录态
    app.dependency_overrides[get_current_user] = lambda: AuthUser(user_id="admin-1", role="admin", session_id="s1")
    return app


async def _get(app: FastAPI, url: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.get(url)


async def _post(app: FastAPI, url: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.post(url)


class TestClassifierSampleAdmin:
    async def test_aggregate_as_admin(self, app: FastAPI) -> None:
        resp = await _get(app, "/api/admin/classifier-sample/aggregate?window_days=14")
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert body["window_days"] == 14
        assert body["samples"][0]["intent"] == "limit_query"
        app.state.trap_collector.aggregate.assert_awaited_once_with(window_days=14, min_samples=1)

    async def test_purge_as_admin(self, app: FastAPI) -> None:
        resp = await _post(app, "/api/admin/classifier-sample/purge?days=30")
        assert resp.status_code == 200
        assert resp.json() == {"enabled": True, "days": 30, "deleted": 5}
        app.state.trap_collector.purge_older_than.assert_awaited_once_with(days=30)

    async def test_aggregate_denied_for_customer(self, app: FastAPI) -> None:
        app.dependency_overrides[get_current_user] = lambda: AuthUser(user_id="c-1", role="customer", session_id=None)
        resp = await _get(app, "/api/admin/classifier-sample/aggregate")
        assert resp.status_code == 403

    async def test_purge_denied_for_agent(self, app: FastAPI) -> None:
        app.dependency_overrides[get_current_user] = lambda: AuthUser(user_id="a-1", role="agent", session_id=None)
        resp = await _post(app, "/api/admin/classifier-sample/purge")
        assert resp.status_code == 403

    async def test_disabled_collector_returns_empty(self, app: FastAPI) -> None:
        app.state.trap_collector = None
        resp = await _get(app, "/api/admin/classifier-sample/aggregate")
        assert resp.status_code == 200
        assert resp.json() == {"enabled": False, "samples": []}


# ── 闭环 P2 归因端点 ────────────────────────────────────────────────────────


class TestClosedLoopRootCauses:
    @pytest.fixture(autouse=True)
    def _patch_attribution(self, monkeypatch) -> None:
        from lumio.services.common import trap_eval

        async def fake_attribute_recent(**kwargs):
            return {
                "total": 3,
                "by_layer": {"classification": 1, "retrieval": 1},
                "by_verdict": {"failure": 1, "pending": 1, "healthy": 1},
                "actionable": [],
            }

        monkeypatch.setattr(trap_eval, "attribute_recent", fake_attribute_recent)

    async def test_root_causes_as_agent(self, app: FastAPI) -> None:
        app.dependency_overrides[get_current_user] = lambda: AuthUser(user_id="a-1", role="agent", session_id=None)
        resp = await _get(app, "/api/admin/closed-loop/root-causes?window_days=3&limit=50")
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert body["window_days"] == 3
        assert body["total"] == 3
        assert body["by_layer"]["classification"] == 1

    async def test_root_causes_denied_for_customer(self, app: FastAPI) -> None:
        app.dependency_overrides[get_current_user] = lambda: AuthUser(user_id="c-1", role="customer", session_id=None)
        resp = await _get(app, "/api/admin/closed-loop/root-causes")
        assert resp.status_code == 403
