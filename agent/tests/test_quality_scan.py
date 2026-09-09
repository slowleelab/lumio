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
        v = _parse_verdict(
            {"verdict": "fail", "problems": [{"type": "A", "turn": 2, "reason": "答非所问"}], "summary": "x"}
        )
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
        judge.chat_json = AsyncMock(
            return_value={"verdict": "warn", "problems": [{"type": "E", "reason": "引导不足"}], "summary": "ok"}
        )
        v = await scan_session(
            MagicMock(), judge, MagicMock(), "s2", [{"speaker": "customer", "content": "怎么分期"}], "m"
        )
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
        assert (
            quality_scan._parse_redis_verdict("s1", '{"verdict": "BANANA", "scanned_at": "2026-09-01T10:00:00+00:00"}')
            is None
        )
        # scanned_at 缺失 = 幂等键缺失, 宁可不回填
        assert quality_scan._parse_redis_verdict("s1", '{"verdict": "pass"}') is None


def test_rubric_mentions_all_dimensions() -> None:
    """评分标准五维度与正确行为豁免都要在 prompt 里 (防误删)"""
    for dim in ("答非所问", "幻觉", "越界", "漏转人工", "未解决"):
        assert dim in QA_RUBRIC_PROMPT
    assert "不算问题" in QA_RUBRIC_PROMPT
    assert "JSON" in QA_RUBRIC_PROMPT


# ── 覆率加固: 巡检装载/单会话分支/后台任务/裁判构造/存量回填 ──


from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows


class _FakeSession:
    """按调用序返回预置结果 (计数器挂工厂: _load_sessions 会开两个会话连续查询)"""

    def __init__(self, owner: _FakeSF) -> None:
        self._owner = owner
        self.added: list = []
        self.commit_calls = 0

    async def execute(self, _q: object) -> _FakeResult:
        rows = self._owner._results[min(self._owner._i, len(self._owner._results) - 1)]
        self._owner._i += 1
        return _FakeResult(rows)

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        self.commit_calls += 1


class _FakeSF:
    def __init__(self, results: list) -> None:
        self._results = results
        self._i = 0
        self.last_session: _FakeSession | None = None

    def __call__(self) -> _FakeSF:
        return self

    async def __aenter__(self) -> _FakeSession:
        self.last_session = _FakeSession(self)
        return self.last_session

    async def __aexit__(self, *_a: object) -> bool:
        return False


def _turn(ts: datetime, sid: str = "s1", speaker: str = "customer", content: str = "查账单") -> SimpleNamespace:
    return SimpleNamespace(
        session_id=sid, speaker=speaker, content=content, intent=None, response_source=None, timestamp=ts
    )


