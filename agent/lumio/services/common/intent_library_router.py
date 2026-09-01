"""意图库管理 API (目标架构 管理端模块② · 分流表 B)

意图树 (五域→组→叶子, 合并运营注册表) + 种子样本 + 属性表 (TrafficClass/链路)
+ 运营意图注册表 (流派二生命周期: 登记→双人评审→评测闸门→影子→生效)
+ L2 索引蓝绿重建/回滚。

种子样本存储 = data/intent_classification/seed_dataset.json (运行时可写副本,
带 .bak 备份); 注册表 = data/intent_classification/intent_registry.json
(建议纳入 Git 管理 —— 控制台变更后由 ops 提交, 即 Config-as-Code 的落点)。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import shutil
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from lumio.services.bot.routing import TrafficClass, classify_traffic
from lumio.services.common.classifier import INTENT_DOMAINS
from lumio.shared.auth import AuthUser, require_role
from lumio.shared.exceptions import LumioError
from lumio.shared.intent_registry import RegistryError, RegistryState, get_registry
from lumio.shared.intent_taxonomy import (
    _DOMAIN_DEFAULT_GROUP,
    _GROUP_OVERRIDES,
    IntentDomain,
    domain_of,
    group_of,
)
from lumio.shared.models import IntentLabel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/intent-library", tags=["intent-library"])

SEED_PATH = Path(__file__).resolve().parents[3] / "data" / "intent_classification" / "seed_dataset.json"

AdminOnlyUser = Annotated[AuthUser, Depends(require_role("admin"))]
AdminAgentUser = Annotated[AuthUser, Depends(require_role("admin", "agent"))]

# 后台 task 引用防 GC (项目规范)
_pending_tasks: set[asyncio.Task] = set()


def _wrap_registry_error(exc: Exception) -> LumioError:
    # 注册表操作违规 (校验失败/非法迁移/双人复核冲突) 属输入类错误 → 2xxx → HTTP 400
    return LumioError(code=2001, message=str(exc))


def _load_seed_data() -> dict[str, Any]:
    with open(SEED_PATH, encoding="utf-8") as f:
        return json.load(f)


def _save_seed_data(data: dict[str, Any]) -> None:
    """写回种子文件, 保留 .bak 一份 (可回滚)"""
    backup = SEED_PATH.with_suffix(".json.bak")
    if SEED_PATH.exists():
        shutil.copy2(SEED_PATH, backup)
    with open(SEED_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")


@router.get("/tree")
async def intent_tree(user: AdminAgentUser) -> dict[str, Any]:
    """意图树: 五域 → 子域组 → 叶子意图 (出厂枚举 + 运营注册表合并展示)"""

    domains: dict[str, dict[str, Any]] = {}
    for label in INTENT_DOMAINS:
        dom = domain_of(label)
        grp = _GROUP_OVERRIDES.get(label) or _DOMAIN_DEFAULT_GROUP.get(dom, "")
        d = domains.setdefault(
            dom.value,
            {"domain": dom.value, "groups": {}},
        )
        g = d["groups"].setdefault(grp, {"group": grp, "intents": []})
        from lumio.shared.intent_labels_zh import intent_desc_zh, intent_name_zh

        g["intents"].append(
            {
                "intent": label.value,
                "name_zh": intent_name_zh(label.value),
                "definition": intent_desc_zh(label.value),
                "domain": dom.value,
                "group": grp,
                "source": "factory",
            }
        )

    # 运营注册表意图 (非驳回均展示, 带状态; 下线的也保留可见性供审计)
    for entry in get_registry().list_entries():
        if entry.state == RegistryState.REJECTED:
            continue
        grp = entry.group or _DOMAIN_DEFAULT_GROUP.get(IntentDomain(entry.domain), "")
        d = domains.setdefault(entry.domain, {"domain": entry.domain, "groups": {}})
        g = d["groups"].setdefault(grp, {"group": grp, "intents": []})
        g["intents"].append(
            {
                "intent": entry.slug,
                "name_zh": entry.name_zh,
                "definition": entry.definition,
                "domain": entry.domain,
                "group": grp,
                "source": "registry",
                "state": entry.state,
            }
        )

    # 排序: 域按固定顺序, 组按名, 叶子按名
    order = [d.value for d in IntentDomain]
    sorted_domains = {k: domains[k] for k in sorted(domains, key=order.index)}
    for dv in sorted_domains.values():
        for gv in dv["groups"].values():
            gv["intents"].sort(key=lambda x: x["intent"])
    return {"domains": sorted_domains}


@router.get("/seeds")
async def list_seeds(
    user: AdminAgentUser,
    intent: str | None = None,
) -> dict[str, Any]:
    """种子样本列表 (可按意图过滤)"""
    data = _load_seed_data()
    examples = data.get("examples", [])
    if intent:
        examples = [e for e in examples if e.get("intent") == intent]
    return {"total": len(examples), "examples": examples}


@router.post("/seeds")
async def add_seed(
    user: AdminOnlyUser,
    body: dict[str, Any],
) -> dict[str, Any]:
    """新增种子样本 (去重: 同 intent + 同 text 已存在则跳过)"""
    intent = str(body.get("intent") or "")
    text = str(body.get("text") or "").strip()
    if not intent or not text:
        raise LumioError(code=2001, message="intent 与 text 必填")
    try:
        IntentLabel(intent)  # 合法性 (旧 flat 别名也允许)
    except ValueError:
        logger.warning("种子样本意图标签非标准: %s", intent)

    data = _load_seed_data()
    existing = {(e.get("intent"), e.get("text")) for e in data.get("examples", [])}
    if (intent, text) in existing:
        return {"created": 0, "duplicate": True}

    data["examples"].append({"text": text, "intent": intent})
    _save_seed_data(data)
    logger.info("种子样本新增: intent=%s text=%s by=%s", intent, text[:30], user.user_id)
    return {"created": 1, "total": len(data["examples"])}


@router.delete("/seeds")
async def delete_seed(
    user: AdminOnlyUser,
    intent: str,
    text: str,
) -> dict[str, Any]:
    """删除种子样本 (精确 intent+text 匹配)"""
    data = _load_seed_data()
    before = len(data["examples"])
    data["examples"] = [e for e in data["examples"] if not (e.get("intent") == intent and e.get("text") == text)]
    removed = before - len(data["examples"])
    if removed:
        _save_seed_data(data)
    return {"removed": removed}


@router.get("/attributes")
async def attribute_table(user: AdminAgentUser) -> dict[str, Any]:
    """属性表 (只读总览): 全部意图的五域/组/交易性质/链路映射 (含注册表)"""

    rows = []
    for label in INTENT_DOMAINS:
        dom = domain_of(label)
        grp = group_of(label)
        _dom, traffic = classify_traffic(label)
        from lumio.shared.intent_labels_zh import intent_name_zh

        rows.append(
            {
                "intent": label.value,
                "name_zh": intent_name_zh(label.value),
                "domain": dom.value,
                "group": grp,
                "traffic_class": traffic.value if traffic else None,
                "touches_account": traffic is not None,  # 有交易性质即触碰账户
                "source": "factory",
            }
        )
    # 注册表意图: traffic_class 显式声明优先, 否则按域默认推导
    _domain_default_traffic = {
        IntentDomain.TRANSACTION.value: TrafficClass.FINANCIAL_TRANSACTION.value,
        IntentDomain.QUERY.value: TrafficClass.READ_ONLY_QUERY.value,
        IntentDomain.SERVICE.value: TrafficClass.HIGH_RISK.value,
    }
    for entry in get_registry().list_entries():
        if entry.state in (RegistryState.REJECTED, RegistryState.DRAFT, RegistryState.PENDING_REVIEW):
            continue
        reg_traffic: str | None = entry.traffic_class or _domain_default_traffic.get(entry.domain)
        rows.append(
            {
                "intent": entry.slug,
                "name_zh": entry.name_zh,
                "domain": entry.domain,
                "group": entry.group or _DOMAIN_DEFAULT_GROUP.get(IntentDomain(entry.domain), ""),
                "traffic_class": reg_traffic,
                "touches_account": reg_traffic is not None,
                "source": "registry",
                "state": entry.state,
            }
        )
    rows.sort(key=lambda x: str(x["intent"]))
    return {"total": len(rows), "rows": rows}


# ══════════════════ 运营意图注册表 (流派二生命周期) ══════════════════


@router.get("/registry")
async def list_registry(user: AdminAgentUser, state: str | None = None) -> dict[str, Any]:
    """注册表列表 (可按状态过滤)"""
    entries = get_registry().list_entries(state=state)
    return {"total": len(entries), "entries": [e.to_dict() for e in entries]}


@router.post("/registry")
async def create_registry_intent(user: AdminOnlyUser, body: dict[str, Any]) -> dict[str, Any]:
    """登记新意图 (maker): draft 状态, 校验命名规范/最少种子/枚举冲突"""
    try:
        entry = get_registry().create(
            slug=str(body.get("slug") or ""),
            domain=str(body.get("domain") or ""),
            name_zh=str(body.get("name_zh") or ""),
            definition=str(body.get("definition") or ""),
            group=str(body.get("group") or ""),
            traffic_class=str(body.get("traffic_class") or ""),
            seeds=[str(s) for s in body.get("seeds") or []],
            actor=user.user_id,
        )
    except RegistryError as exc:
        raise _wrap_registry_error(exc) from exc
    return {"created": True, "entry": entry.to_dict()}


@router.put("/registry/{slug}")
async def update_registry_intent(user: AdminOnlyUser, slug: str, body: dict[str, Any]) -> dict[str, Any]:
    """编辑意图 (仅 draft/eval_failed 可改后重提)"""
    try:
        entry = get_registry().update(
            slug,
            actor=user.user_id,
            seeds=body.get("seeds"),
            name_zh=body.get("name_zh"),
            definition=body.get("definition"),
            traffic_class=body.get("traffic_class"),
        )
    except RegistryError as exc:
        raise _wrap_registry_error(exc) from exc
    return {"updated": True, "entry": entry.to_dict()}


async def _run_publish_gates_task(slug: str, embed: Any) -> None:
    """后台评测任务: 跑两道闸门 → 影子/评测失败"""
    registry = get_registry()
    try:
        from lumio.services.common.intent_eval import run_publish_gates

        entry = registry.get(slug)
        if entry is None or entry.state != RegistryState.EVALUATING:
            return
        report = await run_publish_gates(entry, embed)
        action = "gates_pass" if report.passed else "gates_fail"
        registry.transition(
            slug,
            action,
            actor="system:eval_gates",
            note=f"overlap_max={report.overlap.get('max_similarity')}, golden_drop={report.golden.get('drop')}",
            extra={"eval_report": report.to_dict()},
        )
    except Exception as exc:  # 评测自身异常 → 落 eval_failed 并留痕
        logger.warning("意图发布闸门异常 %s: %s", slug, exc)
        with contextlib.suppress(RegistryError):
            registry.transition(
                slug,
                "gates_fail",
                actor="system:eval_gates",
                note=f"评测异常: {exc}",
                extra={"eval_report": {"passed": False, "error": str(exc)}},
            )


async def _run_rebuild_task(request: Request) -> None:
    index: Any = getattr(request.app.state, "intent_vector", None)
    if index is None:
        return
    try:
        await index.rebuild_versioned()
    except Exception as exc:
        logger.warning("L2 意图索引重建任务失败: %s", exc)


@router.post("/registry/{slug}/submit")
async def submit_registry_intent(user: AdminOnlyUser, slug: str) -> dict[str, Any]:
    """提交评审: draft → pending_review"""
    try:
        entry = get_registry().transition(slug, "submit", actor=user.user_id)
    except RegistryError as exc:
        raise _wrap_registry_error(exc) from exc
    return {"entry": entry.to_dict()}


@router.post("/registry/{slug}/review")
async def review_registry_intent(
    user: AdminOnlyUser,
    slug: str,
    request: Request,
    body: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """双人复核 (checker 不得为登记者): 通过即自动触发评测闸门"""
    body = body or {}
    approve = bool(body.get("approve"))
    action = "approve" if approve else "reject"
    try:
        entry = get_registry().transition(
            slug,
            action,
            actor=user.user_id,
            note=str(body.get("note") or ""),
        )
    except RegistryError as exc:
        raise _wrap_registry_error(exc) from exc
    if approve:
        embed = getattr(request.app.state, "embedding_provider", None)
        if embed is None:
            get_registry().transition(
                slug,
                "gates_fail",
                actor="system:eval_gates",
                note="嵌入服务不可用, 无法评测",
                extra={"eval_report": {"passed": False, "error": "embedding provider unavailable"}},
            )
            raise LumioError(code=4001, message="嵌入服务不可用, 无法执行评测闸门")
        task = asyncio.create_task(_run_publish_gates_task(slug, embed))
        _pending_tasks.add(task)
        task.add_done_callback(_pending_tasks.discard)
    fresh: RegistryEntry | None = get_registry().get(slug)
    return {"entry": fresh.to_dict() if fresh else None}


@router.post("/registry/{slug}/evaluate")
async def evaluate_registry_intent(user: AdminOnlyUser, slug: str, request: Request) -> dict[str, Any]:
    """重跑评测 (eval_failed 修复种子后)"""
    try:
        get_registry().transition(slug, "evaluate", actor=user.user_id)
    except RegistryError as exc:
        raise _wrap_registry_error(exc) from exc
    embed = getattr(request.app.state, "embedding_provider", None)
    if embed is None:
        raise LumioError(code=4001, message="嵌入服务不可用, 无法执行评测闸门")
    task = asyncio.create_task(_run_publish_gates_task(slug, embed))
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)
    return {"evaluating": True}


@router.post("/registry/{slug}/activate")
async def activate_registry_intent(user: AdminOnlyUser, slug: str, request: Request) -> dict[str, Any]:
    """影子 → 生效: 种子并入 L2 索引 (蓝绿重建后台执行, 见 /index/status)"""
    try:
        entry = get_registry().transition(slug, "activate", actor=user.user_id)
    except RegistryError as exc:
        raise _wrap_registry_error(exc) from exc
    task = asyncio.create_task(_run_rebuild_task(request))
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)
    return {"entry": entry.to_dict(), "rebuild": "scheduled"}


@router.post("/registry/{slug}/deprecate")
async def deprecate_registry_intent(user: AdminOnlyUser, slug: str, request: Request) -> dict[str, Any]:
    """生效 → 下线: 从 L2 索引移除种子 (蓝绿重建), 条目保留审计"""
    entry = get_registry().get(slug)
    if entry is None:
        raise LumioError(code=3001, message=f"注册表不存在意图: {slug}")
    was_active = entry.state == RegistryState.ACTIVE
    try:
        entry = get_registry().transition(slug, "deprecate", actor=user.user_id)
    except RegistryError as exc:
        raise _wrap_registry_error(exc) from exc
    if was_active:
        task = asyncio.create_task(_run_rebuild_task(request))
        _pending_tasks.add(task)
        task.add_done_callback(_pending_tasks.discard)
    return {"entry": entry.to_dict()}


@router.post("/registry/{slug}/reject")
async def reject_registry_intent(user: AdminOnlyUser, slug: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    """驳回 (影子阶段也可驳回)"""
    body = body or {}
    try:
        entry = get_registry().transition(slug, "reject", actor=user.user_id, note=str(body.get("note") or ""))
    except RegistryError as exc:
        raise _wrap_registry_error(exc) from exc
    return {"entry": entry.to_dict()}


# ══════════════════ L2 索引蓝绿重建 ══════════════════


@router.get("/index/status")
async def index_status(user: AdminAgentUser) -> dict[str, Any]:
    """索引版本与重建状态"""
    from lumio.services.common.intent_vector import get_rebuild_status

    return dict(get_rebuild_status())


@router.post("/index/rebuild")
async def index_rebuild(user: AdminOnlyUser, request: Request) -> dict[str, Any]:
    """手动触发蓝绿重建 (语料变化后生效)"""
    index = getattr(request.app.state, "intent_vector", None)
    if index is None:
        raise LumioError(code=4001, message="意图向量索引未初始化")
    from lumio.services.common.intent_vector import get_rebuild_status

    status: dict[str, Any] = dict(get_rebuild_status())
    if status.get("running"):
        raise LumioError(code=3001, message="重建进行中, 请稍候")
    task = asyncio.create_task(_run_rebuild_task(request))
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)
    return {"scheduled": True}


@router.post("/index/rollback")
async def index_rollback(user: AdminOnlyUser, request: Request) -> dict[str, Any]:
    """回滚到上一版本索引"""
    index = getattr(request.app.state, "intent_vector", None)
    if index is None:
        raise LumioError(code=4001, message="意图向量索引未初始化")
    try:
        result = await index.rollback_version()
    except Exception as exc:
        raise LumioError(code=3001, message=f"回滚失败: {exc}") from exc
    return dict(result)
