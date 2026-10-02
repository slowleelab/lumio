<template>
  <div class="pipeline-map">
    <div class="page-head">
      <div>
        <h2>处理链路</h2>
        <p class="muted head-sub">客户消息从接收到结果输出的完整旅程 — 中央主干自上而下, 点击节点看环节详情</p>
      </div>
      <div class="head-right">
        <el-select v-model="statsHours" size="small" style="width: 108px" @change="loadStats">
          <el-option label="近 24 小时" :value="24" />
          <el-option label="近 3 天" :value="72" />
          <el-option label="近 7 天" :value="168" />
        </el-select>
        <span v-if="stats" class="muted stats-total">共 {{ fmtCount(stats.total) }} 条决策</span>
      </div>
    </div>

    <!-- 演示控制 -->
    <div class="demo-bar">
      <el-button size="small" :type="demo.running ? 'danger' : 'primary'" round @click="toggleDemo">
        {{ demo.running ? "■ 停止演示" : "▶ 演示一次消息流转" }}
      </el-button>
      <span v-if="demo.running" class="demo-step muted">{{ demo.label }}</span>
      <span v-else class="muted demo-hint">示例: "上个月账单多少" — 沿主线高亮走一遍入闸 → 理解 → 路由 → 链 B → 出闸</span>
    </div>

    <!-- 图例 -->
    <div class="legend">
      <span class="lg"><i class="fsym sc">⏹</i>命中短路</span>
      <span class="lg"><i class="fsym br">↳</i>条件分支</span>
      <span class="lg"><i class="fsym pa">‖</i>并行</span>
      <span class="lg"><i class="fsym ca">⇢</i>级联注入</span>
      <span class="lg"><i class="fsym as">⚡</i>异步</span>
      <span class="lg-sep" />
      <span class="lg"><i class="dot dot-danger" />拦截环节</span>
      <span class="lg"><i class="dot dot-primary" />理解/路由</span>
      <span class="lg"><i class="dot dot-success" />执行链</span>
      <span class="lg muted">节点角标 = 真实流量 (近 {{ stats?.hours ?? 24 }}h)</span>
    </div>

    <!-- ── 主干链路: 中央垂直干线, 节点挂线 (手机友好, 自带连线视觉) ── -->
    <div class="trunk" :class="{ demoing: demo.running }">
      <div v-for="(lane, li) in PIPELINE_STAGES" :key="lane.key" class="stage" :class="'stage-' + lane.key">
        <!-- 阶段带 -->
        <div class="stage-band">
          <span class="stage-idx">{{ lane.idx }}</span>
          <span class="stage-name">{{ lane.name }}</span>
          <span class="stage-sub muted">{{ lane.sub }}</span>
          <el-tag v-if="lane.cost" size="small" effect="plain" :type="lane.costType || 'info'">{{ lane.cost }}</el-tag>
        </div>

        <!-- 入闸/理解/路由/出闸: 主干节点序列 -->
        <div v-if="lane.key !== 'exec'" class="stage-nodes">
          <template v-for="(node, i) in lane.nodes" :key="node.id">
            <div v-if="i" class="link-seg"><span class="link-dot" />通过</div>
            <button
              class="t-node"
              :class="{
                guard: node.kind === 'guard',
                lit: demoSeq.includes(node.id),
                now: demo.running && demoSeq[demo.step] === node.id,
                selected: selected?.id === node.id,
              }"
              @click="select(node)"
            >
              <span class="tn-head">
                <span class="tn-name">{{ node.name }}</span>
                <span v-if="badgeFor(node)" class="tn-badge" :class="{ hot: node.kind === 'guard' }">{{ badgeFor(node) }}</span>
              </span>
              <span class="tn-sub">{{ node.sub }}</span>
              <span v-if="node.flows?.length" class="tn-flows">
                <span v-for="f in node.flows" :key="f.label" class="flow-tag" :class="'fk-' + f.kind">{{ FLOW_SYM[f.kind] }} {{ f.label }}</span>
              </span>
            </button>
          </template>
        </div>

        <!-- 执行层: 分叉扇形 → 五链 → 收敛 -->
        <div v-else class="stage-exec">
          <div class="fan-head">
            <span class="fan-label">决策分叉 — 按消息性质走且只走一条链</span>
          </div>
          <svg class="fan-svg" viewBox="0 0 1000 90" preserveAspectRatio="none">
            <path
              v-for="(chain, ci) in EXEC_CHAINS" :key="chain.id"
              class="fan-path"
              :class="{ lit: demoSeq.includes(chain.id) }"
              :d="fanPath(ci, EXEC_CHAINS.length)"
            />
          </svg>
          <div class="exec-grid">
            <div
              v-for="chain in EXEC_CHAINS" :key="chain.id"
              class="chain-card"
              :class="[
                'chain-' + chain.id,
                { lit: demoSeq.includes(chain.id), now: demo.running && demoSeq[demo.step] === chain.id, selected: selected?.id === chain.id },
              ]"
              @click="select(chain)"
            >
              <div class="chain-head">
                <span class="chain-name">{{ chain.name }}</span>
                <el-tag size="small" effect="dark" :type="chain.tagType">{{ chain.table }}</el-tag>
              </div>
              <div class="chain-route muted">{{ chain.route }}</div>
              <div class="chain-steps">
                <template v-for="(st, j) in chain.steps" :key="st.label">
                  <span v-if="j" class="step-join" :class="'sj-' + st.join">{{ st.join === "parallel" ? "‖" : st.join === "cascade" ? "⇢" : "→" }}</span>
                  <span class="step-chip">{{ st.label }}</span>
                </template>
              </div>
              <span v-if="badgeFor(chain)" class="chain-badge">{{ badgeFor(chain) }}</span>
            </div>
          </div>
          <div class="fan-join"><span class="link-dot" />各链输出统一汇入出闸</div>
        </div>
      </div>

      <!-- 旁路 -->
      <div class="stage stage-side">
        <div class="stage-band">
          <span class="stage-idx side">⓪</span>
          <span class="stage-name">旁路</span>
          <span class="stage-sub muted">与主链并行 · 异步生效</span>
          <span class="flow-tag fk-async">⚡ 不阻塞消息处理</span>
        </div>
        <div class="stage-nodes">
          <button class="t-node side" :class="{ selected: selected?.id === SIDE_NODE.id }" @click="select(SIDE_NODE)">
            <span class="tn-head"><span class="tn-name">跨会话画像学习</span></span>
            <span class="tn-sub">首次对话异步推断客户画像, 写入会话状态供后续轮参考</span>
          </button>
        </div>
      </div>
    </div>

    <!-- 节点详情: 桌面右侧吸附 / 手机底部滑出 -->
    <el-drawer v-model="detailOpen" :position="isMobile ? 'bottom' : 'right'" :size="isMobile ? '62%' : '400px'" :with-header="false" class="node-drawer">
      <div v-if="selected" class="node-detail">
        <div class="detail-head">
          <span class="detail-name">{{ selected.name }}</span>
          <el-button size="small" link @click="detailOpen = false">关闭 ×</el-button>
        </div>
        <p class="detail-desc">{{ selected.desc }}</p>
        <template v-if="selected.actions?.length">
          <div class="detail-label">决策链动作</div>
          <div class="detail-tags">
            <el-tag v-for="a in selected.actions" :key="a" size="small" effect="plain">{{ a }}</el-tag>
          </div>
        </template>
        <template v-if="selected.defects?.length">
          <div class="detail-label">常见缺陷 (点击跳转问题治理)</div>
          <div class="detail-tags">
            <el-tag v-for="d in selected.defects" :key="d" size="small" type="warning" effect="plain" style="cursor: pointer" @click="gotoPatterns(d)">{{ DEFECT_LABELS[d] ?? d }}</el-tag>
          </div>
        </template>
        <template v-if="selected.failures">
          <div class="detail-label">失败表现</div>
          <p class="detail-text">{{ selected.failures }}</p>
        </template>
        <template v-if="selected.fix">
          <div class="detail-label">修复入口</div>
          <p class="detail-text">{{ selected.fix }}</p>
        </template>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue"