class TestLoadSessions:
    @pytest.mark.asyncio
    async def test_happy_path_min_turns_filter(self) -> None:
        """≥2 轮会话入选, 1 轮会话被 min_turns 过滤; 会话时间=最后一轮"""
        t0 = datetime.now(UTC)
        sid_rows = [SimpleNamespace(session_id="s1"), SimpleNamespace(session_id="s2")]
        turn_rows = [
            _turn(t0, "s1"),
            _turn(t0 + timedelta(seconds=1), "s1", "bot", "已出账"),
            _turn(t0, "s2"),  # s2 仅 1 轮
        ]
        sf = _FakeSF([sid_rows, turn_rows])
        sessions, skipped, raw_n = await quality_scan._load_sessions(sf, lookback_hours=720, limit=10, sample_rate=1.0)
        assert raw_n == 2 and skipped == 0
        assert [s[0] for s in sessions] == ["s1"]
        assert sessions[0][2] == t0 + timedelta(seconds=1)

    @pytest.mark.asyncio
    async def test_exclude_checked_via_redis(self) -> None:
        """已检会话 (redis 有判定) 被排除并计入 skipped"""
        sid_rows = [SimpleNamespace(session_id="s1"), SimpleNamespace(session_id="s2")]
        # fake 不执行 where: 预置"真库在该过滤下会返回"的数据 (仅 s2 的轮次)
        turn_rows = [
            _turn(datetime.now(UTC), "s2"),
            _turn(datetime.now(UTC), "s2", "bot", "r"),
        ]
        redis = MagicMock()
        redis.mget = AsyncMock(return_value=["x", None])  # s1 已检
        sf = _FakeSF([sid_rows, turn_rows])
        sessions, skipped, raw_n = await quality_scan._load_sessions(
            sf, lookback_hours=720, limit=10, sample_rate=1.0, exclude_checked=True, redis_client=redis, offset=3
        )
        assert [s[0] for s in sessions] == ["s2"]
        assert skipped == 1 and raw_n == 2

    @pytest.mark.asyncio
    async def test_empty_and_redis_failure(self) -> None:
        """首批为空直接返回; mget 异常降级为不过滤"""
        sf = _FakeSF([[]])
        assert await quality_scan._load_sessions(sf, lookback_hours=1, limit=5, sample_rate=1.0) == ([], 0, 0)

        sid_rows = [SimpleNamespace(session_id="s1")]
        turn_rows = [_turn(datetime.now(UTC)), _turn(datetime.now(UTC), speaker="bot", content="r")]
        redis = MagicMock()
        redis.mget = AsyncMock(side_effect=Exception("redis down"))
        sf2 = _FakeSF([sid_rows, turn_rows])
        sessions, skipped, _ = await quality_scan._load_sessions(
            sf2, lookback_hours=1, limit=5, sample_rate=1.0, exclude_checked=True, redis_client=redis
        )
        assert [s[0] for s in sessions] == ["s1"] and skipped == 0

    @pytest.mark.asyncio
    async def test_sample_rate_downsamples(self) -> None:
        """sample_rate<1 抽样且不超过 limit"""
        sid_rows = [SimpleNamespace(session_id=f"s{i}") for i in range(6)]
        turn_rows = [
            r
            for i in range(6)
            for r in (
                _turn(datetime.now(UTC), f"s{i}"),
                _turn(datetime.now(UTC), f"s{i}", "bot", "r"),
            )
        ]
        sf = _FakeSF([sid_rows, turn_rows])
        sessions, _, _ = await quality_scan._load_sessions(sf, lookback_hours=1, limit=2, sample_rate=0.5)
        assert 1 <= len(sessions) <= 2


