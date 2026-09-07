"""FAQ 知识库服务单元测试

覆盖 faq_service 的纯逻辑层：查询归一化、缓存 key、审批状态机、
语义去重、三级检索（精确/语义/miss）。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from lumio.services.common.faq_service import (
    _FAQ_TRANSITIONS,
    _cache_key,
    _normalize_query,
    check_faq_duplicate,
    expire_overdue_faqs,
    search_faq,
)


class TestNormalizeQuery:
    """查询归一化测试"""

    def test_lowercase(self) -> None:
        assert _normalize_query("HELLO") == "hello"

    def test_trim_and_collapse_spaces(self) -> None:
        # 2026-08-29: 归一化加强为去全部空白与标点 (精确缓存 key 一致性)
        assert _normalize_query("  你好   世界  ") == "你好世界"

    def test_nfkc_fullwidth_to_ascii(self) -> None:
        """全角字符→半角（NFKC）"""
        result = _normalize_query("ＡＢＣ１２３")
        assert result == "abc123"

    def test_chinese_unchanged(self) -> None:
        assert _normalize_query("信用卡年费怎么减免") == "信用卡年费怎么减免"


class TestCacheKey:
    """精确匹配缓存 key 测试"""

    def test_normalized_produces_deterministic_key(self) -> None:
        assert _cache_key(" 你好 ") == _cache_key("你好")

    def test_different_queries_different_keys(self) -> None:
        assert _cache_key("年费") != _cache_key("额度")

    def test_key_has_prefix(self) -> None:
        assert _cache_key("test").startswith("lumio:faq:exact:")


class TestApprovalTransitions:
    """审批状态机测试"""

    def test_draft_to_review(self) -> None:
        assert "IN_REVIEW" in _FAQ_TRANSITIONS["DRAFT"]

    def test_review_to_approved_or_rejected(self) -> None:
        assert _FAQ_TRANSITIONS["IN_REVIEW"] == {"APPROVED", "REJECTED"}

    def test_approved_to_published(self) -> None:
        assert "PUBLISHED" in _FAQ_TRANSITIONS["APPROVED"]

    def test_rejected_back_to_draft(self) -> None:
        assert "DRAFT" in _FAQ_TRANSITIONS["REJECTED"]

    def test_published_to_superseded_or_archived(self) -> None:
        assert _FAQ_TRANSITIONS["PUBLISHED"] == {"SUPERSEDED", "ARCHIVED"}

    def test_superseded_to_archived(self) -> None:
        assert "ARCHIVED" in _FAQ_TRANSITIONS["SUPERSEDED"]

    def test_illegal_jump_not_allowed(self) -> None:
        """不能从 DRAFT 直接跳到 PUBLISHED"""
        assert "PUBLISHED" not in _FAQ_TRANSITIONS["DRAFT"]


class TestDuplicateCheck:
    """语义去重检测测试"""

    @pytest.mark.asyncio
    async def test_no_providers_returns_empty(self) -> None:
        """无 embedding/milvus 时返回空列表"""
        result = await check_faq_duplicate("测试", None, None)
        assert result == []

    @pytest.mark.asyncio
    async def test_exception_graceful(self) -> None:
        """检索异常时吞掉返回空"""
        embedding = MagicMock()
        embedding.embed_query = AsyncMock(side_effect=RuntimeError("down"))
        collection = MagicMock()

        result = await check_faq_duplicate("测试", embedding, collection)
        assert result == []

    @pytest.mark.asyncio
    async def test_match_above_threshold_returns_duplicates(self) -> None:
        """相似度≥阈值视为重复"""
        embedding = MagicMock()
        embedding.embed_query = AsyncMock(return_value=[0.1] * 768)
        collection = MagicMock()
        collection.search.return_value = [
            [
                MagicMock(score=0.95, entity={"chunk_id": "faq-1", "content": "重复问题", "category": "billing"}),
                MagicMock(score=0.91, entity={"chunk_id": "faq-2", "content": "另一重复", "category": "billing"}),
            ]
        ]

        result = await check_faq_duplicate("测试", embedding, collection, threshold=0.92)
        assert len(result) == 1  # 0.91 < 0.92 应被过滤
        assert result[0]["faq_id"] == "faq-1"

    @pytest.mark.asyncio
    async def test_no_match_returns_empty(self) -> None:
        """所有命中低于阈值"""
        embedding = MagicMock()
        embedding.embed_query = AsyncMock(return_value=[0.1] * 768)
        collection = MagicMock()
        collection.search.return_value = [
            [
                MagicMock(score=0.6, entity={"chunk_id": "faq-3", "content": "不重复"}),
            ]
        ]

        result = await check_faq_duplicate("测试", embedding, collection)
        assert result == []


class TestSearchFaq:
    """FAQ 三级检索测试"""

    @pytest.mark.asyncio
    async def test_exact_match_hit(self) -> None:
        """Redis 缓存命中直接返回 exact"""
        import json

        faq_data = {"id": "faq-1", "question": "年费", "answer": "减免方法..."}
        redis = MagicMock()
        redis.get = AsyncMock(return_value=json.dumps(faq_data).encode())

        result = await search_faq("年费怎么减", redis, session_factory=None)
        assert result["match_type"] == "exact"
        assert result["results"][0]["id"] == "faq-1"

    @pytest.mark.asyncio
    async def test_exact_match_role_filtered_falls_back(self) -> None:
        """精确命中但角色无权限→降级到语义"""
        import json

        faq_data = {"id": "faq-1", "answer": "xxx", "allowed_roles": ["admin"], "card_types": []}
        redis = MagicMock()
        redis.get = AsyncMock(return_value=json.dumps(faq_data).encode())

        result = await search_faq("test", redis, user_role="agent", session_factory=None)
        # 非 admin 角色应被过滤，无 embedding 则降级到 miss
        assert result["match_type"] == "miss"

    @pytest.mark.asyncio
    async def test_semantic_match(self) -> None:
        """Milvus 语义检索返回结果"""
        embedding = MagicMock()
        embedding.embed_query = AsyncMock(return_value=[0.1] * 768)
        collection = MagicMock()
        collection.search.return_value = [
            [
                MagicMock(
                    score=0.88,
                    entity={
                        "chunk_id": "faq-10",
                        "content": "账单日是什么",
                        "category": "billing",
                        "card_types": "",
                    },
                ),
            ]
        ]

        result = await search_faq(
            "账单日",
            None,
            embedding,
            collection,
            session_factory=None,
        )
        assert result["match_type"] == "semantic"
        assert result["results"][0]["faq_id"] == "faq-10"

    @pytest.mark.asyncio
    async def test_no_redis_or_milvus_returns_miss(self) -> None:
        """无 Redis 也无 Milvus→miss"""
        result = await search_faq("任意问题", None, session_factory=None)
        assert result["match_type"] == "miss"
        assert result["results"] == []

    @pytest.mark.asyncio
    async def test_semantic_exception_falls_to_miss(self) -> None:
        """语义检索异常→miss（不抛）"""
        embedding = MagicMock()
        embedding.embed_query = AsyncMock(return_value=[0.1] * 768)
        collection = MagicMock()
        collection.search.side_effect = RuntimeError("down")

        result = await search_faq("测试", None, embedding, collection, session_factory=None)
        assert result["match_type"] == "miss"


class TestExpireOverdue:
    """FAQ 自动过期测试"""

    @pytest.mark.asyncio
    async def test_expire_published_past_expiry(self) -> None:
        """已过期 PUBLISHED FAQ 自动下线"""
        expired_faq = MagicMock()
        expired_faq.approval_status = "PUBLISHED"
        expired_faq.is_current_version = True
        expired_faq.is_deleted = False

        # 构造符合 async_sessionmaker 协议的假 session 工厂
        result = MagicMock()
        result.scalars.return_value.all.return_value = [expired_faq]

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def execute(self, *_a, **_kw):
                return result

            async def commit(self):
                pass

        sf = MagicMock(return_value=_FakeSession())

        count = await expire_overdue_faqs(sf)
        assert expired_faq.approval_status == "SUPERSEDED"
        assert expired_faq.is_current_version is False
        assert count == 1


class TestFaqCrud:
    """FAQ CRUD 单元测试 (mock session)"""

    def _make_session(self, execute_result=None, rows=None):
        result = MagicMock()
        result.scalars.return_value.all.return_value = rows or []
        if execute_result is not None:
            result.scalar.return_value = execute_result

        class _FakeSession:
            def __init__(self):
                self.added = []
                self.committed = False
                self.refreshed = []

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, *a, **kw):
                return result

            async def commit(self):
                self.committed = True

            async def refresh(self, obj):
                self.refreshed.append(obj)

            def add(self, obj):
                self.added.append(obj)

        return _FakeSession()

    @pytest.mark.asyncio
    async def test_create_faq(self) -> None:
        """创建 FAQ: DRAFT + doc_group 生成"""
        from lumio.services.common.faq_service import create_faq

        fake = self._make_session()
        sf = MagicMock(return_value=fake)
        faq = await create_faq(sf, question="年费多少", answer="首年免年费", category="fee")
        assert faq.approval_status == "DRAFT"
        assert faq.doc_group.startswith("faq_")
        assert fake.committed is True
        assert len(fake.refreshed) == 1

    @pytest.mark.asyncio
    async def test_create_faq_with_optional_fields(self) -> None:
        """可选字段透传"""
        from lumio.services.common.faq_service import create_faq

        fake = self._make_session()
        sf = MagicMock(return_value=fake)
        faq = await create_faq(
            sf,
            question="q",
            answer="a",
            category="fee",
            card_types=["platinum"],
            keywords=["年费"],
            created_by="admin",
        )
        assert faq.card_types == ["platinum"]
        assert faq.keywords == ["年费"]
        assert faq.created_by == "admin"

    @pytest.mark.asyncio
    async def test_list_faqs(self) -> None:
        """列表 + 总数"""
        from lumio.services.common.faq_service import list_faqs

        faq = MagicMock()
        faq.id = "11111111-2222-3333-4444-555555555555"
        faq.question = "年费"
        faq.category = "fee"
        faq.approval_status = "PUBLISHED"
        faq.version = 1
        faq.is_current_version = True
        faq.card_types = []
        faq.effective_date = None
        faq.expiry_date = None
        faq.created_at = None

        result = MagicMock()
        result.scalars.return_value.all.return_value = [faq]

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, stmt, *a, **kw):
                if "count" in str(stmt) or "func" in str(stmt):
                    r = MagicMock()
                    r.scalar.return_value = 1
                    return r
                return result

        sf = MagicMock(return_value=_FakeSession())
        faqs, total = await list_faqs(sf, category="fee", approval_status="PUBLISHED")
        assert total == 1
        assert faqs[0]["question"] == "年费"
        assert faqs[0]["approval_status"] == "PUBLISHED"

    @pytest.mark.asyncio
    async def test_get_faq_found(self) -> None:
        """按 ID 获取"""
        from lumio.services.common.faq_service import get_faq

        faq = MagicMock()
        faq.id = "11111111-2222-3333-4444-555555555555"
        faq.question = "q"
        faq.answer = "a"

        result = MagicMock()
        result.scalar_one_or_none.return_value = faq

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, *a, **kw):
                return result

        sf = MagicMock(return_value=_FakeSession())
        found = await get_faq(sf, "11111111-2222-3333-4444-555555555555")
        assert found["question"] == "q"  # get_faq 返回序列化 dict
        assert found["answer"] == "a"

    @pytest.mark.asyncio
    async def test_get_faq_missing(self) -> None:
        """不存在 → None"""
        from lumio.services.common.faq_service import get_faq

        result = MagicMock()
        result.scalar_one_or_none.return_value = None

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, *a, **kw):
                return result

        sf = MagicMock(return_value=_FakeSession())
        assert await get_faq(sf, "missing") is None

    @pytest.mark.asyncio
    async def test_update_faq(self) -> None:
        """更新字段 + 版本递增"""
        from lumio.services.common.faq_service import update_faq

        faq = MagicMock()
        faq.question = "旧"
        faq.answer = "旧答"
        faq.version = 1

        result = MagicMock()
        result.scalar_one_or_none.return_value = faq

        class _FakeSession:
            def __init__(self):
                self.committed = False

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, *a, **kw):
                return result

            async def commit(self):
                self.committed = True

        sf = MagicMock(return_value=_FakeSession())
        updated = await update_faq(sf, "id-1", question="新", answer="新答")
        assert updated is True  # 返回 bool
        assert faq.question == "新"
        assert faq.answer == "新答"

    @pytest.mark.asyncio
    async def test_update_faq_missing_returns_none(self) -> None:
        """更新不存在的 FAQ → None"""
        from lumio.services.common.faq_service import update_faq

        result = MagicMock()
        result.scalar_one_or_none.return_value = None

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, *a, **kw):
                return result

        sf = MagicMock(return_value=_FakeSession())
        assert await update_faq(sf, "missing") is False

    @pytest.mark.asyncio
    async def test_delete_faq_soft_delete(self) -> None:
        """软删除: is_deleted=True"""
        from lumio.services.common.faq_service import delete_faq

        faq = MagicMock()
        faq.is_deleted = False

        result = MagicMock()
        result.scalar_one_or_none.return_value = faq

        class _FakeSession:
            def __init__(self):
                self.committed = False

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, *a, **kw):
                return result

            async def commit(self):
                self.committed = True

        sf = MagicMock(return_value=_FakeSession())
        assert await delete_faq(sf, "id-1") is True
        assert faq.is_deleted is True

    @pytest.mark.asyncio
    async def test_delete_faq_missing(self) -> None:
        """删除不存在 → False"""
        from lumio.services.common.faq_service import delete_faq

        result = MagicMock()
        result.scalar_one_or_none.return_value = None

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, *a, **kw):
                return result

        sf = MagicMock(return_value=_FakeSession())
        assert await delete_faq(sf, "missing") is False

    @pytest.mark.asyncio
    async def test_transition_approval_valid(self) -> None:
        """合法状态流转"""
        from lumio.services.common.faq_service import transition_faq_approval

        faq = MagicMock()
        faq.approval_status = "DRAFT"

        result = MagicMock()
        result.scalar_one_or_none.return_value = faq

        class _FakeSession:
            def __init__(self):
                self.committed = False

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, *a, **kw):
                return result

            async def commit(self):
                self.committed = True

        sf = MagicMock(return_value=_FakeSession())
        result = await transition_faq_approval(sf, "id-1", "IN_REVIEW", actor_id="admin", actor_role="admin")
        assert result == {"status": "ok", "faq_id": "id-1", "approval_status": "IN_REVIEW"}
        assert faq.approval_status == "IN_REVIEW"

    @pytest.mark.asyncio
    async def test_transition_approval_invalid_raises(self) -> None:
        """非法状态流转抛异常"""
        from lumio.services.common.faq_service import transition_faq_approval

        faq = MagicMock()
        faq.approval_status = "DRAFT"

        result = MagicMock()
        result.scalar_one_or_none.return_value = faq

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def execute(self, *a, **kw):
                return result

        sf = MagicMock(return_value=_FakeSession())
        from lumio.shared.exceptions import LumioError

        with pytest.raises(LumioError):
            await transition_faq_approval(sf, "id-1", "PUBLISHED", actor_id="admin", actor_role="admin")


@pytest.mark.asyncio
async def test_semantic_match_blocked_for_personal_query() -> None:
    """个人查询诉求防截胡 (qa_scan 第五轮: "我的额度是什么"被"分期占用额度吗"截胡)"""

    from lumio.services.common import faq_service as fs

    class _Emb:
        async def embed_query(self, text: str) -> list[float]:
            return [1.0, 0.0]

    class _Hit:
        def __init__(self, score: float, question: str) -> None:
            self.score = score
            self.entity = {"chunk_id": "f1#0", "content": question, "category": "分期", "card_type": "", "keywords": []}

    class _Coll:
        def __init__(self, q: str) -> None:
            self._q = q

        def search(self, **kw):
            # content 即语义命中的 question (词面支撑校验读 question 字段)
            return [[_Hit(0.91, self._q), _Hit(0.80, self._q)]]  # 双门槛全过

    class _Redis:
        async def get(self, _k):
            return None

        async def setex(self, *a):
            return None

    res = await fs.search_faq("我的额度是什么", _Redis(), _Emb(), _Coll("分期占用额度吗"))
    assert res["match_type"] == "miss"  # 防截胡放行 (有词面支撑"额度", 但含个人诉求)

    res2 = await fs.search_faq("积分怎么兑换步骤", _Redis(), _Emb(), _Coll("积分兑换步骤详解"))
    assert res2["match_type"] == "semantic"  # 有支撑且无个人诉求 → 命中


def test_shares_informative_gram() -> None:
    """语义命中词面支撑校验 (mxbai 分数漂移 0.85→0.92 的嵌入噪声防线)"""
    from lumio.services.common.faq_service import _shares_informative_gram

    # 「逾期」不在「丢失怎么办」中 → 无支撑 → 拦
    assert _shares_informative_gram("信用卡逾期了会有什么影响", "信用卡丢失怎么办？") is False
    # 「积分」「换话费」共享 → 有支撑 → 放行
    assert _shares_informative_gram("怎么用积分换话费", "积分如何换话费") is True
    # 硬钱包共享 → 放行
    assert _shares_informative_gram("硬钱包如何充值", "数字人民币硬钱包怎么充值") is True
    # 无可判词块 (纯停用词) → 不拦
    assert _shares_informative_gram("信用卡", "信用卡丢失怎么办？") is True


def test_normalize_strips_polite_prefix() -> None:
    """礼貌前缀剥离 (第十二轮: 模拟器随机前缀致 FAQ exact 全线击穿)"""
    from lumio.services.common.faq_service import _normalize_query

    assert _normalize_query("请问一下 积分怎么兑换礼品") == _normalize_query("积分怎么兑换礼品")
    assert _normalize_query("那个 帮我查下账单") == _normalize_query("帮我查下账单")
    assert _normalize_query("我想问下 信用卡怎么挂失呢") == _normalize_query("信用卡怎么挂失")
    # 业务句本身不含前缀词时不误伤
    assert _normalize_query("帮我查一下账单") == "帮我查一下账单"


class TestFaqBm25Channel:
    """FAQ BM25 通道 (范式升级: exact 快路径, BM25 主力 — 变体结构性免疫)"""

    @pytest.fixture
    def fake_es(self):
        class _Hits:
            def __init__(self, hits):
                self.hits = hits

        class _ES:
            def __init__(self, results=None, exc=None):
                self.results = results or []
                self.exc = exc
                self.queries = []

            async def search(self, index, body):
                self.queries.append(body["query"]["match"]["content"]["query"])
                if self.exc:
                    raise self.exc
                return {"hits": {"hits": self.results}}

        return _ES

    @pytest.mark.asyncio
    async def test_bm25_hit_returns_faq(self, fake_es) -> None:
        """BM25 命中 → 回查 PG 返回标准答案 (match_type=bm25)"""
        import lumio.services.common.faq_service as fs

        es = fake_es(results=[{"_score": 8.2, "_source": {"doc_id": FAQ_ID}}])
        mock_db = MagicMock()
        mock_db.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=_faq_row())))

        # 简化: 直接测 _bm25_faq_match (检索层) + 集成验证走 E2E
        faq_id, score = await fs._bm25_faq_match(es, "请问一下哈 积分怎么兑换礼品呢")
        assert faq_id == FAQ_ID and score == 8.2

    @pytest.mark.asyncio
    async def test_bm25_margin_blocks_ambiguous(self, fake_es) -> None:
        """top1/top2 区分度不足 → 不赌 (返回 None)"""
        import lumio.services.common.faq_service as fs

        # 边缘低分区间才判 margin (高分 ≥6 有旁路): 4.0/3.6 <1.3 → 拦
        es = fake_es(
            results=[
                {"_score": 4.0, "_source": {"doc_id": FAQ_ID}},
                {"_score": 3.6, "_source": {"doc_id": "other"}},
            ]
        )
        faq_id, _ = await fs._bm25_faq_match(es, "积分")
        assert faq_id is None
        # 高分同量级 (通用词让次名也高分) → 旁路放行
        es_hi = fake_es(
            results=[
                {"_score": 8.0, "_source": {"doc_id": FAQ_ID}},
                {"_score": 7.0, "_source": {"doc_id": "other"}},
            ]
        )
        fid_hi, _ = await fs._bm25_faq_match(es_hi, "信用卡怎么挂失")
        assert fid_hi == FAQ_ID

    @pytest.mark.asyncio
    async def test_bm25_no_hits(self, fake_es) -> None:
        import lumio.services.common.faq_service as fs

        es = fake_es(results=[])
        faq_id, _ = await fs._bm25_faq_match(es, "完全无关的内容查询")
        assert faq_id is None

    @pytest.mark.asyncio
    async def test_bm25_es_down_degrades(self, fake_es) -> None:
        """ES 故障 → 静默降级 (None), 不抛异常"""
        import lumio.services.common.faq_service as fs

        es = fake_es(exc=RuntimeError("es down"))
        faq_id, score = await fs._bm25_faq_match(es, "积分")
        assert faq_id is None and score == 0.0


FAQ_ID = "01a048ee-136e-7192-8f60-6f3857bf542c"


def _faq_row():
    row = MagicMock()
    row.id = FAQ_ID
    row.question = "积分可以兑换什么？"
    row.answer = "积分可兑换航空里程/商城商品/话费/年费抵扣"
    row.category = "积分"
    row.card_types = []
    row.allowed_roles = []
    return row


def _async_ctx(session):
    class _Ctx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *a):
            return False

    return _Ctx()


class TestBm25CoverageGate:
    """BM25 主体词覆盖门 (会话 sf2 复盘: "信用卡逾期后果"仅凭"信用卡"命中年费 FAQ@7.74)"""

    def test_coverage_rejects_single_term_overlap(self) -> None:
        """主体词 3 个只覆盖 1 个 (信用卡) → 拒绝"""
        from lumio.services.common.faq_service import _bm25_coverage_ok

        # 主体词: 信用卡/逾期/后果; 命中文本只含"信用卡"
        assert _bm25_coverage_ok(["信用卡", "逾期", "后果"], "信用卡年费是多少？") is False

    def test_coverage_passes_full_overlap(self) -> None:
        """主体词全覆盖 → 放行"""
        from lumio.services.common.faq_service import _bm25_coverage_ok

        assert _bm25_coverage_ok(["信用卡", "挂失"], "信用卡丢失了怎么挂失？") is True
        assert _bm25_coverage_ok(["积分", "兑换", "礼品"], "积分怎么兑换礼品") is True

    def test_coverage_two_of_three_passes(self) -> None:
        """2/3 覆盖 (≥2 词且 ≥50%) → 放行 (变体措辞容忍)"""
        from lumio.services.common.faq_service import _bm25_coverage_ok

        assert _bm25_coverage_ok(["信用卡", "逾期", "后果"], "信用卡逾期一次会不会上征信") is True

    def test_coverage_skipped_for_short_queries(self) -> None:
        """主体词 <2 (单词问句) → 不拦, 交分数门"""
        from lumio.services.common.faq_service import _bm25_coverage_ok

        assert _bm25_coverage_ok(["年费"], "信用卡年费是多少") is True
        assert _bm25_coverage_ok(None, "随便") is True

    def test_bm25_match_applies_coverage(self) -> None:
        """_bm25_faq_match 集成: 高分单词共现被覆盖门拦下"""
        import lumio.services.common.faq_service as fs

        class FakeES:
            async def search(self, **kw):
                return {
                    "hits": {
                        "hits": [
                            {"_score": 7.74, "_source": {"doc_id": "d1", "content": "信用卡年费是多少？"}},
                            {"_score": 2.0, "_source": {"doc_id": "d2", "content": "其他"}},
                        ]
                    }
                }

            class _Indices:
                @staticmethod
                async def analyze(**kw):
                    return {"tokens": [{"token": "信用卡"}, {"token": "逾期"}, {"token": "后果"}]}

            indices = _Indices()

        import asyncio

        faq_id, score = asyncio.run(fs._bm25_faq_match(FakeES(), "信用卡逾期了会有什么后果"))
        assert faq_id is None and score == 0.0
