"""闭环路由端点进程内直调测试 (覆盖率口径: HTTP 服务器型测试跑在子进程, 不计入主进程覆盖)。

全部 monkeypatch 掉外部依赖 (store/LLM/Redis), 只验证端点自身的参数分支、
错误码与返回组装。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import lumio.services.common.closed_loop_router as clr
from lumio.shared.exceptions import LumioError


def _request(state: dict[str, Any] | None = None) -> MagicMock:
    req = MagicMock()
    req.app.state = SimpleNamespace(**(state or {}))
    return req


class _SeqResult:
    """按脚本喂 scalar()/all(): 标量走 scalar, 列表走 all"""

    def __init__(self, v: Any) -> None:
        self._v = v

    def scalar(self):
        return self._v if not isinstance(self._v, list) else None

    def all(self):
        return self._v if isinstance(self._v, list) else []


class TestListAndStats:
    async def test_badcases_list_passthrough(self, monkeypatch: pytest.MonkeyPatch) -> None:
        row = {"id": "b1", "user_input": "x"}
        monkeypatch.setattr(clr, "list_badcases", AsyncMock(return_value=([row], 1)))
        out = await clr.list_badcases_endpoint(user=None, db=MagicMock(), keyword="x", limit=5, offset=0)
        assert out == {"total": 1, "badcases": [row]}

    async def test_badcase_stats_endpoint(self) -> None:
        db = MagicMock()
        script = iter(
            [
                _SeqResult(628),  # total
                _SeqResult(519),  # pending_review
                _SeqResult(109),  # confirmed
                _SeqResult(1),  # today_new
                _SeqResult(126),  # deployed
                _SeqResult(4),  # human_confirmed
                _SeqResult([("layer_3", 67), ("layer_5", 52)]),  # layer_dist
                _SeqResult([("qa_scan", 358)]),  # signal_dist
            ]
        )
        db.execute = AsyncMock(side_effect=lambda _q: next(script))
        out = await clr.badcase_stats(user=None, db=db)
        assert out["total"] == 628 and out["pending_review"] == 519
        assert out["deployed"] == 126 and out["llm_pass_rate"] == round(105 / 109, 3)
        assert out["layer_dist"] == {"layer_3": 67, "layer_5": 52}
        assert out["signal_dist"] == {"qa_scan": 358}


class TestQualityReads:
    async def test_sessions_records_coverage_trend(self, monkeypatch: pytest.MonkeyPatch) -> None:
        row = {"session_id": "s1", "qc_status": "ai", "review_status": None}
        monkeypatch.setattr("lumio.services.common.badcase_store.list_qc_sessions", AsyncMock(return_value=([row], 1)))
        out = await clr.quality_sessions_endpoint(user=None, db=MagicMock(), category="all")
        assert out == {"total": 1, "sessions": [row]}

        with pytest.raises(LumioError):
            await clr.quality_sessions_endpoint(user=None, db=MagicMock(), category="bogus")

        rec = {"session_id": "s1", "verdict": "pass"}
        monkeypatch.setattr(
            "lumio.services.common.badcase_store.list_quality_records", AsyncMock(return_value=([rec], 1))
        )
        out2 = await clr.quality_records_endpoint(user=None, db=MagicMock(), limit=5)
        assert out2["records"] == [rec]

        cov = {"total_sessions": 10, "scanned_sessions": 9, "coverage": 0.9, "pass_rate": 0.8, "by_verdict": {}}
        monkeypatch.setattr("lumio.services.common.badcase_store.quality_coverage_stats", AsyncMock(return_value=cov))
        assert (await clr.quality_coverage_endpoint(user=None, db=MagicMock()))["coverage"] == 0.9

        pts = [{"date": "2026-01-01", "pass": 1, "warn": 0, "fail": 0, "new_cases": 0}]
        monkeypatch.setattr("lumio.services.common.badcase_store.quality_trend", AsyncMock(return_value=pts))
        assert (await clr.quality_trend_endpoint(user=None, db=MagicMock(), days=1))["days"] == pts


class TestHumanVerdictAndRescan:
    async def test_human_verdict(self, monkeypatch: pytest.MonkeyPatch) -> None:
        rec = SimpleNamespace(verdict="pass", judge_model="人工判定", scanned_at=None)
        monkeypatch.setattr("lumio.services.common.badcase_store.record_human_verdict", AsyncMock(return_value=rec))
        out = await clr.quality_human_verdict_endpoint(
            user=None, db=MagicMock(), body={"session_id": "s1", "verdict": "pass", "note": "复核无误"}
        )
        assert out["status"] == "ok" and out["judge_model"] == "人工判定"

        with pytest.raises(LumioError):
            await clr.quality_human_verdict_endpoint(user=None, db=MagicMock(), body={})

    async def test_rescan(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import lumio.services.common.quality_scan as qs

        monkeypatch.setattr(qs, "scan_session_by_id", AsyncMock(return_value=None))
        req = _request({"db_session_factory": object(), "redis_client": object(), "llm_client": object()})
        out = await clr.quality_rescan_endpoint(user=None, request=req, body={"session_id": "s1"})
        assert out["status"] == "skipped"

        with pytest.raises(LumioError):
            await clr.quality_rescan_endpoint(user=None, request=req, body={})


class TestStatusEndpoints:
    async def test_scan_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import lumio.services.common.quality_scan as qs

        monkeypatch.setattr(qs, "scan_status", lambda redis_client=None: {"running": False})
        monkeypatch.setattr(qs, "last_run", AsyncMock(return_value={"total": 3}))
        out = await clr.quality_scan_status(user=None, request=_request({"redis_client": object()}))
        assert out["running"] is False and out["last_run"]["total"] == 3

    async def test_replay_missing_sid_raises(self) -> None:
        with pytest.raises(LumioError):
            await clr.quality_replay_endpoint(user=None, request=_request({}), body={})


class TestFaqRouterDirect:
    """faq_router 直调 (HTTP 型 CRUD 回路测试在子进程不计覆盖, 此处进程内补)"""

    async def test_list_and_get(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import lumio.services.common.faq_router as fr

        faq = {"id": "f1", "question": "q", "approval_status": "PUBLISHED"}
        monkeypatch.setattr(fr, "list_faqs", AsyncMock(return_value=([faq], 1)))
        out = await fr.list_faqs_endpoint(
            request=_request({"db_session_factory": object()}), approval_status="PUBLISHED"
        )
        assert out["faqs"] == [faq] and out["total"] == 1

        monkeypatch.setattr(fr, "get_faq", AsyncMock(return_value=faq))
        out2 = await fr.get_faq_endpoint("f1", request=_request({"db_session_factory": object()}))
        assert out2["question"] == "q"

    async def test_create_with_duplicate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import lumio.services.common.faq_router as fr

        created = SimpleNamespace(id="f2", approval_status="DRAFT")
        monkeypatch.setattr(fr, "check_faq_duplicate", AsyncMock(return_value=[]))
        monkeypatch.setattr(fr, "create_faq", AsyncMock(return_value=created))
        req = _request({"db_session_factory": object()})
        user = SimpleNamespace(user_id="u1")
        body = fr.FaqCreateRequest(question="新问题?", answer="答案", category="general")
        out = await fr.create_faq_endpoint(body, req, user)
        assert out == {"faq_id": "f2", "approval_status": "DRAFT"}

        # 相似重复 → 409 JSONResponse
        import httpx

        monkeypatch.setattr(fr, "check_faq_duplicate", AsyncMock(return_value=[{"faq_id": "f1", "question": "旧问题"}]))
        resp = await fr.create_faq_endpoint(body, req, user)
        assert isinstance(resp, httpx.Response) is False and getattr(resp, "status_code", None) == 409

    async def test_update_and_approval_chain(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import lumio.services.common.faq_router as fr

        updated = SimpleNamespace(id="f1", question="q2")
        monkeypatch.setattr(fr, "update_faq", AsyncMock(return_value=updated))
        # 草稿态更新跳过索引联动 (_load_faq_orm 也一并 mock, 不触真工厂)
        monkeypatch.setattr(fr, "_load_faq_orm", AsyncMock(return_value=SimpleNamespace(approval_status="DRAFT")))
        req = _request({"db_session_factory": object()})
        body = fr.FaqUpdateRequest(answer="新答案")
        out = await fr.update_faq_endpoint("f1", body, req, user=SimpleNamespace(user_id="u1"))
        assert out["status"] == "ok"

        for action, target in [
            (fr.submit_faq, "IN_REVIEW"),
            (fr.approve_faq, "APPROVED"),
            (fr.publish_faq, "PUBLISHED"),
            (fr.archive_faq, "ARCHIVED"),
        ]:
            monkeypatch.setattr(fr, "_do_approval", AsyncMock(return_value={"id": "f1", "approval_status": target}))
            r = await action("f1", fr.FaqApprovalRequest(comment="cov"), req, user=SimpleNamespace(user_id="u1"))
            assert r["approval_status"] == target

    async def test_create_without_db_raises(self) -> None:
        import lumio.services.common.faq_router as fr

        body = fr.FaqCreateRequest(question="q", answer="a", category="general")
        with pytest.raises(LumioError):
            await fr.create_faq_endpoint(body, _request({}), SimpleNamespace(user_id="u"))
