"""PromptOps 测试: 注册中心三层降级/lazy seed/草稿校验 + 提示词内容回归

内容回归测试守护的是内置基线 (代码常量, 即 seed 源与四层兜底) — 后台发布的新版本
永远不能低于这条地板线; 每条断言都源自真实会话复盘 (工号编造/系统故障话术/死胡同反问).
"""

from __future__ import annotations

import pytest

from lumio.services.bot import prompt_registry as pr
from lumio.services.bot.prompt_registry import PromptRegistry, ResolvedPrompt
from lumio.services.bot.prompts import (
    BUSINESS_SYSTEM_PROMPT,
    COMPLAINT_SYSTEM_PROMPT,
    FALLBACK_SYSTEM_PROMPT,
    KNOWLEDGE_SYSTEM_PROMPT,
)
from lumio.shared.exceptions import PromptValidationError

# ── 注册中心: 三层降级 / lazy seed / 缓存失效 ──


class FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    async def get(self, k: str):
        return self.data.get(k)

    async def set(self, k: str, v: str, ex: int = 0):
        self.data[k] = v

    async def delete(self, *keys: str):
        for k in keys:
            self.data.pop(k, None)


class FakeResult:
    def __init__(self, val):
        self._v = val

    def scalar_one_or_none(self):
        return self._v


class FakeRow:
    """DB 活跃版本行 (registry 只读 version/content)"""

    def __init__(self, version: int, content: str):
        self.version = version
        self.content = content


class FakeSession:
    def __init__(self, first_result):
        self.results = [first_result]
        self.added: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        pass

    async def execute(self, q):
        return FakeResult(self.results.pop(0) if self.results else None)


def _broken_factory(*a, **kw):
    raise RuntimeError("db down")


@pytest.fixture()
def reg():
    """独立 registry 实例: redis 关闭, DB 默认挂 (单测内 monkeypatch)"""
    r = PromptRegistry()
    r._redis = False
    return r


async def test_local_fallback_when_db_down(monkeypatch, reg):
    """DB 挂 → 本地常量兜底, version=0, 内容与代码常量逐字节一致"""
    monkeypatch.setattr("lumio.services.common.database.get_async_session_factory", _broken_factory)
    rp = await reg.resolve("knowledge_system")
    assert rp.source == "local"
    assert rp.version == 0
    assert rp.content == KNOWLEDGE_SYSTEM_PROMPT


async def test_resolve_from_db_then_proc_cache(monkeypatch, reg):
    """DB 命中 source=db; TTL 内二次取走进程缓存 (DB 挂也不受影响)"""
    session = FakeSession(FakeRow(3, "DB版本内容"))
    monkeypatch.setattr("lumio.services.common.database.get_async_session_factory", lambda: (lambda: session))
    rp = await reg.resolve("knowledge_system")
    assert (rp.source, rp.version, rp.content) == ("db", 3, "DB版本内容")
    monkeypatch.setattr("lumio.services.common.database.get_async_session_factory", _broken_factory)
    assert (await reg.resolve("knowledge_system")).version == 3


async def test_lazy_seed_builds_v1(monkeypatch, reg):
    """DB 无记录 → 本地常量建 template+v1(published), 指针指向 v1"""
    session = FakeSession(None)
    monkeypatch.setattr("lumio.services.common.database.get_async_session_factory", lambda: (lambda: session))
    rp = await reg.resolve("fallback_system")
    assert (rp.source, rp.version) == ("db", 1)
    tmpl, ver = session.added[0], session.added[1]
    assert tmpl.active_version_id == ver.id
    assert ver.status == "published"
    assert ver.content == FALLBACK_SYSTEM_PROMPT


async def test_redis_tier(monkeypatch):
    """进程缓存空 → Redis 命中 (source=redis)"""
    r = PromptRegistry()
    redis = FakeRedis()
    redis.data["lumio:prompt:v2:business_system"] = '{"v": 7, "c": "redis内容"}'
    monkeypatch.setattr("lumio.services.common.redis_client.get_redis_client", lambda: redis)
    monkeypatch.setattr("lumio.services.common.database.get_async_session_factory", _broken_factory)
    rp = await r.resolve("business_system")
    assert (rp.source, rp.version, rp.content) == ("redis", 7, "redis内容")


async def test_invalidate_clears_both_tiers(monkeypatch, reg):
    redis = FakeRedis()
    redis.data["lumio:prompt:v2:knowledge_system"] = '{"v": 2, "c": "x"}'
    monkeypatch.setattr("lumio.services.common.redis_client.get_redis_client", lambda: redis)
    reg._redis = None  # 重连拿 fake
    reg._proc_cache["knowledge_system"] = (ResolvedPrompt("knowledge_system", 2, "x", "redis"), 0.0)
    await reg.invalidate("knowledge_system")
    assert "knowledge_system" not in reg._proc_cache
    assert "lumio:prompt:v2:knowledge_system" not in redis.data


async def test_unknown_name_local_empty(monkeypatch, reg):
    """未注册名字: 空内容返回 (调用方有上层降级链, 不崩溃)"""
    monkeypatch.setattr("lumio.services.common.database.get_async_session_factory", _broken_factory)
    rp = await reg.resolve("no_such_prompt")
    assert (rp.source, rp.content) == ("local", "")


def test_validate_content_rules():
    """草稿校验: 空/超长/未声明占位符拒绝; 声明过放行"""
    from lumio.services.common.prompt_router import _validate_content

    with pytest.raises(PromptValidationError):
        _validate_content("   ", [])
    with pytest.raises(PromptValidationError):
        _validate_content("x" * 12001, [])
    with pytest.raises(PromptValidationError):
        _validate_content("请提供 {card_no} 后四位", [])  # generation 类禁止占位符
    _validate_content("文本 {reason}", ["reason"])
    _validate_content("普通文本", [])


def test_local_defs_cover_generation_prompts():
    """本地基线覆盖四条链 + 摘要 (seed 源完整性)"""
    assert set(pr._local_prompt_defs()) == {
        "knowledge_system",
        "business_system",
        "complaint_system",
        "fallback_system",
        "summarize_system",
    }


# ── 提示词内容回归 (内置基线地板线, 均源自真实会话复盘) ──
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

    for p in (KNOWLEDGE_SYSTEM_PROMPT, BUSINESS_SYSTEM_PROMPT, COMPLAINT_SYSTEM_PROMPT, FALLBACK_SYSTEM_PROMPT):
        assert "完整卡号" in p, "红线必须禁止索要完整卡号"
        assert "复述" in p, "红线必须禁止要求复述已提供凭证"
        assert "后四位" in p, "红线必须限定核验只可用后四位"
        # P0 (会话 3c388195 复盘): 能力边界 — 不得承诺查询个人账户数据
        assert "无法查询客户的个人账户数据" in p, "红线必须声明无个人账户数据查询能力"
        assert "帮您查询" in p, "红线必须禁止承诺/暗示帮查个人账户数据"
