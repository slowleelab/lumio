"""提示词中心路由端点测试 (管理边界/校验/发布回滚全流程)

复用 bot_server 子进程夹具 (CI 干净环境真实启动); 本地 dev 服务占用 8000 时
该组自动跳过 — 与 test_closed_loop_router 同一套约束。
"""

from __future__ import annotations

import httpx
import pytest_asyncio


@pytest_asyncio.fixture
async def admin_client(db_schema: None, bot_server: str):
    from lumio.shared.auth import create_access_token

    token = create_access_token("test-admin", "admin")
    async with httpx.AsyncClient(
        base_url=bot_server, timeout=30.0, headers={"Authorization": f"Bearer {token}"}
    ) as client:
        yield client


@pytest_asyncio.fixture
async def customer_client(db_schema: None, bot_server: str):
    from lumio.shared.auth import create_access_token

    token = create_access_token("test-customer", "customer")
    async with httpx.AsyncClient(
        base_url=bot_server, timeout=30.0, headers={"Authorization": f"Bearer {token}"}
    ) as client:
        yield client


def _err_code(resp: httpx.Response) -> int:
    return int(resp.json().get("error", {}).get("code", 0))


class TestPromptList:
    async def test_list_merges_db_and_locked_and_unseeded(self, admin_client: httpx.AsyncClient) -> None:
        r = await admin_client.get("/api/admin/prompts")
        assert r.status_code == 200
        names = {i["name"] for i in r.json()["items"]}
        # 可管理 (已 seed 或未 seed 合并展示)
        assert {"knowledge_system", "business_system", "complaint_system"} <= names
        # 工程锁定类只读展示
        assert {"safety_redlines", "qa_judge", "attribution_judge"} <= names
        locked = next(i for i in r.json()["items"] if i["name"] == "safety_redlines")
        assert locked["editable"] is False and locked["source"] == "code"

    async def test_customer_forbidden(self, customer_client: httpx.AsyncClient) -> None:
        r = await customer_client.get("/api/admin/prompts")
        assert r.status_code in (401, 403)


class TestPromptDetail:
    async def test_detail_locked_readonly(self, admin_client: httpx.AsyncClient) -> None:
        r = await admin_client.get("/api/admin/prompts/safety_redlines")
        assert r.status_code == 200
        body = r.json()
        assert body["editable"] is False
        assert "工号" in body["active_content"]  # 红线内容随代码
        assert body["versions"] == []

    async def test_detail_unknown_404(self, admin_client: httpx.AsyncClient) -> None:
        r = await admin_client.get("/api/admin/prompts/no_such_prompt")
        assert r.status_code == 400 and _err_code(r) == 2004  # 2xxx → 400 (分段映射)


class TestDraftValidation:
    async def test_draft_rejects_placeholder(self, admin_client: httpx.AsyncClient) -> None:
        r = await admin_client.post(
            "/api/admin/prompts/knowledge_system/draft",
            json={"content": "请提供 {card_no} 后四位", "changelog": "非法"},
        )
        assert r.status_code == 400 and _err_code(r) == 2011  # 2xxx → 400 (分段映射)

    async def test_draft_rejects_empty(self, admin_client: httpx.AsyncClient) -> None:
        r = await admin_client.post(
            "/api/admin/prompts/knowledge_system/draft", json={"content": "  ", "changelog": "空"}
        )
        assert r.status_code == 400 and _err_code(r) == 2011  # 2xxx → 400 (分段映射)

    async def test_draft_on_locked_rejected(self, admin_client: httpx.AsyncClient) -> None:
        r = await admin_client.post("/api/admin/prompts/safety_redlines/draft", json={"content": "x", "changelog": "y"})
        assert r.status_code == 403 and _err_code(r) == 3011

    async def test_preview_reports_issues(self, admin_client: httpx.AsyncClient) -> None:
        r = await admin_client.post("/api/admin/prompts/knowledge_system/preview", json={"content": "正常内容一段"})
        assert r.status_code == 200
        body = r.json()
        assert body["issues"] == [] and body["chars"] == 6 and body["tokens_est"] > 0

        r2 = await admin_client.post("/api/admin/prompts/knowledge_system/preview", json={"content": "带 {var} 占位"})
        assert r2.status_code == 200 and r2.json()["issues"]


class TestPublishRollbackFlow:
    async def test_draft_publish_rollback_lifecycle(self, admin_client: httpx.AsyncClient) -> None:
        name = "knowledge_system"
        # 基线详情 (触发 lazy seed)
        d0 = (await admin_client.get(f"/api/admin/prompts/{name}")).json()
        base_content = d0["active_content"]
        base_version = d0["active_version"]
        assert base_content  # seed/兜底必有内容

        # 草稿 → 发布
        marker = f"\n(promptops 测试标记 vN {id(base_content)})"
        r = await admin_client.post(
            f"/api/admin/prompts/{name}/draft",
            json={"content": base_content + marker, "changelog": "router 测试发布"},
        )
        assert r.status_code == 200
        draft = r.json()
        assert draft["status"] == "draft" and draft["version"] > base_version

        rp = await admin_client.post(f"/api/admin/prompts/{name}/versions/{draft['id']}/publish")
        assert rp.status_code == 200 and rp.json()["active_version"] == draft["version"]

        # 重复发布同版本 → 409
        rp2 = await admin_client.post(f"/api/admin/prompts/{name}/versions/{draft['id']}/publish")
        assert rp2.status_code == 409

        # 发布后详情切到新版本
        d1 = (await admin_client.get(f"/api/admin/prompts/{name}")).json()
        assert d1["active_version"] == draft["version"] and marker in d1["active_content"]
        statuses = {v["version"]: v["status"] for v in d1["versions"]}
        assert statuses[base_version] == "archived"

        # 回滚到基线版本 → 指针回拨
        base_vid = next(v["id"] for v in d1["versions"] if v["version"] == base_version)
        rr = await admin_client.post(f"/api/admin/prompts/{name}/rollback/{base_vid}")
        assert rr.status_code == 200 and rr.json()["active_version"] == base_version
        d2 = (await admin_client.get(f"/api/admin/prompts/{name}")).json()
        assert d2["active_content"] == base_content  # 内容不可变, 回滚即原样

    async def test_draft_delete_only_draft(self, admin_client: httpx.AsyncClient) -> None:
        name = "business_system"
        d0 = (await admin_client.get(f"/api/admin/prompts/{name}")).json()
        r = await admin_client.post(
            f"/api/admin/prompts/{name}/draft",
            json={"content": d0["active_content"] + "\n(待删草稿)", "changelog": "删草稿"},
        )
        draft = r.json()

        # 已发布版本不可删
        active_vid = next(v["id"] for v in d0["versions"] if v["status"] == "published") if d0["versions"] else None
        if active_vid:
            rd = await admin_client.delete(f"/api/admin/prompts/{name}/versions/{active_vid}")
            assert rd.status_code == 409

        rd2 = await admin_client.delete(f"/api/admin/prompts/{name}/versions/{draft['id']}")
        assert rd2.status_code == 200
        d1 = (await admin_client.get(f"/api/admin/prompts/{name}")).json()
        assert draft["version"] not in {v["version"] for v in d1["versions"]}
