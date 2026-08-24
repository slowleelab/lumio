"""P1 并发修复: IntentResult.energy 按次透传 + 不透出序列化.

验证核心: energy 挂在每次分类返回的 IntentResult 对象上(而非共享 classifier
属性), 从而多会话并发时不串线; 且 exclude=True 保证不随 model_dump 泄漏到
决策日志/对外响应。
"""

from __future__ import annotations

from lumio.shared.models import IntentLabel, IntentResult


class TestIntentResultEnergy:
    def test_energy_field_settable_and_roundtrip(self) -> None:
        r = IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.9, energy=-3.2)
        assert r.energy == -3.2
        # 默认构造为 None (未算 energy 时)
        assert IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.5).energy is None

    def test_energy_excluded_from_serialization(self) -> None:
        # 决策日志 / 对外响应若 model_dump 该对象, energy 不应外泄
        r = IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.9, energy=-3.2)
        dumped = r.model_dump()
        assert "energy" not in dumped
        assert dumped["primary_intent"] == IntentLabel.FAQ
