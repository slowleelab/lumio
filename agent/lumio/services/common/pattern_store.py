"""问题治理存储 (共性聚合层)

问题组 = badcase 按 (intent_label, root_cause_layer) 实时聚合的未销案例集合:
- 新案例自动入组 (无需登记); 治理完成后同类复发自动重开组
- 治理方案挂 fix_pattern (按 group_key upsert), 展示状态全部派生
- 批量执行复用 badcase_store.update_fix_status (状态机守门/确认根因语义不变)

准入纪律保持: 组方案本身就是人工把关 (治理人确认"组内共性根因"), 批量动作
仅覆盖"申报类"流转 (确认根因→修复中 / canary / deployed), 单案例异常仍走案例工作台.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from lumio.shared.logger import get_logger

logger = get_logger(__name__)

# 未销 (open) = 非终态
_OPEN_STATUSES = ("pending", "fixing", "canary", "deployed", "reopened")

from lumio.services.common.badcase_loop import fix_table_for_layer as _fix_table_for_layer  # noqa: E402


def LAYER_FIX_TABLE(layer: str | None) -> str | None:  # noqa: N802 — 保持调用点可读
    """根因层 → 默认分流表 (唯一真源: badcase_loop._LAYER_TO_FIX_TABLE)"""
    ft = _fix_table_for_layer(layer)
    return ft if ft != "none" else None


# 分流表 → 方案建议模板 (按组影响面填充)
PLAN_TEMPLATES: dict[str, str] = {
    "A_knowledge": (
        "【知识补充】组内 {n} 个案例均为知识缺失/检索不命中。到 FAQ 管理补标准问答对, "
        "或在文档管理补充知识文档并重新摄入; 重点覆盖下方共性问题句的话术变体。完成后组内案例批量转灰度验证。"
    ),
    "B_intent": (
        "【意图语料补充】组内 {n} 个案例均为意图误判。到意图库维护页补充规则词/种子样本 "
        "(重点话术见共性问题句), 重跑评测闸门后激活; 误判对【M入 N出】记录进种子集。"
    ),
    "C_rule": (
        "【规则/映射核对】组内 {n} 个案例涉及路由或规则配置。核对意图注册表与流量分类映射 (归并表), "
        "调整后走代码评审; 注意核对同类会话是否系统性走错链路。"
    ),
    "D_model": (
        "【提示词调整】组内 {n} 个案例为生成质量问题。到提示词中心对应链路建草稿修改 "
        "(发布即版本化、可回滚、决策链可溯源到版本); 完成后组内案例批量重放验证。"
    ),
}


def group_key_for(intent_label: str | None, root_cause_layer: str | None) -> str:
    return f"{(intent_label or '_').strip() or '_'}::{root_cause_layer or '_'}"


async def list_patterns(sf: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    """问题组清单: 实时聚合 open 案例 × 左连治理方案; 附待归因计数"""
    from lumio.shared.orm_models import Badcase, FixPattern

    async with sf() as db:
        rows = (
            await db.execute(
                select(
                    Badcase.intent_label,
                    Badcase.root_cause_layer,
                    func.count().label("case_count"),
                    func.sum(case((Badcase.fix_status == "pending", 1), else_=0)).label("pending_count"),
                    func.sum(case((Badcase.needs_human_review, 1), else_=0)).label("unconfirmed_count"),
                    func.count(func.distinct(Badcase.session_id)).label("session_count"),
                    func.max(Badcase.session_time).label("last_seen"),
                    func.sum(
                        case((Badcase.fix_status.in_(("fixing", "canary", "deployed", "reopened")), 1), else_=0)
                    ).label("fixing_count"),
                )
                .where(
                    Badcase.fix_status.in_(_OPEN_STATUSES),
                    Badcase.root_cause_layer.isnot(None),
                    Badcase.root_cause_layer != "uncertain",
                )
                .group_by(Badcase.intent_label, Badcase.root_cause_layer)
                .order_by(func.count().desc())
            )
        ).all()
        plans = {
            p.group_key: p
            for p in (
                await db.execute(
                    select(FixPattern).where(
                        FixPattern.group_key.in_(
                            [group_key_for(r.intent_label, r.root_cause_layer) for r in rows] or ["_::_"]
                        )
                    )
                )
            ).scalars()
        }
        # 待归因 (无根因/uncertain) 的 open 案例 — 留在案例工作台流程, 此处只提示
        unattributed = (
            await db.execute(
                select(func.count())
                .select_from(Badcase)
                .where(
                    Badcase.fix_status.in_(("pending", "reopened")),
                    (Badcase.root_cause_layer.is_(None)) | (Badcase.root_cause_layer == "uncertain"),
                )
            )
        ).scalar_one()

    items = []
    for r in rows:
        key = group_key_for(r.intent_label, r.root_cause_layer)
        plan = plans.get(key)
        status = ("fixing" if (r.fixing_count or 0) > 0 else "planned") if plan else "none"
        items.append(
            {
                "group_key": key,
                "intent_label": r.intent_label,
                "root_cause_layer": r.root_cause_layer,
                "case_count": r.case_count,
                "pending_count": r.pending_count,
                "unconfirmed_count": r.unconfirmed_count,
                "session_count": r.session_count,
                "fixing_count": r.fixing_count,
                "last_seen": r.last_seen.isoformat() if r.last_seen else None,
                "has_plan": plan is not None,
                "plan_status": status,
                "plan_owner": plan.plan_owner if plan else "",
                "fix_table": (plan.fix_table if plan and plan.fix_table else LAYER_FIX_TABLE(r.root_cause_layer)),
                "title": plan.title if plan and plan.title else "",
            }
        )
    return {"items": items, "unattributed": unattributed}


async def get_pattern(sf: async_sessionmaker[AsyncSession], group_key: str) -> dict[str, Any] | None:
    """组详情: 方案 + 组内 open 案例清单 (问题句/状态/根因确认)"""
    from lumio.shared.orm_models import Badcase, FixPattern

    intent_label, root_cause_layer = group_key.split("::", 1) if "::" in group_key else (None, None)
    intent_label = None if intent_label in (None, "_") else intent_label
    root_cause_layer = None if root_cause_layer in (None, "_") else root_cause_layer
    async with sf() as db:
        conds: list[Any] = [Badcase.fix_status.in_(_OPEN_STATUSES)]
        if intent_label is None:
            conds.append(Badcase.intent_label.is_(None))
        else:
            conds.append(Badcase.intent_label == intent_label)
        if root_cause_layer is not None:
            conds.append(Badcase.root_cause_layer == root_cause_layer)
        cases = (
            (
                await db.execute(
                    select(Badcase).where(*conds).order_by(Badcase.session_time.desc().nullslast()).limit(100)
                )
            )
            .scalars()
            .all()
        )
        if not cases:
            return None
        plan = (await db.execute(select(FixPattern).where(FixPattern.group_key == group_key))).scalar_one_or_none()
    n = len(cases)
    return {
        "group_key": group_key,
        "intent_label": intent_label,
        "root_cause_layer": root_cause_layer,
        "case_count": n,
        "unconfirmed_count": sum(1 for c in cases if c.needs_human_review),
        "fix_table": (plan.fix_table if plan and plan.fix_table else LAYER_FIX_TABLE(root_cause_layer)),
        "plan_text": plan.plan_text if plan else "",
        "plan_owner": plan.plan_owner if plan else "",
        "plan_template": PLAN_TEMPLATES.get(
            (plan.fix_table if plan and plan.fix_table else LAYER_FIX_TABLE(root_cause_layer)) or "", ""
        ).format(n=n),
        "cases": [
            {
                "id": str(c.id),
                "user_input": (c.user_input or "")[:80],
                "bot_output": (c.bot_output or "")[:80],
                "session_id": c.session_id,
                "intent_label": c.intent_label,
                "root_cause_layer": c.root_cause_layer,
                "needs_human_review": c.needs_human_review,
                "human_confirmed_layer": c.human_confirmed_layer,
                "fix_status": c.fix_status,
                "fix_table": c.fix_table,
                "session_time": c.session_time.isoformat() if c.session_time else None,
            }
            for c in cases
        ],
    }


async def save_plan(
    sf: async_sessionmaker[AsyncSession],
    group_key: str,
    *,
    intent_label: str | None,
    root_cause_layer: str | None,
    plan_text: str,
    plan_owner: str,
    fix_table: str | None,
    created_by: str,
) -> dict[str, Any]:
    """方案 upsert (按 group_key); 方案落定 = 治理人对组内共性根因的确认"""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from lumio.shared.orm_models import FixPattern

    async with sf() as db:
        stmt = pg_insert(FixPattern)
        stmt = stmt.values(
            group_key=group_key,
            intent_label=intent_label,
            root_cause_layer=root_cause_layer,
            fix_table=fix_table,
            plan_text=plan_text,
            plan_owner=plan_owner,
            created_by=created_by,
            title="",
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["group_key"],
            set_={
                "plan_text": stmt.excluded.plan_text,
                "plan_owner": stmt.excluded.plan_owner,
                "fix_table": stmt.excluded.fix_table,
            },
        )
        await db.execute(stmt)
        await db.commit()
    return {"status": "ok"}
