"""Prompt 注册中心 (PromptOps) — DB 为运营源, 三层降级, 版本指针切换

能力:
1. 版本管理: prompt_version append-only; template.active_version_id 指针切换, 回滚=拨回指针
2. 热加载: PG → Redis 缓存 → 进程缓存 (各 60s TTL), 发布即失效, 改动不重启
3. lazy seed: 首次访问 DB 无记录时, 用本地代码常量自动建 template + v1 (published), 幂等

降级链 (resolve):
  进程缓存 → Redis → PG (含 seed) → 本地常量兜底
  四层全考虑: DB 挂了服务照常对话 — 本地常量永不删除, "永不上线空提示词".

工程锁定类 (裁判口径/分类基线/安全红线) 不入 DB, 由 prompt_router 从代码常量只读展示.

关键: Prompt 内容不含 session_id/时间戳等动态信息 (否则无法命中 KV cache);
变量通过渲染层注入, generation 类模板禁止任何占位符 (variables 契约为空).
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from lumio.shared.config import get_settings
from lumio.shared.logger import get_logger

logger = get_logger(__name__)

_REDIS_KEY = "lumio:prompt:v2:{name}"


@dataclass
class ResolvedPrompt:
    """解析结果: 内容 + 版本溯源 (版本进决策链, 坏例可归因到具体提示词版本)"""

    name: str
    version: int  # DB 版本序号; 本地兜底为 0
    content: str
    source: str  # db | redis | local


# ── 本地基线定义 (seed 源 + 四层兜底; 内容与 prompts/__init__.py 常量同源) ──


def _local_prompt_defs() -> dict[str, dict[str, Any]]:
    from lumio.services.bot import prompts as prompts_mod

    return {
        "knowledge_system": {
            "category": "generation",
            "description": "知识问答链 — 咨询类问题的回复生成（基于知识库检索作答，约束简洁与澄清规范）",
            "content": prompts_mod.KNOWLEDGE_SYSTEM_PROMPT,
        },
        "business_system": {
            "category": "generation",
            "description": "业务办理链 — 办理类请求的回复生成（缺参数时逐项追问，敏感操作交系统确认）",
            "content": prompts_mod.BUSINESS_SYSTEM_PROMPT,
        },
        "complaint_system": {
            "category": "generation",
            "description": "投诉安抚链 — 投诉场景的回复生成（先共情再处理，严重投诉转人工）",
            "content": prompts_mod.COMPLAINT_SYSTEM_PROMPT,
        },
        "fallback_system": {
            "category": "generation",
            "description": "兜底闲聊链 — 无意义输入/离题闲聊的回复生成（接住话题并引导回业务）",
            "content": prompts_mod.FALLBACK_SYSTEM_PROMPT,
        },
        "summarize_system": {
            "category": "generation",
            "description": "多轮对话压缩摘要 — 长会话滚动总结，供后续轮次作上下文",
            "content": prompts_mod._SUMMARIZE_SYSTEM_PROMPT,
        },
    }


class PromptRegistry:
    """Prompt 注册中心 (进程内单例; Redis/PG 懒连接, 不可用时逐层降级)"""

    def __init__(self) -> None:
        self._settings = get_settings().prompt
        self._proc_cache: dict[str, tuple[ResolvedPrompt, float]] = {}
        self._redis: Any = None  # None=未尝试, False=不可用

    # ── 对外主入口 ──

    async def resolve(self, name: str) -> ResolvedPrompt:
        ttl = self._settings.cache_ttl_seconds or 60
        cached = self._proc_cache.get(name)
        if cached and time.time() - cached[1] < ttl:
            return cached[0]

        rp = await self._from_redis(name) or await self._from_db(name)
        if rp is None:
            rp = self._local(name)
        else:
            self._proc_cache[name] = (rp, time.time())
        if rp.source == "local":
            # 兜底路径不缓存 (DB 恢复后下一轮即切回), 但避免刷日志
            logger.debug("prompt 走本地兜底: name=%s", name)
        return rp

    async def invalidate(self, name: str | None = None) -> None:
        """发布/回滚后调用: 清进程缓存 + 删 Redis key (60s 内全实例收敛)"""
        names = [name] if name else list(self._proc_cache)
        for n in names:
            self._proc_cache.pop(n, None)
        redis = await self._get_redis()
        if redis:
            with_keys = [_REDIS_KEY.format(name=n) for n in names]
            try:
                await redis.delete(*with_keys)
            except Exception as exc:
                logger.warning("prompt 缓存失效失败 (将靠 TTL 收敛): err=%s", exc)

    # ── 三层取数 ──

    async def _get_redis(self) -> Any:
        if self._redis is None:
            try:
                from lumio.services.common.redis_client import get_redis_client

                self._redis = get_redis_client()
            except Exception as exc:
                logger.debug("Redis 客户端不可用: %s", exc)
                self._redis = False
        return self._redis or None

    async def _from_redis(self, name: str) -> ResolvedPrompt | None:
        redis = await self._get_redis()
        if not redis:
            return None
        try:
            raw = await redis.get(_REDIS_KEY.format(name=name))
            if not raw:
                return None
            payload = json.loads(raw)
            return ResolvedPrompt(name=name, version=int(payload["v"]), content=payload["c"], source="redis")
        except Exception as exc:
            logger.debug("Redis prompt 读取失败: name=%s err=%s", name, exc)
            return None

    async def _from_db(self, name: str) -> ResolvedPrompt | None:
        """PG 取活跃版本; 无记录时 lazy seed (本地常量建 v1)。任何异常返回 None 走兜底"""
        try:
            from lumio.services.common.database import get_async_session_factory
            from lumio.shared.orm_models import PromptTemplate, PromptVersion

            factory = get_async_session_factory()
            async with factory() as session:
                result = await session.execute(
                    select(PromptVersion)
                    .join(PromptTemplate, PromptTemplate.active_version_id == PromptVersion.id)
                    .where(PromptTemplate.name == name)
                )
                row = result.scalar_one_or_none()
                if row is not None:
                    rp = ResolvedPrompt(name=name, version=row.version, content=row.content, source="db")
                    await self._cache_to_redis(name, rp)
                    return rp
                # lazy seed (并发下唯一键冲突 → 对方已建, 重读)
                seeded = await self._seed(session, name)
                if seeded is not None:
                    await self._cache_to_redis(name, seeded)
                return seeded
        except Exception as exc:
            logger.warning("prompt DB 读取失败, 走兜底: name=%s err=%s", name, exc)
            return None

    async def _seed(self, session: Any, name: str) -> ResolvedPrompt | None:
        from lumio.shared.orm_models import PromptTemplate, PromptVersion, _uuid_v7

        defs = _local_prompt_defs()
        d = defs.get(name)
        if d is None:
            logger.error("prompt 未注册且无本地兜底: name=%s", name)
            return None
        # 列默认值在 flush 时才生成, 显式预生成 ID 以便互相引用 (template.active_version_id ↔ version.template_id)
        tmpl = PromptTemplate(
            id=_uuid_v7(),
            name=name,
            category=d["category"],
            description=d["description"],
            variables=[],
        )
        ver = PromptVersion(
            id=_uuid_v7(),
            template_id=tmpl.id,
            version=1,
            content=d["content"],
            changelog="内置基线 (代码常量 lazy seed)",
            created_by="system",
            status="published",
        )
        tmpl.active_version_id = ver.id
        session.add(tmpl)
        session.add(ver)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()  # 并发 seed: 对方已建
            from sqlalchemy import select as _sel

            row = (
                await session.execute(
                    _sel(PromptVersion)
                    .join(PromptTemplate, PromptTemplate.active_version_id == PromptVersion.id)
                    .where(PromptTemplate.name == name)
                )
            ).scalar_one_or_none()
            if row is None:
                logger.warning("prompt seed 撞唯一键且重读为空: name=%s", name)
                return None
            return ResolvedPrompt(name=name, version=row.version, content=row.content, source="db")
        logger.info("prompt lazy seed 完成: name=%s v1", name)
        return ResolvedPrompt(name=name, version=1, content=d["content"], source="db")

    async def _cache_to_redis(self, name: str, rp: ResolvedPrompt) -> None:
        redis = await self._get_redis()
        if not redis:
            return
        try:
            ttl = self._settings.cache_ttl_seconds or 60
            await redis.set(_REDIS_KEY.format(name=name), json.dumps({"v": rp.version, "c": rp.content}), ex=ttl)
        except Exception as exc:
            logger.debug("Redis prompt 写入失败: name=%s err=%s", name, exc)

    @staticmethod
    def _local(name: str) -> ResolvedPrompt:
        d = _local_prompt_defs().get(name)
        content = d["content"] if d else ""
        if not d:
            logger.error("prompt 未注册且无本地兜底: name=%s", name)
        return ResolvedPrompt(name=name, version=0, content=content, source="local")


_registry: PromptRegistry | None = None


def get_prompt_registry() -> PromptRegistry:
    global _registry
    if _registry is None:
        _registry = PromptRegistry()
    return _registry


async def get_prompt(name: str) -> str:
    """便捷入口: 取 prompt 内容 (调用方不需版本信息时用)"""
    return (await get_prompt_info(name)).content


async def get_prompt_info(name: str) -> ResolvedPrompt:
    """便捷入口: 取内容 + 版本 (需进决策链 evidence 时用)"""
    return await get_prompt_registry().resolve(name)
