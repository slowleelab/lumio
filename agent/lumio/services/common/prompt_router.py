"""提示词管理 API (PromptOps 管理端 · 修复分流表 D·模型 的落地页)

运营可管理 (DB 源, 版本 append-only + 指针切换):
- generation 类: 四条对话链 system prompt + 摘要 (后台可编辑/发布/回滚)

工程锁定 (代码常量只读展示, editable=False, 变更走代码评审):
- judge 类: 质检裁判/归因裁判/评测裁判 — 改动会让历史判定口径不可比
- classify 类: L3 分类基线/输入仲裁 — 路由准确性的工程基准
- safety 类: 身份与安全红线 — 合规红线不暴露给后台误改

生命周期: 草稿(校验: 长度/占位符契约) → 发布(指针切换, 旧版 archived) → 可回滚(拨指针)
发布即失效缓存 (进程 + Redis), 60s 内全实例收敛; 决策链 llm_generate 事件带
prompt 版本溯源 — 坏例可归因到具体提示词版本.
"""

from __future__ import annotations

import logging
import re
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from lumio.services.bot.prompt_registry import _local_prompt_defs, get_prompt_registry
from lumio.shared.auth import AuthUser, require_role
from lumio.shared.exceptions import LumioError, PromptLockedError, PromptValidationError
from lumio.shared.token_utils import estimate_tokens

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/prompts", tags=["prompts"])

AdminOnlyUser = Annotated[AuthUser, Depends(require_role("admin"))]

MAX_CONTENT_CHARS = 12000
_PLACEHOLDER_RE = re.compile(r"\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}")


# ── 工程锁定类 (代码常量只读) ──


def _locked_prompt_defs() -> dict[str, dict[str, str]]:
    """锁定类清单: 内容直接读代码常量, 保证与线上零漂移"""
    from lumio.services.bot import prompts as _prompts
    from lumio.services.bot.prompts import _SAFETY_REDLINES

    items: dict[str, dict[str, str]] = {
        "safety_redlines": {
            "category": "safety",
            "description": "身份与安全红线 — 自动拼接在所有对话链之后，合规底线，不可后台修改",
            "scene": "所有对话链路每轮自动拼接生效，覆盖全部生成场景",
            "content": _SAFETY_REDLINES,
        }
    }
    # 裁判/分类基线 (存在性判断, 模块重构常量改名时自动跳过而非崩溃)
    try:
        from lumio.services.common.quality_scan import QA_RUBRIC_PROMPT

        items["qa_judge"] = {
            "category": "judge",
            "description": "全量质检裁判 — 判定会话合格/提醒/不合格的审查口径，改动会使历史判定不可比",
            "scene": "每日全量质检巡检，逐会话给出判定结论",
            "content": QA_RUBRIC_PROMPT,
        }
    except Exception:
        pass
    try:
        from lumio.services.common.badcase_loop import _JUDGE_SYSTEM_PROMPT

        items["attribution_judge"] = {
            "category": "judge",
            "description": "坏例归因裁判 — 七层根因的判定口径",
            "scene": "案例工作台点击「归因判定」时，分析坏例的根因层",
            "content": _JUDGE_SYSTEM_PROMPT,
        }
    except Exception:
        pass
    try:
        from lumio.services.common.classifier import _ARBITRATE_SYSTEM_PROMPT, _CLASSIFY_SYSTEM_PROMPT

        items["classify_base"] = {
            "category": "classify",
            "description": "意图分类基线 — 决定客户每句话被分到哪个意图（意图库新增的意图会自动追加）",
            "scene": "每条客户消息进来的第一站，分类结果决定走哪条链路",
            "content": _CLASSIFY_SYSTEM_PROMPT,
        }
        items["input_arbitrate"] = {
            "category": "classify",
            "description": "输入仲裁 — 判定客户回复是噪声、补槽位还是新需求",
            "scene": "客户回复短句（如“是的”、卡号）时，判定其性质",
            "content": _ARBITRATE_SYSTEM_PROMPT,
        }
    except Exception:
        pass
    # 话术类常量 (P1 锁定, P2 评估开放): (slug, 描述, 运用场景, 常量名)
    _script_specs = [
        ("greeting", "会话开场问候语", "客户发送第一条消息时", "GREETING_RESPONSE"),
        ("farewell", "会话结束告别语", "客户告别、会话结束时", "FAREWELL_RESPONSE"),
        (
            "crisis",
            "危机干预话术 — 客户表露自伤意图时的安抚与转人工（合规锁定）",
            "客户表露自伤或轻生意图时，最高优先级触发",
            "CRISIS_RESPONSE",
        ),
        (
            "chitchat_redirect",
            "闲聊引导 — 接住离题话题并引回业务",
            "客户闲聊被识别为闲聊意图时",
            "CHITCHAT_REDIRECT_RESPONSE",
        ),
        (
            "confirm_followup",
            '反问确认跟进 — 客户答"是的"之后给出能力引导',
            "机器人上轮自由反问后，客户回答“是的/好的”时",
            "CONFIRM_FOLLOWUP_RESPONSE",
        ),
    ]
    for slug, desc, scene, const in _script_specs:
        val = getattr(_prompts, const, None)
        if isinstance(val, str):
            items[slug] = {"category": "script", "description": desc, "scene": scene, "content": val}
    return items


