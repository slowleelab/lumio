"""查询工程测试 (三层: 归一/同义/多路)"""

from __future__ import annotations

from lumio.services.bot.query_engine import build_retrieval_queries, normalize_query, synonym_terms


class TestNormalize:
    def test_inverted_word_order(self) -> None:
        """错序口语归一: '就是费年？' → 年费 (replay-clarify_loop-92659 案例)"""
        assert "年费" in normalize_query("就是费年？")

    def test_colloquial_slang(self) -> None:
        """黑话归一: 提额 → 提升额度 (词汇鸿沟)"""
        assert "提升额度" in normalize_query("怎么提额啊")

    def test_filler_and_particle(self) -> None:
        """填充词剥离 + 语气词清理"""
        assert normalize_query("那个 麻烦 卡里还有多少钱呢") == "信用卡账户还有多少钱"

    def test_entity_preserved(self) -> None:
        """实体保全: 数字 (卡号/金额/日期) 原样保留"""
        out = normalize_query("查下尾号 8888 的账单")
        assert "8888" in out

    def test_clean_input_unchanged(self) -> None:
        """书面句不改写 (宁可不改不可改错)"""
        assert normalize_query("信用卡年费减免规则") == "信用卡年费减免规则"


class TestSynonym:
    def test_hits_group(self) -> None:
        syn = synonym_terms("怎么提升额度")
        assert "提额" in syn or "提高额度" in syn

    def test_no_hit_empty(self) -> None:
        assert synonym_terms("今天天气怎么样") == []

    def test_capped(self) -> None:
        """同义扩展上限 (防词表膨胀打爆 ES 查询)"""
        assert len(synonym_terms("提升额度 年费 账单 还款日 积分 挂失 消费")) <= 8


class TestBuildQueries:
    def test_rewritten_wins_as_main(self) -> None:
        """改写句 (自包含) 优先于归一句作主查询"""
        qe = build_retrieval_queries("那还款日呢", "我的信用卡还款日是哪一天")
        assert qe["main"] == "我的信用卡还款日是哪一天"
        # 归一原句 (剥语气词后) 进多路词法 — "还款日"关键词对 BM25 仍有价值
        assert qe["alt_queries"] == ["那还款日"]

    def test_normalized_as_main_and_alt(self) -> None:
        """无改写时归一句作主查询; 原句书面化差异大时进多路"""
        qe = build_retrieval_queries("那个 我想问下 就是费年？")
        assert "年费" in qe["main"]

    def test_synonyms_only_for_lexical(self) -> None:
        """同义词列表只含组内其他词 (不含已在句中的)"""
        qe = build_retrieval_queries("提额怎么办", None)
        for s in qe["synonym_terms"]:
            assert s not in qe["main"]
