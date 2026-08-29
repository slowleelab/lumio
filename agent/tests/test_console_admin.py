"""管理控制台 API 单元测试（console_router: /api/admin/*）

全部 mock，无中间件 / 无真实 DB：fake AsyncSession 按调用顺序返回预制结果。
鉴权用 dependency_overrides[get_current_user] 固定角色，覆盖 403 越权路径。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from lumio.services.common.console_router import router as console_router
from lumio.services.common.deps import get_db_session
from lumio.shared.auth import AuthUser, get_current_user
from lumio.shared.middleware import register_exception_handlers
from lumio.shared.orm_models import ChatMessageStatus

_NOW = datetime(2026, 8, 28, 12, 0, 0, tzinfo=UTC)


class _ScalarResult:
    def __init__(self, rows: list):
        self._rows = rows

    def all(self) -> list:
        return self._rows


class _Result:
    """按构造参数模拟 SQLAlchemy Result 的三种取数形态"""

    def __init__(
        self,
        *,
        rows: list | None = None,  # .all() → Row 元组/命名行
        scalars: list | None = None,  # .scalars().all() → ORM 对象
        scalar: object = None,  # .scalar_one()
        one: object = None,  # .one()
    ):
        self._rows = rows or []
        self._scalars = scalars or []
        self._scalar = scalar
        self._one = one

    def all(self) -> list:
        return self._rows

    def scalars(self) -> _ScalarResult:
        return _ScalarResult(self._scalars)

    def scalar_one(self) -> object:
        return self._scalar

    def one(self) -> object:
        return self._one


class FakeSession:
    """顺序吐出预制 Result 的假 AsyncSession"""

    def __init__(self, results: list[_Result]):
        self._results = results
        self.executed: list = []

    async def execute(self, stmt):
        self.executed.append(stmt)
        return self._results.pop(0)


def _db_override(fake: FakeSession):
    async def _gen() -> AsyncGenerator[FakeSession, None]:
        yield fake

    return _gen


def _make_app(user: AuthUser, fake: FakeSession) -> FastAPI:
    app = FastAPI()
    app.include_router(console_router, prefix="/api")
    register_exception_handlers(app)
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db_session] = _db_override(fake)
    return app


async def _get(app: FastAPI, url: str):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.get(url)


ADMIN = AuthUser(user_id="admin-1", role="admin", session_id=None)
AGENT = AuthUser(user_id="agent-1", role="agent", session_id=None)
CUSTOMER = AuthUser(user_id="c-1", role="customer", session_id=None)


# ── 会话列表 ────────────────────────────────────────────────────────────────


class TestConversations:
    async def test_list_as_admin(self) -> None:
        row = SimpleNamespace(
            session_id="s-1",
            turns=6,
            started_at=_NOW,
            last_at=_NOW,
            avg_bot_confidence=0.8123,
            customer_id="c-1",
            channel_type="web",
            top_intent="faq",
            messages=3,
            errors=1,
            avg_duration_ms=88.6,
        )
        fake = FakeSession([_Result(scalar=1), _Result(rows=[row])])
        resp = await _get(_make_app(ADMIN, fake), "/api/admin/conversations?limit=10&offset=0")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        conv = body["conversations"][0]
        assert conv["session_id"] == "s-1"
        assert conv["top_intent"] == "faq"
        assert conv["avg_bot_confidence"] == 0.8123
        assert conv["errors"] == 1
        assert conv["started_at"] == _NOW.isoformat()

    async def test_agent_allowed_customer_denied(self) -> None:
        fake = FakeSession([_Result(scalar=0), _Result(rows=[])])
        ok = await _get(_make_app(AGENT, fake), "/api/admin/conversations")
        assert ok.status_code == 200
        denied = await _get(_make_app(CUSTOMER, fake), "/api/admin/conversations")
        assert denied.status_code == 403

    async def test_bad_time_filter_maps_to_400(self) -> None:
        fake = FakeSession([])
        resp = await _get(_make_app(ADMIN, fake), "/api/admin/conversations?start=not-a-date")
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == 2001


# ── 会话回放 ────────────────────────────────────────────────────────────────


class TestReplay:
    async def test_replay_three_sections(self) -> None:
        turn = SimpleNamespace(
            turn_id="t1",
            speaker="customer",
            content="你好",
            intent=None,
            confidence=None,
            entities=[],
            response_source=None,
            emotion_label=None,
            emotion_score=None,
            retrieval_context="机密检索原文",
            timestamp=_NOW,
        )
        decision = SimpleNamespace(
            decision_id="d1",
            turn_id="t1",
            agent_name="bot_agent",
            action="reply",
            reasoning="命中知识",
            evidence_json={"k": "v"},
            latency_ms=12.34,
            created_at=_NOW,
        )
        message = SimpleNamespace(
            message_id="m1",
            content="你好",
            intent="faq",
            processing_status=ChatMessageStatus.DONE,
            processing_duration_ms=50,
            source="llm",
            error_message=None,
            created_at=_NOW,
        )
        fake = FakeSession([_Result(rows=[turn]), _Result(rows=[decision]), _Result(rows=[message])])
        resp = await _get(_make_app(ADMIN, fake), "/api/admin/conversations/s-1/replay")
        assert resp.status_code == 200
        body = resp.json()
        assert body["session_id"] == "s-1"
        assert len(body["turns"]) == len(body["decisions"]) == len(body["messages"]) == 1
        # 默认脱敏 retrieval_context
        assert body["turns"][0]["retrieval_context"] is None
        assert body["decisions"][0]["action"] == "reply"
        assert body["messages"][0]["processing_status"] == "done"

    async def test_replay_include_context(self) -> None:
        turn = SimpleNamespace(
            turn_id="t1",
            speaker="bot",
            content="答",
            intent="faq",
            confidence=0.9,
            entities=None,
            response_source="knowledge",
            emotion_label=None,
            emotion_score=None,
            retrieval_context="知识原文",
            timestamp=_NOW,
        )
        fake = FakeSession([_Result(rows=[turn]), _Result(rows=[]), _Result(rows=[])])
        resp = await _get(_make_app(ADMIN, fake), "/api/admin/conversations/s-1/replay?include_context=true")
        assert resp.status_code == 200
        assert resp.json()["turns"][0]["retrieval_context"] == "知识原文"

    async def test_replay_denied_for_customer(self) -> None:
        fake = FakeSession([])
        resp = await _get(_make_app(CUSTOMER, fake), "/api/admin/conversations/s-1/replay")
        assert resp.status_code == 403


# ── 操作审计 ────────────────────────────────────────────────────────────────


class TestOperationLogs:
    async def test_admin_only(self) -> None:
        log = SimpleNamespace(
            id="00000000-0000-0000-0000-000000000001",
            timestamp=_NOW,
            actor_id="agent-1",
            actor_role="agent",
            action="session.transition",
            target_type="session",
            target_id="s-1",
            method="POST",
            path="/api/chat/end",
            status_code=200,
            ip_address="127.0.0.1",
            detail=None,
        )
        fake = FakeSession([_Result(scalar=1), _Result(scalars=[log])])
        ok = await _get(_make_app(ADMIN, fake), "/api/admin/operation-logs")
        assert ok.status_code == 200
        assert ok.json()["logs"][0]["action"] == "session.transition"

        denied = await _get(_make_app(AGENT, fake), "/api/admin/operation-logs")
        assert denied.status_code == 403


# ── RAG 质量聚合 ────────────────────────────────────────────────────────────


class TestRagQualitySummary:
    async def test_summary_shape_and_hit_rate(self) -> None:
        results = [
            _Result(rows=[(_NOW, 10, 3)]),  # daily_volume
            _Result(rows=[(_NOW, "knowledge", 6)]),  # source_daily
            _Result(rows=[("knowledge", 6), ("fallback", 2)]),  # source_total
            _Result(rows=[(_NOW, "exact", 1)]),  # faq_daily
            _Result(rows=[("exact", 1), ("semantic", 2), ("miss", 1)]),  # faq_total
            _Result(rows=[SimpleNamespace(intent="faq", cnt=8, avg_conf=0.9)]),  # intent_top
            _Result(one=SimpleNamespace(bot_turns=10, avg_conf=0.75, low_conf_bot=2)),  # confidence
            _Result(rows=[
                SimpleNamespace(agent_name="bot_agent", action="intent_classify", cnt=5, avg_ms=300.0, p95_ms=400.0),
                SimpleNamespace(agent_name="bot_agent", action="rag_retrieve", cnt=4, avg_ms=100.0, p95_ms=250.0),
            ]),  # latency
        ]
        fake = FakeSession(results)
        resp = await _get(_make_app(ADMIN, fake), "/api/admin/rag/quality-summary?days=7")
        assert resp.status_code == 200
        body = resp.json()
        assert body["days"] == 7
        assert body["daily_volume"][0]["turns"] == 10
        assert body["response_source"]["total"][0]["source"] == "knowledge"
        # hit_rate = (exact 1 + semantic 2) / (1+2+1) = 0.75
        assert body["faq"]["hit_rate"] == 0.75
        assert body["intent_top"][0]["intent"] == "faq"
        assert body["confidence"]["low_confidence_share"] == 0.2
        assert body["decision_latency"][1]["action"] == "rag_retrieve"
        assert body["decision_latency"][1]["p95_ms"] == 250.0

    async def test_days_out_of_range_rejected(self) -> None:
        fake = FakeSession([])
        resp = await _get(_make_app(ADMIN, fake), "/api/admin/rag/quality-summary?days=999")
        assert resp.status_code == 422


# ── RAG 实时指标 ────────────────────────────────────────────────────────────


class TestRagLiveMetrics:
    async def test_live_metrics_shape_and_histogram_folding(self) -> None:
        # 用真实 REGISTRY（shared.metrics 导入即注册），打一个点再读
        from lumio.shared.metrics import RETRIEVE_DURATION

        RETRIEVE_DURATION.labels(search_type="hybrid").observe(0.25)

        fake = FakeSession([])
        resp = await _get(_make_app(ADMIN, fake), "/api/admin/rag/live-metrics")
        assert resp.status_code == 200
        body = resp.json()
        assert body["retrieval"]["hybrid"]["count"] >= 1
        assert body["retrieval"]["hybrid"]["avg"] > 0
        assert isinstance(body["agent_responses"], dict)
        assert isinstance(body["faq_match"], dict)
        assert isinstance(body["circuit_breakers"], dict)
        assert "degradation_level" in body
        # 回归: Counter 的 *_created 时间戳样本不能被求和进计数 (修复前 ~1.8e9)
        assert body["fast_reply_total"] < 1e7
        assert body["bad_cases_total"] < 1e7

    async def test_denied_for_customer(self) -> None:
        fake = FakeSession([])
        resp = await _get(_make_app(CUSTOMER, fake), "/api/admin/rag/live-metrics")
        assert resp.status_code == 403


# ── 埋点冒烟：4 个新指标可被 REGISTRY 采集 ───────────────────────────────────


class TestMetricInstrumentation:
    async def test_new_metrics_registered(self) -> None:
        from prometheus_client import REGISTRY

        from lumio.shared.metrics import (
            CIRCUIT_BREAKER_STATE,
            FAQ_MATCH,
            RAG_CACHE_OPS,
            RERANK_DEGRADATION,
        )

        RAG_CACHE_OPS.labels(result="hit", search_type="hybrid").inc()
        RERANK_DEGRADATION.labels(reason="unavailable").inc()
        FAQ_MATCH.labels(match_type="exact").inc()
        CIRCUIT_BREAKER_STATE.labels(name="embedding").set(2)

        # prometheus_client 的 Counter family 名不带 _total 后缀，统一归一后比较
        family_names = {m.name.removesuffix("_total") for m in REGISTRY.collect()}
        assert {
            "lumio_rag_cache_ops",
            "lumio_rerank_degradation",
            "lumio_faq_match",
            "lumio_circuit_breaker_state",
        } <= family_names

    async def test_breaker_transition_reports_gauge(self) -> None:
        from prometheus_client import REGISTRY

        from lumio.services.common.circuit_breaker import CircuitBreaker

        breaker = CircuitBreaker(name="test-console-cb")
        breaker._transition_to_open()
        # 状态值映射正确：open → 2
        samples = [
            s
            for family in REGISTRY.collect()
            if family.name == "lumio_circuit_breaker_state"
            for s in family.samples
            if s.labels.get("name") == "test-console-cb"
        ]
        assert samples and samples[0].value == 2
