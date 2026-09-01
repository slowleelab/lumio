"""意图注册表 (流派二生命周期) 测试: 状态机/双人复核/评测闸门/蓝绿重建/管线接入"""

from __future__ import annotations

import asyncio
import json
import sys
import types

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import lumio.shared.intent_registry as ir
from lumio.services.common import intent_library_router as lib_router
from lumio.services.common import intent_vector
from lumio.shared.auth import AuthUser, get_current_user
from lumio.shared.intent_registry import IntentRegistry, RegistryError, RegistryState
from lumio.shared.middleware import register_exception_handlers

SEEDS_OK = [f"测试种子问法第{i}条" for i in range(10)]


@pytest.fixture
def registry_env(tmp_path, monkeypatch):
    """独立注册表 (临时文件) + 重置单例"""
    reg_path = tmp_path / "intent_registry.json"
    monkeypatch.setattr(ir, "REGISTRY_PATH", reg_path)
    ir.reset_registry_singleton()
    yield ir.get_registry(), reg_path
    ir.reset_registry_singleton()


# ── 状态机 ─────────────────────────────────────────────────────────────


def test_state_machine_happy_path(registry_env) -> None:
    reg, _ = registry_env
    reg.create(slug="fx_rate_query", domain="query", name_zh="外汇牌价查询", seeds=SEEDS_OK, actor="maker")
    reg.transition("fx_rate_query", "submit", actor="maker")
    reg.transition("fx_rate_query", "approve", actor="checker")
    reg.transition("fx_rate_query", "gates_pass", actor="system", extra={"eval_report": {"passed": True}})
    entry = reg.transition("fx_rate_query", "activate", actor="checker")
    assert entry.state == RegistryState.ACTIVE
    # 生效意图: 种子进路由语料, 进 L3 候选
    assert len(reg.routing_seeds()) == 10
    assert reg.routing_seeds()[0]["intent"] == "query"
    assert [p["slug"] for p in reg.prompt_intents()] == ["fx_rate_query"]


def test_maker_checker_self_review_blocked(registry_env) -> None:
    reg, _ = registry_env
    reg.create(slug="fx_rate_query", domain="query", name_zh="外汇牌价查询", seeds=SEEDS_OK, actor="maker")
    reg.transition("fx_rate_query", "submit", actor="maker")
    with pytest.raises(RegistryError, match="双人复核"):
        reg.transition("fx_rate_query", "approve", actor="maker")


def test_illegal_transition_blocked(registry_env) -> None:
    reg, _ = registry_env
    reg.create(slug="fx_rate_query", domain="query", name_zh="外汇牌价查询", seeds=SEEDS_OK, actor="maker")
    # draft 不能直接 approve / activate
    with pytest.raises(RegistryError):
        reg.transition("fx_rate_query", "approve", actor="checker")
    with pytest.raises(RegistryError):
        reg.transition("fx_rate_query", "activate", actor="checker")
    # 下线是终态
    reg.transition("fx_rate_query", "submit", actor="maker")
    reg.transition("fx_rate_query", "approve", actor="checker")
    reg.transition("fx_rate_query", "gates_pass", actor="system")
    reg.transition("fx_rate_query", "activate", actor="checker")
    reg.transition("fx_rate_query", "deprecate", actor="checker")
    with pytest.raises(RegistryError):
        reg.transition("fx_rate_query", "activate", actor="checker")
    # 下线不删除条目 (审计保留), 种子退出索引
    assert reg.get("fx_rate_query") is not None
    assert reg.routing_seeds() == []
    assert reg.prompt_intents() == []


