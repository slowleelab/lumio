"""意图库管理 API 测试 (intent_library_router): 意图树/种子 CRUD/属性表"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lumio.services.common.intent_library_router import router
from lumio.shared.auth import AuthUser, get_current_user
from lumio.shared.middleware import register_exception_handlers


def _make_app(seed_data: dict, tmp_path) -> FastAPI:
    seed_file = tmp_path / "seed_dataset.json"
    seed_file.write_text(json.dumps(seed_data, ensure_ascii=False), encoding="utf-8")
    app = FastAPI()
    app.include_router(router, prefix="/api")
    register_exception_handlers(app)
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        user_id="admin-1", role="admin", session_id=None
    )
    patcher = patch(
        "lumio.services.common.intent_library_router.SEED_PATH",
        seed_file,
    )
    patcher.start()
    return app


def _override_role(app: FastAPI, role: str) -> None:
    app.dependency_overrides[get_current_user] = lambda: AuthUser(
        user_id=f"{role}-x", role=role, session_id=None
    )


async def _req(app: FastAPI, method: str, url: str, **kwargs):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.request(method, url, **kwargs)


ADMIN = lambda: AuthUser(user_id="admin-x", role="admin", session_id=None)


@pytest.fixture
def seed_env(tmp_path):
    data = _seed_data()
    app = _make_app(data, tmp_path)
    yield app, tmp_path / "seed_dataset.json"


def _seed_data() -> dict:
    return {
        "meta": {},
        "labels": [],
        "examples": [
            {"text": "信用卡年费怎么减免", "intent": "faq"},
            {"text": "我的额度是多少", "intent": "limit_query"},
        ],
        "confusable_pairs": [],
    }


@pytest.mark.asyncio
async def test_tree_five_domains(seed_env) -> None:
    app, _ = seed_env
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.get("/api/admin/intent-library/tree")
    assert resp.status_code == 200
    body = resp.json()
    for dom in ("query", "transaction", "consulting", "service", "chitchat"):
        assert dom in body["domains"], dom


@pytest.mark.asyncio
async def test_seeds_add_and_dedup(seed_env) -> None:
    app, seed_file = seed_env
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            "/api/admin/intent-library/seeds",
            json={"intent": "faq", "text": "年费怎么免"},
        )
        assert r.status_code == 200
        assert r.json()["created"] == 1

        r2 = await c.post(
            "/api/admin/intent-library/seeds",
            json={"intent": "faq", "text": "年费怎么免"},
        )
        assert r2.json()["duplicate"] is True

        data = json.loads(seed_file.read_text(encoding="utf-8"))
        assert len(data["examples"]) == 3


@pytest.mark.asyncio
async def test_seeds_delete(seed_env) -> None:
    app, seed_file = seed_env
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.request(
            "DELETE",
            "/api/admin/intent-library/seeds",
            params={"intent": "faq", "text": "信用卡年费怎么减免"},
        )
        assert r.status_code == 200
        assert r.json()["removed"] == 1


@pytest.mark.asyncio
async def test_seeds_delete_denied_customer(seed_env) -> None:
    app, _ = seed_env
    _override_role(app, "customer")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.request(
            "DELETE",
            "/api/admin/intent-library/seeds",
            params={"intent": "faq", "text": "x"},
        )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_attribute_table_149_intents(seed_env) -> None:
    app, _ = seed_env
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/admin/intent-library/attributes")
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert len(rows) == 149
    by_intent = {x["intent"]: x for x in rows}
    assert by_intent["account_bill_query"]["traffic_class"] == "read_only_query"
    assert by_intent["card_loss_report"]["traffic_class"] == "financial_transaction"
    assert by_intent.get("faq", {}).get("traffic_class") is None  # 咨询域无交易性质