import { useRouter } from "vue-router"
import { DEFECT_LABELS } from "@/utils/defects"
import { getPipelineStats, type PipelineStats } from "@/api/console"

const router = useRouter()

interface PipelineNode {
  id: string
  name: string
  sub: string
  flows?: Array<{ kind: FlowKind; label: string }>
  kind?: "guard"
  desc: string
  actions?: string[]
  defects?: string[]
  failures?: string
  fix?: string
}

type FlowKind = "short-circuit" | "next" | "branch" | "parallel" | "cascade" | "async"
const FLOW_SYM: Record<FlowKind, string> = {
  "short-circuit": "⏹",
  next: "→",
  branch: "↳",
  parallel: "‖",
  cascade: "⇢",
  async: "⚡",
}

const PIPELINE_STAGES: Array<{
  key: string
  idx: string
  name: string
  sub: string
  cost?: string
  costType?: string
  nodes: PipelineNode[]
}> = [
  {
    key: "ingress",
    idx: "①",
    name: "入闸 — 安全与快速路径",
    sub: "零 LLM 成本, 命中即短路",
    cost: "零 LLM",
    costType: "success",
    nodes: [
      {
        id: "pending",
        name: "确认状态机",
        sub: "pending_action 拦截",
        flows: [
        { kind: "branch", label: "有窗口 → 解读确认/取消 (短路返回)" },
        { kind: "next", label: "无窗口 → 危机干预" },
        ],
        desc: "存在未过期的确认窗口 (敏感操作/L3 转人工确认) 时, 本轮消息不再走分类, 直接解读为「确认/取消」。连续无法判定则自动取消窗口、放行新消息。",
        actions: ["user_confirm"],
        failures: "客户回「是」被当成新消息重新分类 (P0 已修复: 拦截只依赖会话状态, 不依赖工具执行器)",
      },
      {
        id: "crisis",
        name: "危机干预",
        sub: "自伤/轻生 → 安抚+转人工",
        kind: "guard",
        flows: [
        { kind: "short-circuit", label: "命中 → 安抚+强制转人工" },
        { kind: "next", label: "通过 → 入站护栏" },
        ],
        desc: "客户表达自伤/轻生意图时最高优先级介入: 安抚话术 + 强制转人工, 不走任何 LLM 应答。",
        actions: ["transfer_agent"],
      },
      {
        id: "guard",
        name: "入站护栏",
        sub: "注入/越权指令拦截",
        kind: "guard",
        flows: [
        { kind: "short-circuit", label: "命中 → 拦截话术" },
        { kind: "next", label: "通过 → 问候/告别" },
        ],
        desc: "检测提示注入、诱导系统指令等攻击性输入, 命中直接返回拦截话术, 不进入理解层。",
        actions: ["injection_blocked"],
        failures: "绕过护栏诱导系统行为",
        fix: "C·规则 (护栏词表)",
      },
      {
        id: "greeting",
        name: "问候/告别",
        sub: "固定话术直出",
        flows: [
        { kind: "short-circuit", label: "命中 → 模板直出 (告别同时结束会话)" },
        { kind: "next", label: "通过 → 意图分类" },
        ],
        desc: "问候/告别不调 LLM 直接模板回复; 告别同时真正结束会话并清除残留确认窗口。",
        actions: ["turn_start"],
      },
    ],
  },
  {
    key: "understand",
    idx: "②",
    name: "理解 — 意图与实体",
    sub: "三级漏斗按成本递增, 快慢双路互验",
    nodes: [
      {
        id: "classify",
        name: "意图分类",
        sub: "L1 规则 → L2 BERT → L3 LLM",
        flows: [
        { kind: "branch", label: "L1 命中 → 高置信直连工具" },
        { kind: "next", label: "逐级兜底 → 指代消解" },
        ],
        desc: "三级漏斗: L1 规则关键词零成本, L2 本地模型 (置信封顶防假置信), L3 才动 LLM 兜底。同时产出实体、情感与候补意图, 快慢两路互验防幻觉。",
        actions: ["intent_classify"],
        defects: ["intent_misread", "intent_uncovered"],
        failures: "问账单判成闲聊 / 客户口语说法识别不出",
        fix: "B·意图库 (种子语料) 或 C·规则 (高频词)",
      },
      {
        id: "anaphora",
        name: "指代消解",
        sub: "「它/那张卡」回指历史",
        flows: [
        { kind: "next", label: "消解结果 → 噪声门 (与槽位/路由共用同一份实体)" },
        ],
        desc: "当前句回指历史实体时解析回具体所指 (closed_loop 灰度开关), 消解结果与槽位填充、路由共用同一份。",
        defects: ["intent_misread"],
      },
      {
        id: "noise",
        name: "噪声门",
        sub: "弱识别 → 澄清话术",
        kind: "guard",
        flows: [
        { kind: "short-circuit", label: "命中 → 固定澄清话术" },
        { kind: "branch", label: "连续 2 次未懂 → 标记误杀候选" },
        { kind: "next", label: "通过 → 决策一" },
        ],
        desc: "弱识别/乱码/孤词输入用固定澄清话术回应, 不让 AI 猜测作答; 连续两次没听懂标记误杀候选待人工复核。",
        actions: ["noise_blocked", "mis_kill_candidate"],
        defects: ["intent_uncovered"],
        failures: "把真实业务诉求当噪声打发 (无效澄清)",
        fix: "B·意图库",
      },
    ],
  },
  {
    key: "route",
    idx: "③",
    name: "路由 — 两级决策",
    sub: "先判交易性质, 再分咨询流量",
    nodes: [
      {
        id: "decision1",
        name: "决策一 · 交易性质",
        sub: "高风险/交易/查询/咨询",
        branch: "高风险 → 转人工",
        flows: [
        { kind: "branch", label: "高风险 ↳ 直接转人工" },
        { kind: "branch", label: "交易 ↳ 链 A" },
        { kind: "branch", label: "查询 ↳ 链 B" },
        { kind: "branch", label: "咨询 ↳ 决策二" },
        ],
        desc: "按意图的交易性质三分流: 高风险诉求 (争议/欺诈/信贷) 直接转人工; 资金变动类进链 A; 只读查询进链 B; 咨询类进决策二。",
        actions: ["route_decision"],
        defects: ["fallback_poor", "rule_flaw"],
        failures: "意图对了但该查的诉求被「无法查询」打发",
        fix: "C·规则 (流量分类映射)",
      },
      {
        id: "decision2",
        name: "决策二 · 咨询分流",
        sub: "复合/低置信/高置信",
        flows: [
        { kind: "branch", label: "复合意图 ↳ 链 C (级联)" },
        { kind: "branch", label: "低置信 ↳ 链 D (并行竞速)" },
        { kind: "branch", label: "高置信 ↳ 链 F" },
        ],
        desc: "咨询流量二次分流: 检出复合意图进链 C 级联; 置信 0.4~0.6 进链 D 并行竞速; 高置信咨询进链 F 知识问答。",
        actions: ["route_decision"],
        defects: ["rule_flaw"],
      },
    ],
  },
  {
    key: "exec",
    idx: "④",
    name: "执行 — 五条链路",
    sub: "决策分叉后各走各的最短路径",
    nodes: [],
  },
  {
    key: "egress",
    idx: "⑤",
    name: "出闸 — 合规与留痕",
    sub: "所有链路统一收口",
    nodes: [
      {
        id: "outbound",
        name: "出站合规闸门",
        sub: "幻觉/违规话术拦截",
        kind: "guard",
        flows: [
        { kind: "short-circuit", label: "命中 → 替换安全话术 (留痕)" },
        { kind: "next", label: "通过 → 转人工判定" },
        ],
        desc: "回复出站前统一审查: 索要卡号密码等敏感话术、无知识依据的编造数字、声称已办理但实际未执行、敏感词 — 命中即替换为安全话术并留痕。",
        actions: ["outbound_guard"],
        defects: ["compliance_risk", "reply_quality"],
        failures: "编造具体金额 / 违规承诺",
        fix: "D·模型 (提示词) 或 C·规则 (词表)",
      },
      {
        id: "transfer",
        name: "转人工判定",
        sub: "L1/L2/L3 触发分级",
        flows: [
        { kind: "short-circuit", label: "L1/L2 触发 → 直接转接" },
        { kind: "branch", label: "L3 触发 → 先征询客户再转" },
        { kind: "next", label: "未触发 → 结果组装" },
        ],
        desc: "负面反馈、投诉、连续澄清等信号分级触发转人工; L3 走确认链 (先征询客户) 而非直接转接。澄清两轮仍未解决也会主动提议转人工。",
        actions: ["transfer_agent"],
        defects: ["fallback_poor"],
        failures: "该转人工没转",
        fix: "C·规则 (触发配置)",
      },
      {
        id: "sink",
        name: "结果落库",
        sub: "对话/消息/决策三表",
        flows: [
        { kind: "parallel", label: "对话/消息/决策 三表同时落库" },
        { kind: "async", label: "回复推送客户" },
        ],
        desc: "回复推送客户的同时落三表: dialogue_log 对话轮次、chat_message 消息处理、decision_log 每步决策 (决策链页签的数据来源) — 供审计与质检回放。",
        actions: ["chain_complete", "topic_track"],
      },
    ],
  },
]

