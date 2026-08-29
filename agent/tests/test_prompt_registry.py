"""Prompt 注册中心单元测试 (prompt_registry.py)"""

from __future__ import annotations

import json
import time

from lumio.services.bot.prompt_registry import (
    _LOCAL_PROMPTS,
    PromptRegistry,
    PromptVersion,
    get_prompt,
    get_prompt_registry,
)
from lumio.services.bot.prompts import (
    BUSINESS_SYSTEM_PROMPT,
    COMPLAINT_SYSTEM_PROMPT,
    FALLBACK_SYSTEM_PROMPT,
    KNOWLEDGE_SYSTEM_PROMPT,
)


def test_prompt_version_rollout_clamp():
    """rollout_pct 限制在 0~100"""
    assert PromptVersion("n", "v", "c", rollout_pct=150.0).rollout_pct == 100.0
    assert PromptVersion("n", "v", "c", rollout_pct=-10.0).rollout_pct == 0.0
    assert PromptVersion("n", "v", "c").rollout_pct == 100.0
    assert "rollout=100.0%" in repr(PromptVersion("n", "v", "c"))


def test_local_prompts_initialized():
    """本地兜底库含 3 个 prompt (实例化后初始化)"""
    PromptRegistry()  # 触发 _init_local_prompts
    assert set(_LOCAL_PROMPTS) == {"knowledge_agent", "business_agent", "fallback_agent"}
    for pv in _LOCAL_PROMPTS.values():
        assert pv.content  # 内容非空


def test_get_prompt_local_fallback():
    """无 Nacos/Redis 时兜底本地 prompt"""
    reg = PromptRegistry()
    content = reg.get_prompt("knowledge_agent")
    assert content == _LOCAL_PROMPTS["knowledge_agent"].content


def test_get_prompt_missing():
    """不存在的 prompt 返回占位符"""
    reg = PromptRegistry()
    assert reg.get_prompt("no_such_prompt") == "[PROMPT_MISSING:no_such_prompt]"


# 回归: 客户随便发句无意义输入, 兜底话术不得谎称"系统故障" (见 S6 诊断)
def test_fallback_prompt_does_not_claim_system_outage():
    """兜底系统提示词不得诱导模型说系统故障/服务不可用, 应引导客户换说法"""
    banned = ["系统故障", "系统遇到", "技术问题", "服务暂时不可用", "服务受限", "系统不可用"]
    for w in banned:
        assert w not in FALLBACK_SYSTEM_PROMPT, f"兜底提示词仍含敏感/误导措辞: {w}"
    # 引导重新表述
    assert "换个说法" in FALLBACK_SYSTEM_PROMPT
    # 明确: 检索无结果也不得暗示平台异常
    assert "运行异常" in FALLBACK_SYSTEM_PROMPT
    assert "未能理解" in FALLBACK_SYSTEM_PROMPT
    # 要求简短, 不得罗列问题清单 (回归: 兜底回复不得一长段举例)
    assert "简短" in FALLBACK_SYSTEM_PROMPT
    assert "罗列问题清单" in FALLBACK_SYSTEM_PROMPT


def test_knowledge_prompt_brief_when_no_context():
    """知识路径在未检索到上下文(如客户发"889")时, 引导简短澄清而非冗长列举"""
    banned = ["系统故障", "系统遇到", "技术问题", "服务暂时不可用"]
    for w in banned:
        assert w not in KNOWLEDGE_SYSTEM_PROMPT
    assert "未检索到相关知识" in KNOWLEDGE_SYSTEM_PROMPT
    assert "简短" in KNOWLEDGE_SYSTEM_PROMPT
    assert "不要罗列问题清单" in KNOWLEDGE_SYSTEM_PROMPT