async def _load_template(session: Any, name: str) -> Any:
    from lumio.shared.orm_models import PromptTemplate

    return (await session.execute(select(PromptTemplate).where(PromptTemplate.name == name))).scalar_one_or_none()


def _validate_content(content: str, variables: list | None) -> None:
    """草稿校验: 非空/长度上限/占位符契约 (未声明的 {var} 拒绝, 防 f-string 拼错打挂渲染)"""
    if not content or not content.strip():
        raise PromptValidationError("内容不能为空")
    if len(content) > MAX_CONTENT_CHARS:
        raise PromptValidationError(f"内容超长: {len(content)} > {MAX_CONTENT_CHARS} 字符")
    allowed = set(variables or [])
    found = set(_PLACEHOLDER_RE.findall(content))
    illegal = found - allowed
    if illegal:
        raise PromptValidationError(
            f"包含未声明的占位符 {sorted(illegal)} (该模板声明的变量: {sorted(allowed) or '无'})"
        )


# ── 视图模型 ──


class DraftBody(BaseModel):
    content: str
    changelog: str = Field(default="", max_length=200)
    source_case_id: str | None = Field(default=None, max_length=64)


class PreviewBody(BaseModel):
    content: str


# ── API ──


@router.get("")
async def list_prompts(user: AdminOnlyUser) -> dict:
    """提示词资产清单: DB 可管理模板 + 工程锁定类 (只读)"""
    from lumio.services.common.database import get_async_session_factory
    from lumio.shared.orm_models import PromptTemplate, PromptVersion

    items: list[dict] = []
    # 中文描述/运用场景以代码内定义为唯一真源 (DB 行的 description 是 seed 时刻快照, 会过期)
    local_defs = _local_prompt_defs()
    local_desc = {n: d["description"] for n, d in local_defs.items()}
    local_scene = {n: d.get("scene", "") for n, d in local_defs.items()}
    async with get_async_session_factory()() as session:
        tmpls = (
            (await session.execute(select(PromptTemplate).order_by(PromptTemplate.category, PromptTemplate.name)))
            .scalars()
            .all()
        )
        for t in tmpls:
            ver = None
            if t.active_version_id:
                ver = (
                    await session.execute(select(PromptVersion).where(PromptVersion.id == t.active_version_id))
                ).scalar_one_or_none()
            cnt = (
                await session.execute(
                    select(func.count()).select_from(PromptVersion).where(PromptVersion.template_id == t.id)
                )
            ).scalar_one()
            items.append(
                {
                    "name": t.name,
                    "category": t.category,
                    "description": local_desc.get(t.name, t.description),
                    "scene": local_scene.get(t.name, ""),
                    "editable": True,
                    "source": "db",
                    "active_version": ver.version if ver else 0,
                    "active_changelog": (ver.changelog if ver else ""),
                    "version_count": cnt,
                    "updated_at": t.updated_at.isoformat() if t.updated_at else None,
                    "source_case_id": t.source_case_id,
                }
            )
    # 锁定类 (代码常量, 零漂移)
    for slug, d in _locked_prompt_defs().items():
        items.append(
            {
                "name": slug,
                "category": d["category"],
                "description": d["description"],
                "scene": d.get("scene", ""),
                "editable": False,
                "source": "code",
                "active_version": 0,
                "active_changelog": "随代码发版",
                "version_count": 0,
                "updated_at": None,
                "source_case_id": None,
            }
        )
    # 可管理但尚未 lazy seed 的模板 (首次访问/编辑时自动入库 v1)
    seeded = {i["name"] for i in items}
    for slug, d in _local_prompt_defs().items():
        if slug not in seeded:
            items.append(
                {
                    "name": slug,
                    "category": d["category"],
                    "description": d["description"],
                    "scene": d.get("scene", ""),
                    "editable": True,
                    "source": "db",
                    "active_version": 1,
                    "active_changelog": "内置基线 (未入库, 首次访问自动初始化)",
                    "version_count": 0,
                    "updated_at": None,
                    "source_case_id": None,
                }
            )
    return {"items": items}