class TestScanSessionBranches:
    @pytest.mark.asyncio
    async def test_judge_failure_returns_error(self) -> None:
        judge = MagicMock()
        judge.chat_json = AsyncMock(side_effect=Exception("LLM 不可用"))
        v = await scan_session(MagicMock(), judge, None, "s1", [{"speaker": "customer", "content": "x"}], "m")
        assert v["verdict"] == "error"

    @pytest.mark.asyncio
    async def test_redis_and_db_failure_do_not_block(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Redis 写判定失败 / 判定落库失败 均不阻断, 判定照常返回"""
        judge = MagicMock()
        judge.chat_json = AsyncMock(return_value={"verdict": "pass", "problems": [], "summary": "ok"})
        redis = MagicMock()
        redis.setex = AsyncMock(side_effect=Exception("redis down"))
        rec = AsyncMock(side_effect=Exception("db down"))
        monkeypatch.setattr("lumio.services.common.badcase_store.record_quality", rec)
        v = await scan_session(
            MagicMock(),
            judge,
            redis,
            "s1",
            [
                {"speaker": "customer", "content": "你好"},
                {"speaker": "bot", "content": "您好"},
            ],
            "m",
        )
        assert v["verdict"] == "pass"
        assert redis.setex.await_count == 1

    @pytest.mark.asyncio
    async def test_fail_problem_turn_out_of_range_falls_back(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """problem.turn 超出轮数范围 → 回退首轮客户输入, bot_output None"""
        recorded = MagicMock()
        recorded.id = "bc-2"

        async def fake_capture(_sf, **kw):
            return recorded

        monkeypatch.setattr("lumio.services.common.badcase_store.capture_badcase", fake_capture)
        monkeypatch.setattr("lumio.services.common.badcase_store.record_quality", AsyncMock())
        judge = MagicMock()
        judge.chat_json = AsyncMock(
            return_value={"verdict": "fail", "problems": [{"type": "A", "turn": 99, "reason": "r"}], "summary": "s"}
        )
        redis = MagicMock()
        redis.setex = AsyncMock()
        v = await scan_session(
            MagicMock(),
            judge,
            redis,
            "s1",
            [{"speaker": "customer", "content": "首轮"}, {"speaker": "bot", "content": "回复"}],
            "m",
        )
        assert v["verdict"] == "fail"


class TestScanSessionById:
    @pytest.mark.asyncio
    async def test_dedup_and_force(self, monkeypatch: pytest.MonkeyPatch) -> None:
        redis = MagicMock()
        redis.get = AsyncMock(return_value='{"verdict":"pass"}')
        assert await quality_scan.scan_session_by_id(MagicMock(), MagicMock(), redis, "s1", "m") is None

        called: dict = {}

        async def fake_scan(sf, judge, rc, sid, turns, model, session_time=None):
            called["sid"] = sid
            return {"verdict": "pass", "problems": [], "summary": ""}

        monkeypatch.setattr(quality_scan, "scan_session", fake_scan)
        redis.get = AsyncMock(return_value=None)
        v = await quality_scan.scan_session_by_id(
            _FakeSF(
                [
                    [
                        SimpleNamespace(
                            session_id="s1",
                            speaker="customer",
                            content="a",
                            intent=None,
                            response_source=None,
                            timestamp=datetime.now(UTC),
                        ),
                        SimpleNamespace(
                            session_id="s1",
                            speaker="bot",
                            content="b",
                            intent=None,
                            response_source=None,
                            timestamp=datetime.now(UTC),
                        ),
                    ]
                ]
            ),
            MagicMock(),
            redis,
            "s1",
            "m",
            force=True,
        )
        assert v["verdict"] == "pass" and called["sid"] == "s1"

    @pytest.mark.asyncio
    async def test_short_session_skipped(self) -> None:
        sf = _FakeSF(
            [
                [
                    SimpleNamespace(
                        session_id="s1",
                        speaker="customer",
                        content="a",
                        intent=None,
                        response_source=None,
                        timestamp=datetime.now(UTC),
                    )
                ]
            ]
        )
        assert await quality_scan.scan_session_by_id(sf, MagicMock(), None, "s1", "m") is None


class TestBuildJudge:
    def test_none_client_and_local(self) -> None:
        assert quality_scan.build_judge_llm(None, MagicMock()) is None
        s = MagicMock()
        s.llm.judge_base_url = ""
        s.llm.judge_api_key = ""
        llm = MagicMock()
        assert quality_scan.build_judge_llm(llm, s) is llm
        assert quality_scan.judge_model_name(s) == str(s.llm.primary_model)

    def test_remote_judge(self) -> None:
        from lumio.services.common.judge_client import RemoteJudgeClient

        s = MagicMock()
        s.llm.judge_base_url = "https://judge.example.com/v1"
        s.llm.judge_api_key = "sk-x"
        llm = MagicMock()
        built = quality_scan.build_judge_llm(llm, s)
        assert isinstance(built, RemoteJudgeClient)
        assert quality_scan.judge_model_name(s) == str(s.llm.judge_model)


class TestStartScanTask:
    @pytest.mark.asyncio
    async def test_task_runs_batches_and_writes_last_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        t0 = datetime.now(UTC)
        calls = {"n": 0}

        async def fake_load(_sf, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return [("s1", [{"speaker": "customer", "content": "a"}], t0)], 0, 1
            return [], 0, 0  # 连续两个短批 → 翻尽

        async def fake_scan(_sf, _judge, _rc, sid, _turns, _model, session_time=None):
            return {"verdict": "pass", "problems": [], "summary": ""}

        monkeypatch.setattr(quality_scan, "_load_sessions", fake_load)
        monkeypatch.setattr(quality_scan, "scan_session", fake_scan)
        redis = MagicMock()
        redis.setex = AsyncMock()

        assert quality_scan.start_scan(MagicMock(), MagicMock(), redis, "m", limit=10) is True
        assert quality_scan.start_scan(MagicMock(), MagicMock(), redis, "m", limit=10) is False  # 已在跑
        await asyncio.gather(*list(quality_scan._scan_tasks))

        st = quality_scan.scan_status()
        assert st["running"] is False and st["done"] == 1 and st["n_pass"] == 1 and st["error_msg"] == ""
        assert redis.setex.await_count == 1  # last_run 写入

    @pytest.mark.asyncio
    async def test_scan_error_counted_and_task_exception_recorded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import asyncio

        t0 = datetime.now(UTC)
        load_calls = {"n": 0}

        async def fake_load(_sf, **kw):
            load_calls["n"] += 1
            if load_calls["n"] == 1:
                return [("s1", [{"speaker": "customer", "content": "a"}], t0)], 0, 1
            return [], 0, 0

        async def bad_scan(*_a, **_kw):
            raise Exception("judge timeout")

        monkeypatch.setattr(quality_scan, "_load_sessions", fake_load)
        monkeypatch.setattr(quality_scan, "scan_session", bad_scan)
        redis = MagicMock()
        redis.setex = AsyncMock()
        quality_scan.start_scan(MagicMock(), MagicMock(), redis, "m", limit=5)
        await asyncio.gather(*list(quality_scan._scan_tasks))
        st = quality_scan.scan_status()
        assert st["done"] == 1 and st["n_error"] == 1

        # 任务级异常 (装载抛错) → error_msg 记录
        async def boom_load(*_a, **_kw):
            raise Exception("db gone")

        monkeypatch.setattr(quality_scan, "_load_sessions", boom_load)
        quality_scan.start_scan(MagicMock(), MagicMock(), redis, "m", limit=5)
        await asyncio.gather(*list(quality_scan._scan_tasks))
        assert "db gone" in quality_scan.scan_status()["error_msg"]

    @pytest.mark.asyncio
    async def test_last_run(self) -> None:
        assert await quality_scan.last_run(None) is None
        redis = MagicMock()
        redis.get = AsyncMock(return_value='{"total": 3}')
        assert (await quality_scan.last_run(redis))["total"] == 3
        redis.get = AsyncMock(side_effect=Exception("x"))
        assert await quality_scan.last_run(redis) is None


class TestBackfillRedisVerdicts:
    @staticmethod
    def _redis_with(payloads: dict[str, str]) -> MagicMock:
        redis = MagicMock()

        async def _scan_iter(match=None):
            for k in payloads:
                yield k

        async def _get(k):
            return payloads.get(k)

        redis.scan_iter = _scan_iter
        redis.get = _get
        return redis

    @pytest.mark.asyncio
    async def test_none_args_and_empty(self) -> None:
        assert await quality_scan.backfill_redis_verdicts(None, MagicMock()) == 0
        assert await quality_scan.backfill_redis_verdicts(MagicMock(), None) == 0
        redis = self._redis_with({})
        assert await quality_scan.backfill_redis_verdicts(MagicMock(), redis) == 0

    @pytest.mark.asyncio
    async def test_backfill_inserts_new_and_skips_existing(self) -> None:
        """新增判定入库 (fail 关联最新 qa_scan 案例, pass 带预览), 已存在跳过"""
        t0 = datetime.now(UTC)
        scanned = datetime.now(UTC) - timedelta(hours=1)
        redis = self._redis_with(
            {
                "lumio:qa:verdict:s1": f'{{"verdict":"fail","problems":[{{"type":"A"}}],"summary":"x",'
                f'"model":"GLM","turns":2,"scanned_at":"{scanned.isoformat()}"}}',
                "lumio:qa:verdict:s2": f'{{"verdict":"pass","summary":"ok","model":"GLM",'
                f'"turns":2,"scanned_at":"{scanned.isoformat()}"}}',
                "lumio:qa:verdict:s3": "not-json",  # 解析失败过滤
                "lumio:qa:verdict:s4": '{"verdict":"pass"}',  # 缺 scanned_at 过滤
            }
        )
        turn_rows = [
            SimpleNamespace(session_id="s1", speaker="customer", content="问题句", timestamp=t0),
            SimpleNamespace(session_id="s1", speaker="bot", content="回复", timestamp=t0),
            SimpleNamespace(session_id="s2", speaker="customer", content="你好", timestamp=t0),
            SimpleNamespace(session_id="s2", speaker="bot", content="您好", timestamp=t0),
        ]
        bc_rows = [SimpleNamespace(id="bc-9", session_id="s1")]
        # existing: s2 同 scanned_at 已入库 → 跳过
        existing_rows = [("s2", scanned)]
        sf = _FakeSF([turn_rows, bc_rows, existing_rows])

        inserted = await quality_scan.backfill_redis_verdicts(sf, redis)
        assert inserted == 1
        rec = sf.last_session.added[0]
        assert rec.session_id == "s1" and rec.verdict == "fail"
        assert rec.badcase_id == "bc-9" and rec.preview == "问题句"
        assert sf.last_session.commit_calls == 1

    @pytest.mark.asyncio
    async def test_backfill_exception_returns_zero(self) -> None:
        scanned = datetime.now(UTC).isoformat()
        redis = self._redis_with(
            {"lumio:qa:verdict:s1": f'{{"verdict":"pass","summary":"ok","scanned_at":"{scanned}"}}'}
        )

        class _BoomSF(_FakeSF):
            async def __aenter__(self):  # type: ignore[override]
                raise Exception("db down")

        assert await quality_scan.backfill_redis_verdicts(_BoomSF([]), redis) == 0