interface ChainStep {
  label: string
  join?: "serial" | "parallel" | "cascade" // 与前一步的连接语义
}

interface ExecChain extends PipelineNode {
  table: string
  tagType: string
  route: string
  steps: ChainStep[]
}


interface ChainStep {
  label: string
  join?: "serial" | "parallel" | "cascade"
}

interface ExecChain extends PipelineNode {
  table: string
  tagType: string
  route: string
  steps: ChainStep[]
}

const EXEC_CHAINS: ExecChain[] = [
  {
    id: "chainA",
    name: "链 A · 交易办理",
    sub: "确认状态机 + MCP 工具循环",
    route: "决策一 → 资金变动类 (还款/挂失/调额)",
    table: "C·规则",
    tagType: "warning",
    steps: [
    { label: "确认状态机" },
    { label: "MCP 工具循环", join: "serial" },
    { label: "敏感操作核验", join: "serial" },
      ],
    desc: "交易类诉求走确认状态机 + LLM 编排的 MCP 工具循环; 敏感操作强制二次确认, 高风险动作走短信核验信号。",
    actions: ["tool_call"],
    defects: ["rule_flaw", "compliance_risk"],
    failures: "办理流程走错 / 未经确认执行敏感操作",
    fix: "C·规则 (流程配置)",
  },
  {
    id: "chainB",
    name: "链 B · 查询直达",
    sub: "直连工具 + 结果缓存",
    route: "决策一 → 只读查询 (账单/明细/积分)",
    table: "C·规则",
    tagType: "primary",
    steps: [
    { label: "槽位抽取" },
    { label: "直连工具", join: "serial" },
    { label: "结果缓存", join: "serial" },
    { label: "单次摘要", join: "serial" },
      ],
    desc: "查询类诉求不走 LLM 工具循环: 槽位抽取参数 → 直连 MCP 工具 → Redis 结果缓存 → LLM 单次摘要成回复。缺参数时槽位反问补齐。",
    actions: ["tool_call", "cache_hit"],
    defects: ["fallback_poor", "reply_quality"],
    failures: "参数缺失死循环 / 摘要丢失关键数字",
    fix: "D·模型 (摘要提示词)",
  },
  {
    id: "chainC",
    name: "链 C · 复合意图级联",
    sub: "先取数再联合生成",
    route: "决策二 → 复合意图 (账单为什么这么多)",
    table: "D·模型",
    tagType: "success",
    steps: [
    { label: "链 B 取数" },
    { label: "注入 RAG 上下文", join: "cascade" },
    { label: "联合生成", join: "serial" },
      ],
    desc: "「查询 + 咨询」复合诉求: 链 B 先取业务数据, 结构化结果注入 RAG 上下文, 联合生成数据与解释融合的回复 (cascade 标记留痕)。",
    actions: ["tool_call", "rag_retrieve", "llm_generate"],
    defects: ["reply_quality"],
    failures: "只答了数据没答原因 (或反之)",
    fix: "D·模型 (联合生成提示词)",
  },
  {
    id: "chainD",
    name: "链 D · 并行竞速",
    sub: "FAQ × RAG 双路并发",
    route: "决策二 → 低置信 (0.4~0.6)",
    table: "D·模型",
    tagType: "warning",
    steps: [
    { label: "FAQ 三路" },
    { label: "RAG 检索", join: "parallel" },
    { label: "归并取优", join: "serial" },
      ],
    desc: "低置信咨询双路并发: FAQ 三路匹配与 RAG 检索各自带超时, 归并取高分 — FAQ 高分直出标准答案, 否则 RAG 生成, 双空回落澄清。两路得分均留痕。",
    actions: ["faq_retrieve", "rag_retrieve"],
    defects: ["knowledge_missing"],
    failures: "双路都空 → 澄清循环",
    fix: "A·知识库",
  },
  {
    id: "chainF",
    name: "链 F · 知识问答",
    sub: "查询工程 + 混合检索",
    route: "决策二 → 高置信咨询",
    table: "A·知识库",
    tagType: "primary",
    steps: [
    { label: "查询工程" },
    { label: "FAQ 网关", join: "serial" },
    { label: "混合检索", join: "serial" },
    { label: "重排" },
    { label: "生成 (带引用)", join: "serial" },
      ],
    desc: "知识咨询主链: 查询工程三层 (口语归一/同义扩展/多查询展开, 全规则零成本) → FAQ 检索网关 (命中即直出标准答案) → RAG 混合检索 (BM25+向量+RRF) → 重排 → LLM 生成带引用来源。",
    actions: ["faq_retrieve", "rag_retrieve", "llm_generate"],
    defects: ["knowledge_missing", "knowledge_outdated", "reply_quality"],
    failures: "检索不命中 / 引用过时内容 / 答非所问",
    fix: "A·知识库 (补文档) 或 D·模型 (生成提示词)",
  },
]


