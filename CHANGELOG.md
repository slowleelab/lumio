# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/lang/zh-CN/).

## [Unreleased]

### Added
- **客户端模拟 Agent + 对话模拟管理页**（虚拟客户黑盒压测, 喂闭环/验链路/测延迟）
  - `services/common/simulator.py`：12 个场景剧本（账单查询槽位补齐/挂失确认链/分期 RAG/定义句式/复合意图/投诉/主动转人工/闲聊/乱码噪声/FAQ 直出/知识缺口+差评等）× N 并发虚拟客户；黑盒走自身 HTTP 链路 send→poll→feedback, 速率可配（用户数/间隔）, 期望命中/延迟 P95/差评计数实时统计
  - `/admin/simulator/{scenarios,start,stop,status}` 管理端点 + 前端「对话模拟」页（启停按钮/场景勾选/并发与间隔配置/统计卡/实时轮次表 3s 轮询）
  - 闭环两处采集缺口修复：即时转人工路径补采集钩子（此前仅 L3 低置信确认链采集）；`/chat/feedback` 差评自动采集 Badcase（此前只写 Redis 从未进闭环）
  - 真实 E2E：12 会话/14 轮 → 7 条坏例自动入闭环（5 transfer + 2 negative_feedback）；并暴露真实 badcase（bot 对账单查询过度推能力边界, 应走链 B 而非拒绝）

### Added
- **意图注册表：流派二（Config-as-Code + 治理流水线）全链路落地** —— 新增意图不再要求发版
  - `shared/intent_registry.py`：运营意图注册表（`intent_registry.json` 持久化 + .bak 备份，建议入 Git 管理）与生命周期状态机 `draft → pending_review →(双人复核)→ 评测闸门 → shadow → active → deprecated/rejected`；双人复核强制评审人≠登记者（maker-checker）；最少种子 10 条（建议 20）+ slug 命名规范 + 出厂枚举冲突校验；每步迁移留审计历史（actor/action/note）
  - 评测闸门 `intent_eval.py`（全本地无 LLM）：重叠检测（候选种子 vs 出厂+已生效语料近重复比对，warn 0.94 / hard 0.97，按 mxbai 中文短句底噪实测标定）+ 金标域回归（虚拟最近邻域分类器，加入新种子后金标准确率跌幅 ≤0.02 才放行）
  - L2 索引蓝绿重建 `intent_vector.py`：物理集合按版本命名 `_v{N}`，别名原子切换，保留上一版支持一键回滚，旧版本自动 GC；激活/下线意图自动触发重建（后台 task 持引用防 GC）
  - 管线四接入点：`domain_of/group_of` 注册表回退（五域骨架权威扩展到运营意图）、中文标签回退、L3 分类 prompt 动态追加影子/生效意图（影子标注且只记日志不改路由）、L2 种子语料并入已生效意图
  - 新指标：`lumio_intent_registry_hits_total`（按 slug/state）、`lumio_intent_index_rebuilds_total`（success/failed/rolled_back）
  - 管理端 11 个新端点（注册表 CRUD/提交/评审/评测/激活/下线/驳回 + 索引 status/rebuild/rollback）+ 前端「意图注册表」页签（索引状态卡/状态机操作/新增意图弹窗/审计历史时间线）+ 意图树合并展示运营意图（运营/影子/已下线徽标）
  - 真实 E2E 已验证：登记→自审被拒→交叉复核→评测通过→影子→激活（索引 201 条）→下线（重建 191 条）→树保留审计痕迹

- **目标架构 ④ 两级路由决策 + 四条执行链 + 出站合规闸门**
  - ④ 决策一交易性质三分流（金融交易/只读查询/高风险转人工/咨询）+ 决策二只读四分流（FAQ/RAG/复合/低置信竞速），分类表以 INTENT_DOMAINS+SENSITIVE_INTENTS 归并生成
  - ⑤B 查询轻链路：槽位参数→直连 MCP 工具（绕过 LLM 循环）→Redis 结果缓存（5min）→单次摘要；缺槽反问
  - ⑤C 复合意图：查询取数→数据注入 RAG→联合生成；⑤D 低置信(0.4~0.6) FAQ/RAG 并行竞速取高分
  - ⑥ 引用来源标注：知识回复尾部附《文档标题》；⑦ 出站闸门：敏感词+幻觉数字检测，拦截替换澄清