@router.get("/{name}")
async def prompt_detail(name: str, user: AdminOnlyUser) -> dict:
    """详情: 当前生效内容 + 版本时间线 (锁定类返回代码常量内容)"""
    locked = _locked_prompt_defs().get(name)
    if locked is not None:
        return {
            "name": name,
            **locked,
            "editable": False,
            "source": "code",
            "active_version": 0,
            "active_content": locked["content"],
            "versions": [],
        }
    if name not in _local_prompt_defs():
        raise LumioError(message="提示词不存在", code=2004, status_code=404)
    from lumio.services.common.database import get_async_session_factory
    from lumio.shared.orm_models import PromptVersion

    async with get_async_session_factory()() as session:
        t = await _load_template(session, name)
        if t is None:
            # 尚未 seed: 返回本地基线 (首次 resolve 时才落库)
            d = _local_prompt_defs()[name]
            return {
                "name": name,
                "category": d["category"],
                "description": d["description"],
                "scene": d.get("scene", ""),
                "editable": True,
                "source": "db",
                "active_version": 1,
                "active_content": d["content"],
                "versions": [],
            }
        versions = (
            (
                await session.execute(
                    select(PromptVersion)
                    .where(PromptVersion.template_id == t.id)
                    .order_by(PromptVersion.version.desc())
                )
            )
            .scalars()
            .all()
        )
        active = next((v for v in versions if v.id == t.active_version_id), None)
        return {
            "name": name,
            "category": t.category,
            "description": _local_prompt_defs().get(name, {}).get("description", t.description),
            "scene": _local_prompt_defs().get(name, {}).get("scene", ""),
            "editable": True,
            "source": "db",
            "active_version": active.version if active else 0,
            "active_content": active.content if active else "",
            "versions": [
                {
                    "id": str(v.id),
                    "version": v.version,
                    "status": v.status,
                    "changelog": v.changelog,
                    "created_by": v.created_by,
                    "rollout_pct": v.rollout_pct,
                    "source_case_id": v.source_case_id,
                    "created_at": v.created_at.isoformat() if v.created_at else None,
                    "content": v.content,
                }
                for v in versions
            ],
        }


@router.post("/{name}/draft")
async def create_draft(name: str, body: DraftBody, user: AdminOnlyUser) -> dict:
    """存草稿: 校验(长度/占位符契约) 后新建 draft 版本 (不生效)"""
    if name in _locked_prompt_defs():
        raise PromptLockedError()
    if name not in _local_prompt_defs():
        raise LumioError(message="提示词不存在", code=2004, status_code=404)
    from lumio.services.common.database import get_async_session_factory
    from lumio.shared.orm_models import PromptTemplate, PromptVersion

    async with get_async_session_factory()() as session:
        t = await _load_template(session, name)
        if t is None:
            # 首次编辑前 seed (与 resolve 的 lazy seed 同构; ID 显式预生成以便互相引用)
            from lumio.shared.orm_models import _uuid_v7

            d = _local_prompt_defs()[name]
            t = PromptTemplate(
                id=_uuid_v7(), name=name, category=d["category"], description=d["description"], variables=[]
            )
            v1 = PromptVersion(
                id=_uuid_v7(),
                template_id=t.id,
                version=1,
                content=d["content"],
                changelog="内置基线 (代码常量 lazy seed)",
                created_by="system",
                status="published",
            )
            t.active_version_id = v1.id
            session.add(t)
            session.add(v1)
            await session.flush()
        _validate_content(body.content, t.variables)
        next_ver = (
            await session.execute(
                select(func.coalesce(func.max(PromptVersion.version), 0)).where(PromptVersion.template_id == t.id)
            )
        ).scalar_one() + 1
        draft = PromptVersion(
            template_id=t.id,
            version=next_ver,
            content=body.content,
            changelog=body.changelog or "",
            created_by=user.user_id,
            status="draft",
            source_case_id=body.source_case_id,
        )
        session.add(draft)
        await session.commit()
        return {"id": str(draft.id), "version": draft.version, "status": "draft"}