def test_prompts_safety_redlines():
    """P0/P1/P2 安全红线必须存在于各系统提示词: 不编造工号、拒绝角色越权、不协助违法违规"""
    for p in (KNOWLEDGE_SYSTEM_PROMPT, BUSINESS_SYSTEM_PROMPT, COMPLAINT_SYSTEM_PROMPT, FALLBACK_SYSTEM_PROMPT):
        assert "工号" in p, "必须禁止编造/出示工号"
        assert "越权" in p, "必须拒绝忽略指令/替换角色等越权要求"
        assert "套现" in p, "必须不协助套现等违法违规行为"
        assert "不承诺任何收益" in p, "必须不承诺收益"


def test_get_prompt_from_redis_cache(monkeypatch):
    """Redis 缓存命中优先于本地"""
    reg = PromptRegistry()

    class _FakeRedisSync:
        def __init__(self, data: dict) -> None:
            self.data = data

        def get(self, key: str):
            return self.data.get(key)

    payload = json.dumps({"version": "v2", "content": "新版 prompt 内容", "rollout_pct": 100.0})
    reg._redis_client = _FakeRedisSync({"lumio:prompt:knowledge_agent": payload})
    content = reg.get_prompt("knowledge_agent")
    assert content == "新版 prompt 内容"
    assert reg._cache["knowledge_agent"].version == "v2"


def test_get_prompt_rollout_switch_back():
    """rollout_pct < 100 且未命中 → 切回主版本"""
    reg = PromptRegistry()
    now = time.time()
    reg._cache["knowledge_agent"] = PromptVersion(
        name="knowledge_agent",
        version="canary-v2",
        content="灰度内容",
        rollout_pct=1.0,  # 1% 灰度
    )
    reg._cache_loaded_at["knowledge_agent"] = now
    # 用 hash 不在 1% 内的 customer_id
    content = reg.get_prompt("knowledge_agent", customer_id="customer-x")
    assert content == _LOCAL_PROMPTS["knowledge_agent"].content  # 主版本


def test_get_prompt_rollout_hit():
    """rollout_pct < 100 且命中灰度 → 灰度内容"""
    reg = PromptRegistry()
    now = time.time()
    reg._cache["knowledge_agent"] = PromptVersion(
        name="knowledge_agent",
        version="canary-v2",
        content="灰度内容",
        rollout_pct=100.0,
    )
    reg._cache_loaded_at["knowledge_agent"] = now
    assert reg.get_prompt("knowledge_agent", customer_id="c") == "灰度内容"


def test_is_in_rollout_sticky():
    """粘性分桶: 同一 customer 结果稳定"""
    assert PromptRegistry._is_in_rollout("", 50.0) is True  # 空 customer 全量
    r1 = PromptRegistry._is_in_rollout("cust-1", 50.0)
    r2 = PromptRegistry._is_in_rollout("cust-1", 50.0)
    assert r1 == r2


def test_get_metadata():
    """元数据查询"""
    reg = PromptRegistry()
    meta = reg.get_metadata("knowledge_agent")
    assert meta["version"] == "local-v1"
    assert "changelog" in meta
    assert reg.get_metadata("no_such") is None


def test_get_metadata_from_cache():
    """缓存版本元数据优先"""
    reg = PromptRegistry()
    reg._cache["business_agent"] = PromptVersion(name="business_agent", version="v9", content="c", changelog="改")
    assert reg.get_metadata("business_agent")["version"] == "v9"


def test_invalidate_cache():
    """缓存清除: 单名与全量"""
    reg = PromptRegistry()
    reg._cache["a"] = PromptVersion("a", "v1", "c")
    reg._cache["b"] = PromptVersion("b", "v1", "c")
    reg._cache_loaded_at["a"] = time.time()

    reg.invalidate_cache("a")
    assert "a" not in reg._cache
    assert "b" in reg._cache

    reg.invalidate_cache()
    assert reg._cache == {}


