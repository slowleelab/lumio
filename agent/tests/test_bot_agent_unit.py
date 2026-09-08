

# ── 对话理解升级: 答案已在手复述守卫 ──


def test_answer_grounded_digits_guard() -> None:
    """复述守卫: 答案中的数字串必须逐个出现在来源里, 防转述走样。"""
    from lumio.services.bot.bot_agent import _answer_grounded

    src = "账单金额 8650.00元, 还款日 2026年7月25日"
    assert _answer_grounded("您的还款日为2026年7月25日", src) is True
    assert _answer_grounded("您的还款日为2026年7月26日", src) is False  # 日期被改
    assert _answer_grounded("账单金额是8650.00元", src) is True
    assert _answer_grounded("金额是9999元", src) is False  # 编造金额
    assert _answer_grounded("是的", src) is True  # 无数字短复述放行