const SIDE_NODE: PipelineNode = {
  id: "profile",
  name: "跨会话画像学习",
  sub: "异步旁路",
  desc: "客户首次对话时异步从历史会话推断客户画像 (偏好/风险特征), 写入会话状态供后续轮次的路由与话术参考。不阻塞主链, 失败仅告警。",
  defects: [],
}

// 详情: 抽屉 (手机底部 / 桌面右侧)
const selected = ref<PipelineNode | null>(null)
const detailOpen = ref(false)
const isMobile = ref(typeof window !== "undefined" ? window.innerWidth < 900 : false)
if (typeof window !== "undefined") {
  const onResize = () => (isMobile.value = window.innerWidth < 900)
  window.addEventListener("resize", onResize)
  onBeforeUnmount(() => window.removeEventListener("resize", onResize))
}
function select(node: PipelineNode) {
  if (selected.value?.id === node.id) {
    detailOpen.value = false
    return
  }
  selected.value = node
  detailOpen.value = true
}
watch(detailOpen, (v) => {
  if (!v) selected.value = null
})
function gotoPatterns(defect: string) {
  router.push({ path: "/admin/patterns", query: { defect } })
}

// ── 实时流量徽标 ──
const stats = ref<PipelineStats | null>(null)
const statsHours = ref(24)
const statsLoading = ref(false)
async function loadStats() {
  statsLoading.value = true
  try {
    stats.value = await getPipelineStats(statsHours.value)
  } catch {
    stats.value = null
  } finally {
    statsLoading.value = false
  }
}
function fmtCount(n: number): string {
  return n >= 10000 ? `${(n / 10000).toFixed(1)}w` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n)
}
function badgeFor(node: PipelineNode): string {
  const key = node.actions?.[0]
  if (!key || !stats.value) return ""
  const a = stats.value.actions[key]
  if (!a) return ""
  const ms = a.avg_ms >= 1000 ? `${(a.avg_ms / 1000).toFixed(1)}s` : `${Math.round(a.avg_ms)}ms`
  return `${fmtCount(a.count)} · ${ms}`
}
onMounted(loadStats)