### Added
- **管理控制台一期**（对话审计 + RAG 指标监控）
  - `console_router.py` 挂 `/api/admin/*`：会话审计列表（dialogue_log 聚合 + chat_message 统计，支持意图/来源/时间过滤与分页）、会话回放（对话轮次 + 决策链 + 处理记录三段结构，retrieval_context 默认脱敏）、操作审计（audit_log 分页查询）、RAG 质量聚合（响应来源/FAQ 命中/意图 TOP/低置信占比/决策延迟 P95，按天 date_trunc）、RAG 实时指标（进程内 Prometheus REGISTRY 直读）
  - 4 个观测埋点：`lumio_rag_cache_ops_total`（检索缓存命中/未命中）、`lumio_rerank_degradation_total`（重排降级按原因）、`lumio_faq_match_total`（FAQ 命中分布）、`lumio_circuit_breaker_state`（熔断器状态 Gauge）
  - web 前端三页：对话审计（列表+回放抽屉：气泡时间线/决策链/处理记录）、操作审计、RAG 指标看板（ECharts，新增依赖；指标卡 30s 轮询）
  - Grafana 新增 `lumio-rag` 看板（14 面板）+ 4 条 RAG 告警（检索 P95/持续降级/FAQ 命中率/缓存命中率）；prometheus 增加 kb-service scrape target
  - nginx 补 `/api/admin`、`/api/auth`、`/api/bot/chat`、`/api/chat-svc` 代理（与 dev 代理对齐的存量缺口）

### Changed
- 修正文档中 LangGraph/LangChain 的错误声称，改为如实描述"asyncio + 规则路由"
- 坐席辅助引擎收敛为单一编排路径（删除旧 AssistOrchestrator 双轨）
- E3 风控独立于 ai_executor 可用（LLM 缺失时合规底线不绕过）
- detect_scene 融合 intent 入参，增加否定语义检测
- 审计中间件改用路由元数据（endpoint 函数名）推断操作类型

### Renamed
- **子工程命名收敛**（跨 3 个 commit）
  - `20c8779` — Java 端 `star-connection/` → `chat-svc/`，groupId `com.example` → `com.lumio.chatsvc`，artifactId 全部 `chat-*`，包路径 `com.example.*` → `com.lumio.chatsvc.*`
  - `5800235` — Python 端 `knowledge-platform/` → `kb-service/`，包 `app/` → `kb/`，配置前缀 `KP_` → `KB_`，ES 索引 `kp_*` → `kb_*`，Kafka 主题 `kp.ingest.*` → `kb.ingest.*`
  - (本 commit) — 主仓 `star_client.py` → `chat_client.py` + 70+ 跨仓引用；env `LUMIO_STAR_CONNECTION_URL` → `LUMIO_CHAT_SVC_URL`；类 `StarConnectionClient` → `ChatSvcClient`

### Fixed
- 删除 save_push_tracker 孤儿函数（OE 改名遗留 + Redis key 前缀不一致）

## [Unreleased] - 2026-08-04

第二轮架构师审核修复（19 项：3 P0 + 9 P1 + 7 P2）。

### Fixed (P0 — 必修)
- **P0-1** `SessionState` 缺 `*_updated_at` 字段 → `customer_memory.AttributeError`。`shared/models.py` 加 `vip_level_updated_at` / `risk_tolerance_updated_at` / `card_types_updated_at`（默认 0.0 → 触发 999 天 → 强制降级）
- **P0-2** `ws_router.cancel_event` 重赋值 `Event()` 引发 race（旧 event 仍被 `stream_chat` 闭包持有）。改用 `event.set()` + `event.clear()` 保持同一对象引用
- **P0-3** `streaming.py` 在 `CancelledError` 仍 yield fake 事件（浪费 token + flush 假消息）。移除假 yield，直接 `raise`，让 FastAPI 终止 generator

### Fixed (P1 — 高优)
- **P1-1** 4 处 `asyncio.create_task` 无引用（`bot_agent` / `decision_log` / `budget` / `tool_robustness`）。加 `_pending_tasks: set[asyncio.Task]` + `add_done_callback(discard)`，防 GC
- **P1-2** 模板缓存无锁 + I/O 阻塞。`prompts/renderer.py` 加 `asyncio.Lock` + `warmup()` 启动预热
- **P1-3** `prompt_registry` 单例 race。改用 `@functools.cache`
- **P1-4** `injection_guard.logger.warning` 记原始 `text[:100]` 泄露 PII（卡号/身份证）。改用 `mask_pii(text[:100])`
- **P1-5** `injection_guard` sanitize 替换文本含 pattern 名（信息泄露）。3 处统一改为 `"[已净化]"`
- **P1-6** `tool_robustness.ToolQuotaGuard` 配额 `incr` + 条件 `expire` 非原子（崩溃导致 key 永不过期）。改用 Lua 脚本 `INCR + EXPIRE` 原子执行
- **P1-7** 缺 `DecisionLog` 表 + alembic 迁移。新增 `c7d8e9f0a1b2_add_decision_log_table.py` + `shared/orm_models.DecisionLog` + `decision_log._write_pg()` 落库 + GDPR 删除路径覆盖
- **P1-8** `bot_agent._estimate_tokens` 与 `token_utils.estimate_tokens` 重复实现且系数不一致（0.55/0.3/0.8 vs 0.5/0.3/0.75）。删除本地实现，统一委托 `token_utils`（保留 `base_overhead=4` 的 +4 消息格式开销）
- **P1-9** `gdpr` Redis scan pattern 缺 `lumio:` 前缀。2 处 pattern 修正为 `lumio:session:*customer:{id}*`

