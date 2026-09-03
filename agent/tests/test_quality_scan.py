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

        async def fake_capture(sf, **kw):
            captured.update(kw)
            return MagicMock()

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
        v = await scan_session(MagicMock(), judge, redis, "s1", turns, model="GLM-5.3-Flash")

        assert v["verdict"] == "fail"
        assert captured["signal_source"] == "qa_scan"
        assert captured["user_input"] == "帮我查账单"
        assert "挂失" in captured["bot_output"]
        assert captured["signal_detail"]["judge_model"] == "GLM-5.3-Flash"
        assert redis.setex.await_count == 1

    @pytest.mark.asyncio
    async def test_pass_no_capture(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_capture(sf, **kw):
            raise AssertionError("pass 不应采集")

        monkeypatch.setattr("lumio.services.common.badcase_store.capture_badcase", fake_capture)
        judge = MagicMock()
        judge.chat_json = AsyncMock(return_value={"verdict": "pass", "problems": [], "summary": "ok"})
        v = await scan_session(MagicMock(), judge, MagicMock(), "s2", [{"speaker": "customer", "content": "x"}], "m")
        assert v["verdict"] == "pass"

    @pytest.mark.asyncio
    async def test_judge_error_returns_error_verdict(self) -> None:
        judge = MagicMock()
        judge.chat_json = AsyncMock(side_effect=RuntimeError("judge down"))
        v = await scan_session(MagicMock(), judge, None, "s3", [{"speaker": "customer", "content": "x"}], "m")
        assert v["verdict"] == "error"


def test_rubric_mentions_all_dimensions() -> None:
    """评分标准五维度与正确行为豁免都要在 prompt 里 (防误删)"""
    for dim in ("答非所问", "幻觉", "越界", "漏转人工", "未解决"):
        assert dim in QA_RUBRIC_PROMPT
    assert "不算问题" in QA_RUBRIC_PROMPT
    assert "JSON" in QA_RUBRIC_PROMPT