// ── 分叉扇形: 决策汇点 → 五链入口的贝塞尔连线 ──
function fanPath(ci: number, total: number): string {
  const w = 1000
  const x0 = w / 2
  const x1 = ((ci + 0.5) / total) * w
  return `M ${x0} 4 C ${x0} 44, ${x1} 44, ${x1} 86`
}

// ── 演示动线: 示例消息沿主线逐环节点亮 ──
const demoSeqFull = ["pending", "crisis", "guard", "classify", "anaphora", "noise", "decision1", "decision2", "chainB", "outbound", "transfer", "sink"]
const demo = ref({ running: false, step: 0, label: "", timer: 0 as unknown as ReturnType<typeof setInterval> })
const demoLabels: Record<string, string> = {
  pending: "① 无确认窗口, 通过",
  crisis: "② 非危机表述, 通过",
  guard: "③ 无注入风险, 通过",
  classify: "④ 识别为 账单查询 (查询类, 置信 88%)",
  anaphora: "⑤ 无指代, 通过",
  noise: "⑥ 识别清晰, 通过",
  decision1: "⑦ 决策一: 只读查询 → 链 B",
  decision2: "—",
  chainB: "⑧ 链 B: 直连账单工具 → 缓存 → 摘要",
  outbound: "⑨ 出站审查通过 (数字有据)",
  transfer: "⑩ 无转人工信号",
  sink: "⑪ 回复推送 + 三表落库, 完成 ✓",
}
const demoSeq = computed(() => demoSeqFull)
function toggleDemo() {
  if (demo.value.running) {
    stopDemo()
    return
  }
  demo.value.running = true
  demo.value.step = 0
  demo.value.label = demoLabels[demoSeqFull[0]] ?? ""
  demo.value.timer = setInterval(() => {
    demo.value.step += 1
    if (demo.value.step >= demoSeqFull.length) {
      demo.value.label = "完成 ✓ (2 秒后自动复位)"
      setTimeout(stopDemo, 2000)
      return
    }
    demo.value.label = demoLabels[demoSeqFull[demo.value.step]] ?? ""
    document.getElementById("node-" + demoSeqFull[demo.value.step])?.scrollIntoView({ block: "center", behavior: "smooth" })
  }, 1300)
}
function stopDemo() {
  clearInterval(demo.value.timer)
  demo.value.running = false
  demo.value.step = 0
  demo.value.label = ""
}
onBeforeUnmount(stopDemo)
</script>

