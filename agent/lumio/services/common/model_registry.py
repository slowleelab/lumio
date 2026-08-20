"""闭环 P3 模型注册表 + canary 灰度 (ModelRegistry)

管理已微调 BERT 意图分类器的版本生命周期:
- staging(待审) → canary(灰度) → active(线上) / retired(退役)
- promote: canary 必须四门评估全 PASS 才转 active (银行场景不自动上线, 见 eval_gates)
- rollback: 保留上一 active 指针, 一键回退
- canary_traffic: 灰度流量占比 (0..1) — 语义上代表"灰度放量阶段", 真实流量切换
  由部署侧按该占比在入口层分流; 代码库存改指针与裁决, 不在单进程内双载两个
  完整 BERT (显存代价), 这一点在 to_dict 中如实标注.

持久化: 一个 JSON 状态文件 (默认 data/intent_classification/model_registry.json),
仅存版本指针与裁决记录, 不搬运权重 (权重目录由各版本 path 指向, 不入 git).
"""
from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from lumio.shared.logger import get_logger

logger = get_logger(__name__)

_AGENT_ROOT = Path(__file__).resolve().parents[3]


def _resolve_agent_path(p: str) -> str:
    path = Path(p)
    return str(path if path.is_absolute() else _AGENT_ROOT / p)


@dataclass
class ModelVersion:
    id: str
    path: str
    status: str = "staging"  # staging | canary | active | retired
    notes: str = ""
    created_at: str | None = None
    gate_report: list[dict[str, Any]] = field(default_factory=list)  # 最近一次评估门明细

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ModelVersion:
        return cls(
            id=d["id"],
            path=d["path"],
            status=d.get("status", "staging"),
            notes=d.get("notes", ""),
            created_at=d.get("created_at"),
            gate_report=d.get("gate_report", []),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "status": self.status,
            "notes": self.notes,
            "created_at": self.created_at,
            "gate_report": self.gate_report,
        }


GatesRunner = Callable[[], list[dict[str, Any]]]  # 返回 gate 结果 dict 列表


class ModelRegistry:
    """版本状态机 (本地 JSON 持久化). """

    def __init__(self, state_path: str = "", allow_ungated: bool = False) -> None:
        self._state_path = _resolve_agent_path(state_path) if state_path else ""
        self._allow_ungated = allow_ungated
        self._versions: dict[str, ModelVersion] = {}
        self._active: str | None = None
        self._previous_active: str | None = None
        self._canary: str | None = None
        self._canary_traffic: float = 0.0
        if self._state_path and Path(self._state_path).exists():
            self._load()

    # ── 持久化 ──
    def _load(self) -> None:
        try:
            data = json.loads(Path(self._state_path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("模型注册表状态文件损坏, 以空状态启动: %s", self._state_path)
            return
        self._versions = {
            k: ModelVersion.from_dict(v) for k, v in data.get("versions", {}).items()
        }
        self._active = data.get("active")
        self._previous_active = data.get("previous_active")
        self._canary = data.get("canary")
        self._canary_traffic = float(data.get("canary_traffic", 0.0) or 0.0)

    def save(self) -> None:
        if not self._state_path:
            return
        data = {
            "versions": {k: v.to_dict() for k, v in self._versions.items()},
            "active": self._active,
            "previous_active": self._previous_active,
            "canary": self._canary,
            "canary_traffic": self._canary_traffic,
        }
        Path(self._state_path).parent.mkdir(parents=True, exist_ok=True)
        Path(self._state_path).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ── 版本管理 ──
    def register(self, version_id: str, path: str, notes: str = "") -> ModelVersion:
        if version_id in self._versions:
            raise ValueError(f"版本已存在: {version_id}")
        v = ModelVersion(
            id=version_id,
            path=_resolve_agent_path(path),
            status="staging",
            notes=notes,
            created_at=datetime.now(UTC).isoformat(),
        )
        self._versions[version_id] = v
        self.save()
        return v

    def set_canary(self, version_id: str, traffic: float = 1.0) -> ModelVersion:
        if version_id not in self._versions:
            raise ValueError(f"版本不存在: {version_id}")
        traffic = max(0.0, min(1.0, traffic))
        # 原 canary 若与 active 不同则降为 staging
        if self._canary and self._canary in self._versions and self._canary != version_id:
            self._versions[self._canary].status = "staging"
        v = self._versions[version_id]
        v.status = "canary"
        self._canary = version_id
        self._canary_traffic = traffic
        self.save()
        return v

    def promote(
        self,
        gate_runner: GatesRunner | None = None,
        *,
        force: bool = False,
    ) -> tuple[bool, list[dict[str, Any]]]:
        """canary → active. 必须 gate 全 PASS (force=True 除外, 慎用). """
        report: list[dict[str, Any]] = []
        if gate_runner is not None:
            report = list(gate_runner())
            passed = all(r.get("passed", False) for r in report)
            self._record_gate(report)
            if not passed and not force:
                logger.warning("canary %s 未过门, 拒绝 promote", self._canary)
                self.save()
                return False, report
        elif not self._allow_ungated and not force:
            logger.warning("未提供评估门且未授权无门上线, 拒绝 promote")
            return False, []

        if self._canary not in self._versions:
            return False, report
        canary = self._versions[self._canary]
        if self._active and self._active in self._versions:
            old = self._versions[self._active]
            old.status = "canary" if old.id != canary.id else "active"  # 上一线上转灰度
            self._previous_active = old.id
        else:
            self._previous_active = self._active
        canary.status = "active"
        self._active = canary.id
        self._canary = None
        self._canary_traffic = 0.0
        self.save()
        logger.info("模型 promote: %s 升为 active (gate=%d)", canary.id, len(report))
        return True, report

    def rollback(self) -> str | None:
        """回退到 previous_active (若存在). """
        if self._previous_active and self._previous_active in self._versions:
            prev = self._versions[self._previous_active]
            prev.status = "active"
            if self._active and self._active in self._versions:
                self._versions[self._active].status = "retired"
            self._active = prev.id
            self._canary = None
            self._canary_traffic = 0.0
            self.save()
            return prev.id
        return None

    def compose_classifier_path(self, fallback: str) -> str:
        """当前应使用的 BERT 模型路径: active > fallback. """
        if self._active and self._active in self._versions:
            return self._versions[self._active].path
        return _resolve_agent_path(fallback)

    def _record_gate(self, report: list[dict[str, Any]]) -> None:
        if self._canary and self._canary in self._versions:
            self._versions[self._canary].gate_report = report

    # ── 只读视图 ──
    def to_dict(self) -> dict[str, Any]:
        return {
            "active": self._active,
            "previous_active": self._previous_active,
            "canary": self._canary,
            "canary_traffic": self._canary_traffic,
            "dual_serve_note": "单进程仅载 active 权重; canary 真实放量由入口层按 canary_traffic 分流",
            "versions": {k: v.to_dict() for k, v in self._versions.items()},
        }
