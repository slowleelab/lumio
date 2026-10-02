<template>
  <div class="pipeline-map">
    <div class="page-head">
      <div>
        <h2>处理链路</h2>
        <p class="muted head-sub">客户消息从接收到结果输出的完整链路 — 点击节点查看环节职责、决策链对应动作与常见缺陷映射</p>
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

    <!-- 图例 -->
    <div class="legend">
      <span class="lg"><i class="dot dot-danger" />拦截/风险</span>
      <span class="lg"><i class="dot dot-primary" />理解/路由</span>
      <span class="lg"><i class="dot dot-success" />执行链路</span>
      <span class="lg"><i class="dot dot-info" />留痕</span>
      <span class="lg-sep" />
      <span class="lg"><i class="fsym sc">⏹</i>命中短路 (拦截后直接返回)</span>
      <span class="lg"><i class="fsym nx">→</i>串行 (依次传递)</span>
      <span class="lg"><i class="fsym br">↳</i>条件分支</span>
      <span class="lg"><i class="fsym pa">‖</i>并行 (同时执行)</span>
      <span class="lg"><i class="fsym ca">⇢</i>级联 (上游结果喂下游)</span>
      <span class="lg"><i class="fsym as">⚡</i>异步 (不等待)</span>
      <span class="lg muted">右上角 = 真实流量 (近 {{ stats?.hours ?? 24 }}h)</span>
    </div>

    <div class="map-body">
      <!-- 链路主体: 五层泳道垂直流 -->
      <div class="lanes">
        <div v-for="lane in PIPELINE_STAGES" :key="lane.key" class="lane" :class="'lane-' + lane.key">
          <div class="lane-head">
            <span class="lane-idx">{{ lane.idx }}</span>
            <div>
              <div class="lane-name">{{ lane.name }}</div>
              <div class="lane-sub muted">{{ lane.sub }}</div>
            </div>
            <el-tag v-if="lane.cost" size="small" effect="plain" :type="lane.costType || 'info'">{{ lane.cost }}</el-tag>
          </div>
          <div class="lane-nodes" :class="{ chains: lane.key === 'exec' }">
            <template v-if="lane.key !== 'exec'">
              <template v-for="(node, i) in lane.nodes" :key="node.id">
                <div v-if="i" class="arrow" title="串行: 上一环通过后进入本环">›</div>
                <button class="node" :class="{ active: selected?.id === node.id, guard: node.kind === 'guard' }" @click="select(node)">
                  <span class="node-name">{{ node.name }}</span>
                  <span class="node-sub">{{ node.sub }}</span>
                  <!-- 出边流转: 命中短路 / 通过下一环 / 条件分支 -->
                  <span v-if="node.flows?.length" class="node-flows">
                    <span v-for="f in node.flows" :key="f.label" class="flow-tag" :class="'fk-' + f.kind">{{ FLOW_SYM[f.kind] }} {{ f.label }}</span>
                  </span>
                  <span v-if="badgeFor(node)" class="node-badge" :class="{ hot: node.kind === 'guard' }">{{ badgeFor(node) }}</span>
                </button>
              </template>
            </template>
            <template v-else>
              <!-- 执行链层: 决策分叉后的五条互斥分支 (链内结构各异) -->
              <div v-for="chain in EXEC_CHAINS" :key="chain.id" class="chain-card" :class="'chain-' + chain.id" @click="select(chain)">
                <div class="chain-head">
                  <span class="chain-name">{{ chain.name }}</span>
                  <el-tag size="small" effect="dark" :type="chain.tagType">{{ chain.table }}</el-tag>
                </div>
                <div class="chain-sub">{{ chain.sub }}</div>
                <!-- 链内 mini 步骤流: 串行 → / 并行 ‖ / 级联 ⇢ -->
                <div class="chain-steps">
                  <template v-for="(st, j) in chain.steps" :key="st.label">
                    <span v-if="j" class="step-join" :class="'sj-' + st.join">{{ st.join === "parallel" ? "‖" : st.join === "cascade" ? "⇢" : "→" }}</span>
                    <span class="step-chip" :class="{ group: st.group }">{{ st.label }}</span>
                  </template>
                </div>
                <div class="chain-route muted">{{ chain.route }}</div>
                <div v-if="badgeFor(chain)" class="chain-badge">{{ badgeFor(chain) }}</div>
              </div>
            </template>
          </div>
        </div>
        <!-- 旁路 -->
        <div class="lane lane-side">
          <div class="lane-head">
            <span class="lane-idx">⓪</span>
            <div>
              <div class="lane-name">旁路 (不阻塞主链)</div>
              <div class="lane-sub muted">与主链并行执行, 结果异步生效</div>
            </div>
          </div>
          <div class="lane-nodes">
            <button class="node side" @click="select(SIDE_NODE)">
              <span class="node-name">跨会话画像学习</span>
              <span class="node-sub">首次对话从历史推断客户画像</span>
            </button>
          </div>
        </div>
      </div>

      <!-- 节点详情: 右侧固定栏 -->
      <aside class="node-detail" v-if="selected">
        <div class="detail-head">
          <span class="detail-name">{{ selected.name }}</span>
          <el-button size="small" link @click="selected = null">关闭 ×</el-button>
        </div>
        <p class="detail-desc">{{ selected.desc }}</p>
        <template v-if="selected.actions?.length">
          <div class="detail-label">决策链动作</div>
          <div class="detail-tags">
            <el-tag v-for="a in selected.actions" :key="a" size="small" effect="plain">{{ a }}</el-tag>
          </div>
        </template>
        <template v-if="selected.defects?.length">
          <div class="detail-label">常见缺陷 (点击可跳转问题治理)</div>
          <div class="detail-tags">
            <el-tag
              v-for="d in selected.defects" :key="d" size="small" type="warning" effect="plain"
              style="cursor: pointer" @click="gotoPatterns(d)"
            >{{ DEFECT_LABELS[d] ?? d }}</el-tag>
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
      </aside>
    </div>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from "vue"
