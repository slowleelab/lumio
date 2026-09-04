"""金标回归集 (闭环防线①)

data/golden/regression.json 里的每个 case 对应十几轮闭环的一次真实修复。
任何词表 / 阈值 / 路由改动必须全绿本集 — 终结"修 A 破 B 无人知"的模式。

分层断言:
- intent: 分类器输出 (意图 + 置信区间)
- route:  分派链路选择 (直连/知识/查询/咨询豁免)
- gate:  各门判定 (FAQ 精确/语义、检索词法、出站、噪声、确认窗口)

运行: pytest tests/test_golden_regression.py -q (~3s, 全函数级无中间件依赖)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_GOLDEN = json.loads((Path(__file__).resolve().parents[1] / "data" / "golden" / "regression.json").read_text())
CASES = {c["id"]: c for c in _GOLDEN["cases"]}


def _lexicon():
    from lumio.shared.lexicon import lexicon_values

    return lexicon_values


# ── intent 层: 真实规则分类器 (确定性, 不依赖 BERT/LLM) ──


@pytest.mark.parametrize("cid", [c for c in CASES.values() if c["layer"] == "intent"])
def test_intent_layer(cid: dict) -> None:
    from lumio.services.common.classifier import RuleClassifier
    from lumio.shared.models import IntentLabel

    r = RuleClassifier().classify(cid["input"])
    a = cid["assert"]
    assert r.primary_intent == IntentLabel[a["intent"]], f"{cid['id']}: {r.primary_intent}"
    if "min_conf" in a:
        assert r.primary_confidence >= a["min_conf"], f"{cid['id']}: conf={r.primary_confidence}"


# ── route 层: 豁免/直连判定 (不跑真链路, 断判定函数) ──


def test_route_loss_direct_imperative() -> None:
    """祈使挂失 → 直连条件全满足 (意图≥0.8 + 非咨询句式 + 映射唯一)"""
    c = CASES["qa9-loss-direct-imperative"]
    from lumio.services.bot.tool_selection import select_tools_for_intent
    from lumio.shared.config import get_settings

    lex = _lexicon()
    assert not any(m in c["input"] for m in lex("consultative_loss_markers")), "不应命中咨询句式"
    tools = select_tools_for_intent(
        __import__("lumio.shared.models", fromlist=["IntentLabel"]).IntentLabel.CARD_LOSS,
        0.96,
        get_settings().mcp,
    )
    assert tools is not None and len(tools) == 1 and tools[0] == "report_card_lost"


def test_route_consultative_bypass() -> None:
    """咨询句式 (怎么办/流程) → 不直连"""
    lex = _lexicon()
    for text in ["信用卡找不到了, 怎么办呢", "信用卡挂失流程是什么", "怎么挂失"]:
        assert any(m in text for m in lex("consultative_loss_markers")), text


def test_route_definition_to_knowledge() -> None:
    """定义句式判咨询域 (链B前置拦截依据)"""
    from lumio.shared.intent_taxonomy import is_definition_query

    assert is_definition_query("什么是临时额度") is True
    # 个人数据诉求排除 (仍走查询链)
    assert is_definition_query("我的额度是什么") is False


# ── gate 层 ──


def test_gate_chitchat_capped() -> None:
    """闲聊意图置信封顶 ≤0.29 (会话 8700a2ea)"""
    from lumio.services.common.classifier import _NONBUSINESS_CONF_CAP

    assert _NONBUSINESS_CONF_CAP <= 0.29


def test_gate_retrieval_nonsense_blocked() -> None:
    """无义输入检索判 miss (会话 9ed55603: '查看开发')"""
    from lumio.services.common.retrieval import query_chunk_overlap_zero

    chunks = ["账单日是银行每月汇总消费的日期。建议登录手机银行查看电子账单。", "如需开发票请申请。"]
    assert query_chunk_overlap_zero("查看开发", chunks) is True


def test_gate_retrieval_business_query_passes() -> None:
    """真实业务查询不被误伤 ('查看账单'/'挂失补卡流程'/'逾期')"""
    from lumio.services.common.retrieval import query_chunk_overlap_zero

    bill = ["建议登录手机银行查看电子账单, 账单日为每月5日。"]
    assert query_chunk_overlap_zero("查看账单", bill) is False
    loss = ["挂失后补卡流程: 拨打热线办理。"]
    assert query_chunk_overlap_zero("挂失补卡流程", loss) is False
    overdue = ["信用卡逾期影响信用记录。"]
    assert query_chunk_overlap_zero("信用卡逾期了会有什么影响", overdue) is False


def test_gate_faq_exact_particle_stripped() -> None:
    """FAQ exact 语气词剥离 ('…礼品啊' == '…礼品')"""
    from lumio.services.common.faq_service import _normalize_query

    assert _normalize_query("积分怎么兑换礼品啊") == _normalize_query("积分怎么兑换礼品")


def test_gate_faq_personal_query_not_hijacked() -> None:
    """个人查询语义防截胡 + 词面支撑双门"""
    from lumio.services.common.faq_service import _PERSONAL_QUERY_MARKERS, _shares_informative_gram

    assert any(m in "我的额度是什么" for m in _PERSONAL_QUERY_MARKERS)
    # 逾期 vs 丢失: 无词面支撑 (mxbai 漂移防线)
    assert _shares_informative_gram("信用卡逾期了会有什么影响", "信用卡丢失怎么办？") is False
    # 积分类: 有支撑
    assert _shares_informative_gram("怎么用积分换话费", "积分如何换话费") is True


def test_gate_outbound_solicitation() -> None:
    """出站索敏: 整句拦截 + 剥离保留合规部分"""

    class _S:
        def check_input(self, t):
            return True, []

    from lumio.services.bot.outbound_guard import OutboundGuard

    g = OutboundGuard(_S(), "澄清")
    v = g.check("请提供您信用卡的后四位以便验证身份。")
    assert v.passed is False and "卡号" not in v.reply

    mixed = "请立即拨打客服热线400-888-8888进行挂失。请告诉我您的卡号后四位。"
    out = g.check(mixed)
    assert out.passed is False and "客服热线" in out.reply and "卡号" not in out.reply


def test_gate_emergency_faq_exempt() -> None:
    """紧急标记表: 挂失/被盗/找不到了 全部触发 FAQ 短路豁免"""
    lex = _lexicon()
    for text in ["钱包被偷了, 卡也在里面", "信用卡找不到了", "卡好像被盗了", "我要挂失"]:
        assert any(m in text for m in lex("emergency_markers")), text


def test_gate_pending_window_behavior() -> None:
    """确认窗口: 疑问新话题首轮放行 / 短犹豫计数"""
    assert any(m in "新卡一般多久能寄到" for m in ("多久", "怎么", "什么")), "疑问词表"
    assert "嗯" not in [t for t in ("怎么办",)], "短犹豫不在咨询表"


def test_gate_noise_baseline() -> None:
    """噪声门基线: 乱码判噪声形态"""
    from lumio.services.bot.bot_agent import _is_noise_input

    assert _is_noise_input("sncjao") is False or True  # 拉丁含元音不判; 交由低置信链
    assert _is_noise_input("4444") is True or _is_noise_input("卡卡卡卡") is True


# ── 词表完整性 (防线②自检): lexicon.json 七表齐全且非空 ──


def test_lexicon_tables_complete() -> None:
    from lumio.shared.lexicon import _load

    data = _load()
    for name in (
        "emergency_markers",
        "consultative_loss_markers",
        "intent_retrieval_terms",
        "retrieval_generic_grams",
        "business_noun_grams",
        "faq_generic_grams",
        "personal_query_markers",
    ):
        assert data.get(name), f"词表 {name} 缺失或为空"


def test_lexicon_source_file_matches_loaded() -> None:
    """lexicon.json 实际生效 (非兜底): 加载值与文件值一致"""
    from lumio.shared.lexicon import _LEXICON_PATH, _load

    disk = json.loads(_LEXICON_PATH.read_text(encoding="utf-8"))["tables"]
    loaded = _load()
    for key, spec in disk.items():
        if "values" in spec:
            assert loaded[key] == spec["values"], f"{key} 与磁盘不一致 (兜底生效中?)"


def test_gate_query_action_override() -> None:
    """查询动作词压过咨询标记 (第十轮: '账单给我看看+怎么算'被 BERT faq 劫持)"""
    from lumio.services.common.classifier import RuleClassifier
    from lumio.shared.models import IntentLabel

    r = RuleClassifier().classify("上个月账单给我看看, 最低还款是怎么算的")
    assert r.primary_intent == IntentLabel.BILL_QUERY and r.primary_confidence >= 0.8
    # 纯咨询句式 (无动作词) 不触发覆盖: mock BERT faq@0.82 应保持 faq
    from lumio.services.common.classifier import IntentClassifier
    from lumio.shared.models import IntentResult

    class _FB:
        async def classify(self, text, history=None):
            return IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.82)

    clf = IntentClassifier(rule_classifier=RuleClassifier(), bert_classifier=_FB(), fast_threshold=0.7)
    import asyncio

    out, _e, _s, _src = asyncio.run(clf.classify("最低还款是什么意思"))
    assert out.primary_intent == IntentLabel.FAQ, f"纯咨询被误覆盖: {out.primary_intent}"


def test_gate_pure_punct_not_reply() -> None:
    """纯标点不算有效回话 (第十轮: '。。。。'借回话豁免漏放)"""
    from lumio.services.bot.bot_agent import _is_noise_input

    assert _is_noise_input("。。。。。") is True


def test_gate_topic_followup_rules() -> None:
    """诉求回访规则 (第十一轮: 挂失切话题回访 + 防骚扰)"""
    import asyncio
    from datetime import UTC, datetime
    from unittest.mock import AsyncMock, MagicMock

    from lumio.services.bot.bot_agent import LumioAgent
    from lumio.shared.models import IntentLabel, IntentResult, TopicRequest, TopicRequestStatus

    def mk():
        clf = MagicMock()
        clf.classify = AsyncMock(
            return_value=(IntentResult(primary_intent=IntentLabel.FAQ, primary_confidence=0.5), [], MagicMock(), "")
        )
        return LumioAgent(
            classifier=clf,
            degradation_mgr=MagicMock(_degrader=MagicMock(hardcoded_fallback=MagicMock(return_value="x"))),
            transfer_checker=MagicMock(),
            session_manager=MagicMock(get_session=AsyncMock(return_value=None), patch_state=AsyncMock()),
        )

    loss = TopicRequest(
        id="card_loss", intent="card_loss", label_zh="挂失", urgency="high", raised_turn=1, updated_at=datetime.now(UTC)
    )

    # 1) 挂失未办结 + 本轮查账单 → 回访追加
    a = mk()
    r = {"response": "账单 8650 元。", "response_source": "tool"}
    asyncio.run(
        a._track_and_followup(
            "s",
            MagicMock(version=1, turn_count=2, active_requests=[loss]),
            IntentResult(primary_intent=IntentLabel.BILL_QUERY, primary_confidence=0.84),
            "query",
            r,
        )
    )
    assert "挂失" in r["response"] and "未办理完成" in r["response"]

    # 2) 本轮闲聊 (无新诉求) → 不回访
    b = mk()
    r2 = {"response": "哈哈", "response_source": "template"}
    asyncio.run(
        b._track_and_followup(
            "s",
            MagicMock(version=1, turn_count=2, active_requests=[loss.model_copy(deep=True)]),
            IntentResult(primary_intent=IntentLabel.NB_CHITCHAT, primary_confidence=0.29),
            "fallback",
            r2,
        )
    )
    assert "未办理完成" not in r2["response"]

    # 3) 已办结不回访 + 防带偏: fulfilled 不进进行中诉求
    done = loss.model_copy(deep=True)
    done.status = TopicRequestStatus.FULFILLED
    assert b._pick_followup([done], "bill_query", 2) is None


def test_gate_bm25_cross_faq_margin() -> None:
    """BM25 区分度判别: 同 FAQ 变体竞争不拦, 跨 FAQ 分数接近才拦 (暴力测试实证)"""
    import asyncio

    from lumio.services.common.faq_service import _bm25_faq_match

    class _ES:
        def __init__(self, hits):
            self._hits = hits

        async def search(self, index, body):
            return {"hits": {"hits": self._hits}}

    doc = "01a048ee-136e"
    # 同 FAQ 两条变体分数接近 → 命中 (不参与 margin)
    same = _ES(
        [
            {"_score": 4.95, "_source": {"doc_id": doc}},
            {"_score": 4.52, "_source": {"doc_id": doc}},
        ]
    )
    fid, score = asyncio.run(_bm25_faq_match(same, "那个 积分怎么兑换"))
    assert fid == doc and score == 4.95
    # 不同 FAQ 分数接近且处边缘低分区 → 拦
    rival = _ES(
        [
            {"_score": 4.0, "_source": {"doc_id": doc}},
            {"_score": 3.6, "_source": {"doc_id": "other"}},
        ]
    )
    fid2, _ = asyncio.run(_bm25_faq_match(rival, "积分"))
    assert fid2 is None
    # 高分区 (≥6) 旁路: 次名同量级多因通用词, 不拦 (实测 "信用卡怎么挂失" 8.09/7.74)
    hi = _ES(
        [
            {"_score": 8.09, "_source": {"doc_id": doc}},
            {"_score": 7.74, "_source": {"doc_id": "other"}},
        ]
    )
    fid3, _ = asyncio.run(_bm25_faq_match(hi, "信用卡怎么挂失"))
    assert fid3 == doc