<style scoped lang="scss">
.pipeline-map {
  padding: 4px 2px 24px;
}
.page-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  margin-bottom: 10px;
  h2 {
    margin: 0 0 4px;
    font-size: 17px;
  }
  .head-sub {
    font-size: 12.5px;
    margin: 0;
  }
}
.head-right {
  display: flex;
  align-items: center;
  gap: 10px;
  .stats-total {
    font-size: 11.5px;
    white-space: nowrap;
  }
}
.demo-bar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 14px;
  border-radius: 10px;
  background: linear-gradient(120deg, var(--el-color-primary-light-9), var(--el-color-success-light-9));
  margin-bottom: 10px;
  .demo-step {
    font-size: 13px;
    font-weight: 600;
    color: var(--el-color-primary);
  }
  .demo-hint {
    font-size: 11.5px;
  }
}
.legend {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
  padding: 7px 12px;
  border: 1px dashed var(--el-border-color-lighter);
  border-radius: 8px;
  margin-bottom: 14px;
  font-size: 11.5px;
  color: var(--color-text-secondary);
  .lg {
    display: inline-flex;
    align-items: center;
    gap: 5px;
  }
  .lg-sep {
    width: 1px;
    height: 12px;
    background: var(--el-border-color-lighter);
  }
  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    &.dot-danger { background: var(--el-color-danger); }
    &.dot-primary { background: var(--el-color-primary); }
    &.dot-success { background: var(--el-color-success); }
  }
  .fsym {
    font-style: normal;
    font-weight: 700;
    &.sc { color: var(--el-color-danger); }
    &.br { color: var(--el-color-primary); }
    &.pa { color: var(--el-color-success); }
    &.ca { color: var(--el-color-warning); }
    &.as { color: var(--el-color-info); }
  }
}

