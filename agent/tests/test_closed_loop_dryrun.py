"""闭环干跑链路测试: 合成样本端到端走通 归因→精选→人审→并入→四门→promote 门控。

无 PG/GPU 依赖: 全链路落在 tmp_path, 验证的是各段代码接线与数据一致性
(真实采集/重训见 docs/意图识别_闭环runbook.md)。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "closed_loop_dryrun.py"


def _load_dryrun():
    spec = importlib.util.spec_from_file_location("closed_loop_dryrun", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_chain_with_auto_approve_merges_and_gates_promote(tmp_path: Path) -> None:
    mod = _load_dryrun()
    summary = mod.run_chain(tmp_path, auto_approve=True)

    # 归因→精选: 3 条合成失败样本全部入选
    assert summary["candidates"] == 3 and summary["approved"] == 3
    # 并入: 版本 +0.0.1, counts 与 examples 长度一致 (回流 meta 键修复的链路级验证)
    assert summary["merge"]["before_version"] == "0.3.2"
    assert summary["merge"]["after_version"] == "0.3.3"
    assert summary["merge"]["added"] == 3
    assert summary["merge"]["counts_consistent"] is True
    # 四门如实执行, promote 严格受门控 (规则预测器 golden 不过 → active 为 None)
    assert len(summary["gates"]) == 4
    assert summary["promote"]["all_gates_pass"] is False
    assert summary["promote"]["active"] is None
    # 门控后 registry 里的 canary 未转 active
    registry = (tmp_path / "model_registry.json").read_text(encoding="utf-8")
    assert '"active"' in registry and "dryrun-v0.0.1" in registry


def test_chain_without_approve_merges_nothing(tmp_path: Path) -> None:
    mod = _load_dryrun()
    summary = mod.run_chain(tmp_path, auto_approve=False)

    # 未批准 → 并入 0 条, 版本不变 (人审是并入的前置, 不可绕过)
    assert summary["merge"]["added"] == 0
    assert summary["merge"]["after_version"] == ""
    assert summary["merge"]["counts_consistent"] is True
