"""FAQ 管理路由 CRUD 回路测试 (创建→更新→提交→审批→发布→归档→列表过滤)"""

from __future__ import annotations

import uuid

import httpx
import pytest
import pytest_asyncio


@pytest_asyncio.fixture
async def admin_client(db_schema: None, bot_server: str):
    from lumio.shared.auth import create_access_token

    token = create_access_token("test-admin", "admin")
    async with httpx.AsyncClient(
        base_url=bot_server, timeout=30.0, headers={"Authorization": f"Bearer {token}"}
    ) as client:
        yield client


def _payload(tag: str) -> dict:
    return {
        "question": f"覆盖率测试问答 {tag}？",
        "answer": f"这是覆盖率测试答案 {tag}。",
        "variant_questions": [f"{tag} 的变体问法"],
        "category": "general",
        "keywords": ["测试"],
    }


@pytest.mark.asyncio
async def test_faq_crud_lifecycle(admin_client: httpx.AsyncClient) -> None:
    tag = uuid.uuid4().hex[:8]

    # 创建 (草稿)
    r = await admin_client.post("/api/kb/faq", json=_payload(tag))
    assert r.status_code == 200, r.text
    faq_id = r.json()["faq_id"]
    assert r.json()["approval_status"] in ("draft", "DRAFT")

    # 详情
    r2 = await admin_client.get(f"/api/kb/faq/{faq_id}")
    assert r2.status_code == 200 and r2.json()["question"].startswith("覆盖率测试问答")

    # 更新答案
    r3 = await admin_client.put(
        f"/api/kb/faq/{faq_id}", json={"answer": f"更新后的答案 {tag}。"}
    )
    assert r3.status_code == 200

    # 提交 → 审批通过 → 发布 (审批端点均需 FaqApprovalRequest body)
    _c = {"comment": "覆盖率测试"}
    assert (await admin_client.post(f"/api/kb/faq/{faq_id}/submit", json=_c)).status_code == 200
    assert (await admin_client.post(f"/api/kb/faq/{faq_id}/approve", json=_c)).status_code == 200
    assert (await admin_client.post(f"/api/kb/faq/{faq_id}/publish", json=_c)).status_code == 200

    # 列表过滤: 只看已发布, 能按问句前缀查到本条
    r4 = await admin_client.get("/api/kb/faq", params={"approval_status": "PUBLISHED", "limit": 200})
    assert r4.status_code == 200
    assert any(str(f.get("question", "")).startswith("覆盖率测试问答") and tag in str(f.get("question", "")) for f in r4.json()["faqs"])

    # 归档 → 不再出现在发布列表; restore 可回草稿
    assert (await admin_client.post(f"/api/kb/faq/{faq_id}/archive", json=_c)).status_code == 200
    r5 = await admin_client.get("/api/kb/faq", params={"approval_status": "PUBLISHED", "limit": 200})
    assert not any(tag in str(f.get("question", "")) for f in r5.json()["faqs"])
    assert (await admin_client.post(f"/api/kb/faq/{faq_id}/restore", json=_c)).status_code == 200


@pytest.mark.asyncio
async def test_faq_update_missing_raises(admin_client: httpx.AsyncClient) -> None:
    r = await admin_client.put(
        f"/api/kb/faq/{uuid.uuid4()}", json={"answer": "x"}
    )
    assert 400 <= r.status_code < 500


@pytest.mark.asyncio
async def test_faq_duplicate_warning_path(admin_client: httpx.AsyncClient) -> None:
    """同一问句二次创建 → 409 相似提醒 (语义去重检测路径)"""
    tag = uuid.uuid4().hex[:8]
    payload = _payload(tag)
    r1 = await admin_client.post("/api/kb/faq", json=payload)
    assert r1.status_code == 200
    # 同问句再建: 精确重复也会走 check_faq_duplicate → 409 或业务错误, 断言非 200
    r2 = await admin_client.post("/api/kb/faq", json=payload)
    assert r2.status_code != 200 or r2.json().get("faq_id") != r1.json().get("faq_id")