@router.post("/{name}/versions/{version_id}/publish")
async def publish_version(name: str, version_id: UUID, user: AdminOnlyUser) -> dict:
    """发布: 指针切换 (draft/archived → published, 原 published → archived) + 缓存失效"""
    if name in _locked_prompt_defs():
        raise PromptLockedError()
    from lumio.services.common.database import get_async_session_factory
    from lumio.shared.orm_models import PromptVersion

    async with get_async_session_factory()() as session:
        t = await _load_template(session, name)
        if t is None:
            raise LumioError(message="提示词不存在", code=2004, status_code=404)
        ver = (
            await session.execute(
                select(PromptVersion).where(PromptVersion.id == version_id, PromptVersion.template_id == t.id)
            )
        ).scalar_one_or_none()
        if ver is None:
            raise LumioError(message="版本不存在", code=2004, status_code=404)
        if ver.status == "published":
            raise LumioError(message="该版本已是当前生效版本", code=3010, status_code=409)
        # 同一 template 同时仅一个 published
        olds = (
            (
                await session.execute(
                    select(PromptVersion).where(PromptVersion.template_id == t.id, PromptVersion.status == "published")
                )
            )
            .scalars()
            .all()
        )
        for o in olds:
            o.status = "archived"
        ver.status = "published"
        t.active_version_id = ver.id
        t.source_case_id = ver.source_case_id
        await session.commit()
    await get_prompt_registry().invalidate(name)
    logger.info("prompt 发布: name=%s v%d by=%s", name, ver.version, user.user_id)
    return {"name": name, "active_version": ver.version}


@router.post("/{name}/rollback/{version_id}")
async def rollback_version(name: str, version_id: UUID, user: AdminOnlyUser) -> dict:
    """回滚: 指针拨回历史版本 (内容不可变, 等价于重新发布该版本)"""
    return await publish_version(name, version_id, user)


@router.delete("/{name}/versions/{version_id}")
async def delete_draft(name: str, version_id: UUID, user: AdminOnlyUser) -> dict:
    """删除草稿 (仅 draft 可删, published/archived 不可逆删除)"""
    if name in _locked_prompt_defs():
        raise PromptLockedError()
    from lumio.services.common.database import get_async_session_factory
    from lumio.shared.orm_models import PromptVersion

    async with get_async_session_factory()() as session:
        t = await _load_template(session, name)
        if t is None:
            raise LumioError(message="提示词不存在", code=2004, status_code=404)
        ver = (
            await session.execute(
                select(PromptVersion).where(PromptVersion.id == version_id, PromptVersion.template_id == t.id)
            )
        ).scalar_one_or_none()
        if ver is None:
            raise LumioError(message="版本不存在", code=2004, status_code=404)
        if ver.status != "draft":
            raise LumioError(message="仅草稿可删除 (历史版本不可逆删除, 回滚请用发布指针)", code=3010, status_code=409)
        await session.delete(ver)
        await session.commit()
    return {"deleted": str(version_id)}


@router.post("/{name}/preview")
async def preview_prompt(name: str, body: PreviewBody, user: AdminOnlyUser) -> dict:
    """编辑预览: 字符数/token 估算/占位符检查 (所见即所得校验, 不落库)"""
    if name in _locked_prompt_defs():
        raise PromptLockedError("锁定类无需预览")
    if name not in _local_prompt_defs():
        raise LumioError(message="提示词不存在", code=2004, status_code=404)
    issues: list[str] = []
    try:
        # generation 类模板变量契约恒为空 (动态内容走分层消息注入, 非模板占位符)
        _validate_content(body.content, [])
    except PromptValidationError as exc:
        issues = [str(exc.message)]
    return {
        "chars": len(body.content),
        "tokens_est": estimate_tokens(body.content),
        "issues": issues,
    }
