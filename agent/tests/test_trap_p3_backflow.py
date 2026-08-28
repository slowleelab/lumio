"""闭环 P3 单元测试: 误杀回流探针 + record_backflow + 噪声闸决策报表.

全部纯逻辑 + 临时文件 + mock, 无 DB / 无 torch.
覆盖:
  - LumioAgent._mark_reply_pass / _maybe_flag_mis_kill 感知→归因接线
  - TrapCollector.record_backflow (JSONL 追加 + PII 打码 + 失败降级)
  - default_trap_collector 单例与 reset
  - DecisionLogger.query_noise_gate_stats (无 PG 返回空, 不阻断)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lumio.services.bot.bot_agent import (
    _REPLY_PASS_FRESH_SECONDS,
    _REPLY_PASS_MAX_SESSIONS,
    LumioAgent,
)
from lumio.services.common.decision_log import DecisionAction, DecisionLogger
from lumio.services.common.trap_collector import (
    TrapCollector,
    _backflow_path,
    default_trap_collector,
    reset_default_trap,
)

# ── 动作枚举基线 ──────────────────────────────────────────────────────────────


class TestMisKillAction:
    def test_mis_kill_candidate_action_exists(self) -> None:
        assert DecisionAction.MIS_KILL_CANDIDATE == "mis_kill_candidate"


# ── record_backflow: staging 落盘 ───────────────────────────────────────────


class TestRecordBackflow:
    # 344: 用手机号/卡号验证 PII 打码
    _PII_TEXT = "请打 13800138000 或卡号 6222000011112222 处理"
    _PATCH_TARGET = "lumio.services.common.trap_collector._backflow_path"

    def _path(self, tmp_path: Path) -> Path:
        return tmp_path / "backflow.jsonl"

    def test_append_jsonl_and_mask_pii(self, tmp_path: Path) -> None:
        path = self._path(tmp_path)
        with patch(self._PATCH_TARGET, return_value=path):
            collector = TrapCollector()
            assert (
                collector.record_backflow(
                    text=self._PII_TEXT, session_id="s-1", reason="reply_pass_then_blocked:low_conf"
                )
                is True
            )
            assert (
                collector.record_backflow(text="干净的句子", session_id="s-2", reason="reply_pass_then_blocked:noise")
                is True
            )

        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        row = json.loads(lines[0])
        assert row["session_id"] == "s-1"
        assert row["reason"] == "reply_pass_then_blocked:low_conf"
        # 手机号/卡号已打码
        assert "13800138000" not in row["text"]
        assert "6222000011112222" not in row["text"]
        # 非 PII 行原样保留
        assert json.loads(lines[1])["text"] == "干净的句子"
        assert "ts" in row  # 时间戳字段

    def test_failure_returns_false_does_not_raise(self, tmp_path: Path) -> None:
        with patch(self._PATCH_TARGET, side_effect=PermissionError("denied")):
            assert TrapCollector().record_backflow(text="x", session_id="s", reason="r") is False

    def test_default_singleton_and_reset(self) -> None:
        reset_default_trap()
        a = default_trap_collector()
        b = default_trap_collector()
        assert a is b
        reset_default_trap()
        assert default_trap_collector() is not a


# ── LumioAgent 感知→归因接线 ────────────────────────────────────────────────


class TestMisKillWiring:
    @pytest.fixture
    def agent(self) -> LumioAgent:
        return LumioAgent(
            classifier=MagicMock(),
            degradation_mgr=MagicMock(),
            transfer_checker=MagicMock(),
            session_manager=MagicMock(),
        )

    async def test_reply_pass_then_block_flags_mis_kill(self, agent: LumioAgent) -> None:
        recorded: list[dict] = []

        def fake_record(**kw: object) -> bool:
            recorded.append(kw)
            return True

        with (
            patch(
                "lumio.services.common.trap_collector.default_trap_collector",
                return_value=MagicMock(record_backflow=fake_record),
            ),
            patch("lumio.services.bot.bot_agent.log_decision", new=MagicMock()) as log_patch,
        ):
            # 上轮: 回话放行
            agent._mark_reply_pass("sess", "嗯好的")
            assert agent._recent_reply_pass.get("sess")["input"] == "嗯好的"
            # 本轮: 又被噪声门拦 → 触发疑似误杀
            await agent._maybe_flag_mis_kill("sess", "嗯", "low_conf")

        # 回流样本 + 决策留痕都已触达
        assert recorded and recorded[0]["reason"].startswith("reply_pass_then_blocked:")
        assert recorded[0]["extra"]["prev_reply_input"] == "嗯好的"
        # MIS_KILL_CANDIDATE 已留痕
        assert log_patch.call_args.kwargs["action"] == DecisionAction.MIS_KILL_CANDIDATE

    async def test_no_prior_pass_is_noop(self, agent: LumioAgent) -> None:
        with (
            patch(
                "lumio.services.common.trap_collector.default_trap_collector",
                return_value=MagicMock(record_backflow=MagicMock(return_value=True)),
            ) as collector_patch,
            patch("lumio.services.bot.bot_agent.log_decision", new=MagicMock()) as log_patch,
        ):
            # 没有上轮"回话放行"记录 → 不判误杀
            await agent._maybe_flag_mis_kill("fresh-sess", "随便说说", "noise")
        collector_patch.return_value.record_backflow.assert_not_called()
        log_patch.assert_not_called()

    async def test_empty_session_noop_and_exception_soft(self, agent: LumioAgent) -> None:
        # 空 session_id 直接返回
        await agent._maybe_flag_mis_kill("", "x", "r")
        # record 抛异常也不阻断
        agent._mark_reply_pass("s", "嗯")
        with (
            patch(
                "lumio.services.common.trap_collector.default_trap_collector",
                return_value=MagicMock(record_backflow=MagicMock(side_effect=RuntimeError("boom"))),
            ),
            patch("lumio.services.bot.bot_agent.log_decision", new=MagicMock()),
        ):
            await agent._maybe_flag_mis_kill("s", "嗯", "r")  # 不应 raise

    async def test_stale_reply_pass_not_flagged(self, agent: LumioAgent) -> None:
        """放行记录已超过新鲜度窗口 → 即便本轮被拦也不算误杀 (不过久错归因)."""
        with (
            patch(
                "lumio.services.common.trap_collector.default_trap_collector",
                return_value=MagicMock(record_backflow=MagicMock(return_value=True)),
            ) as collector_patch,
            patch("lumio.services.bot.bot_agent.log_decision", new=MagicMock()) as log_patch,
        ):
            import time as _t

            agent._mark_reply_pass("stale", "嗯好的")
            # 把放行时间人为拨老(超过窗口), 模拟"放行后很久才来一句被拦"
            agent._recent_reply_pass["stale"]["ts"] = _t.monotonic() - (_REPLY_PASS_FRESH_SECONDS + 10)
            await agent._maybe_flag_mis_kill("stale", "嗯", "noise")
        collector_patch.return_value.record_backflow.assert_not_called()
        log_patch.assert_not_called()

    def test_reply_pass_cap_evicts_oldest(self, agent: LumioAgent) -> None:
        """定长 LRU: 超过上限时淘汰最早未访问的会话, 内存有界."""
        for i in range(_REPLY_PASS_MAX_SESSIONS + 1):
            agent._mark_reply_pass(f"sid-{i}", f"r{i}")
        assert len(agent._recent_reply_pass) == _REPLY_PASS_MAX_SESSIONS
        # 最老的 sid-0 被淘汰, 最新的 sid-N 保留
        assert "sid-0" not in agent._recent_reply_pass
        assert f"sid-{_REPLY_PASS_MAX_SESSIONS}" in agent._recent_reply_pass


# ── 噪声闸决策报表 (无 PG 非阻断) ────────────────────────────────────────────


class TestNoiseGateStats:
    async def test_default_actions_cover_three_groups(self) -> None:
        logger = DecisionLogger()
        with patch.object(logger, "_get_db_session_factory", new=AsyncMock(return_value=None)):
            out = await logger.query_noise_gate_stats()
        # PG 不可用 → 空 dict, 不阻断
        assert out == {}

    async def test_custom_actions_window(self) -> None:
        logger = DecisionLogger()
        with patch.object(logger, "_get_db_session_factory", new=AsyncMock(return_value=None)):
            out = await logger.query_noise_gate_stats(window_days=30, actions=("noise_blocked",))
        assert out == {}


# ── _backflow_path 解析 (相对路径基于 agent/ 根) ──────────────────────────────


class TestBackflowPath:
    def test_absolute_path_passthrough(self) -> None:
        with patch(
            "lumio.shared.config.get_settings",
            return_value=MagicMock(classification=MagicMock(backflow_review_path="/tmp/abs/backflow.jsonl")),
        ):
            assert str(_backflow_path()) == "/tmp/abs/backflow.jsonl"

    def test_relative_path_resolves_under_agent_root(self) -> None:
        # 文件位于 lumio/services/common/, 上溯 3 级即 agent/ 根
        here = Path(__file__).resolve().parents[1]  # agent/
        expected = here / "backflow.jsonl"
        with patch(
            "lumio.shared.config.get_settings",
            return_value=MagicMock(classification=MagicMock(backflow_review_path="backflow.jsonl")),
        ):
            got = _backflow_path()
        assert got == expected
