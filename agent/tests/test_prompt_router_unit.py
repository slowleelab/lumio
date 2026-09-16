"""提示词路由进程内测试 (直调端点函数, 真库) — 覆盖率计在主进程

bot_server 子进程的集成测试 (test_prompt_router) 验证 HTTP 语义, 但其执行不计入
主进程 coverage; 本组直调端点函数覆盖 prompt_router/registry 的 DB 路径.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from lumio.services.common import prompt_router as pr
from lumio.shared.auth import AuthUser
from lumio.shared.exceptions import LumioError, PromptLockedError, PromptValidationError

_ADMIN = AuthUser(user_id="unit-admin", role="admin")


@pytest_asyncio.fixture()
async def prompt_tables(db_schema: None):
    """建表幂等 (CI 裸库); 返回后清掉本组残留, 保证测试间独立"""
    yield
    from lumio.services.common.database import get_async_session_factory
    from lumio.shared.orm_models import PromptTemplate, PromptVersion

    factory = get_async_session_factory()
    async with factory() as s:
        for v in (await s.execute(select_all(PromptVersion))).scalars():
            await s.delete(v)
        for t in (await s.execute(select_all(PromptTemplate))).scalars():
            await s.delete(t)
        await s.commit()


def select_all(model):
    from sqlalchemy import select

    return select(model)


class TestListAndDetail:
    async def test_list_merges_all_three_sources(self, prompt_tables: None) -> None:
        res = await pr.list_prompts(_ADMIN)
        names = {i["name"] for i in res["items"]}
        assert {"knowledge_system", "complaint_system"} <= names  # 未 seed 合并
        assert "safety_redlines" in names  # 锁定
        # 首次访问前 DB 为空 → 全部走"未入库"展示
        unseeded = next(i for i in res["items"] if i["name"] == "complaint_system")
        assert unseeded["editable"] is True and unseeded["version_count"] == 0

    async def test_detail_locked_and_unknown(self, prompt_tables: None) -> None:
        locked = await pr.prompt_detail("qa_judge", _ADMIN)
        assert locked["editable"] is False and "质检" in locked["description"]
        with pytest.raises(LumioError):
            await pr.prompt_detail("nope", _ADMIN)

    async def test_detail_unseeded_returns_baseline(self, prompt_tables: None) -> None:
        d = await pr.prompt_detail("fallback_system", _ADMIN)
        assert d["editable"] is True
        assert "闲聊" in d["active_content"] and d["versions"] == []


class TestDraftPublishRollback:
    async def test_full_lifecycle_in_process(self, prompt_tables: None) -> None:
        name = "knowledge_system"
        d0 = await pr.prompt_detail(name, _ADMIN)
        base_content, base_ver = d0["active_content"], d0["active_version"]

        draft = await pr.create_draft(
            name, pr.DraftBody(content=base_content + "\n(unit 标记)", changelog="单测"), _ADMIN
        )
        assert draft["version"] == base_ver + 1

        pub = await pr.publish_version(name, _uuid(draft["id"]), _ADMIN)
        assert pub["active_version"] == draft["version"]

        # 内容生效 + 旧版归档
        d1 = await pr.prompt_detail(name, _ADMIN)
        assert "(unit 标记)" in d1["active_content"]
        assert next(v for v in d1["versions"] if v["version"] == base_ver)["status"] == "archived"

        # resolve (registry 主进程路径) 取到新版本
        from lumio.services.bot.prompt_registry import get_prompt_registry

        await get_prompt_registry().invalidate(name)
        rp = await get_prompt_registry().resolve(name)
        assert rp.version == draft["version"] and "(unit 标记)" in rp.content

        # 回滚 → 内容不可变还原
        back = await pr.rollback_version(
            name, _uuid(next(v["id"] for v in d1["versions"] if v["version"] == base_ver)), _ADMIN
        )
        assert back["active_version"] == base_ver
        await get_prompt_registry().invalidate(name)
        assert (await get_prompt_registry().resolve(name)).content == base_content

    async def test_draft_validation_paths(self, prompt_tables: None) -> None:
        with pytest.raises(PromptValidationError):
            await pr.create_draft("business_system", pr.DraftBody(content="带 {foo}", changelog="x"), _ADMIN)
        with pytest.raises(PromptValidationError):
            await pr.create_draft("business_system", pr.DraftBody(content="", changelog="x"), _ADMIN)

    async def test_locked_write_rejected(self, prompt_tables: None) -> None:
        with pytest.raises(PromptLockedError):
            await pr.create_draft("safety_redlines", pr.DraftBody(content="x", changelog="y"), _ADMIN)

    async def test_delete_draft_only(self, prompt_tables: None) -> None:
        name = "summarize_system"
        d0 = await pr.prompt_detail(name, _ADMIN)
        draft = await pr.create_draft(name, pr.DraftBody(content=d0["active_content"] + "x", changelog="d"), _ADMIN)
        deleted = await pr.delete_draft(name, _uuid(draft["id"]), _ADMIN)
        assert deleted["deleted"] == draft["id"]
        d1 = await pr.prompt_detail(name, _ADMIN)
        assert draft["version"] not in {v["version"] for v in d1["versions"]}
        # published 版本不可删
        active = next(v for v in d1["versions"] if v["status"] == "published")
        with pytest.raises(LumioError):
            await pr.delete_draft(name, _uuid(active["id"]), _ADMIN)

    async def test_preview(self, prompt_tables: None) -> None:
        ok = await pr.preview_prompt("knowledge_system", pr.PreviewBody(content="正常内容"), _ADMIN)
        assert ok["issues"] == [] and ok["tokens_est"] > 0
        bad = await pr.preview_prompt("knowledge_system", pr.PreviewBody(content="{var}"), _ADMIN)
        assert bad["issues"]


def _uuid(s: str):
    from uuid import UUID

    return UUID(s)
