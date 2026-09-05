"""全量质检巡检单元测试 (qa_scan: 从原始对话内容分析质量, 不依赖置信度/信号)"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from lumio.services.common import quality_scan
from lumio.services.common.quality_scan import (
    QA_RUBRIC_PROMPT,
    _parse_verdict,
    build_transcript,
    scan_session,
)


class TestBuildTranscript:
    def test_roles_and_order(self) -> None:
        turns = [
            {"speaker": "customer", "content": "帮我查账单"},
            {"speaker": "bot", "content": "请问您要查哪个月的账单？"},
        ]
        t = build_transcript(turns)
        assert "[1] 客户: 帮我查账单" in t
        assert "[2] 客服: 请问您要查哪个月的账单？" in t

    def test_budget_truncation_keeps_ends(self) -> None:
        turns = [
            {"speaker": "customer", "content": "开头标记" + "长" * 4000},
            {"speaker": "bot", "content": "结尾标记"},
        ]
        t = build_transcript(turns)
        assert len(t) <= quality_scan._TRANSCRIPT_BUDGET + 40
        assert "开头标记" in t and "结尾标记" in t
        assert "中段截断" in t

    def test_empty_content_skipped(self) -> None:
        turns = [{"speaker": "customer", "content": "  "}, {"speaker": "bot", "content": "回复"}]
        # 空内容跳过但轮次序号保留原位 (问题轮次引用与原始对话对齐)
        assert build_transcript(turns) == "[2] 客服: 回复"


class TestParseVerdict:
    def test_normal_fail(self) -> None:
        v = _parse_verdict({"verdict": "fail", "problems": [{"type": "A", "turn": 2, "reason": "答非所问"}], "summary": "x"})
        assert v["verdict"] == "fail" and len(v["problems"]) == 1

    def test_fail_without_problems_downgrades_to_pass(self) -> None:
        """无证据不下判 — 裁判误输出 fail 但没给 problems 时保守放行"""
        assert _parse_verdict({"verdict": "fail", "problems": []})["verdict"] == "pass"

    def test_invalid_enum_and_missing_fields(self) -> None:
        assert _parse_verdict({"verdict": "BANANA"})["verdict"] == "pass"
        assert _parse_verdict({})["verdict"] == "pass"
        assert _parse_verdict({"verdict": "warn", "problems": "not-a-list"})["verdict"] == "pass"

    def test_problems_capped(self) -> None:
        v = _parse_verdict({"verdict": "warn", "problems": [{"type": "A"}] * 9})
        assert len(v["problems"]) == 5


class TestScanSession:
    @pytest.mark.asyncio
    async def test_fail_captures_badcase(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """fail → 采集 badcase (signal_source=qa_scan), 判定写 Redis 去重"""
        captured: dict = {}

        recorded = MagicMock()
        recorded.id = "bc-1"

        async def fake_capture(sf, **kw):
            captured.update(kw)
            return recorded

        monkeypatch.setattr("lumio.services.common.badcase_store.capture_badcase", fake_capture)

        judge = MagicMock()
        judge.chat_json = AsyncMock(
            return_value={
                "verdict": "fail",
                "problems": [{"type": "A", "turn": 1, "reason": "回复与账单问题无关"}],
                "summary": "答非所问",
            }
        )
        redis = MagicMock()
        redis.setex = AsyncMock()

        turns = [
            {"speaker": "customer", "content": "帮我查账单", "intent": "bill_query", "response_source": None},
            {"speaker": "bot", "content": "信用卡挂失请致电客服热线。", "intent": None, "response_source": "knowledge"},
        ]
        rec_mock = AsyncMock()
        monkeypatch.setattr("lumio.services.common.badcase_store.record_quality", rec_mock)

        from datetime import UTC, datetime

        session_time = datetime.now(UTC)
        v = await scan_session(MagicMock(), judge, redis, "s1", turns, model="GLM-5.3-Flash", session_time=session_time)

        assert v["verdict"] == "fail"
        assert captured["signal_source"] == "qa_scan"
        assert captured["user_input"] == "帮我查账单"
        assert "挂失" in captured["bot_output"]
        assert captured["signal_detail"]["judge_model"] == "GLM-5.3-Flash"
        assert captured["session_time"] == session_time, "会话时间锚点应透传到 badcase"
        assert redis.setex.await_count == 1
        # fail 判定同时落质检记录 (badcase_id 关联), 每一个会话都进质检列表
        assert rec_mock.await_count == 1
        kw = rec_mock.await_args.kwargs
        assert kw["verdict"] == "fail"
        assert kw["badcase_id"] == "bc-1"
        assert kw["session_time"] == session_time
        assert kw["preview"] == "帮我查账单"

    @pytest.mark.asyncio
    async def test_pass_no_capture_but_recorded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """pass 不采集 badcase, 但判定照样落质检记录 (全量纳入口径)"""
        async def fake_capture(sf, **kw):
            raise AssertionError("pass 不应采集")

        monkeypatch.setattr("lumio.services.common.badcase_store.capture_badcase", fake_capture)
        rec_mock = AsyncMock()
        monkeypatch.setattr("lumio.services.common.badcase_store.record_quality", rec_mock)
        judge = MagicMock()
        judge.chat_json = AsyncMock(return_value={"verdict": "warn", "problems": [{"type": "E", "reason": "引导不足"}], "summary": "ok"})
        v = await scan_session(MagicMock(), judge, MagicMock(), "s2", [{"speaker": "customer", "content": "怎么分期"}], "m")
        assert v["verdict"] == "warn"
        assert rec_mock.await_count == 1
        kw = rec_mock.await_args.kwargs
        assert kw["verdict"] == "warn"
        assert kw["badcase_id"] is None
        assert kw["preview"] == "怎么分期"

    @pytest.mark.asyncio
    async def test_judge_error_returns_error_verdict(self) -> None:
        """裁判调用失败 → error 判定, 不写 Redis/不落库 (下轮重扫)"""
        judge = MagicMock()
        judge.chat_json = AsyncMock(side_effect=RuntimeError("judge down"))
        v = await scan_session(MagicMock(), judge, None, "s3", [{"speaker": "customer", "content": "x"}], "m")
        assert v["verdict"] == "error"

    @pytest.mark.asyncio
    async def test_by_id_skips_already_scanned(self) -> None:
        """chat_end 钩子: 30 天内已检会话直接跳过 (不打 DB)"""
        redis = MagicMock()
        redis.get = AsyncMock(return_value='{"verdict": "pass"}')
        factory = MagicMock()
        v = await quality_scan.scan_session_by_id(factory, MagicMock(), redis, "s9", "m")
        assert v is None
        factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_by_id_skips_short_session(self) -> None:
        """对话不足 2 轮 (问候/噪声) 不进质检"""
        redis = MagicMock()
        redis.get = AsyncMock(return_value=None)

        async def fake_load_turns(sf, sid):
            return [{"speaker": "customer", "content": "你好"}], None

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(quality_scan, "_load_session_turns", fake_load_turns)
            v = await quality_scan.scan_session_by_id(MagicMock(), MagicMock(), redis, "s10", "m")
        assert v is None


class TestJudgeHelpers:
    def _settings(self, remote: bool) -> object:
        from types import SimpleNamespace

        llm = SimpleNamespace(
            judge_base_url="https://x" if remote else "",
            judge_api_key="k" if remote else "",
            judge_model="GLM-5.3-Flash",
            primary_model="qwen2.5:7b",
        )
        return SimpleNamespace(llm=llm)

    def test_judge_model_name_remote_vs_local(self) -> None:
        assert quality_scan.judge_model_name(self._settings(True)) == "GLM-5.3-Flash"
        assert quality_scan.judge_model_name(self._settings(False)) == "qwen2.5:7b"

    def test_build_judge_llm_none_when_not_ready(self) -> None:
        assert quality_scan.build_judge_llm(None, self._settings(True)) is None


class TestRedisBackfill:
    def test_parse_redis_verdict_ok(self) -> None:
        raw = '{"verdict": "pass", "problems": [], "summary": "ok", "model": "GLM-5.3-Flash", "turns": 4, "scanned_at": "2026-09-01T10:00:00+00:00"}'
        r = quality_scan._parse_redis_verdict("s1", raw)
        assert r is not None
        assert r["verdict"] == "pass" and r["turns"] == 4 and r["judge_model"] == "GLM-5.3-Flash"

    def test_parse_redis_verdict_rejects_bad(self) -> None:
        assert quality_scan._parse_redis_verdict("s1", "not-json") is None
        assert quality_scan._parse_redis_verdict("s1", '{"verdict": "BANANA", "scanned_at": "2026-09-01T10:00:00+00:00"}') is None
        # scanned_at 缺失 = 幂等键缺失, 宁可不回填
        assert quality_scan._parse_redis_verdict("s1", '{"verdict": "pass"}') is None


def test_rubric_mentions_all_dimensions() -> None:
    """评分标准五维度与正确行为豁免都要在 prompt 里 (防误删)"""
    for dim in ("答非所问", "幻觉", "越界", "漏转人工", "未解决"):
        assert dim in QA_RUBRIC_PROMPT
    assert "不算问题" in QA_RUBRIC_PROMPT
    assert "JSON" in QA_RUBRIC_PROMPT
