"""RAG 检索质量离线评测

用一组人工标注的「查询 → 期望文档」案例, 对真实 ES 跑 BM25 检索 (ik_smart 分词),
统计 hit@k / MRR, 并输出每个案例的命中详情。

运行: poetry run python scripts/eval_retrieval.py

判定方式: 检索结果 chunk 的 content 含期望文档的特征短语即视为命中该文档。
(注: 生产检索是 BM25+向量 RRF 融合 + reranker 精排, 本脚本是 BM25 单路的下界近似。
chunk 的 frontmatter 已在 parse_markdown 剥离, content 特征短语判定稳定。)
"""

from __future__ import annotations

from dataclasses import dataclass

from elasticsearch import Elasticsearch

ES_URL = "http://127.0.0.1:9200"
INDEX = "lumio_kb_chunks"
TOP_K = 5


@dataclass(frozen=True)
class Case:
    query: str
    expected_doc: str
    phrase: str  # 期望文档内容中的特征短语, 用于命中判定


# 30 个评测案例: 覆盖知识库全部文档 (含办理介绍), 每个案例标注期望命中的文档 + 特征短语
CASES: list[Case] = [
    Case("开门红活动有什么优惠", "信用卡新年开门红活动规则", "开门红"),
    Case("夏天消费有什么返现活动", "信用卡夏日消费季活动规则", "夏日消费季"),
    Case("哪些卡的年费不能减免", "信用卡年费硬性减免条件", "刚性年费"),
    Case("年费怎么减免", "信用卡年费减免政策", "消费达标减免"),
    Case("年费一年多少钱", "信用卡年费常见问题", "年费是多少"),
    Case("账单日和还款日分别是什么", "信用卡账单常见问题", "还款日是账单日后第20天"),
    Case("分期有哪些类型", "信用卡分期常见问题", "现金分期"),
    Case("信用卡丢了怎么挂失", "信用卡挂失补办常见问题", "书面挂失"),
    Case("积分是怎么获得的", "信用卡积分常见问题", "积分如何获取"),
    Case("各卡种年费标准是多少", "信用卡年费费率表", "年费标准"),
    Case("取现手续费怎么收", "信用卡取现及手续费费率表", "取现手续费"),
    Case("分期手续费率是多少", "信用卡分期手续费费率表", "提前还款违约金"),
    Case("客服电话和渠道有哪些", "信用卡客户服务指南", "官方网站"),
    Case("积分获取有什么规则", "信用卡积分获取规则", "生日月消费"),
    Case("积分怎么兑换", "信用卡积分兑换规则", "积分商城"),
    Case("积分有效期是多久", "信用卡积分有效期及清零规则", "自获得之日起"),
    Case("信用卡章程内容是什么", "信用卡章程摘要", "银行保险监督管理委员会"),
    Case("领用合约有什么条款", "信用卡领用合约摘要", "还款顺序"),
    Case("免息期有多长", "信用卡账单日与还款日说明", "免息期"),
    Case("有哪些还款方式", "信用卡还款方式说明", "自动还款"),
    Case("挂失后怎么补卡", "信用卡挂失补卡流程", "补卡"),
    Case("被盗刷了怎么处理", "信用卡盗刷处理流程", "盗刷申报"),
    # ── 办理介绍 (process) 文档: 写类意图知识问答的知识来源 (会话 48882b05) ──
    Case("怎么申请提额", "信用卡额度调整（提额/降额）办理介绍", "临时额度提升"),
    Case("我想降额怎么办理", "信用卡额度调整（提额/降额）办理介绍", "额度下调"),
    Case("怎么开通电子账单", "电子账单设置与纸质账单补寄办理介绍", "电子账单设置"),
    Case("自动还款怎么设置", "自动还款设置办理介绍", "自动还款设置"),
    Case("出国旅游要不要锁境外交易", "境外交易锁定与解锁办理介绍", "境外交易锁"),
    Case("信用卡怎么绑定微信支付宝", "手机钱包绑定与解绑办理介绍", "添加银行卡"),
    Case("新卡怎么激活", "信用卡激活与销卡办理介绍", "卡片激活"),
    Case("分期不想用了怎么取消", "账单分期取消、结清与期数变更办理介绍", "撤销申请"),
]


def _bm25_search(es: Elasticsearch, query: str, top_k: int) -> list[tuple[str, float]]:
    """BM25 检索, 返回 [(content, score)]"""
    resp = es.search(
        index=INDEX,
        query={"match": {"content": {"query": query, "analyzer": "ik_smart"}}},
        size=top_k,
    )
    return [(h["_source"].get("content", ""), h["_score"] or 0.0) for h in resp["hits"]["hits"]]


def main() -> None:
    es = Elasticsearch(ES_URL)
    if not es.ping():
        print(f"[错误] 无法连接 ES: {ES_URL}")
        return

    hits_at_k = {1: 0, 3: 0, 5: 0}
    reciprocal_ranks: list[float] = []
    details: list[str] = []

    for case in CASES:
        results = _bm25_search(es, case.query, TOP_K)
        # 找到特征短语命中的排名 (0-based)
        rank = next(
            (i for i, (content, _) in enumerate(results) if case.phrase in content),
            None,
        )
        if rank is None:
            details.append(f"✗ {case.query} -> 未命中「{case.expected_doc}」")
            reciprocal_ranks.append(0.0)
            continue

        for k in hits_at_k:
            if rank < k:
                hits_at_k[k] += 1
        reciprocal_ranks.append(1.0 / (rank + 1))
        details.append(f"✓ {case.query} -> 「{case.expected_doc}」@rank{rank + 1}")

    total = len(CASES)
    print("=" * 70)
    print("RAG 检索质量评测报告 (BM25, ik_smart, 22 个标注案例)")
    print("=" * 70)
    print(f"案例总数: {total}")
    print(f"Hit@1: {hits_at_k[1]}/{total} = {hits_at_k[1] / total:.1%}")
    print(f"Hit@3: {hits_at_k[3]}/{total} = {hits_at_k[3] / total:.1%}")
    print(f"Hit@5: {hits_at_k[5]}/{total} = {hits_at_k[5] / total:.1%}")
    print(f"MRR: {sum(reciprocal_ranks) / total:.3f}")
    print("-" * 70)
    for d in details:
        print(d)


if __name__ == "__main__":
    main()