def test_redis_error_soft():
    """Redis 读取异常 → 回退本地"""
    reg = PromptRegistry()

    class _BoomRedis:
        def get(self, key: str):
            raise RuntimeError("redis down")

    reg._redis_client = _BoomRedis()
    content = reg.get_prompt("knowledge_agent")
    assert content == _LOCAL_PROMPTS["knowledge_agent"].content


def test_registry_singleton_and_helper():
    """单例 + 便捷函数"""
    r1 = get_prompt_registry()
    r2 = get_prompt_registry()
    assert r1 is r2
    assert get_prompt("fallback_agent") == _LOCAL_PROMPTS["fallback_agent"].content


def test_fallback_prompt_has_capability_anchor_for_chitchat():
    """理想形态 (会话 b561cd04): 闲聊/离题时承认帮不上 + 列出能力锚, 禁裸 yes/no 反问."""
    assert "超出业务范围" in FALLBACK_SYSTEM_PROMPT
    assert "账单、额度、分期、积分" in FALLBACK_SYSTEM_PROMPT
    assert "裸 yes/no" in FALLBACK_SYSTEM_PROMPT
    assert "死胡同" in FALLBACK_SYSTEM_PROMPT
    # 先接话再列锚 + few-shot 示例 (含本会话生硬回复的反例)
    assert "接住客户的话题" in FALLBACK_SYSTEM_PROMPT
    assert "天气的事我帮不上忙" in FALLBACK_SYSTEM_PROMPT
    assert "播报式列举" in FALLBACK_SYSTEM_PROMPT


def test_confirm_followup_reply_has_capability_anchor():
    """确认跟进话术直接给能力锚, 不留开放式提问."""
    from lumio.services.bot.prompts import CONFIRM_FOLLOWUP_RESPONSE

    assert "账单" in CONFIRM_FOLLOWUP_RESPONSE
    assert "分期" in CONFIRM_FOLLOWUP_RESPONSE
    assert "？" not in CONFIRM_FOLLOWUP_RESPONSE


def test_business_prompt_confirmation_and_no_yesno():
    """P1b+P2 (会话 1fb54681): BUSINESS prompt 必须约束 pending 确认机制 + 禁裸 yes/no + 禁复述凭证"""
    assert "确认" in BUSINESS_SYSTEM_PROMPT, "敏感操作确认须由 pending 状态机完成"
    assert "复述" in BUSINESS_SYSTEM_PROMPT, "禁止要求客户复述卡号等凭证来二次确认"
    assert "裸 yes/no" in BUSINESS_SYSTEM_PROMPT, "禁止您是想办理X吗这类死胡同反问"
    assert "后四位" in BUSINESS_SYSTEM_PROMPT, "参数索取只能要卡号后四位"
    assert "一次只问最关键的一项" in BUSINESS_SYSTEM_PROMPT, "参数索取一次一项, 不罗列清单"


def test_safety_redlines_forbid_full_pan():
    """P1a: 安全红线必须禁止索取完整卡号/复述凭证 (所有提示词生效)"""
    from lumio.services.bot.prompts import COMPLAINT_SYSTEM_PROMPT, FALLBACK_SYSTEM_PROMPT, KNOWLEDGE_SYSTEM_PROMPT

    for p in (KNOWLEDGE_SYSTEM_PROMPT, BUSINESS_SYSTEM_PROMPT, COMPLAINT_SYSTEM_PROMPT, FALLBACK_SYSTEM_PROMPT):
        assert "完整卡号" in p, "红线必须禁止索要完整卡号"
        assert "复述" in p, "红线必须禁止要求复述已提供凭证"
        assert "后四位" in p, "红线必须限定核验只可用后四位"
        # P0 (会话 3c388195 复盘): 能力边界 — 不得承诺查询个人账户数据
        assert "无法查询客户的个人账户数据" in p, "红线必须声明无个人账户数据查询能力"
        assert "帮您查询" in p, "红线必须禁止承诺/暗示帮查个人账户数据"
