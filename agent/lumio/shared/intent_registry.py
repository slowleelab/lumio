"""运营意图注册表（流派二 · Config-as-Code + 治理流水线）

目标: 新增意图不再要求发版 —— 管理端走「登记 → 双人评审 → 评测闸门 → 影子
观察 → 生效」五状态流; 出厂 149 意图仍以代码枚举为权威, 注册表只承接增量。

存储: data/intent_classification/intent_registry.json (Git 可追踪的运行时副本,
写前 .bak 备份)。生产部署建议把该文件纳入版本管理, 控制台变更后由 ops 提交。

生命周期状态机:

    draft ──submit──▶ pending_review ──approve──▶ evaluating
                        │                            │ gates.run
                        │reject                      ├─pass──▶ shadow ──activate──▶ active
                        ▼                            │                       │deprecate
                     rejected ◀──reject── eval_failed ◀─fail─┘                ▼
                     (终态)         │                                        deprecated
                                    └─retry evaluate                          (终态, 保留审计)

- 影子 (shadow): 进 L3 候选清单但只记日志不参与 L2 路由索引, 观察命中率/冲突
- 生效 (active): 种子并入 L2 域索引 (蓝绿重建), L3 候选正式生效
- 下线 (deprecated): 只下线不删除 —— 历史会话审计仍需引用
- 双人复核: approve 的 actor 必须不同于 created_by (maker-checker)
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from lumio.shared.logger import get_logger

logger = get_logger(__name__)

REGISTRY_PATH = Path(__file__).resolve().parents[2] / "data" / "intent_classification" / "intent_registry.json"

# 治理策略常量 (生产最佳实践: 新意图最少语料门槛 / 命名规范)
MIN_SEEDS = 10  # 种子样本下限 (建议 20, 硬下限 10)
MAX_SEEDS = 200
SLUG_PATTERN = re.compile(r"^[a-z][a-z0-9_]{2,63}$")

_VALID_DOMAINS = {"query", "transaction", "consulting", "service", "chitchat"}
_VALID_TRAFFIC = {"financial_transaction", "read_only_query", "high_risk", ""}


class RegistryState:
    """生命周期状态 (str 常量, 避免与五域 StrEnum 混淆)"""

    DRAFT = "draft"
    PENDING_REVIEW = "pending_review"
    EVALUATING = "evaluating"  # 评测运行中的瞬态 (持久化里通常看不到)
    EVAL_FAILED = "eval_failed"
    SHADOW = "shadow"
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    REJECTED = "rejected"


# 合法迁移表: (当前状态, 动作) → 目标状态
_TRANSITIONS: dict[tuple[str, str], str] = {
    (RegistryState.DRAFT, "submit"): RegistryState.PENDING_REVIEW,
    (RegistryState.PENDING_REVIEW, "approve"): RegistryState.EVALUATING,
    (RegistryState.PENDING_REVIEW, "reject"): RegistryState.REJECTED,
    (RegistryState.EVALUATING, "gates_pass"): RegistryState.SHADOW,
    (RegistryState.EVALUATING, "gates_fail"): RegistryState.EVAL_FAILED,
    (RegistryState.EVAL_FAILED, "evaluate"): RegistryState.EVALUATING,
    (RegistryState.SHADOW, "activate"): RegistryState.ACTIVE,
    (RegistryState.SHADOW, "reject"): RegistryState.REJECTED,
    (RegistryState.ACTIVE, "deprecate"): RegistryState.DEPRECATED,
}

# 允许编辑字段的状态 (maker 补种子/修正定义后重新提审)
_EDITABLE_STATES = {RegistryState.DRAFT, RegistryState.EVAL_FAILED}

# 进入"已生效"集合 (参与 L2 索引) 的状态
_ROUTING_STATES = {RegistryState.ACTIVE}
# 进入 L3 候选清单的状态 (影子只观察)
_PROMPT_STATES = {RegistryState.SHADOW, RegistryState.ACTIVE}


class RegistryError(Exception):
    """注册表操作违规 (非法迁移/校验失败/双人复核冲突)"""


@dataclass
class RegistryEntry:
    """一条运营新增意图"""

    slug: str
    domain: str  # 五域之一 (query/transaction/consulting/service/chitchat)
    group: str = ""  # 子域组 key (空则域默认组)
    name_zh: str = ""
    definition: str = ""
    traffic_class: str = ""  # 空 → 按域默认推导
    seeds: list[str] = field(default_factory=list)
    state: str = RegistryState.DRAFT
    created_by: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    history: list[dict[str, Any]] = field(default_factory=list)
    eval_report: dict[str, Any] | None = None
    shadow_hits: int = 0
    active_hits: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IntentRegistry:
    """注册表存储 + 状态机 (进程内单例, 文件持久化)"""

    def __init__(self, path: Path | None = None) -> None:
        # 运行时解析默认路径 (默认参数在 def 时绑定, 测试无法 patch 模块属性)
        self._path = path if path is not None else REGISTRY_PATH
        self._lock = threading.Lock()
        self._entries: dict[str, RegistryEntry] = {}
        self._load()

    # ── 持久化 ──

    def _load(self) -> None:
        if not self._path.exists():
            self._entries = {}
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._entries = {e["slug"]: RegistryEntry(**e) for e in data.get("intents", [])}
        except Exception as exc:
            logger.error("意图注册表加载失败(视作空表): %s %s", self._path, exc)
            self._entries = {}

    def _save_locked(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if self._path.exists():
            shutil.copy2(self._path, self._path.with_suffix(".json.bak"))
        payload = {"version": 1, "intents": [e.to_dict() for e in self._entries.values()]}
        self._path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    # ── 查询 ──

    def list_entries(self, *, state: str | None = None) -> list[RegistryEntry]:
        entries = sorted(self._entries.values(), key=lambda e: e.slug)
        if state:
            entries = [e for e in entries if e.state == state]
        return entries

    def get(self, slug: str) -> RegistryEntry | None:
        return self._entries.get(slug)

    def slug_exists(self, slug: str) -> bool:
        return slug in self._entries

    def routing_seeds(self) -> list[dict[str, str]]:
        """已生效意图的种子 (L2 域索引数据源): [{text, intent=domain}]"""
        rows: list[dict[str, str]] = []
        for e in self._entries.values():
            if e.state in _ROUTING_STATES:
                rows.extend({"text": s, "intent": e.domain} for s in e.seeds)
        return rows

    def prompt_intents(self) -> list[dict[str, str]]:
        """L3 候选清单追加项 (影子+生效): [{slug, name_zh, definition, state}]"""
        return [
            {"slug": e.slug, "name_zh": e.name_zh, "definition": e.definition, "state": e.state}
            for e in self._entries.values()
            if e.state in _PROMPT_STATES
        ]

    # ── 校验 ──

    @staticmethod
    def _validate_fields(
        slug: str,
        domain: str,
        seeds: list[str],
        name_zh: str,
        traffic_class: str,
    ) -> None:
        if not SLUG_PATTERN.match(slug):
            raise RegistryError(f"slug 不符合命名规范 (小写字母开头的 a-z0-9_, 3-64 位): {slug!r}")
        if domain not in _VALID_DOMAINS:
            raise RegistryError(f"domain 必须是五域之一 {sorted(_VALID_DOMAINS)}, 收到 {domain!r}")
        if traffic_class not in _VALID_TRAFFIC:
            raise RegistryError(f"traffic_class 非法: {traffic_class!r}")
        clean = [s.strip() for s in seeds if s.strip()]
        if len(clean) < MIN_SEEDS:
            raise RegistryError(f"种子样本不足: 至少 {MIN_SEEDS} 条 (建议 20), 收到 {len(clean)} 条")
        if len(clean) > MAX_SEEDS:
            raise RegistryError(f"种子样本过多: 上限 {MAX_SEEDS} 条")
        if len(set(clean)) != len(clean):
            raise RegistryError("种子样本存在重复文本")
        if not name_zh.strip():
            raise RegistryError("中文名 name_zh 必填")

    def _validate_no_collision(self, slug: str) -> None:
        if self.slug_exists(slug):
            raise RegistryError(f"注册表已存在同名意图: {slug}")
        from lumio.shared.models import IntentLabel

        try:
            IntentLabel(slug)
            raise RegistryError(f"slug 与出厂意图枚举冲突: {slug}")
        except ValueError:
            pass  # 不在枚举中 = 无冲突

    # ── 写操作 ──

    def create(
        self,
        *,
        slug: str,
        domain: str,
        name_zh: str,
        definition: str = "",
        group: str = "",
        traffic_class: str = "",
        seeds: list[str],
        actor: str,
    ) -> RegistryEntry:
        clean = [s.strip() for s in seeds if s.strip()]
        self._validate_fields(slug, domain, clean, name_zh, traffic_class)
        with self._lock:
            self._validate_no_collision(slug)
            entry = RegistryEntry(
                slug=slug,
                domain=domain,
                group=group,
                name_zh=name_zh.strip(),
                definition=definition.strip(),
                traffic_class=traffic_class,
                seeds=clean,
                created_by=actor,
            )
            self._record(entry, "create", actor, None, RegistryState.DRAFT)
            self._entries[slug] = entry
            self._save_locked()
            logger.info("意图登记: %s (%s) by=%s", slug, name_zh, actor)
            return entry

    def update(
        self,
        slug: str,
        *,
        actor: str,
        seeds: list[str] | None = None,
        name_zh: str | None = None,
        definition: str | None = None,
        traffic_class: str | None = None,
    ) -> RegistryEntry:
        with self._lock:
            entry = self._require(slug)
            if entry.state not in _EDITABLE_STATES:
                raise RegistryError(f"状态 {entry.state} 不允许编辑 (仅 draft/eval_failed 可改后重提)")
            if seeds is not None:
                clean = [s.strip() for s in seeds if s.strip()]
                self._validate_fields(
                    slug, entry.domain, clean, name_zh or entry.name_zh, traffic_class or entry.traffic_class
                )
                entry.seeds = clean
            if name_zh is not None and name_zh.strip():
                entry.name_zh = name_zh.strip()
            if definition is not None:
                entry.definition = definition.strip()
            if traffic_class is not None:
                if traffic_class not in _VALID_TRAFFIC:
                    raise RegistryError(f"traffic_class 非法: {traffic_class!r}")
                entry.traffic_class = traffic_class
            entry.updated_at = time.time()
            self._record(entry, "update", actor, entry.state, entry.state)
            self._save_locked()
            return entry

    def transition(
        self,
        slug: str,
        action: str,
        *,
        actor: str,
        note: str = "",
        extra: dict[str, Any] | None = None,
    ) -> RegistryEntry:
        """执行状态迁移 (submit/approve/reject/evaluate/gates_pass/gates_fail/activate/deprecate)"""
        with self._lock:
            entry = self._require(slug)
            key = (entry.state, action)
            target = _TRANSITIONS.get(key)
            if target is None:
                raise RegistryError(f"状态 {entry.state} 不允许动作 {action}")
            if action == "approve" and actor == entry.created_by:
                raise RegistryError("双人复核: 评审人不得与登记者为同一用户 (maker-checker)")
            entry.state = target
            entry.updated_at = time.time()
            if extra:
                entry.eval_report = extra.get("eval_report", entry.eval_report)
            self._record(entry, action, actor, key[0], target, note=note)
            self._save_locked()
            logger.info("意图状态迁移: %s %s --%s--> %s by=%s", slug, key[0], action, target, actor)
            return entry

    def record_hit(self, slug: str, *, shadow: bool) -> None:
        """L3 命中计数 (影子/生效), 进程内累计, 不落盘 (审计走日志与指标)"""
        entry = self._entries.get(slug)
        if entry is None:
            return
        if shadow:
            entry.shadow_hits += 1
        else:
            entry.active_hits += 1

    def _require(self, slug: str) -> RegistryEntry:
        entry = self._entries.get(slug)
        if entry is None:
            raise RegistryError(f"注册表不存在意图: {slug}")
        return entry

    def _record(
        self,
        entry: RegistryEntry,
        action: str,
        actor: str,
        frm: str | None,
        to: str,
        note: str = "",
    ) -> None:
        entry.history.append(
            {
                "ts": time.time(),
                "actor": actor,
                "action": action,
                "from": frm,
                "to": to,
                "note": note,
            }
        )


# ── 进程内单例 (测试可传自定义 path 构造独立实例) ──

_registry: IntentRegistry | None = None
_singleton_lock = threading.Lock()


def get_registry() -> IntentRegistry:
    global _registry
    if _registry is None:
        with _singleton_lock:
            if _registry is None:
                _registry = IntentRegistry()
    return _registry


def reset_registry_singleton() -> None:
    """测试用: 重置单例 (下个 get_registry 重新读文件)"""
    global _registry
    with _singleton_lock:
        _registry = None


# ── 分类管线的注册表回退 (taxonomy / labels 的接入点) ──


def registry_domain(slug: str) -> str | None:
    """slug → 五域 (仅生效/影子状态参与判定; 下线/驳回的不算)"""
    entry = get_registry().get(slug)
    if entry is not None and entry.state in _PROMPT_STATES:
        return entry.domain
    return None


def registry_group(slug: str) -> str | None:
    entry = get_registry().get(slug)
    if entry is not None and entry.state in _PROMPT_STATES:
        return entry.group or None
    return None


def registry_label(slug: str) -> tuple[str, str] | None:
    """slug → (中文名, 定义)"""
    entry = get_registry().get(slug)
    if entry is not None:
        return entry.name_zh, entry.definition
    return None