def test_validation_rules(registry_env) -> None:
    reg, _ = registry_env
    with pytest.raises(RegistryError, match="命名规范"):
        reg.create(slug="Bad-Slug", domain="query", name_zh="x", seeds=SEEDS_OK, actor="m")
    with pytest.raises(RegistryError, match="种子样本不足"):
        reg.create(slug="ok_slug_x", domain="query", name_zh="x", seeds=["只有一条"], actor="m")
    with pytest.raises(RegistryError, match="五域"):
        reg.create(slug="ok_slug_x", domain="wealth", name_zh="x", seeds=SEEDS_OK, actor="m")
    with pytest.raises(RegistryError, match="枚举冲突"):
        reg.create(slug="bill_query", domain="query", name_zh="x", seeds=SEEDS_OK, actor="m")
    with pytest.raises(RegistryError, match="重复"):
        reg.create(
            slug="ok_slug_x", domain="query", name_zh="x", seeds=["a", "a", *[f"s{i}" for i in range(8)]], actor="m"
        )


def test_history_audit_trail(registry_env) -> None:
    reg, _ = registry_env
    reg.create(slug="fx_rate_query", domain="query", name_zh="外汇牌价查询", seeds=SEEDS_OK, actor="maker")
    reg.transition("fx_rate_query", "submit", actor="maker", note="首次提审")
    entry = reg.get("fx_rate_query")
    actions = [h["action"] for h in entry.history]
    assert actions == ["create", "submit"]
    assert entry.history[-1]["note"] == "首次提审"
    # 持久化往返
    reg2 = IntentRegistry()
    assert reg2.get("fx_rate_query").state == RegistryState.PENDING_REVIEW
    assert len(reg2.get("fx_rate_query").history) == 2


# ── 分类管线接入 ───────────────────────────────────────────────────────


def test_domain_of_registry_fallback(registry_env) -> None:
    from lumio.shared.intent_taxonomy import IntentDomain, domain_of, group_of

    reg, _ = registry_env
    reg.create(
        slug="fx_rate_query",
        domain="consulting",
        group="C1_product",
        name_zh="外汇牌价查询",
        seeds=SEEDS_OK,
        actor="maker",
    )
    # draft 状态不参与判定 → 归一化兜底 FAQ → 咨询域 (凑巧同域, 用 query 域意图验证负例)
    reg.create(slug="metro_card_bind", domain="transaction", name_zh="地铁卡绑定", seeds=SEEDS_OK, actor="maker")
    # 全部还在 draft: domain_of 不该把 transaction 域给它
    assert domain_of("metro_card_bind") != IntentDomain.TRANSACTION
    # 推到 active 后回退生效
    for action, actor in [("submit", "m"), ("approve", "c"), ("gates_pass", "s"), ("activate", "c")]:
        reg.transition("metro_card_bind", action, actor=actor)
    assert domain_of("metro_card_bind") == IntentDomain.TRANSACTION
    assert group_of("metro_card_bind") == "B2_account_change"  # 域默认组
    assert domain_of("fx_rate_query") == IntentDomain.CONSULTING
    # 出厂意图不受注册表影响
    assert domain_of("account_bill_query") == IntentDomain.QUERY


def test_labels_zh_registry_fallback(registry_env) -> None:
    from lumio.shared.intent_labels_zh import intent_desc_zh, intent_name_zh

    reg, _ = registry_env
    reg.create(
        slug="fx_rate_query",
        domain="query",
        name_zh="外汇牌价查询",
        definition="查各行外汇牌价",
        seeds=SEEDS_OK,
        actor="m",
    )
    # draft 也应有中文标签 (展示用), registry_label 不筛状态
    assert intent_name_zh("fx_rate_query") == "外汇牌价查询"
    assert intent_desc_zh("fx_rate_query") == "查各行外汇牌价"
    assert intent_name_zh("unknown_slug_x") == "unknown_slug_x"


def test_build_prompt_with_registry(registry_env) -> None:
    from lumio.services.common.classifier import _CLASSIFY_SYSTEM_PROMPT, build_classify_system_prompt

    reg, _ = registry_env
    # 空注册表: 与静态基线逐字节一致
    assert build_classify_system_prompt() == _CLASSIFY_SYSTEM_PROMPT
    reg.create(
        slug="fx_rate_query", domain="query", name_zh="外汇牌价查询", definition="查牌价", seeds=SEEDS_OK, actor="m"
    )
    for action, actor in [("submit", "m"), ("approve", "c"), ("gates_pass", "s")]:
        reg.transition("fx_rate_query", action, actor=actor)
    # 影子意图进 prompt 且带标记
    prompt = build_classify_system_prompt()
    assert "fx_rate_query" in prompt and "影子观察中" in prompt
    reg.transition("fx_rate_query", "activate", actor="c")
    prompt = build_classify_system_prompt()
    assert "fx_rate_query" in prompt and "影子观察中" not in prompt