### Fixed (P2 — 中优)
- **P2-1** `entity_sandbox.DEFAULT_ENTITY_ALLOWLIST`（7 项）与 `config.entity_pool_allowlist`（5 项）双源不一致。config 同步到 7 项
- **P2-2** `alerting.build_alert_rules` 启动时 `asyncio.create_task(_loop())` 无引用。加 `_alert_loop_tasks: set[asyncio.Task]` 防 GC
- **P2-3** `ws_router` 异常时 `str(exc)[:200]` 直接发给客户端（泄露 SQL/文件路径/库版本）。改返回 `trace_id` + 通用文案 "服务暂时不可用, 请稍后重试"
- **P2-4** `few_shot.get_cot_trigger` 接受凭空捏造的字符串（`"BILL_INSTALLMENT"` 等，与 `IntentLabel` 不匹配）。新增 `CoTIntent` enum + 别名兼容层
- **P2-5** `customer_memory` 90 天 SQL 聚合不限长（并入 P0-1 + 加 `LIMIT 1000` 防内存爆炸）
- **P2-6** `test_reflector` 缺失 + 仅 mock 假路径。新增 `tests/test_reflector.py` 8 个真实失败路径用例（client 不可用 / HTTP 异常 / JSON 解析失败 / 非法 enum / 3 次升级 ESCALATE / reset / LRU 淘汰 / singleton）

### Added
- `agent/alembic/versions/c7d8e9f0a1b2_add_decision_log_table.py` — `decision_log` 表（`session_id` / `turn_id` / `customer_id` / `agent_name` / `action` / `reasoning` / `evidence_json` / `latency_ms` / `created_at`，含 3 个复合索引支持 GDPR 删除 + 监管审计）
- `agent/lumio/shared/orm_models.py` — 新增 `DecisionLog` 模型
- `agent/lumio/services/common/decision_log.py` — 新增 `_write_pg()` 后台落库 task（持有引用防 GC，失败软处理降级 Redis-only）
- `agent/lumio/services/common/database.py` — 新增 `init_global_session_factory()` / `get_async_session_factory()` / `close_global_session_factory()`（供非 FastAPI 上下文的后台任务使用）
- `agent/lumio/main.py` — `_safe_init_global_factory()` 启动时挂入 `_COMMON_INIT_STEPS`，PG 不可用时降级不抛
- `agent/lumio/services/bot/prompts/few_shot.py` — 新增 `CoTIntent` 枚举 + `_COT_ALIASES` 别名映射
- `agent/tests/test_reflector.py` — 8 个真实失败路径单元测试

### Changed
- `bot_agent._estimate_tokens` 现为薄包装 `token_utils.estimate_tokens(text, base_overhead=4)`，保留 +4 消息格式开销语义
- `gdpr.GDPRService` 硬删除路径覆盖 `decision_log` 表（与 `dialogue_log` 一起在 `DELETE /api/customer/{id}/data` 中一并清理）

### Tests
- 单元测试：**713 passed / 0 failed / 5 skipped**（`test_reflector` 新增 8 个用例；`test_bot_memory` 因 token_utils 统一后 +4 行为差异已修复）
- 集成测试（18 个）需 Docker 中间件启动，CI 中跑；本地跳过（与本 PR 无关）

## [0.1.0] - 2026-07-28

首次公开发布。

### Added
- **Bot 自助服务**：RAG 混合检索（BM25 + 向量 + RRF 融合）、意图分类（规则快路 + LLM 慢路）、多轮对话记忆、槽位追踪、转人工
- **坐席辅助引擎**：D1/D2/D3 评估器 + E1/E2/E3 执行器并行编排（asyncio.gather + PydanticAI）、展示决策（场景+时间+反馈驱动）、仲裁融合、PII 脱敏
- **知识库管理**：文档上传/分块/双写（ES + Milvus）、FAQ CRUD + 审批工作流、语义去重
- **合规安全**：敏感词过滤、PII 脱敏、审计日志（中间件自动记录）、JWT 认证、请求限流
- **可观测性**：Prometheus 指标、Grafana 仪表盘、JSON 结构化日志（含 PII 脱敏过滤器）
- **运维后台**：Vue 3 + Element Plus 管理界面（文档管理、FAQ 管理、接入监控）
- **一键 Demo**：`make demo` 启动完整栈（含 Ollama 本地大模型降级）
- **压测脚本**：Locust 负载测试（chat send→poll 完整链路）
- **gRPC 服务端**：Classification / Retrieval / SafetyFilter 三个 proto 定义服务的 Python 实现

### Infrastructure
- Docker Compose 编排 16 个中间件（PostgreSQL / Redis / ES+IK / Milvus / Kafka / MinIO / Prometheus / Grafana）
- Alembic 数据库迁移
- GitHub Actions CI（lint / unit tests / Docker build / E2E smoke）
- Apache 2.0 License

[Unreleased]: https://github.com/slowleelab/lumio/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/slowleelab/lumio/releases/tag/v0.1.0
