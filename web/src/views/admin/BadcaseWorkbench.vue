<template>
  <div class="badcase-page">
    <div class="page-header">
      <h2>智能质检 <span class="page-subtitle">全量会话质检记录 · 问题案例归因整改闭环</span></h2>
      <div class="header-actions">
        <el-tooltip placement="left" effect="light">
          <template #content>
            <div class="judge-tip">
              <b>GLM-5.3-Flash 裁判 · 批量归因</b><br />
              对全部「未归因」坏例逐条跑 LLM 裁判 (n=3 多数票),<br />
              每条约 20-40 秒后台执行, 完成后自动刷新。<br />
              采集落库后不会自动归因 —— 由你在此触发。
            </div>
          </template>
          <el-button size="small" type="primary" plain :loading="batch.running" @click="doBatchAttribution">
            {{ batch.running ? `GLM 裁判中 ${batch.done}/${batch.total}` : "GLM 裁判 · 批量归因待归因项" }}
          </el-button>
        </el-tooltip>
        <el-tooltip placement="left" effect="light">
          <template #content>
            <div class="judge-tip">
              <b>全量质检巡检</b><br />
              所有会话从<b>原始对话内容</b>过 GLM 裁判质检,<br />
              不依赖置信度/差评信号 — 高置信但答非所问也逃不掉。<br />
              fail 自动采集进待复核队列; 合格率见按钮下方。
            </div>
          </template>
          <el-button size="small" type="success" plain :loading="scan.running" @click="doQualityScan">
            {{ scan.running ? `全量质检中 ${scan.done}/${scan.total}` : "全量质检 · 扫描全部会话" }}
          </el-button>
        </el-tooltip>
      </div>
    </div>

    <!-- 全量质检进度条 -->
    <el-progress
      v-if="scan.running"
      :percentage="scanPct"
      :stroke-width="10"
      striped
      striped-flow
      status="success"
      style="margin-top: 10px"
    >
      <template #default>
        <span class="batch-progress-text">
          全量质检中 {{ scan.done }}/{{ scan.total }}
          · 合格 {{ scan.n_pass }} / 提醒 {{ scan.n_warn }} / 不合格 {{ scan.n_fail }}
          <template v-if="scan.n_error"> (失败 {{ scan.n_error }})</template>
        </span>
      </template>
    </el-progress>
    <div v-else-if="scan.lastRun" class="scan-summary muted">
      上轮全量质检 {{ scan.lastRun.total }} 个会话 · 合格率 {{ ((scan.lastRun.pass_rate ?? 0) * 100).toFixed(1) }}%
      · 不合格 {{ scan.lastRun.n_fail }} 已采入待复核 ({{ scan.lastRun.finished_at?.slice(5, 16).replace("T", " ") }})
    </div>

    <!-- 批量归因进度条 -->
    <el-progress
      v-if="batch.running"
      :percentage="batchPct"
      :stroke-width="10"
      striped
      striped-flow
      style="margin-top: 10px"
    >
      <template #default>
        <span class="batch-progress-text">
          GLM 裁判批量归因中 {{ batch.done }}/{{ batch.total }}
          <template v-if="batch.failed"> (失败 {{ batch.failed }})</template>
          <template v-if="batch.scope?.signal_source || batch.scope?.keyword"> · 范围: {{ batchScopeText }}</template>
        </span>
      </template>
    </el-progress>

        <div v-if="coverage" class="coverage-line">
      近 30 天应检会话 <b>{{ coverage.total_sessions }}</b> · 已质检 <b class="ok">{{ coverage.scanned_sessions }}</b>
      · 覆盖率 <b>{{ fmtPct(coverage.coverage) }}</b> · 合格率 <b>{{ fmtPct(coverage.pass_rate) }}</b>
      <span class="muted"> (不合格 {{ coverage.by_verdict.fail ?? 0 }} / 提醒 {{ coverage.by_verdict.warn ?? 0 }})</span>
    </div>

    <div class="stat-cards">
      <div class="stat-card" :class="{ active: qcFilters.category === 'pending_review' }" @click="setCategory('pending_review')">
        <span class="label">待复核</span>
        <span class="num warn">{{ stats?.pending_review ?? "-" }}</span>
        <span class="hint">点击查看待人工判定会话</span>
      </div>
      <div class="stat-card" :class="{ active: qcFilters.category === 'fail' }" @click="setCategory('fail')">
        <span class="label">判定有问题</span>
        <span class="num danger">{{ coverage?.by_verdict?.fail ?? "-" }}</span>
        <span class="hint">质检不合格会话</span>
      </div>
      <div class="stat-card" :class="{ active: qcFilters.category === 'pass' }" @click="setCategory('pass')">
        <span class="label">正常</span>
        <span class="num ok">{{ coverage?.by_verdict?.pass ?? "-" }}</span>
        <span class="hint">质检合格会话</span>
      </div>
      <div class="stat-card">
        <span class="label">今日新增案例</span>
        <span class="num">{{ stats?.today_new ?? "-" }}</span>
        <span class="hint">近 24 小时采集</span>
      </div>
      <div class="stat-card">
        <span class="label">已全量</span>
        <span class="num">{{ stats?.deployed ?? "-" }}</span>
        <span class="hint">修复完成上线</span>
      </div>
      <div class="stat-card">
        <span class="label">LLM 直通率</span>
        <span class="num">{{ fmtPct(stats?.llm_pass_rate ?? null) }}</span>
        <span class="hint">免人工确认占比</span>
      </div>
      <!-- 根因分布条 -->
      <div class="dist-card">
        <span class="label">根因层分布</span>
        <div class="dist-bars">
          <div v-for="d in layerDist" :key="d.key" class="dist-row" :title="`${d.label}: ${d.count}`">
            <span class="dist-label">{{ d.label }}</span>
            <div class="dist-track"><div class="dist-fill" :style="{ width: distWidth(d.count) }"></div></div>
            <span class="dist-count">{{ d.count }}</span>
          </div>
          <div v-if="!layerDist.length" class="muted dist-empty">暂无归因数据 — 先跑批量归因</div>
        </div>
      </div>
    </div>

    <div class="filters">
      <el-select v-model="qcFilters.category" placeholder="分类" clearable size="small" style="width: 130px" @change="reloadQc">
        <el-option label="正常" value="pass" />
        <el-option label="提醒" value="warn" />
        <el-option label="判定有问题" value="fail" />
        <el-option label="待人工判定" value="pending_review" />
        <el-option label="未质检" value="unscanned" />
      </el-select>
      <el-input
        v-model="qcFilters.keyword"
        placeholder="搜索会话 ID / 用户输入…"
        clearable
        size="small"
        style="width: 230px"
        :prefix-icon="Search"
        @keyup.enter="reloadQc"
        @clear="reloadQc"
      />
      <el-button size="small" @click="reloadQc">查询</el-button>
      <el-button v-if="qcFilters.category || qcFilters.keyword" size="small" link @click="clearQcFilters">清除筛选</el-button>
      <div class="filter-spacer"></div>
      <template v-if="selected.length">
        <el-button size="small" type="success" plain @click="batchConfirm">批量确认 ({{ selected.length }})</el-button>
        <el-button size="small" type="warning" plain @click="batchTransition('fixing')">批量转修复中</el-button>
      </template>
    </div>

    <el-table
      :data="qcRows"
      v-loading="qcLoading"
      stripe
      size="small"
      style="margin-top: 12px"
      @selection-change="onSelection"
      @row-click="openQcDetail"
    >
      <el-table-column type="selection" width="38" :selectable="(row: QcSessionRow) => !!row.badcase_id" />
      <el-table-column label="会话时间" width="150">
        <template #default="{ row }">
          <span :title="sessionTimeTitle(row)">
            {{ fmtTime(row.session_time || row.scanned_at || row.collected_at) }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="会话 ID" width="150" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="session-id">{{ row.session_id }}</span>
        </template>
      </el-table-column>
      <el-table-column label="轮数" width="54" align="center">
        <template #default="{ row }">{{ row.turns ?? "-" }}</template>
      </el-table-column>
      <el-table-column label="判定" width="110">
        <template #default="{ row }">
          <el-tag size="small" :type="verdictType(row.verdict || '')">{{ verdictLabel(row.verdict || "") || "未质检" }}</el-tag>
          <el-tag v-if="row.category === 'pending_review'" size="small" type="warning" style="margin-left: 2px">待人工</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="质检问题" min-width="160">
        <template #default="{ row }">
          <template v-if="row.problems?.length">
            <el-tooltip
              v-for="(p, i) in row.problems"
              :key="i"
              :content="`${p.turn ? `第 ${p.turn} 轮 · ` : ''}${p.reason || ''}`"
              placement="top"
            >
              <el-tag size="small" type="danger" effect="plain" class="problem-tag">{{ problemLabel(p.type) }}</el-tag>
            </el-tooltip>
          </template>
          <span v-else class="muted">-</span>
        </template>
      </el-table-column>
      <el-table-column label="信号" width="92">
        <template #default="{ row }">
          <el-tag v-if="row.signal_source" size="small" :type="signalType(row.signal_source)">{{ signalLabel(row.signal_source) }}</el-tag>
          <span v-else class="muted">-</span>
        </template>
      </el-table-column>
      <el-table-column label="根因层" width="112">
        <template #default="{ row }">
          <el-tag v-if="row.human_confirmed_layer" size="small" type="success">{{ layerLabel(row.human_confirmed_layer) }}</el-tag>
          <el-tag v-else-if="row.root_cause_layer" size="small" :type="row.root_cause_layer === 'uncertain' ? 'warning' : 'primary'">
            {{ layerLabel(row.root_cause_layer) }}
          </el-tag>
          <span v-else-if="row.badcase_id" class="muted">未归因</span>
          <span v-else class="muted">-</span>
        </template>
      </el-table-column>
      <el-table-column label="修复状态" width="82">
        <template #default="{ row }">
          <el-tag v-if="row.fix_status" size="small" :type="fixStatusType(row.fix_status)">{{ fixStatusLabel(row.fix_status) }}</el-tag>
          <span v-else class="muted">-</span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="130" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" size="small" @click.stop="openQcDetail(row)">详情</el-button>
          <el-button link size="small" @click.stop="gotoAuditSession(row.session_id)">回放</el-button>
          <el-button v-if="row.badcase_id" link type="warning" size="small" @click.stop="openBadcaseById(row.badcase_id)">整改</el-button>
        </template>
      </el-table-column>
      <template #empty>
        <el-empty description="暂无会话 — 跑一轮全量质检或对话模拟; 新会话结束会自动质检" :image-size="64" />
      </template>
    </el-table>

    <el-pagination
      v-model:current-page="qcPage"
      v-model:page-size="qcPageSize"
      :total="qcTotal"
      :page-sizes="[20, 50, 100]"
      layout="total, sizes, prev, pager, next"
      style="margin-top: 14px; justify-content: flex-end"
      @current-change="loadQc"
      @size-change="reloadQc"
    />

    <!-- 详情抽屉 -->
    <el-drawer v-model="detailVisible" size="58%" destroy-on-close>
      <template #header>
        <div class="drawer-title">
          <el-tag :type="fixStatusType(detail?.fix_status || '')" effect="dark">{{ fixStatusLabel(detail?.fix_status || "") }}</el-tag>
          <el-tag size="small" :type="signalType(detail?.signal_source || '')">{{ signalLabel(detail?.signal_source || "") }}</el-tag>
          <span class="muted" style="font-size: 12px">出现 {{ detail?.occurrences ?? 1 }} 次</span>
          <el-button size="small" link :disabled="!nextIdx" @click="openNext">下一条 ›</el-button>
        </div>
      </template>
      <div v-if="detail" class="detail-body">
        <!-- 整改进度步骤条: 处置状态机一眼可见 -->
        <el-steps :active="fixStepActive" align-center size="small" finish-status="success" class="fix-steps">
          <el-step title="归因" :description="detail.root_cause_layer ? layerLabel(detail.root_cause_layer) : '待裁判'" />
          <el-step title="人工确认" :description="detail.needs_human_review ? '待复核' : detail.root_cause_layer ? '已确认' : '-'" />
          <el-step title="修复" :description="{ fixing: '修复中', canary: '已灰度', deployed: '已全量', rejected: '已驳回' }[detail.fix_status] || '-'" />
          <el-step title="验证" :description="detail.fix_status === 'deployed' ? '可复检' : '复检待上线'" />
        </el-steps>
        <el-alert v-if="detail.fix_status === 'rejected'" type="info" :closable="false" class="reject-alert" :title="`已驳回 — ${detail.fix_note || ''}`" />
        <el-descriptions :column="3" border size="small">
          <el-descriptions-item label="信号源">{{ signalLabel(detail.signal_source) }}</el-descriptions-item>
          <el-descriptions-item label="出现次数">{{ detail.occurrences ?? 1 }} 次</el-descriptions-item>
          <el-descriptions-item label="采集时间">{{ fmtTime(detail.created_at) }}</el-descriptions-item>
          <el-descriptions-item label="会话时间">
            <span :title="`会话最后一轮对话时间; 采集时间是信号落库/巡检时刻`">{{ fmtTime(detail.session_time) }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="会话">
            <el-link type="primary" :underline="false" @click="gotoAudit(detail)">{{ detail.session_id?.slice(0, 24) }}…</el-link>
          </el-descriptions-item>
          <el-descriptions-item label="裁判模型">{{ detail.attribution_model || "-" }}</el-descriptions-item>
          <el-descriptions-item label="修复状态">
            <el-tag size="small" :type="fixStatusType(detail.fix_status)">{{ fixStatusLabel(detail.fix_status) }}</el-tag>
          </el-descriptions-item>
        </el-descriptions>

        <div class="section-title">对话现场</div>
        <div v-if="contextLoading" class="muted context-loading">加载会话上下文…</div>
        <template v-else>
          <div v-for="(m, i) in contextMessages" :key="i" class="ctx-row" :class="m.speaker === 'customer' ? 'ctx-user' : 'ctx-bot'">
            <span class="ctx-speaker">{{ m.speaker === "customer" ? "客户" : "Bot" }}</span>
            <span class="ctx-content">{{ m.content }}</span>
          </div>
          <div v-if="!contextMessages.length" class="muted">会话历史已过期 (仅存现场轮)</div>
        </template>
        <div class="ctx-row ctx-user highlight">
          <span class="ctx-speaker">客户</span>
          <span class="ctx-content">{{ detail.user_input }}</span>
        </div>
        <div class="ctx-row ctx-bot highlight">
          <span class="ctx-speaker">Bot</span>
          <span class="ctx-content">{{ detail.bot_output || "-" }}</span>
        </div>

        <template v-if="detail.root_cause_layer">
          <div class="section-title">根因归因 <span class="muted section-hint">(裁判结论 · 可人工改判后确认)</span></div>
          <div class="attrib-row">
            <el-select v-model="judgedLayer" size="small" style="width: 150px">
              <el-option v-for="(label, key) in LAYER_LABELS" :key="key" :label="label" :value="key" />
            </el-select>
            <el-select v-model="judgedTable" size="small" style="width: 170px" placeholder="修复分流表">
              <el-option v-for="(label, key) in FIX_TABLE_LABELS" :key="key" :label="label" :value="key">
                <el-tooltip :content="FIX_TABLE_TIPS[key] || ''" placement="right" :disabled="!FIX_TABLE_TIPS[key]">
                  <span>{{ label }}</span>
                </el-tooltip>
              </el-option>
            </el-select>
            <span class="muted attrib-meta">
              {{ categoryLabel(detail.root_cause_category) }} · 置信 {{ Math.round((detail.attribution_confidence ?? 0) * 100) }}%
            </span>
          </div>
          <div class="evidence">{{ detail.attribution_evidence }}</div>
          <!-- 修复指引: 分流表 → 去哪里改什么 -->
          <el-alert v-if="fixGuide" type="success" :closable="false" class="fix-guide">
            <template #title>
              修复指引 · {{ FIX_TABLE_LABELS[judgedTable || detail.fix_table || ""] || "分流表" }}:
              {{ fixGuide.text }}
              <el-link v-if="fixGuide.to" type="primary" :underline="false" style="margin-left: 6px" @click="router.push(fixGuide.to!)">前往处理 ›</el-link>
            </template>
          </el-alert>
        </template>
        <div v-else class="section-title muted">尚未归因 — 点击下方「GLM 裁判归因」开始 (约 20-40 秒)</div>

        <!-- 质检判定 (qa_scan 采集的案例: 裁判指出的具体问题项) -->
        <template v-if="qaVerdict">
          <div class="section-title">
            质检判定
            <span class="muted section-hint">(全量质检巡检 · 与人工坐席质检同口径)</span>
          </div>
          <div class="qa-verdict">
            <el-tag :type="verdictType(qaVerdict)" size="small">{{ verdictLabel(qaVerdict) }}</el-tag>
            <span v-if="qaSummary" class="qa-summary">{{ qaSummary }}</span>
          </div>
          <div v-for="(p, i) in qaProblems" :key="i" class="qa-problem">
            <el-tag size="small" type="danger" effect="plain">{{ problemLabel(p.type) }}</el-tag>
            <span class="qa-reason"><template v-if="p.turn">第 {{ p.turn }} 轮 · </template>{{ p.reason || "-" }}</span>
          </div>
        </template>

        <div class="section-title">
          采集现场快照
          <span class="muted section-hint">(按问答链路分层展示; 原始数据可折叠查看)</span>
        </div>
        <el-descriptions v-if="snapRows.length" :column="2" border size="small">
          <el-descriptions-item v-for="r in snapRows" :key="r.label" :label="r.label">{{ r.value }}</el-descriptions-item>
        </el-descriptions>
        <div v-if="snapTurnsMeta.length" class="turns-meta">
          <div class="turns-meta-title">逐轮元数据 (对话走向: 每轮意图与回复来源)</div>
          <div v-for="(t, i) in snapTurnsMeta" :key="i" class="turn-meta-row">
            <span class="turn-idx">{{ i + 1 }}</span>
            <span class="turn-speaker" :class="{ 'is-customer': t.speaker === 'customer' }">{{ t.speaker === "customer" ? "客户" : "Bot" }}</span>
            <span class="turn-intent">意图: {{ t.intent || "-" }}</span>
            <span class="turn-src">来源: {{ t.src || "-" }}</span>
          </div>
        </div>
        <pre v-if="snapTranscript" class="snapshot transcript">{{ snapTranscript }}</pre>
        <el-collapse v-if="detail.snapshot && Object.keys(detail.snapshot).length" class="raw-snap">
          <el-collapse-item name="raw">
            <template #title><span class="muted">查看原始快照数据 (调试用)</span></template>
            <pre class="snapshot">{{ snapshotPretty }}</pre>
          </el-collapse-item>
        </el-collapse>
        <div v-if="!detail.snapshot || !Object.keys(detail.snapshot).length" class="muted">(采集时未携带快照)</div>

        <div class="section-title">处理操作 <span class="muted section-hint">(流转需填备注, 供事后追溯)</span></div>
        <div class="action-grid">
          <!-- 主推进: 按当前状态只亮下一步 -->
          <el-button v-if="!detail.root_cause_layer" size="small" type="warning" :loading="acting" @click="runAttribution(detail)">① GLM 裁判归因</el-button>
          <el-button v-if="detail.needs_human_review" size="small" type="success" :loading="acting" @click="confirmResolve">
            ② 确认归因并进入修复
          </el-button>
          <el-button v-if="detail.fix_status === 'fixing'" size="small" type="warning" :loading="acting" @click="transition('canary')">③ 修复完成 · 转灰度</el-button>
          <el-button v-if="detail.fix_status === 'canary'" size="small" type="success" :loading="acting" @click="transition('deployed')">④ 灰度验证通过 · 全量上线</el-button>
          <!-- 验证闭环: 上线后复检原会话确认修复生效 -->
          <el-button
            v-if="detail.fix_status === 'canary' || detail.fix_status === 'deployed'"
            size="small" type="primary" plain :loading="rescanningBadcase" @click="rescanFromBadcase"
          >{{ detail.fix_status === 'deployed' ? "⑤" : "灰度" }}复检原会话</el-button>
          <!-- 次要操作 -->
          <el-button size="small" @click="addToGolden(detail)">扩充金标集</el-button>
          <el-button size="small" @click="gotoAudit(detail)">会话审计</el-button>
          <el-button size="small" type="danger" plain :loading="acting" @click="rejectCase">驳回此例</el-button>
        </div>

        <template v-if="detail.fix_note">
          <div class="section-title">最近处理记录</div>
          <div class="fix-note">{{ detail.fix_note }} <span class="muted" v-if="detail.resolved_at">· {{ fmtTime(detail.resolved_at) }}</span></div>
        </template>
      </div>
    </el-drawer>

    <!-- ══ 质检详情抽屉 (问题导向: 判定 → 问题 → 现场还原 → 链路 → 处置) ══ -->
    <el-drawer v-model="qcDetailVisible" size="52%" destroy-on-close>
      <template #header>
        <div class="drawer-title">
          <el-tag :type="verdictType(qcDetail?.verdict || '')" effect="dark">{{ verdictLabel(qcDetail?.verdict || "") || "未质检" }}</el-tag>
          <span class="session-id">{{ qcDetail?.session_id }}</span>
          <el-tag v-if="qcDetail?.category === 'pending_review'" size="small" type="warning">待人工判定</el-tag>
        </div>
      </template>
      <div v-if="qcDetail" class="qc-detail" v-loading="qcReplayLoading">
        <div v-if="qcDetail.summary" class="qc-summary">{{ qcDetail.summary }}</div>
        <div v-else-if="qcDetail.verdict" class="qc-summary muted">裁判未给出摘要</div>

        <template v-if="qcDetail.problems?.length">
          <div class="section-title">问题定位 · 现场还原</div>
          <div v-for="(p, i) in qcDetail.problems" :key="i" class="qc-problem">
            <div class="qc-problem-head">
              <el-tag size="small" type="danger" effect="plain">{{ problemLabel(p.type) }}</el-tag>
              <span v-if="p.turn" class="muted">第 {{ p.turn }} 轮</span>
            </div>
            <div class="qc-problem-reason">{{ p.reason || "(未说明原因)" }}</div>
            <template v-if="problemTurnDialog(p.turn)">
              <div class="scene-bubble customer">{{ problemTurnDialog(p.turn)!.customer }}</div>
              <div class="scene-bubble bot">
                {{ problemTurnDialog(p.turn)!.bot }}
                <el-tag v-if="problemTurnDialog(p.turn)!.source" size="small" type="info" class="scene-src">{{ problemTurnDialog(p.turn)!.source }}</el-tag>
              </div>
              <div v-if="qcTurnChains[rowRound(p.turn!) - 1]" class="qc-turn-chain">
                该轮链路: {{ qcTurnChains[rowRound(p.turn!) - 1].steps.join(" → ") }}
              </div>
            </template>
            <div v-else-if="p.turn && qcReplay" class="muted" style="font-size: 12px">第 {{ p.turn }} 轮内容超出回放范围</div>
          </div>
        </template>

        <div class="section-title">会话全景</div>
        <el-collapse v-if="qcPanorama.length">
          <el-collapse-item :title="`共 ${qcPanorama.length} 轮对话 (点击展开)`">
            <div
              v-for="r in qcPanorama"
              :key="r.round"
              class="pano-round"
              :class="{ 'pano-problem': r.problem }"
            >
              <span class="pano-round-no">{{ r.round }}</span>
              <div class="pano-text">
                <div class="pano-customer">{{ r.customer }}</div>
                <div class="pano-bot">{{ r.bot }}<el-tag v-if="r.source" size="small" type="info" class="scene-src">{{ r.source }}</el-tag></div>
              </div>
            </div>
          </el-collapse-item>
        </el-collapse>
        <div v-else class="muted">无对话记录 (可能仅被信号采集, 尚未质检)</div>

        <div class="section-title">质检信息</div>
        <el-descriptions :column="2" size="small" border>
          <el-descriptions-item label="判定">
            <el-tag size="small" :type="verdictType(qcDetail.verdict || '')">{{ verdictLabel(qcDetail.verdict || "") || "未质检" }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="对话轮数">{{ qcDetail.turns ?? "-" }}</el-descriptions-item>
          <el-descriptions-item label="裁判模型">{{ shortModel(qcDetail.judge_model || "") || "-" }}</el-descriptions-item>
          <el-descriptions-item label="会话时间">{{ fmtTime(qcDetail.session_time || qcDetail.scanned_at) }}</el-descriptions-item>
          <el-descriptions-item v-if="qcDetail.signal_source" label="案例信号" :span="2">
            {{ signalLabel(qcDetail.signal_source) }}
            <span v-if="qcDetail.fix_status"> · 修复状态: {{ fixStatusLabel(qcDetail.fix_status) }}</span>
          </el-descriptions-item>
        </el-descriptions>

        <div class="qc-actions">
          <el-button type="primary" @click="gotoAuditSession(qcDetail.session_id)">会话回放</el-button>
          <el-button v-if="qcDetail.badcase_id" type="warning" plain @click="openBadcaseById(qcDetail.badcase_id!)">整改闭环</el-button>
          <el-button :loading="qcRescanning" @click="doRescan">复检此会话</el-button>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue"
import { useRouter } from "vue-router"
import { ElMessage, ElMessageBox } from "element-plus"
import { Search } from "@element-plus/icons-vue"
import {
  attributeBadcase,
  resolveBadcase,
  startBatchAttribution,
  getBatchAttributionStatus,
  expandGoldenSet,
  getBadcaseStats,
  getBadcase,
  startQualityScan,
  getQualityScanStatus,
  listQcSessions,
  rescanQualitySession,
  type QcSessionRow,
  getQualityCoverage,
  type Badcase,
  type QualityCoverage,
  type QualityProblem,
  type QualityScanStatus,
} from "@/api/closedLoop"
import { getConversationReplay, type ReplayResponse } from "@/api/console"

const router = useRouter()

// ── 页签: 质检记录 (全量会话判定) / 问题案例 (归因整改闭环) ──
// ── 统一会话质检列表 (判定 ⟕ 问题案例, 会话维度一行) ──
const qcRows = ref<QcSessionRow[]>([])
const qcTotal = ref(0)
const qcPage = ref(1)
const qcPageSize = ref(50)
const qcLoading = ref(false)
const qcFilters = ref<{ category: string; keyword: string }>({ category: "", keyword: "" })
const selected = ref<QcSessionRow[]>([])
const coverage = ref<QualityCoverage | null>(null)

async function loadQc() {
  qcLoading.value = true
  try {
    const res = await listQcSessions({
      category: qcFilters.value.category || undefined,
      keyword: qcFilters.value.keyword || undefined,
      limit: qcPageSize.value,
      offset: (qcPage.value - 1) * qcPageSize.value,
    })
    qcRows.value = res.sessions
    qcTotal.value = res.total
  } catch {
    /* handled */
  } finally {
    qcLoading.value = false
  }
}

function reloadQc() {
  qcPage.value = 1
  loadQc()
}

function clearQcFilters() {
  qcFilters.value = { category: "", keyword: "" }
  reloadQc()
}

function setCategory(cat: string) {
  qcFilters.value.category = qcFilters.value.category === cat ? "" : cat
  reloadQc()
}

function sessionTimeTitle(row: QcSessionRow): string {
  const parts = [`会话 ${fmtTime(row.session_time)}`]
  if (row.scanned_at) parts.push(`质检 ${fmtTime(row.scanned_at)}`)
  if (row.collected_at) parts.push(`案例采集 ${fmtTime(row.collected_at)}`)
  return parts.join(" · ")
}

async function loadCoverage() {
  try {
    coverage.value = await getQualityCoverage()
  } catch {
    /* handled */
  }
}

function verdictLabel(v: string) {
  return { pass: "合格", warn: "提醒", fail: "不合格" }[v] ?? v
}
function verdictType(v: string): string {
  return { pass: "success", warn: "warning", fail: "danger" }[v] ?? "info"
}
const PROBLEM_LABELS: Record<string, string> = {
  A: "答非所问",
  B: "幻觉编造",
  C: "越界承诺",
  D: "漏转人工",
  E: "未解决无引导",
}
function problemLabel(t?: string) {
  return t ? (PROBLEM_LABELS[t] ?? t) : "-"
}

function gotoAuditSession(sessionId: string) {
  router.push({ path: "/admin/audit", query: { session_id: sessionId } })
}

// 质检记录行点击: fail 且已采入闭环 → 打开对应问题案例; 否则跳会话回放
const qcDetailVisible = ref(false)
const qcDetail = ref<QcSessionRow | null>(null)
const qcReplay = ref<ReplayResponse | null>(null)
const qcReplayLoading = ref(false)
const qcRescanning = ref(false)

async function openQcDetail(row: QcSessionRow) {
  qcDetail.value = row
  qcDetailVisible.value = true
  qcReplay.value = null
  qcReplayLoading.value = true
  try {
    qcReplay.value = await getConversationReplay(row.session_id)
  } catch {
    /* 回放拉取失败时抽屉降级为仅判定视图 */
  } finally {
    qcReplayLoading.value = false
  }
}

// 轮号口径: QA 裁判的 turn N 数的是对话日志行 (客户/客服各算一行)。
// rowRound(n) = 第 n 行(1-based)所属的客户轮序 (每遇客户行 +1)
function rowRound(n: number): number {
  const ts = qcReplay.value?.turns ?? []
  let r = 0
  for (let i = 0; i < n && i < ts.length; i++) if (ts[i].speaker === "customer") r++
  return r
}

// 问题轮现场: 定位第 N 行 → 向前取客户句 / 向后取客服句
function problemTurnDialog(turn?: number | null): { customer: string; bot: string; source: string | null } | null {
  if (!turn || !qcReplay.value) return null
  const ts = qcReplay.value.turns
  if (turn < 1 || turn > ts.length) return null
  let ci = ts[turn - 1].speaker === "customer" ? turn - 1 : -1
  for (let i = turn - 1; i >= 0 && ci < 0; i--) if (ts[i].speaker === "customer") ci = i
  let bi = ts[turn - 1].speaker === "bot" ? turn - 1 : -1
  for (let i = turn - 1; i < ts.length && bi < 0; i++) if (ts[i].speaker === "bot") bi = i
  if (ci < 0) return null
  return { customer: ts[ci].content, bot: bi >= 0 ? ts[bi].content : "", source: bi >= 0 ? ts[bi].response_source : null }
}

// 会话决策链按轮分组 (turn_start 起组, 组序 = 轮序)
const qcTurnChains = computed(() => {
  const ds = qcReplay.value?.decisions ?? []
  const groups: { round: number; steps: string[] }[] = []
  for (const d of ds) {
    if (d.action === "turn_start" || !groups.length) groups.push({ round: groups.length + 1, steps: [] })
    if (d.action !== "turn_start") groups[groups.length - 1]?.steps.push(ACTION_STEP[d.action] ?? d.action)
  }
  return groups
})
const ACTION_STEP: Record<string, string> = {
  intent_classify: "意图分类",
  route_decision: "路由",
  tool_call: "工具",
  rag_retrieve: "检索",
  llm_generate: "生成",
  noise_blocked: "噪声拦截",
  faq_direct: "FAQ直出",
  chain_complete: "完成",
  topic_track: "诉求跟踪",
  context_reply_pass: "回话放行",
  outbound_guard: "出站闸门",
}

// 会话全景: 客户轮 → 客服回复配对
const qcPanorama = computed(() => {
  const ts = qcReplay.value?.turns ?? []
  const rounds: { round: number; customer: string; bot: string; source: string | null; problem: boolean }[] = []
  let round = 0
  for (const t of ts) {
    if (t.speaker === "customer") {
      round += 1
      const bot = ts[ts.indexOf(t) + 1]
      rounds.push({
        round,
        customer: t.content,
        bot: bot?.speaker === "bot" ? bot.content : "",
        source: bot?.response_source ?? null,
        problem: (qcDetail.value?.problems ?? []).some((p) => p.turn != null && rowRound(p.turn) === round),
      })
    }
  }
  return rounds
})

async function doRescan() {
  if (!qcDetail.value) return
  qcRescanning.value = true
  try {
    const r = await rescanQualitySession(qcDetail.value.session_id)
    if (r.status === "ok" && qcDetail.value) {
      qcDetail.value = {
        ...qcDetail.value,
        verdict: (r.verdict as QcSessionRow["verdict"]) ?? qcDetail.value.verdict,
        problems: r.problems ?? qcDetail.value.problems,
        summary: r.summary ?? qcDetail.value.summary,
      }
      await Promise.all([loadQc(), loadCoverage()])
      ElMessage.success(`复检完成: ${verdictLabel(r.verdict ?? "")}`)
    } else {
      ElMessage.info(r.status === "skipped" ? "对话不足 2 轮, 跳过" : "复检完成")
    }
  } catch {
    ElMessage.error("复检失败")
  } finally {
    qcRescanning.value = false
  }
}

async function openBadcaseById(badcaseId: string) {
  try {
    const bc = await getBadcase(badcaseId)
    if (bc) openDetail(bc)
  } catch {
    /* handled */
  }
}

// ── 统计与分布 ──
const stats = ref<(Awaited<ReturnType<typeof getBadcaseStats>>) | null>(null)

const LAYER_LABELS: Record<string, string> = {
  layer_1: "① 预处理",
  layer_2: "② 会话管理",
  layer_3: "③ 意图识别",
  layer_4: "④ 路由决策",
  layer_5: "⑤ RAG 检索",
  layer_6: "⑥ 回复生成",
  layer_7: "⑦ 风控合规",
  uncertain: "待人工判定",
}
const SIGNAL_LABELS: Record<string, string> = {
  negative_feedback: "负面反馈",
  transfer: "转人工",
  agent_revoke: "人工撤回",
  behavior_anomaly: "行为异常",
  compliance_alert: "合规告警",
  qa_scan: "质检巡检",
}
const CATEGORY_LABELS: Record<string, string> = {
  semantic: "语义误判",
  knowledge: "知识缺口",
  process: "流程缺陷",
  coverage: "覆盖不足",
  uncertain: "待定",
}
const FIX_TABLE_LABELS: Record<string, string> = {
  A_knowledge: "A · 知识库",
  B_intent: "B · 意图库",
  C_rule: "C · 规则",
  D_model: "D · 模型",
  none: "无需修复",
}

const layerDist = computed(() => {
  const dist = stats.value?.layer_dist ?? {}
  return Object.entries(dist)
    .map(([key, count]) => ({ key, count, label: LAYER_LABELS[key] ?? key }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 5)
})
const distMax = computed(() => Math.max(1, ...layerDist.value.map((d) => d.count)))
function distWidth(count: number) {
  return `${Math.max(4, Math.round((count / distMax.value) * 100))}%`
}

// ── 列表 ──
function onSelection(rows: QcSessionRow[]) {
  // 批量操作作用于问题案例: 行携带 badcase_id 才可选 (模板 :selectable 已限)
  selected.value = rows.filter((r) => r.badcase_id)
}
function fixTableFor(_row: QcSessionRow): string | undefined {
  return undefined // 统一行不携带分流表; 后端 resolve 按根因层自动落表
}

async function loadStats() {
  try {
    stats.value = await getBadcaseStats()
  } catch {
    /* handled */
  }
}

// ── 详情抽屉 ──
const detailVisible = ref(false)
const detail = ref<Badcase | null>(null)
const judgedLayer = ref("")
const judgedTable = ref("")
const acting = ref(false)
const contextMessages = ref<{ speaker: string; content: string }[]>([])
const contextLoading = ref(false)

const nextIdx = computed(() => {
  if (!detail.value) return null
  const i = qcRows.value.findIndex((b) => b.badcase_id === detail.value!.id)
  return i >= 0 && i < qcRows.value.length - 1 ? i + 1 : null
})

function openNext() {
  const row = qcRows.value[nextIdx.value!]
  if (row?.badcase_id) openBadcaseById(row.badcase_id)
}

function openDetail(row: Badcase) {
  detail.value = row
  judgedLayer.value = row.human_confirmed_layer || row.root_cause_layer || "uncertain"
  judgedTable.value = row.fix_table || ""
  detailVisible.value = true
  loadContext(row)
}

async function loadContext(row: Badcase) {
  contextMessages.value = []
  contextLoading.value = true
  try {
    // 管理端会话回放接口 (此前误调客户侧 /sessions/*: admin token + 已归档会话下 404)
    const { getConversationReplay } = await import("@/api/console")
    const r = await getConversationReplay(row.session_id)
    const all: { speaker: string; content: string }[] = (r.turns ?? []).map((t) => ({
      speaker: t.speaker,
      content: t.content,
    }))
    // 只显示现场轮之前的上文 (最后两条是本坏例现场, 模板里高亮单独渲染)
    contextMessages.value = all.slice(0, -2).slice(-4)
  } catch {
    contextMessages.value = [] // 会话已归档/过期时静默降级, 仅显示现场轮
  } finally {
    contextLoading.value = false
  }
}

const snapshotPretty = computed(() => {
  const snap = detail.value?.snapshot
  return snap && Object.keys(snap).length ? JSON.stringify(snap, null, 2) : "(采集时未携带快照)"
})

// ── 快照中文分层对照: 采集时的链路状态字段 → 人话 ──
const SNAP_FIELD_LABELS: Record<string, string> = {
  intent: "命中意图",
  confidence: "意图置信度",
  traffic_class: "流量分类",
  response_source: "回复来源",
  rag_hit: "RAG 检索",
  context_len: "上下文轮数",
  guard_reason: "护栏动作",
  stage_detail: "阶段明细",
}
function snapFieldValue(key: string, v: unknown): string {
  if (key === "confidence") return typeof v === "number" ? `${Math.round(v * 100)}%` : String(v ?? "-")
  if (key === "rag_hit") return v ? "已命中知识库/工具" : "未命中"
  if (v == null || v === "") return "-"
  return typeof v === "object" ? JSON.stringify(v) : String(v)
}
const snapRows = computed(() => {
  const snap = detail.value?.snapshot
  if (!snap) return []
  const rows: { label: string; value: string }[] = []
  for (const [key, label] of Object.entries(SNAP_FIELD_LABELS)) {
    if (key in snap) rows.push({ label, value: snapFieldValue(key, snap[key]) })
  }
  // 未收录的标量字段也照常显示 (兜底, 防止新增字段被吞)
  for (const [key, v] of Object.entries(snap)) {
    if (key in SNAP_FIELD_LABELS || key === "transcript" || key === "turns_meta") continue
    if (typeof v !== "object") rows.push({ label: key, value: snapFieldValue(key, v) })
  }
  return rows
})
const snapTurnsMeta = computed(() => {
  const meta = detail.value?.snapshot?.turns_meta
  return Array.isArray(meta) ? (meta as { speaker: string; intent?: string; src?: string }[]) : []
})
const snapTranscript = computed(() => {
  const t = detail.value?.snapshot?.transcript
  return typeof t === "string" && t.trim() ? t : ""
})

// ── 质检判定 (qa_scan 采集的案例: signal_detail 里裁判结论) ──
const qaDetail = computed(() => detail.value?.signal_detail as { verdict?: string; summary?: string; problems?: QualityProblem[] } | null)
const qaVerdict = computed(() => qaDetail.value?.verdict ?? "")
const qaSummary = computed(() => qaDetail.value?.summary ?? "")
const qaProblems = computed<QualityProblem[]>(() => qaDetail.value?.problems ?? [])

async function refreshAfterAction(msg: string) {
  ElMessage.success(msg)
  detailVisible.value = false
  await Promise.all([loadQc(), loadStats()])
}

async function runAttribution(row: Badcase) {
  acting.value = true
  try {
    const r = (await attributeBadcase(row.id)) as { root_cause_layer?: string; needs_human_review?: boolean }
    ElMessage.success(`归因完成: ${layerLabel(r.root_cause_layer) || "-"}`)
    await Promise.all([loadQc(), loadStats()])
    if (detailVisible.value && detail.value?.id === row.id) {
      const fresh = await getBadcase(row.id)
      if (fresh) openDetail(fresh)
    }
  } catch {
    /* handled */
  } finally {
    acting.value = false
  }
}

// 整改进度步骤条: 0 归因 → 1 确认 → 2 修复(含灰度) → 3 验证
const fixStepActive = computed(() => {
  const d = detail.value
  if (!d) return 0
  if (d.fix_status === "deployed") return 4
  if (d.fix_status === "canary") return 3
  if (d.fix_status === "fixing") return 2
  if (d.root_cause_layer && !d.needs_human_review) return 2
  if (d.root_cause_layer) return 1
  return 0
})

// 分流表说明 (下拉 tooltip) 与修复指引 (去哪里改什么)
const FIX_TABLE_TIPS: Record<string, string> = {
  A_knowledge: "知识缺口/检索不命中 → 补知识内容",
  B_intent: "意图/语义误判 → 意图库与种子语料",
  C_rule: "规则/路由/会话配置 → 规则与映射表",
  D_model: "生成幻觉/Prompt 失效 → 提示词与模型",
  none: "无需改表 (流程/工程问题)",
}
const FIX_TABLE_GUIDES: Record<string, { text: string; to?: string }> = {
  A_knowledge: { text: "到 FAQ 管理补标准问答对, 或在文档管理补充知识文档并重新摄入", to: "/admin/faq" },
  B_intent: { text: "到意图库管理页维护规则词/种子语料, 重跑评测闸门后激活", to: "/admin/intent-library" },
  C_rule: { text: "核对意图注册表与流量分类映射 (归并表), 走代码评审", to: "/admin/intent-library" },
  D_model: { text: "调整回复生成系统提示词与出站闸门话术, 走代码评审", to: "" },
  none: { text: "流程或工程问题, 在代码/配置侧定位处理", to: "" },
}
const fixGuide = computed(() => {
  const key = judgedTable.value || detail.value?.fix_table || ""
  return FIX_TABLE_GUIDES[key] ?? null
})

// 流转带备注 (供 fix_note 追溯)
async function transition(status: string) {
  if (!detail.value) return
  let note = ""
  try {
    const r = await ElMessageBox.prompt("流转备注 (可选, 默认状态名):", `流转 → ${fixStatusLabel(status)}`, {
      inputPlaceholder: "如: 已补 FAQ 词条 / 种子语料已重训",
      inputValue: "",
    })
    note = (r.value || "").trim()
  } catch {
    return /* 取消 */
  }
  acting.value = true
  try {
    await resolveBadcase(detail.value.id, {
      fix_status: status,
      note: note ? `${fixStatusLabel(status)} · ${note}` : `状态流转 → ${fixStatusLabel(status)}`,
    })
    await refreshAfterAction(`已${fixStatusLabel(status)}`)
  } catch {
    /* handled */
  } finally {
    acting.value = false
  }
}

// 复检原会话 (从整改闭环侧验证修复效果)
const rescanningBadcase = ref(false)
async function rescanFromBadcase() {
  if (!detail.value?.session_id) return
  rescanningBadcase.value = true
  try {
    const r = await rescanQualitySession(detail.value.session_id)
    if (r.status === "ok") {
      const v = verdictLabel(r.verdict ?? "")
      const ok = r.verdict === "pass" || r.verdict === "warn"
      ElMessage[ok ? "success" : "warning"](`复检完成: ${v}${ok ? " — 修复验证通过" : " — 仍有问题, 建议回退排查"}`)
    } else {
      ElMessage.info("对话不足 2 轮, 无法复检")
    }
  } catch {
    ElMessage.error("复检失败")
  } finally {
    rescanningBadcase.value = false
  }
}

async function confirmResolve() {
  if (!detail.value) return
  acting.value = true
  try {
    await resolveBadcase(detail.value.id, {
      fix_status: "fixing",
      fix_table: judgedTable.value || detail.value.fix_table || undefined,
      human_confirmed_layer: judgedLayer.value,
      note: `人工确认${judgedLayer.value !== detail.value.root_cause_layer ? " (改判)" : ""}`,
    })
    await refreshAfterAction("归因已确认，进入修复跟踪")
  } catch {
    /* handled */
  } finally {
    acting.value = false
  }
}

async function rejectCase() {
  if (!detail.value) return
  try {
    const { value } = await ElMessageBox.prompt("驳回原因 (必填):", "驳回坏例", { inputPlaceholder: "如: 误采集 / 重复提交" })
    if (!value.trim()) {
      ElMessage.warning("驳回需填原因")
      return
    }
    acting.value = true
    await resolveBadcase(detail.value.id, { fix_status: "rejected", note: `驳回: ${value}` })
    await refreshAfterAction("已驳回")
  } catch {
    return
  } finally {
    acting.value = false
  }
}

// ── 批量操作 ──
async function batchConfirm() {
  const rows = selected.value.filter((r) => r.root_cause_layer && r.needs_human_review)
  if (!rows.length) {
    ElMessage.warning("选中项中没有可确认的 (需已归因且待复核)")
    return
  }
  try {
    await ElMessageBox.confirm(`将 ${rows.length} 条按机器归因结果批量确认进入修复？`, "批量确认", { type: "warning" })
  } catch {
    return
  }
  let ok = 0
  for (const r of rows) {
    try {
      await resolveBadcase(r.badcase_id!, { fix_status: "fixing", fix_table: fixTableFor(r), human_confirmed_layer: r.root_cause_layer!, note: "批量确认" })
      ok++
    } catch {
      /* skip */
    }
  }
  ElMessage.success(`批量确认完成: ${ok}/${rows.length}`)
  await Promise.all([loadQc(), loadStats()])
}

async function batchTransition(status: string) {
  const rows = selected.value
  if (!rows.length) return
  let ok = 0
  for (const r of rows) {
    try {
      await resolveBadcase(r.badcase_id!, { fix_status: status, note: "批量流转" })
      ok++
    } catch {
      /* skip */
    }
  }
  ElMessage.success(`批量流转完成: ${ok}/${rows.length}`)
  await Promise.all([loadQc(), loadStats()])
}

// ── 批量归因 (后台任务轮询, 跟随当前筛选范围) ──
const batch = ref({
  running: false,
  total: 0,
  done: 0,
  failed: 0,
  scope: null as { signal_source?: string; keyword?: string } | null,
})
let batchTimer: ReturnType<typeof setInterval> | null = null

const batchPct = computed(() => (batch.value.total > 0 ? Math.round((batch.value.done / batch.value.total) * 100) : 0))

const batchScopeText = computed(() => {
  const parts: string[] = []
  if (batch.value.scope?.signal_source) parts.push(signalLabel(batch.value.scope.signal_source))
  if (batch.value.scope?.keyword) parts.push(`"${batch.value.scope.keyword}"`)
  return parts.join(" + ") || "全部"
})

async function pollBatch() {
  try {
    const st = await getBatchAttributionStatus()
    batch.value = {
      running: st.running,
      total: st.total,
      done: st.done,
      failed: st.failed,
      scope: (st.scope as { signal_source?: string; keyword?: string }) ?? null,
    }
    if (!st.running) {
      if (batchTimer) {
        clearInterval(batchTimer)
        batchTimer = null
      }
      if (st.total > 0) {
        ElMessage.success(`批量归因完成: 成功 ${st.done} / 失败 ${st.failed} / 共 ${st.total}`)
        await Promise.all([loadQc(), loadStats()])
      }
    }
  } catch {
    /* handled */
  }
}

async function doBatchAttribution() {
  const scope: { signal_source?: string; keyword?: string } = {}
  if (qcFilters.value.keyword) scope.keyword = qcFilters.value.keyword
  const scopeText = Object.keys(scope).length ? " (按当前筛选范围)" : ""
  try {
    await startBatchAttribution(200, scope)
    ElMessage.success(`GLM 裁判批量归因已启动${scopeText}, 每条约 20-40 秒`)
    if (!batchTimer) batchTimer = setInterval(pollBatch, 4000)
  } catch {
    /* handled */
  }
}

// ── 全量质检巡检 (后台任务轮询) ──
const scan = ref({
  running: false,
  total: 0,
  done: 0,
  n_pass: 0,
  n_warn: 0,
  n_fail: 0,
  n_error: 0,
  lastRun: null as QualityScanStatus["last_run"],
})
let scanTimer: ReturnType<typeof setInterval> | null = null

const scanPct = computed(() => (scan.value.total > 0 ? Math.round((scan.value.done / scan.value.total) * 100) : 0))

async function pollScan() {
  try {
    const st = await getQualityScanStatus()
    scan.value = {
      running: st.running,
      total: st.total,
      done: st.done,
      n_pass: st.n_pass,
      n_warn: st.n_warn,
      n_fail: st.n_fail,
      n_error: st.n_error,
      lastRun: st.last_run ?? scan.value.lastRun,
    }
    if (!st.running) {
      if (scanTimer) {
        clearInterval(scanTimer)
        scanTimer = null
      }
      if (st.total > 0) {
        ElMessage.success(`全量质检完成: 不合格 ${st.n_fail} 已采入待复核 (合格率 ${((st.last_run?.pass_rate ?? 0) * 100).toFixed(1)}%)`)
        await Promise.all([loadQc(), loadStats(), loadCoverage()])
      }
    }
  } catch {
    /* handled */
  }
}

async function doQualityScan() {
  try {
    await startQualityScan({ limit: 5000 })  // 全量补扫: 后端批次循环至无未检会话
    ElMessage.success("全量质检已启动, 后台逐会话审查原始对话")
    if (!scanTimer) scanTimer = setInterval(pollScan, 4000)
  } catch {
    /* handled */
  }
}

async function addToGolden(row: Badcase) {
  try {
    const r = await expandGoldenSet([row.user_input])
    ElMessage.success(`金标扩充完成: 生成 ${r.variants.length} 条变体`)
  } catch {
    /* handled */
  }
}

function gotoAudit(row: Badcase) {
  gotoAuditSession(row.session_id)
}

// ── 展示工具 ──
function signalType(s: string): string {
  const m: Record<string, string> = {
    negative_feedback: "danger",
    transfer: "warning",
    agent_revoke: "warning",
    behavior_anomaly: "info",
    compliance_alert: "danger",
    qa_scan: "success",
  }
  return m[s] ?? "info"
}
function signalLabel(s: string) {
  return SIGNAL_LABELS[s] ?? s
}
function layerLabel(s?: string | null) {
  return s ? (LAYER_LABELS[s] ?? s) : ""
}
function categoryLabel(s?: string | null) {
  return s ? (CATEGORY_LABELS[s] ?? s) : ""
}
function fixStatusLabel(s: string) {
  const m: Record<string, string> = { pending: "待修", fixing: "修复中", canary: "已灰度", deployed: "已全量", rejected: "已驳回" }
  return m[s] ?? s
}
function fixStatusType(s: string): string {
  const m: Record<string, string> = { pending: "info", fixing: "warning", canary: "warning", deployed: "success", rejected: "danger" }
  return m[s] ?? "info"
}
function shortModel(m?: string | null) {
  if (!m) return ""
  if (m.includes("GLM")) return "GLM 裁判"
  if (m.includes("qwen")) return "qwen 裁判"
  return m.length > 10 ? m.slice(0, 10) : m
}
function fmtPct(v: number | null | undefined) {
  return v == null ? "-" : `${Math.round(v * 100)}%`
}
function fmtTime(iso?: string | null) {
  return iso ? iso.slice(0, 19).replace("T", " ") : "-"
}
onMounted(() => {
  pollScan() // 恢复可能进行中的全量质检进度
  loadQc()
  pollBatch()
  loadStats()
  loadCoverage()
})
onUnmounted(() => {
  if (batchTimer) clearInterval(batchTimer)
})
</script>

<style scoped lang="scss">
.page-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: var(--space-2);
}
.page-subtitle {
  font-size: var(--fs-sm);
  font-weight: 400;
  color: var(--color-text-secondary);
  margin-left: 8px;
}

.stat-cards {
  display: grid;
  grid-template-columns: repeat(5, minmax(110px, 1fr)) minmax(220px, 1.6fr);
  gap: 10px;
  margin-top: 12px;
}
.stat-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 10px 12px;
  display: flex;
  flex-direction: column;
  gap: 2px;
  background: var(--el-fill-color-extra-light);
  cursor: pointer;
  transition: border-color 0.2s;
  &:hover { border-color: var(--el-color-primary-light-5); }
  &.active { border-color: var(--el-color-primary); background: var(--el-color-primary-light-9); }
  .label { font-size: var(--fs-sm); color: var(--color-text-secondary); }
  .num { font-size: 20px; font-weight: 600; }
  .num.warn { color: var(--el-color-warning); }
  .hint { font-size: var(--fs-xs, 11px); color: var(--color-text-placeholder); }
}
.dist-card {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  padding: 8px 12px;
  background: var(--el-fill-color-extra-light);
  .label { font-size: var(--fs-sm); color: var(--color-text-secondary); }
  .dist-bars { margin-top: 4px; display: flex; flex-direction: column; gap: 3px; }
  .dist-row { display: flex; align-items: center; gap: 6px; }
  .dist-label { font-size: 11px; width: 62px; color: var(--color-text-secondary); flex-shrink: 0; }
  .dist-track { flex: 1; height: 8px; border-radius: 4px; background: var(--el-fill-color); overflow: hidden; }
  .dist-fill { height: 100%; border-radius: 4px; background: var(--el-color-primary-light-3); }
  .dist-count { font-size: 11px; width: 24px; text-align: right; }
  .dist-empty { font-size: var(--fs-sm); }
}

.batch-progress-text { font-size: var(--fs-sm); color: var(--color-text-secondary); }
.qa-tabs {
  margin-top: 8px;
  .tab-hint { font-size: var(--fs-xs, 11px); color: var(--color-text-placeholder); margin-left: 4px; }
}
.coverage-line {
  font-size: var(--fs-sm);
  color: var(--color-text-secondary);
  padding: 4px 2px 0;
  b { color: var(--color-text-primary); margin: 0 2px; }
  b.ok { color: var(--el-color-success); }
}
.problem-tag { margin-right: 4px; margin-bottom: 2px; cursor: default; }
.qa-verdict {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
  .qa-summary { font-size: var(--fs-sm); color: var(--color-text-primary); }
}
.qa-problem {
  display: flex;
  align-items: baseline;
  gap: 8px;
  padding: 4px 8px;
  border-radius: 6px;
  background: var(--el-color-danger-light-9, #fef0f0);
  margin-bottom: 4px;
  font-size: var(--fs-sm);
  .qa-reason { color: var(--color-text-primary); }
}
.turns-meta {
  margin: 8px 0;
  .turns-meta-title { font-size: var(--fs-xs, 11px); margin-bottom: 4px; color: var(--color-text-secondary); }
  .turn-meta-row {
    display: flex;
    gap: 10px;
    font-size: var(--fs-xs, 12px);
    padding: 3px 8px;
    border-radius: 4px;
    &:nth-child(odd) { background: var(--el-fill-color-extra-light); }
    .turn-idx { width: 22px; color: var(--color-text-placeholder); }
    .turn-speaker { width: 30px; font-weight: 600; color: var(--color-text-secondary); &.is-customer { color: var(--el-color-primary); } }
    .turn-intent { width: 130px; }
    .turn-src, .turn-intent { color: var(--color-text-secondary); }
  }
}
.snapshot.transcript { margin-top: 8px; max-height: 260px; overflow: auto; }
.raw-snap { margin-top: 8px; }
.filters {
  display: flex;
  gap: var(--space-2);
  margin-top: 12px;
  flex-wrap: wrap;
  align-items: center;
}
.filter-spacer { flex: 1; }
.muted { color: var(--color-text-secondary); }
.input-text { cursor: pointer; }
.session-id {
  font-family: var(--font-mono, ui-monospace, SFMono-Regular, Menlo, monospace);
  font-size: 11px;
  color: var(--color-text-secondary);
}
.model-tag {
  font-size: var(--fs-xs, 11px);
  border: 1px solid var(--el-border-color-light);
  border-radius: 4px;
  padding: 1px 4px;
  color: var(--color-text-secondary);
}

.drawer-title {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 15px;
}
.detail-body { padding: 0 4px; }
.section-title {
  font-weight: 600;
  margin: 14px 0 6px;
  font-size: var(--fs-sm);
  .section-hint { font-weight: 400; font-size: var(--fs-xs, 11px); }
}
.ctx-row {
  display: flex;
  gap: 8px;
  padding: 5px 10px;
  border-radius: 6px;
  margin-bottom: 4px;
  font-size: var(--fs-sm);
  .ctx-speaker { flex-shrink: 0; font-weight: 600; color: var(--color-text-secondary); }
  .ctx-content { white-space: pre-wrap; word-break: break-all; }
  &.ctx-user .ctx-content { color: var(--color-text-primary); }
  &.ctx-bot { background: var(--el-fill-color-light); }
  &.highlight { border: 1px solid var(--el-color-primary-light-7); }
  &.ctx-user.highlight { background: var(--el-color-primary-light-9); }
}
.context-loading { font-size: var(--fs-sm); padding: 6px 0; }
.attrib-row { display: flex; align-items: center; gap: 8px; margin-bottom: 6px; flex-wrap: wrap; }
.attrib-meta { font-size: var(--fs-sm); }
.evidence, .snapshot {
  background: var(--color-bg-page, #f5f7fa);
  padding: 10px;
  border-radius: 6px;
  font-size: var(--fs-xs, 12px);
  white-space: pre-wrap;
  word-break: break-all;
}
.action-grid { display: flex; flex-wrap: wrap; gap: 8px; }
.fix-note {
  background: var(--el-color-success-light-9, #f0f9eb);
  padding: 8px 12px;
  border-radius: 6px;
  font-size: var(--fs-sm);
}
/* 质检详情抽屉 (问题导向) */
.qc-summary {
  padding: var(--space-3);
  font-size: 14px;
  line-height: 1.7;
  background: var(--color-bg-page);
  border-left: 3px solid var(--color-primary, #409eff);
  border-radius: var(--radius-md);
}
.qc-problem {
  padding: var(--space-2) var(--space-3);
  margin-bottom: var(--space-2);
  background: var(--color-bg-page);
  border-radius: var(--radius-md);
}
.qc-problem-head {
  display: flex;
  align-items: center;
  gap: var(--space-2);
}
.qc-problem-reason {
  margin-top: 4px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--color-text-primary);
}
.qc-actions {
  display: flex;
  gap: var(--space-2);
  margin-top: var(--space-4);
}
/* 问题轮现场还原 */
.scene-bubble {
  max-width: 92%;
  padding: 6px 10px;
  margin: 4px 0;
  font-size: 13px;
  line-height: 1.6;
  border-radius: 8px;
}
.scene-bubble.customer {
  align-self: flex-start;
  background: var(--color-fill, rgba(0, 0, 0, 0.05));
}
.scene-bubble.bot {
  align-self: flex-end;
  margin-left: auto;
  background: var(--color-primary-light-9, rgba(64, 158, 255, 0.1));
}
.scene-src { margin-left: 6px; }
.qc-turn-chain {
  margin-top: 6px;
  padding: 4px 8px;
  font-size: 12px;
  color: var(--color-text-secondary);
  background: var(--color-bg-page);
  border-radius: 6px;
}
.pano-round {
  display: flex;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 6px;
}
.pano-round.pano-problem {
  background: rgba(245, 108, 108, 0.08);
}
.pano-round-no {
  flex: none;
  width: 20px;
  height: 20px;
  font-size: 11px;
  line-height: 20px;
  text-align: center;
  color: var(--color-text-secondary);
  background: var(--color-fill, rgba(0, 0, 0, 0.05));
  border-radius: 50%;
}
.pano-problem .pano-round-no {
  color: #fff;
  background: var(--el-color-danger, #f56c6c);
}
.pano-customer { font-size: 13px; }
.pano-bot { margin-top: 2px; font-size: 12px; color: var(--color-text-secondary); }
/* 整改闭环抽屉优化 */
.fix-steps { margin-bottom: 16px; }
.reject-alert { margin-bottom: 12px; }
.fix-guide { margin-top: 8px; }
</style>