/* ── 主干: 阶段垂直堆叠, 内部连线 ── */
.trunk {
  max-width: 980px;
  margin: 0 auto;
}
.stage {
  position: relative;
  padding: 0 0 6px 0;
  margin-bottom: 4px;
}
.stage-band {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 14px;
  border-radius: 999px;
  background: var(--el-fill-color-light);
  margin: 0 auto 14px;
  width: fit-content;
  max-width: 100%;
  .stage-idx {
    width: 22px;
    height: 22px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
    background: var(--el-color-primary);
    color: #fff;
    font-size: 11.5px;
    font-weight: 700;
    &.side {
      background: var(--el-color-info);
    }
  }
  .stage-name {
    font-size: 13px;
    font-weight: 700;
  }
  .stage-sub {
    font-size: 11px;
  }
}
.stage-nodes {
  display: flex;
  flex-direction: column;
  align-items: center;
}
.link-seg {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 1px;
  padding: 2px 0;
  color: var(--color-text-muted);
  font-size: 10px;
  &::before,
  &::after {
    content: "";
    width: 2px;
    height: 8px;
    background: var(--el-color-primary-light-5);
  }
  .link-dot {
    width: 5px;
    height: 5px;
    border-radius: 50%;
    background: var(--el-color-primary-light-3);
  }
}
.t-node {
  position: relative;
  width: min(560px, 94%);
  display: flex;
  flex-direction: column;
  gap: 3px;
  text-align: left;
  padding: 10px 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-left: 4px solid var(--el-color-primary-light-5);
  border-radius: 10px;
  background: var(--el-bg-color, #fff);
  box-shadow: 0 1px 3px rgb(0 0 0 / 4%);
  cursor: pointer;
  transition: all 0.2s;
  &:hover {
    border-left-color: var(--el-color-primary);
    box-shadow: 0 3px 10px rgb(0 0 0 / 8%);
  }
  &.guard {
    border-left-color: var(--el-color-danger-light-3);
    .tn-name { color: var(--el-color-danger); }
    &:hover { border-left-color: var(--el-color-danger); }
  }
  &.side {
    border-style: dashed;
    box-shadow: none;
    border-left-style: dashed;
  }
  &.selected {
    border-color: var(--el-color-primary);
    border-left-color: var(--el-color-primary);
  }
  /* 演示动线: 路径点亮 / 当前环节呼吸 */
  &.lit {
    border-left-color: var(--el-color-success);
    background: var(--el-color-success-light-9);
  }
  &.now {
    border-left-color: var(--el-color-warning);
    background: var(--el-color-warning-light-9);
    animation: breathe 1.2s ease-in-out infinite;
    box-shadow: 0 0 0 4px var(--el-color-warning-light-9);
  }
  .tn-head {
    display: flex;
    align-items: center;
    gap: 8px;
  }
  .tn-name {
    font-size: 13.5px;
    font-weight: 700;
  }
  .tn-badge {
    font-size: 10px;
    line-height: 1;
    padding: 3px 7px;
    border-radius: 999px;
    background: var(--el-color-primary-light-8);
    color: var(--el-color-primary);
    white-space: nowrap;
    font-weight: 600;
    &.hot {
      background: var(--el-color-danger-light-8);
      color: var(--el-color-danger);
    }
  }
  .tn-sub {
    font-size: 11.5px;
    color: var(--color-text-secondary);
    line-height: 1.5;
  }
  .tn-flows {
    display: flex;
    flex-wrap: wrap;
    gap: 3px 10px;
    margin-top: 4px;
    padding-top: 5px;
    border-top: 1px dashed var(--el-border-color-lighter);
  }
}
.flow-tag {
  font-size: 10.5px;
  line-height: 1.5;
}
.fk-short-circuit { color: var(--el-color-danger); }
.fk-next { color: var(--color-text-muted); }
.fk-branch { color: var(--el-color-primary); }
.fk-parallel { color: var(--el-color-success); }
.fk-cascade { color: var(--el-color-warning); }
.fk-async { color: var(--el-color-info); }
@keyframes breathe {
  0%, 100% { box-shadow: 0 0 0 3px var(--el-color-warning-light-9); }
  50% { box-shadow: 0 0 0 7px var(--el-color-warning-light-9); }
}

/* ── 执行层: 分叉扇形 + 五链 ── */
.stage-exec {
  .fan-head {
    text-align: center;
    margin-bottom: 2px;
    .fan-label {
      font-size: 11px;
      color: var(--color-text-muted);
      background: var(--el-fill-color-light);
      padding: 2px 10px;
      border-radius: 999px;
    }
  }
  .fan-svg {
    width: 100%;
    height: 90px;
    display: block;
  }
  .fan-path {
    fill: none;
    stroke: var(--el-color-primary-light-5);
    stroke-width: 2;
    &.lit {
      stroke: var(--el-color-success);
      stroke-width: 3;
    }
  }
  .exec-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(176px, 1fr));
    gap: 10px;
  }
  .fan-join {
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 6px 0 0;
    color: var(--color-text-muted);
    font-size: 10.5px;
    &::before {
      content: "";
      width: 2px;
      height: 10px;
      background: var(--el-color-primary-light-5);
    }
  }
}
.chain-card {
  position: relative;
  border: 1px solid var(--el-border-color-lighter);
  border-top: 3px solid var(--el-color-info);
  border-radius: 10px;
  padding: 10px 12px;
  cursor: pointer;
  background: var(--el-bg-color, #fff);
  box-shadow: 0 1px 3px rgb(0 0 0 / 4%);
  transition: all 0.2s;
  &:hover {
    box-shadow: 0 3px 10px rgb(0 0 0 / 8%);
  }
  &.chain-A { border-top-color: var(--el-color-warning); }
  &.chain-B { border-top-color: var(--el-color-primary); }
  &.chain-C { border-top-color: var(--el-color-success); }
  &.chain-D { border-top-color: var(--el-color-danger-light-5); }
  &.chain-F { border-top-color: var(--el-color-primary-light-3); }
  &.selected {
    border-color: var(--el-color-primary);
    border-top-color: var(--el-color-primary);
  }
  &.lit {
    border-top-color: var(--el-color-success);
    background: var(--el-color-success-light-9);
  }
  &.now {
    border-top-color: var(--el-color-warning);
    background: var(--el-color-warning-light-9);
    animation: breathe 1.2s ease-in-out infinite;
  }
  .chain-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 6px;
    margin-bottom: 3px;
  }
  .chain-name {
    font-size: 12.5px;
    font-weight: 700;
  }
  .chain-route {
    font-size: 10.5px;
    line-height: 1.4;
  }
  .chain-steps {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 3px 0;
    margin-top: 6px;
  }
  .step-chip {
    font-size: 10px;
    padding: 1px 6px;
    border: 1px solid var(--el-border-color-lighter);
    border-radius: 4px;
    color: var(--color-text-secondary);
    background: var(--el-fill-color-blank);
    white-space: nowrap;
  }
  .step-join {
    font-size: 11px;
    font-weight: 700;
    margin: 0 3px;
    &.sj-parallel { color: var(--el-color-success); }
    &.sj-cascade { color: var(--el-color-warning); }
    &.sj-serial { color: var(--el-border-color); }
  }
  .chain-badge {
    position: absolute;
    top: -9px;
    right: 8px;
    font-size: 10px;
    font-weight: 600;
    line-height: 1;
    padding: 3px 7px;
    border-radius: 999px;
    background: var(--el-color-primary-light-8);
    color: var(--el-color-primary);
  }
}
.stage-side .stage-nodes {
  .t-node {
    width: min(460px, 94%);
  }
}
</style>

<style lang="scss">
/* 抽屉挂 body, 全局样式 */
.node-drawer {
  .el-drawer__body {
    padding: 14px 16px;
  }
  .node-detail {
    .detail-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      .detail-name {
        font-size: 15px;
        font-weight: 700;
      }
    }
    .detail-desc {
      font-size: 12.5px;
      line-height: 1.7;
      margin: 10px 0;
    }
    .detail-label {
      font-size: 11px;
      font-weight: 600;
      color: var(--color-text-secondary);
      margin: 12px 0 5px;
    }
    .detail-tags {
      display: flex;
      flex-wrap: wrap;
      gap: 5px;
    }
    .detail-text {
      font-size: 12px;
      line-height: 1.6;
      margin: 0;
    }
  }
}
</style>