def test_apply_registry_intent(registry_env) -> None:
    from lumio.services.common.classifier import _apply_registry_intent
    from lumio.shared.models import IntentLabel

    reg, _ = registry_env
    reg.create(slug="fx_rate_query", domain="query", name_zh="外汇牌价查询", seeds=SEEDS_OK, actor="m")
    # draft: 不命中
    assert _apply_registry_intent("fx_rate_query") is None
    for action, actor in [("submit", "m"), ("approve", "c"), ("gates_pass", "s")]:
        reg.transition("fx_rate_query", action, actor=actor)
    # 影子: 命中但只记日志, 落域代表叶子
    leaf = _apply_registry_intent("fx_rate_query")
    assert leaf == IntentLabel.ACCOUNT_BILL_QUERY
    assert reg.get("fx_rate_query").shadow_hits == 1
    reg.transition("fx_rate_query", "activate", actor="c")
    leaf = _apply_registry_intent("fx_rate_query")
    assert leaf == IntentLabel.ACCOUNT_BILL_QUERY
    assert reg.get("fx_rate_query").active_hits == 1
    # 未知/出厂意图
    assert _apply_registry_intent("account_bill_query") is None
    assert _apply_registry_intent("nonexistent_x") is None


def test_vector_load_seeds_includes_active_only(registry_env, tmp_path, monkeypatch) -> None:
    seed_file = tmp_path / "seed_dataset.json"
    seed_file.write_text(
        json.dumps({"examples": [{"text": "查账单", "intent": "account_bill_query"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(intent_vector, "SEED_PATH", seed_file)
    reg, _ = registry_env
    reg.create(slug="fx_rate_query", domain="query", name_zh="外汇牌价查询", seeds=SEEDS_OK, actor="m")
    idx = intent_vector.IntentVectorIndex()
    assert len(idx._load_seeds()) == 1  # draft 不进
    for action, actor in [("submit", "m"), ("approve", "c"), ("gates_pass", "s"), ("activate", "c")]:
        reg.transition("fx_rate_query", action, actor=actor)
    rows = idx._load_seeds()
    assert len(rows) == 11
    assert all(r["intent"] in ("query",) for r in rows[1:])


# ── 评测闸门 ───────────────────────────────────────────────────────────


class OneHotEmbed:
    """确定性嵌入: 同文本同向量 (sim=1), 不同文本正交 (sim=0) — 闸门语义可控"""

    def __init__(self) -> None:
        self._index: dict[str, int] = {}

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vecs = []
        for t in texts:
            if t not in self._index:
                self._index[t] = len(self._index)
            v = [0.0] * 512
            v[self._index[t]] = 1.0
            vecs.append(v)
        return vecs


@pytest.fixture
def eval_env(registry_env, tmp_path, monkeypatch):
    from lumio.services.common import intent_eval

    seed_file = tmp_path / "seed_dataset.json"
    seed_file.write_text(
        json.dumps(
            {"examples": [{"text": "存量种子文本甲", "intent": "account_bill_query"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(intent_eval, "SEED_PATH", seed_file)
    yield registry_env[0]


async def test_gates_pass_on_clean_seeds(eval_env) -> None:
    from lumio.services.common.intent_eval import run_publish_gates

    reg = eval_env
    reg.create(slug="fx_rate_query", domain="query", name_zh="外汇牌价查询", seeds=SEEDS_OK, actor="m")
    report = await run_publish_gates(reg.get("fx_rate_query"), OneHotEmbed())
    # 全新种子与存量正交 (sim=0), 金标不回退 → 两门通过
    assert report.overlap["max_similarity"] == 0.0
    assert report.passed is True


async def test_gates_fail_on_duplicate_seed(eval_env) -> None:
    from lumio.services.common.intent_eval import run_publish_gates

    reg = eval_env
    dup_seeds = ["存量种子文本甲", *[f"全新种子{i}" for i in range(9)]]
    reg.create(slug="fx_rate_query", domain="query", name_zh="外汇牌价查询", seeds=dup_seeds, actor="m")
    report = await run_publish_gates(reg.get("fx_rate_query"), OneHotEmbed())
    assert report.passed is False
    assert report.overlap["passed"] is False
    hard = report.overlap["hard_conflicts"]
    assert hard and hard[0]["similarity"] >= 0.92
    assert hard[0]["existing_intent"] == "account_bill_query"


# ── 蓝绿重建 (mock pymilvus) ──────────────────────────────────────────


class _FakeStore:
    def __init__(self) -> None:
        self.collections: dict[str, _FakeCollection] = {}
        self.aliases: dict[str, str] = {}  # alias -> physical
        self.dropped: list[str] = []

    def resolve(self, name: str) -> str:
        return self.aliases.get(name, name)


class _FakeCollection:
    def __init__(self, name: str, schema=None) -> None:
        self.name = name
        self.rows: list = []
        self.loaded = False

    def create_index(self, *a, **k) -> None:
        pass

    def insert(self, columns) -> None:
        self.rows = list(zip(*columns, strict=True))

    def flush(self) -> None:
        pass

    def load(self) -> None:
        self.loaded = True

    @property
    def num_entities(self) -> int:
        return len(self.rows)

    def search(self, **k):
        return []


_STORE = _FakeStore()


def _install_fake_pymilvus(monkeypatch) -> None:
    _STORE.collections.clear()
    _STORE.aliases.clear()
    _STORE.dropped.clear()

    fake_util = types.SimpleNamespace(
        list_collections=lambda: list(_STORE.collections.keys()),
        has_collection=lambda n: n in _STORE.collections or n in _STORE.aliases,
        drop_collection=lambda n: (_STORE.collections.pop(n, None), _STORE.dropped.append(n)),
        create_alias=lambda coll, alias: _STORE.aliases.update({alias: coll}),
        alter_alias=lambda coll, alias: _STORE.aliases.update({alias: coll}),
    )

    def _collection(name: str, schema=None):
        physical = _STORE.resolve(name)
        if physical not in _STORE.collections:
            if schema is None:
                raise ValueError(f"collection not exists: {name}")
            _STORE.collections[physical] = _FakeCollection(physical, schema)
        return _STORE.collections[physical]

    fake_mod = types.ModuleType("pymilvus")
    fake_mod.utility = fake_util
    fake_mod.Collection = _collection
    fake_mod.CollectionSchema = lambda fields, description=None: fields
    fake_mod.FieldSchema = lambda **k: k
    fake_mod.DataType = types.SimpleNamespace(INT64=1, VARCHAR=2, FLOAT_VECTOR=3)
    monkeypatch.setitem(sys.modules, "pymilvus", fake_mod)


class _ListEmbed:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 1024 for _ in texts]


async def test_blue_green_rebuild_and_rollback(registry_env, tmp_path, monkeypatch) -> None:
    _install_fake_pymilvus(monkeypatch)
    seed_file = tmp_path / "seed_dataset.json"
    seed_file.write_text(
        json.dumps({"examples": [{"text": "查账单", "intent": "account_bill_query"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(intent_vector, "SEED_PATH", seed_file)
    reg, _ = registry_env
    reg.create(slug="fx_rate_query", domain="query", name_zh="外汇牌价查询", seeds=SEEDS_OK, actor="m")
    for action, actor in [("submit", "m"), ("approve", "c"), ("gates_pass", "s"), ("activate", "c")]:
        reg.transition("fx_rate_query", action, actor=actor)

    idx = intent_vector.IntentVectorIndex(
        milvus_collection=_FakeCollection("bootstrap"), embedding_provider=_ListEmbed()
    )
    r1 = await idx.rebuild_versioned()
    assert r1 == {"version": 1, "entities": 11}  # 1 出厂 + 10 注册表
    assert _STORE.aliases[intent_vector.COLLECTION_NAME] == f"{intent_vector.COLLECTION_NAME}_v1"
    r2 = await idx.rebuild_versioned()
    assert r2["version"] == 2
    assert _STORE.aliases[intent_vector.COLLECTION_NAME].endswith("_v2")
    assert f"{intent_vector.COLLECTION_NAME}_v1" in _STORE.collections  # 上一版保留
    r3 = await idx.rebuild_versioned()
    assert r3["version"] == 3
    assert f"{intent_vector.COLLECTION_NAME}_v1" in _STORE.dropped  # 只留两版
    out = await idx.rollback_version()
    assert out == {"version": 2}
    assert _STORE.aliases[intent_vector.COLLECTION_NAME].endswith("_v2")
    status = intent_vector.get_rebuild_status()
    assert status["version"] == 2 and status["running"] is False


async def test_rebuild_refuses_empty_corpus(registry_env, tmp_path, monkeypatch) -> None:
    _install_fake_pymilvus(monkeypatch)
    seed_file = tmp_path / "seed_dataset.json"
    seed_file.write_text(json.dumps({"examples": []}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(intent_vector, "SEED_PATH", seed_file)
    idx = intent_vector.IntentVectorIndex(embedding_provider=_ListEmbed())
    with pytest.raises(RuntimeError, match="为空"):
        await idx.rebuild_versioned()


# ── API 全流程 (E2E) ──────────────────────────────────────────────────


class _RecordingIndex:
    def __init__(self) -> None:
        self.rebuild_calls = 0

    async def rebuild_versioned(self) -> dict:
        self.rebuild_calls += 1
        return {"version": 1, "entities": 11}


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    reg_path = tmp_path / "intent_registry.json"
    monkeypatch.setattr(ir, "REGISTRY_PATH", reg_path)
    ir.reset_registry_singleton()
    seed_file = tmp_path / "seed_dataset.json"
    seed_file.write_text(
        json.dumps({"examples": [{"text": "存量种子文本甲", "intent": "account_bill_query"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(lib_router, "SEED_PATH", seed_file)
    from lumio.services.common import intent_eval

    monkeypatch.setattr(intent_eval, "SEED_PATH", seed_file)

    app = FastAPI()
    app.include_router(lib_router.router, prefix="/api")
    register_exception_handlers(app)
    app.state.embedding_provider = OneHotEmbed()
    fake_index = _RecordingIndex()
    app.state.intent_vector = fake_index

    def _user(user_id="maker", role="admin"):
        app.dependency_overrides[get_current_user] = lambda: AuthUser(user_id=user_id, role=role, session_id=None)

    _user()
    yield app, fake_index
    ir.reset_registry_singleton()


async def _wait_state(c: AsyncClient, slug: str, states: set[str], timeout: float = 5.0) -> dict:
    for _ in range(int(timeout / 0.05)):
        r = await c.get("/api/admin/intent-library/registry")
        entry = next((e for e in r.json()["entries"] if e["slug"] == slug), None)
        if entry and entry["state"] in states:
            return entry
        await asyncio.sleep(0.05)
    raise AssertionError(f"等待状态超时: {slug} -> {states}")


@pytest.mark.asyncio
async def test_api_lifecycle_e2e(api_env) -> None:
    app, fake_index = api_env
    slug = "fx_rate_query"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        # 创建 (maker)
        r = await c.post(
            "/api/admin/intent-library/registry",
            json={
                "slug": slug,
                "domain": "query",
                "name_zh": "外汇牌价查询",
                "definition": "查各行外汇牌价",
                "seeds": SEEDS_OK,
            },
        )
        assert r.status_code == 200, r.text
        assert r.json()["entry"]["state"] == "draft"

        # 校验失败路径: 种子不足
        r2 = await c.post(
            "/api/admin/intent-library/registry",
            json={"slug": "short_seeds_x", "domain": "query", "name_zh": "x", "seeds": ["一条"]},
        )
        assert r2.status_code == 400 and "种子样本不足" in r2.json()["error"]["message"]

        # maker 自审被拒
        await c.post(f"/api/admin/intent-library/registry/{slug}/submit")
        app.dependency_overrides[get_current_user] = lambda: AuthUser(user_id="maker", role="admin", session_id=None)
        r3 = await c.post(f"/api/admin/intent-library/registry/{slug}/review", json={"approve": True})
        assert r3.status_code == 400 and "双人复核" in r3.json()["error"]["message"]

        # checker 通过 → 后台闸门 → shadow
        app.dependency_overrides[get_current_user] = lambda: AuthUser(user_id="checker", role="admin", session_id=None)
        r4 = await c.post(
            f"/api/admin/intent-library/registry/{slug}/review", json={"approve": True, "note": "边界清晰"}
        )
        assert r4.status_code == 200
        entry = await _wait_state(c, slug, {"shadow", "eval_failed", "rejected"})
        assert entry["state"] == "shadow", entry
        assert entry["eval_report"]["passed"] is True
        assert entry["history"][-1]["actor"] == "system:eval_gates"

        # 激活 → 触发蓝绿重建 → active + 进树
        r5 = await c.post(f"/api/admin/intent-library/registry/{slug}/activate")
        assert r5.status_code == 200
        await _wait_state(c, slug, {"active"})
        await asyncio.sleep(0.1)
        assert fake_index.rebuild_calls == 1

        tree = (await c.get("/api/admin/intent-library/tree")).json()
        leaves = [i for d in tree["domains"].values() for g in d["groups"].values() for i in g["intents"]]
        mine = [i for i in leaves if i["intent"] == slug]
        assert mine and mine[0]["source"] == "registry" and mine[0]["name_zh"] == "外汇牌价查询"

        attrs = (await c.get("/api/admin/intent-library/attributes")).json()
        row = next(a for a in attrs["rows"] if a["intent"] == slug)
        assert row["traffic_class"] == "read_only_query"  # query 域默认

        # 下线 → 再次重建 + 树里保留
        r6 = await c.post(f"/api/admin/intent-library/registry/{slug}/deprecate")
        assert r6.status_code == 200
        await asyncio.sleep(0.1)
        assert fake_index.rebuild_calls == 2
        tree2 = (await c.get("/api/admin/intent-library/tree")).json()
        leaves2 = [i for d in tree2["domains"].values() for g in d["groups"].values() for i in g["intents"]]
        assert any(i["intent"] == slug and i["state"] == "deprecated" for i in leaves2)


@pytest.mark.asyncio
async def test_api_gates_fail_flow(api_env) -> None:
    app, _ = api_env
    slug = "dup_intent_x"
    dup_seeds = ["存量种子文本甲", *[f"全新种子{i}" for i in range(9)]]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        await c.post(
            "/api/admin/intent-library/registry",
            json={"slug": slug, "domain": "query", "name_zh": "重复意图", "seeds": dup_seeds},
        )
        await c.post(f"/api/admin/intent-library/registry/{slug}/submit")
        app.dependency_overrides[get_current_user] = lambda: AuthUser(user_id="checker", role="admin", session_id=None)
        await c.post(f"/api/admin/intent-library/registry/{slug}/review", json={"approve": True})
        entry = await _wait_state(c, slug, {"eval_failed"})
        assert entry["eval_report"]["passed"] is False
        assert entry["eval_report"]["overlap"]["hard_conflicts"]

        # eval_failed 可修种子后重跑评测
        r = await c.put(
            f"/api/admin/intent-library/registry/{slug}",
            json={"seeds": SEEDS_OK},
        )
        assert r.status_code == 200
        r2 = await c.post(f"/api/admin/intent-library/registry/{slug}/evaluate")
        assert r2.status_code == 200
        entry2 = await _wait_state(c, slug, {"shadow", "eval_failed"})
        assert entry2["state"] == "shadow"
