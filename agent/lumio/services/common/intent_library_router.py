"""意图库管理 API (目标架构 管理端模块② · 分流表 B)

只读视角: 意图树 (五域→组→叶子) + 种子样本 + 属性表 (TrafficClass/链路)。
种子样本存储 = data/intent_classification/seed_dataset.json (运行时可写副本,
带 .bak 备份); 属性表 = taxonomy 显式覆盖 + TrafficClass 映射的只读聚合。
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from lumio.services.bot.routing import classify_traffic
from lumio.services.common.classifier import INTENT_DOMAINS
from lumio.shared.auth import AuthUser, require_role
from lumio.shared.exceptions import LumioError
from lumio.shared.intent_taxonomy import domain_of, group_of
from lumio.shared.models import IntentLabel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/intent-library", tags=["intent-library"])

SEED_PATH = Path(__file__).resolve().parents[3] / "data" / "intent_classification" / "seed_dataset.json"

AdminOnlyUser = Annotated[AuthUser, Depends(require_role("admin"))]
AdminAgentUser = Annotated[AuthUser, Depends(require_role("admin", "agent"))]


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
    """意图树: 五域 → 子域组 → 叶子意图 (含流量元信息占位)"""
    from lumio.shared.intent_taxonomy import (
        _DOMAIN_DEFAULT_GROUP,
        _GROUP_OVERRIDES,
        IntentDomain,
    )

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
    """属性表 (只读总览): 全部意图的五域/组/交易性质/链路映射"""

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
            }
        )
    rows.sort(key=lambda x: x["intent"])
    return {"total": len(rows), "rows": rows}