import { useRouter } from "vue-router"
import { DEFECT_LABELS } from "@/utils/defects"
import { getPipelineStats, type PipelineStats } from "@/api/console"

const router = useRouter()

interface PipelineNode {
  id: string
  name: string
  sub: string
  branch?: string
  kind?: "guard" // 拦截/风险类节点 (红色视觉 + 徽标高亮)
  // 出边流转语义: 该环节结果去哪、怎么去 (短路/串行/分支/并行/级联/异步)
  flows?: Array<{ kind: FlowKind; label: string }>
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

// ── 链路权威数据 (与 bot_agent.run() 同步; 改链路先改代码再同步此处) ──
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

const selected = ref<PipelineNode | null>(null)
function select(node: PipelineNode) {
  selected.value = selected.value?.id === node.id ? null : node
}
function gotoPatterns(defect: string) {
  router.push({ path: "/admin/patterns", query: { defect } })
}

// ── 实时流量: decision_log 按 action 聚合 (节点徽标 = 该环节首选动作的真实量/耗时) ──
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
</script>

<style scoped lang="scss">
.pipeline-map {
  padding: 4px 2px;
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
.legend {
  display: flex;
  align-items: center;
  gap: 16px;
  flex-wrap: wrap;
  padding: 8px 12px;
  border: 1px dashed var(--el-border-color-lighter);
  border-radius: 8px;
  margin-bottom: 12px;
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
  .fsym {
    font-style: normal;
    font-weight: 700;
    &.sc { color: var(--el-color-danger); }
    &.nx { color: var(--color-text-muted); }
    &.br { color: var(--el-color-primary); }
    &.pa { color: var(--el-color-success); }
    &.ca { color: var(--el-color-warning); }
    &.as { color: var(--el-color-info); }
  }
  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    &.dot-danger { background: var(--el-color-danger); }
    &.dot-primary { background: var(--el-color-primary); }
    &.dot-success { background: var(--el-color-success); }
    &.dot-info { background: var(--el-color-info); }
  }
}
.map-body {
  display: grid;
  grid-template-columns: 1fr 300px;
  gap: 14px;
  align-items: start;
}
@media (max-width: 1100px) {
  .map-body {
    grid-template-columns: 1fr;
  }
}

/* 泳道 */
.lanes {
  display: flex;
  flex-direction: column;
  gap: 0;
}
.lane {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px;
  padding: 10px 12px;
  background: var(--el-bg-color, #fff);
  &.lane-side {
    border-style: dashed;
    background: transparent;
    margin-top: 14px;
  }
  /* 层间流向: 每条主泳道底部中心向下箭头 (出闸是终点, 不再流出) */
  &:not(.lane-side) {
    margin-bottom: 18px;
    position: relative;
    &::after {
      content: "▼";
      position: absolute;
      left: 50%;
      bottom: -16px;
      transform: translateX(-50%);
      color: var(--el-color-primary-light-5);
      font-size: 11px;
      line-height: 1;
    }
    &.lane-egress::after {
      display: none;
    }
  }
}
.lane-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}
.lane-idx {
  flex-shrink: 0;
  width: 24px;
  height: 24px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 50%;
  background: var(--el-color-primary-light-8);
  color: var(--el-color-primary);
  font-weight: 700;
  font-size: 12px;
}
.lane-side .lane-idx {
  background: var(--el-fill-color);
  color: var(--color-text-muted);
}
.lane-name {
  font-size: 13px;
  font-weight: 600;
}
.lane-sub {
  font-size: 11.5px;
}
.lane-head .el-tag {
  margin-left: auto;
}
.lane-nodes {
  display: flex;
  align-items: stretch;
  gap: 6px;
  flex-wrap: wrap;
  &.chains {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(168px, 1fr));
    gap: 8px;
  }
}
.node {
  flex: 1 1 120px;
  min-width: 118px;
  position: relative;
  display: flex;
  flex-direction: column;
  gap: 2px;
  text-align: left;
  padding: 8px 10px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  background: var(--el-fill-color-blank);
  cursor: pointer;
  transition: all 0.15s;
  &:hover {
    border-color: var(--el-color-primary-light-5);
  }
  &.active {
    border-color: var(--el-color-primary);
    background: var(--el-color-primary-light-9);
  }
  &.side {
    background: transparent;
    border-style: dashed;
  }
  /* 拦截/风险类节点: 红色系视觉 */
  &.guard {
    border-color: var(--el-color-danger-light-5);
    .node-name {
      color: var(--el-color-danger);
    }
    &.active,
    &:hover {
      border-color: var(--el-color-danger);
      background: var(--el-color-danger-light-9);
    }
  }
  .node-name {
    font-size: 12.5px;
    font-weight: 600;
    color: var(--color-text-primary);
  }
  .node-sub {
    font-size: 11px;
    color: var(--color-text-secondary);
    line-height: 1.4;
  }
  /* 出边流转标签: 命中短路红 / 通过灰 / 分支蓝 / 并行绿 / 级联橙 */
  .node-flows {
    display: flex;
    flex-direction: column;
    gap: 1px;
    margin-top: 4px;
    padding-top: 4px;
    border-top: 1px dashed var(--el-border-color-lighter);
  }
  .flow-tag {
    font-size: 10px;
    line-height: 1.5;
    white-space: normal;
  }
  .fk-short-circuit { color: var(--el-color-danger); }
  .fk-next { color: var(--color-text-muted); }
  .fk-branch { color: var(--el-color-primary); }
  .fk-parallel { color: var(--el-color-success); }
  .fk-cascade { color: var(--el-color-warning); }
  .fk-async { color: var(--el-color-info); }
  .node-badge {
    position: absolute;
    top: -8px;
    right: 6px;
    font-size: 10px;
    line-height: 1;
    padding: 3px 6px;
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
}
.arrow {
  align-self: center;
  color: var(--el-border-color);
  font-size: 13px;
}
.branch-mark {
  align-self: center;
  font-size: 10.5px;
  color: var(--el-color-danger);
  background: var(--el-color-danger-light-9);
  padding: 1px 6px;
  border-radius: 999px;
  white-space: nowrap;
}

/* 执行链卡 */
.chain-card {
  border: 1px solid var(--el-border-color-lighter);
  border-left: 3px solid var(--el-color-info);
  border-radius: 8px;
  padding: 8px 10px;
  cursor: pointer;
  transition: all 0.15s;
  background: var(--el-fill-color-blank);
  &:hover {
    border-color: var(--el-color-primary-light-5);
  }
  &.chain-A {
    border-left-color: var(--el-color-warning);
  }
  &.chain-B {
    border-left-color: var(--el-color-primary);
  }
  &.chain-C {
    border-left-color: var(--el-color-success);
  }
  &.chain-D {
    border-left-color: var(--el-color-danger-light-5);
  }
  &.chain-F {
    border-left-color: var(--el-color-primary-light-3);
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
  .chain-sub {
    font-size: 11px;
    color: var(--color-text-secondary);
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
  .chain-route {
    font-size: 10.5px;
    margin-top: 4px;
    line-height: 1.4;
  }
  .chain-badge {
    margin-top: 6px;
    font-size: 10.5px;
    font-weight: 600;
    color: var(--el-color-primary);
    background: var(--el-color-primary-light-9);
    border-radius: 4px;
    padding: 2px 6px;
    display: inline-block;
    width: fit-content;
  }
}

/* 详情栏 */
.node-detail {
  position: sticky;
  top: 8px;
  border: 1px solid var(--el-color-primary-light-7);
  border-radius: 10px;
  padding: 12px 14px;
  background: var(--el-color-primary-light-9, #f0f7ff);
  .detail-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    .detail-name {
      font-size: 14px;
      font-weight: 700;
    }
  }
  .detail-desc {
    font-size: 12.5px;
    line-height: 1.7;
    margin: 8px 0;
  }
  .detail-label {
    font-size: 11px;
    font-weight: 600;
    color: var(--color-text-secondary);
    margin: 10px 0 5px;
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
    white-space: pre-wrap;
  }
}
</style>
