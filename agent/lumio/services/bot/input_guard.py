"""输入层护栏 — 生产级硬性拦截（不依赖 LLM/提示词判断）。

针对两类对抗/合规输入，在进入路由与 LLM 之前由确定性规则直接拦截，
返回固定话术，杜绝大模型在提示词层面兜不住的情况：

1) 身份/角色覆盖 (P1)：尝试让 bot 放弃「Lumio 银行智能客服」身份
   （成为私人助手/秘书/扮演他人、要求输出系统提示词等）。
2) 第三方信息查询 (P2)：要求查询/核验非本人信用卡信息。

拦截优先级高于所有领域路由（含 knowledge/business/fallback 与 LLM 生成），
但低于危机干预（自伤/轻生由 safety 层单独处理，优先级最高）。

设计要点（生产级）:
- 正则 + 归一化双重匹配：原文本与「去空白/标点/小写」归一化文本都跑一遍，
  兼顾中文全半角与英文 prompt-injection 常见句法（IGNORECASE）。
- 匹配用行表驱动（_RULES），新增规则只加一行，便于审计与回归。
- 命中路径完全不触碰分类器/检索/LLM，延迟恒定（微秒级），零幻觉风险。
- 同类 input_guard 命中聚合收敛为单一话术，测试可直接断言常量。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ── 固定拦截话术 ──

# P1 身份覆盖：明确拒绝改变身份 + 不提供内部编号/设定，落回客服立场。
ROLE_OVERRIDE_RESPONSE = (
    "我是 Lumio 银行智能客服，始终围绕您的用卡安全与用卡需求服务。"
    "我无法改变或放弃这一客服身份，也不会提供或编造内部工号、系统设定等凭据。"
    "请问您需要了解信用卡额度、账单、还款或是其他用卡问题？我随时为您服务。"
)

# P2 第三方查询：合规拒绝，不给任何第三方证件片段，引导本人核实或转人工。
THIRD_PARTY_QUERY_RESPONSE = (
    "出于银行合规与客户信息保护的要求，我们无法代为查询或核实他人的信用卡信息，"
    "也不会索要他人的证件信息。若您是持卡人本人，请通过网上银行、手机银行或"
    "拨打客服热线自行核实；如需人工协助，请输入『转人工』。"
)


def _norm(text: str) -> str:
    """归一化: 去空白/全半角标点 + 小写 (兼顾中文全半角与英文注入句式)."""
    return re.sub(r"[\s，。！？；、—…,.!?;:：~～\"'\"“”]+", "", text).lower()


# ── 匹配规则行表 ──
# 每行一条完整正则。片段内用 .{0,N} 兜底连接词兼顾自然语言变化；
# 必须「身份/AI 语义」与「金融语义」共现才命中，避免误伤正常金融提问。
_ROLE_OVERRIDE_PATTERNS: list[str] = [
    # 身份转移类: 让我变成你 / 你现在是我的 X / 假装你是 X / 当我的 X
    r"你是我的(私人?)?(助手|助理|秘书|男朋友|女朋友|老婆|老公|师傅|同事)",
    r"(现在|从(现在|今天)起|以后)+你是我的(私人?)?(助手|助理|秘书|男朋友|女朋友)",
    r"(扮演|假装|装作|假装你是|你现在是|你就是|当|做)我的(私人?)?(助手|助理|秘书|闺蜜|朋友|男朋友|女朋友|老师|医生)",
    # 摒弃 AI/客服身份类
    r"(忘掉|忘记|忘了|忽略|无视|别管|不要管|override|忽略之前|忽略以上).{0,10}(客服|智能客服|机器人|ai|人工智能|设定|指令|规则)",
    r"(别|不要|别再|不许|禁止)当(人工)?客服",
    r"(不要|别说|不许|禁止)再称(呼)?自己是(客服|机器人|ai|人工智能)",
    # 要求输出内部设定/系统提示词类 (直接披露意图)
    r"(输出|展示|给出|泄露|告诉我|给我看|tell me|show me|give me|reveal).{0,6}(你的|your)?(系统提示词|system\s*prompt|设定|内部指令|内部规则|rules)",
    # 英文 Prompt-Injection 常见句法
    r"(now you are|pretend (to be|you are)|act as|from now on).{0,15}(personal assistant|secretary|system|modal|girlfriend|boyfriend|friend)",
]

_THIRD_PARTY_QUERY_PATTERNS: list[str] = [
    # 亲/友/同事/第三方 + 金融领域词
    r"(我朋友|我同事|我老婆|我老公|我家人|我父母|我爸妈|我孩子|我儿子|我女儿|我领导|别人|他人|第三方|别人的).{0,10}(信用卡|卡|账单|额度|欠款|还款|余额|消费|卡号|征信|积分)",
    # 泛化姓名形式 (王某某 / 李某某) 或「他/她」作主体 + 金融领域词
    r"([\u4e00-\u9fa5]某某|他的|她的|他们的).{0,8}(信用卡|借款额度|账单|额度|欠款|还款|余额|卡号|征信)",
    # 他/她直接问"有多少/有没有"某金融项 (无"的"，如"她有什么欠款吗")
    r"(他|她|别人|他人).{0,6}(有没有|有什么|有多少|欠多少|多少|还了多少).{0,8}(欠款|账单|额度|还款|卡号|余额|征信)",
    # 为第三方代办查询类
    r"(帮|替)(我)?(朋友|同事|家人|邻居|别人|他人|第三方).{0,8}(问|查|看|办理)?(信用卡|账单|额度|卡号|还款)",
]


@dataclass(frozen=True)
class GuardHit:
    """一次输入护栏命中."""

    category: str  # "role_override" | "third_party_query"
    response: str


def check_input_guard(user_input: str) -> GuardHit | None:
    """对输入做三类护栏检查，命中返回对应 GuardHit，未命中返回 None.

    - role_override 规则（身份覆盖）优先级高于第三方规则：身份覆盖往往也伴随
      不安全的身份声明，先拦截更保守；两者互斥，同一输入只返回一个结果。
    """
    if not user_input or not user_input.strip():
        return None
    raw = user_input
    norm = _norm(user_input)

    for pattern in _ROLE_OVERRIDE_PATTERNS:
        if re.search(pattern, raw, re.IGNORECASE) or re.search(pattern, norm, re.IGNORECASE):
            return GuardHit(category="role_override", response=ROLE_OVERRIDE_RESPONSE)

    for pattern in _THIRD_PARTY_QUERY_PATTERNS:
        if re.search(pattern, raw, re.IGNORECASE) or re.search(pattern, norm, re.IGNORECASE):
            return GuardHit(category="third_party_query", response=THIRD_PARTY_QUERY_RESPONSE)

    return None
