"""全局配置管理

基于 pydantic-settings，支持环境变量覆盖。
所有配置项对应设计文档中的技术选型版本锁定。
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import quote_plus

from pydantic import AliasChoices, BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# 让 .env 对所有子 Settings 生效 (子类各自只有 env_prefix, 未带 env_file, 若不在此
# 全局注入, RAG_/LLM_/ES_ 等前缀的值写入 .env 不会生效, 只读进程环境变量).
# 真实进程环境变量优先级仍高于 .env (load_dotenv 默认 override=False).
# 让 .env 对所有子 Settings 生效 (子类各自只有 env_prefix, 未带 env_file, 若不在此
# 全局注入, RAG_/LLM_/ES_ 等前缀的值写入 .env 不会生效, 只读进程环境变量).
# 真实进程环境变量优先级仍高于 .env (load_dotenv 默认 override=False).
def _load_dotenv_search() -> None:
    """探测并加载仓库 .env 到 os.environ (供所有子 Settings 读取)"""
    try:
        from dotenv import load_dotenv
    except ImportError:  # pragma: no cover - python-dotenv 随 pydantic-settings 安装
        return

    # 默认 load_dotenv() 相对进程 CWD 解析, 而服务从 agent/ 启动, 会漏掉仓库根 .env.
    # 仓库内以根 .env 为准 (与 .env.example 同目录, 唯一 config 源): 即使 cwd 下
    # 存在历史残留的 agent/.env 也不允许其遮蔽根文件. 仅当 cwd 位于仓库外
    # (如自包含的临时测试目录) 才退而使用 cwd/.env.
    repo_root = Path(__file__).resolve().parents[3]
    cwd_resolved = Path.cwd().resolve()
    inside_repo = cwd_resolved == repo_root or repo_root in cwd_resolved.parents
    candidates = (
        (repo_root / ".env", cwd_resolved / ".env") if inside_repo else (cwd_resolved / ".env", repo_root / ".env")
    )
    for candidate in candidates:
        if candidate.is_file():
            load_dotenv(candidate, override=False)
            break

    # 密闭配置文件: 意图分类闭环开关(BERT 快路径 + trap 感知缝)的唯一版本化源.
    # 线上 on/off 随代码入库, 不依赖本机未入库的 .env. 与 .env 同样的 override=False
    # 语义 — 显式进程环境变量(CLS_*)仍可覆盖此文件(作逃生舱/临时关闭).
    _inject_closed_loop_env(repo_root)


# 四步闭环开关: 字段 → 对应 CLS_ 环境变量名 (ClassificationSettings env_prefix=CLS_)
_CLOSED_LOOP_ENV_MAP: dict[str, str] = {
    "bert_enabled": "CLS_BERT_ENABLED",
    "trap_enabled": "CLS_TRAP_ENABLED",
    "trap_sampling_band": "CLS_TRAP_SAMPLING_BAND",
    "trap_ambient_rate": "CLS_TRAP_AMBIENT_RATE",
    "rephrase_guard_enabled": "CLS_REPHRASE_GUARD_ENABLED",
    "ood_enabled": "CLS_OOD_ENABLED",
    "ood_energy_threshold": "CLS_OOD_ENERGY_THRESHOLD",
    "ood_ambiguous_band": "CLS_OOD_AMBIGUOUS_BAND",
    "ood_temperature": "CLS_OOD_TEMPERATURE",
    "llm_arbiter_enabled": "CLS_LLM_ARBITER_ENABLED",
    "noise_gate_enabled": "CLS_NOISE_GATE_ENABLED",
    "anaphora_resolver_enabled": "CLS_ANAPHORA_RESOLVER_ENABLED",
    "anaphora_llm_fallback_enabled": "CLS_ANAPHORA_LLM_FALLBACK_ENABLED",
}


def _inject_closed_loop_env(repo_root: Path) -> None:
    """从已提交的 data/intent_classification/closed_loop.json 注入开关到 os.environ."""
    cfg = repo_root / "agent" / "data" / "intent_classification" / "closed_loop.json"
    if not cfg.is_file():
        return
    try:
        data = json.loads(cfg.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    for field, env_key in _CLOSED_LOOP_ENV_MAP.items():
        # 保留 None 让 .env/默认值生效; 值统一转字符串注入
        if data.get(field) is not None:
            os.environ.setdefault(env_key, str(data[field]))


_load_dotenv_search()


class DatabaseSettings(BaseSettings):
    """PostgreSQL 配置"""

    model_config = SettingsConfigDict(env_prefix="POSTGRES_")

    host: str = "localhost"
    port: int = 5432
    user: str = "lumio"
    password: str = "lumio_pass"
    database: str = "lumio"
    pool_size: int = 5
    max_overflow: int = 10

    @property
    def dsn(self) -> str:
        return f"postgresql+asyncpg://{self.user}:{quote_plus(self.password)}@{self.host}:{self.port}/{self.database}"

    @property
    def sync_dsn(self) -> str:
        return f"postgresql+psycopg2://{self.user}:{quote_plus(self.password)}@{self.host}:{self.port}/{self.database}"


class RedisSettings(BaseSettings):
    """Redis 配置"""

    model_config = SettingsConfigDict(env_prefix="REDIS_")

    host: str = "localhost"
    port: int = 6379
    password: str = ""
    db: int = 0
    max_connections: int = 20

    @property
    def url(self) -> str:
        auth = f":{quote_plus(self.password)}@" if self.password else ""
        return f"redis://{auth}{self.host}:{self.port}/{self.db}"


class ElasticsearchSettings(BaseSettings):
    """Elasticsearch 配置"""

    model_config = SettingsConfigDict(env_prefix="ES_")

    hosts: str = "http://localhost:9200"
    # 开发环境关闭安全认证，以下仅生产环境使用
    username: str = ""
    password: str = ""
    index_prefix: str = "lumio"
    verify_certs: bool = False


class MilvusSettings(BaseSettings):
    """Milvus 配置"""

    model_config = SettingsConfigDict(env_prefix="MILVUS_")

    host: str = "localhost"
    port: int = 19530
    collection_name: str = "lumio_knowledge"
    vector_dim: int = 1024  # bge-large-zh-v1.5 输出维度
    # 向量检索硬超时(秒): Milvus gRPC 通道死亡(容器重启/网络断)时 collection.search
    # 无超时会永久挂起, 且挂在 to_thread 线程里事件循环无法自救 -- 必须客户端兜底,
    # 超时降级为 BM25-only (实测: 死通道连噪声输入也拖到 60s 编排超时, 回复全瘫).
    search_timeout: float = 5.0


class MinIOSettings(BaseSettings):
    """MinIO 配置"""

    model_config = SettingsConfigDict(env_prefix="MINIO_")

    endpoint: str = "localhost:9000"
    access_key: str = "minioadmin"
    secret_key: str = "minioadmin"
    bucket: str = "lumio-docs"
    secure: bool = False


class LLMSettings(BaseSettings):
    """大模型推理配置"""

    model_config = SettingsConfigDict(env_prefix="LLM_")

    # OpenAI 兼容 API（vLLM / Ollama）
    base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"
    primary_model: str = "qwen2.5:7b"
    fallback_model: str = "qwen2.5:7b"
    temperature: float = 0.2
    # 生成输出上限: 实测本地 qwen2.5:7b RAG 答复多为 60~90 token, 2048 上限
    # 不收益且给解码预留过大解码预算; 512 有 6x 余量, 并限制异常长解码拖慢一轮。
    # 生成输出上限: M1 decode 26 tok/s, 每 token 都是真金白银的秒数 — 中文回复
    # 已限 100 字 (~150-170 tok), 192 留格式余量, 相比 256 省 ~2.5s/轮
    max_tokens: int = 192
    timeout_seconds: float = 60.0

    # 健康探测
    health_probe_interval_seconds: float = 1.0  # 初始探测间隔
    health_probe_max_interval: float = 30.0  # 指数退避上限
    health_probe_timeout: float = 5.0  # 探测超时
    health_probe_fail_threshold: int = 2  # 连续失败降级阈值
    health_probe_success_threshold: int = 2  # 连续成功恢复阈值
    # 各类独立超时
    # 分类独立超时 (LLM 慢路径硬总时长): 既约束慢路径 LLM 分类最坏等待, 又留足正常
    # 分类余量. 此前 1.5s 只是透传 SDK per-read 超时(对流式永不触发), 实为死配置;
    # 现由 classifier 的 asyncio.wait_for 强制总 deadline. 3.0s 在本地 Ollama 被
    # LLM 对话/嵌入抢占 GPU 时高频超时 (2026-08-30 实测: 149 意图长 prompt 连续
    # 多轮 >3s → faq@0.0 → 低置信误澄清), 抬到 6s: 慢而成功的分类一次走完,
    # 仍由 wait_for 封顶, 超时兜底 FAQ@0.0 → 下游 low_conf 闸回澄清不变.
    classify_timeout: float = 6.0
    # 模块 A 归因 judge 模型 — 固定 GLM-5.3-Flash (方案 §9.3 跨家族选型:
    # 生成 qwen / 裁判 GLM, 消除自我偏好偏差)。远程不可达时的本地兜底
    # 走 primary_model (本地无 GLM 权重), 审计标记"回退本地"。
    judge_model: str = "GLM-5.3-Flash"
    # 跨家族远程裁判 (Anthropic Messages 协议, 如 GLM coding plan):
    # 留空则用本地 base_url 的 primary_model; 配置后归因走远程, 失败自动回退本地。
    judge_base_url: str = ""
    judge_api_key: str = ""
    judge_timeout: float = 90.0
    # 严格模式: 远程裁判失败重试耗尽后直接判该条失败, 绝不回退本地 (全程纯 GLM
    # 审计); 默认关 = 失败回退本地兜底, 闭环不断粮
    judge_strict: bool = False
    # 慢路径分类结果缓存: 相同输入(TTL 内)复用上次分类, 免二次 LLM 分类调用 (#7 降本
    # 第一段; 完整"分类+生成合一"需改造生成链路, 见 docs/意图识别_优化方案.md)。
    classify_cache_enabled: bool = True
    classify_cache_ttl_seconds: int = 300
    classify_cache_max_entries: int = 512
    # 生成独立超时: 15s 对热机 qwen2.5:7b 足够, 但在健康探测或其他渲染抢占 GPU 时,
    # 慢而成功的调用可能顶破 15s → 触发"超时→重试"把一轮翻倍 (实测 24.5s 尖峰)。
    # 抬到 20s: 让慢一点但能成功的单次调用一次走完(20s<25s 长轮询窗口), 避免重试翻倍。
    generate_timeout: float = 20.0

    # ── A0: KV Cache 优化配置 ──
    # 三层 cache 控制
    kv_cache_enabled: bool = True
    static_prefix_anchor: bool = True  # L1: 静态前缀锚定
    layered_injection: bool = True  # L2: 状态分层注入
    # 推理引擎: ollama (0% prefix cache) / vllm (PagedAttention) / tgi
    inference_engine: str = "ollama"
    # 长上下文窗口配置 (A7)
    max_context_tokens: int = 8192
    reserved_tokens: int = 1024  # 留给 LLM 输出
    # 分层 token 预算 (A2)
    budget_static: int = 800  # 稳态层
    budget_customer: int = 400  # 半稳态层 (客户画像)
    # 动态层 (RAG 检索): 1200→600. 实测此类问答 prompt 中 RAG 检索内容占 ~1067/1506
    # token, 是 prefill (~220tok/s) 的头号贡献者; 单轮只需保留 top 1-2 相关 chunk,
    # 600 预算即够, prefill 显著下降. 检索端已按此预算截断, 不会多取。
    budget_rag: int = 600  # 动态层 (RAG 检索)
    budget_history: int = 1500  # 动态层 (历史)
    budget_current: int = 200  # 动态层 (当前轮)


class CompressionSettings(BaseSettings):
    """A1: 上下文压缩配置"""

    model_config = SettingsConfigDict(env_prefix="COMPRESS_")

    # 算法: llmlingua / selective / semantic
    algorithm: str = "selective"
    # 目标压缩比 (original/compressed), e.g. 5.0 表示压到 1/5
    target_ratio: float = 4.0
    # 质量下限 (1-5, 低于此值回退到原文)
    min_quality_score: float = 3.5
    # 启用开关 (灰度用)
    enabled: bool = True
    # Selective Context: 保留多少信息量
    preserve_ratio: float = 0.35
    # 触发压缩的历史长度阈值 (轮数)
    min_history_turns: int = 8


class BudgetSettings(BaseSettings):
    """E0: LLM Token 成本预算配置"""

    model_config = SettingsConfigDict(env_prefix="BUDGET_")

    # 全局月度预算（美元）
    monthly_budget_usd: float = 5000.0
    # 单租户日预算（美元）
    per_tenant_daily_limit_usd: float = 100.0
    # 模型单价 (USD per 1M tokens)
    cost_per_1m_input_tokens: dict[str, float] = Field(
        default_factory=lambda: {
            "qwen2.5:7b": 0.18,
            "qwen2.5:72b": 1.80,
            "deepseek-v3": 0.27,
            "gpt-4o": 5.00,
        }
    )
    cost_per_1m_output_tokens: dict[str, float] = Field(
        default_factory=lambda: {
            "qwen2.5:7b": 0.18,
            "qwen2.5:72b": 1.80,
            "deepseek-v3": 1.10,
            "gpt-4o": 15.00,
        }
    )


class GuardrailSettings(BaseSettings):
    """A3-A5: 提示词注入防御配置"""

    model_config = SettingsConfigDict(env_prefix="GUARD_")

    # User Input 层 (A3)
    user_input_enabled: bool = True
    user_input_layer1_regex: bool = True
    user_input_layer2_role_confusion: bool = True
    user_input_layer3_guard_llm: bool = False  # 默认关, 灰度开
    # RAG 检索内容层 (A4)
    rag_content_sanitizer_enabled: bool = True
    rag_suspicious_threshold: float = 0.6
    # Tool 返回结果层 (A5)
    tool_result_sanitizer_enabled: bool = True
    tool_result_max_size_bytes: int = 4096
    # 实体池白名单 (A6) - 仅这些 entity_type 可跨会话带入
    # P2-1: 双源一致性 - 同步 lumio.shared.entity_sandbox.DEFAULT_ENTITY_ALLOWLIST
    # 之前 entity_sandbox 有 7 个默认白名单, config 仅有 5 个, 不同步会导致环境变量未覆盖时
    # config 实际生效白名单仅 5 个, 但 entity_sandbox 代码侧按 7 个处理, 行为不一致.
    entity_pool_allowlist: list[str] = Field(
        default_factory=lambda: [
            "card_type",  # 卡种 (VISA/银联)
            "vip_level",  # VIP 等级
            "risk_tolerance",  # 风险偏好 R1-R4
            "city",  # 城市
            "occupation",  # 职业
            "age_range",  # 年龄段
            "product_interest",  # 产品兴趣
        ]
    )


class PromptSettings(BaseSettings):
    """B0: 提示词注册中心配置"""

    model_config = SettingsConfigDict(env_prefix="PROMPT_")

    # Nacos 配置中心地址
    nacos_server_addr: str = "localhost:8848"
    nacos_namespace: str = "lumio"
    # Redis 缓存 TTL
    cache_ttl_seconds: int = 60
    # 本地兜底: 离线时使用 prompts.py 硬编码常量
    fallback_to_local: bool = True
    # 启动时打印当前生效版本
    log_active_version: bool = True


class ExperimentSettings(BaseSettings):
    """B5: A/B 实验框架配置"""

    model_config = SettingsConfigDict(env_prefix="EXPERIMENT_")

    # 粘性分桶开关
    enabled: bool = True
    # Redis 持久化 key TTL (秒)
    bucket_ttl_seconds: int = 86400 * 30  # 30 天
    # 最小样本量 (统计显著性检验)
    min_sample_size: int = 1000
    # 显著性水平 (p-value 阈值)
    p_value_threshold: float = 0.05


class ClassificationSettings(BaseSettings):
    """分类模型配置"""

    model_config = SettingsConfigDict(env_prefix="CLS_")

    # 意图分类置信度阈值
    intent_threshold: float = 0.6
    # 实体抽取
    min_entity_confidence: float = 0.7
    # 轻量 BERT 意图分类器 (torch 推理快路径): 开启后取代规则快路径, 置信度不足仍回退 LLM
    bert_enabled: bool = False
    # 已微调 BERT 模型目录 (相对路径以 agent/ 为基准; 权重不入 git, 部署需随服携带)
    bert_model_path: str = "data/intent_classification/out_intent_clf"

    # ── 闭环 P1 感知缝 (TrapCollector) ──
    # 采样: 走慢路径/兜底、贴近决策边界、规则-BERT 分歧 + 固定小概率随机的样本落库
    trap_enabled: bool = False
    # 有界留存: 超过该天数自动清档
    trap_retention_days: int = 90
    # 贴近决策边界的带宽 (abs(final_confidence - threshold) < band 即采样)
    trap_sampling_band: float = 0.15
    # 环境随机采样率 (打破"只采异常"的选择偏差, 支撑漂移检测)
    trap_ambient_rate: float = 0.02

    # ── 多轮噪声/闲聊治理 (P0) ──
    # 噪声门先判"是否在回上文话"(填上缺的必填槽/纯数字/确认词)再判噪声的开关.
    # 关闭 = 旧行为(只看当前句); 开启 = 回话放行, 防"上轮问卡号/金额, 本句答数字"被误判噪声.
    # 灰度: 先在 staging 开 + 用 decision_log 的 NOISE_BLOCKED 对账真实误伤, 再上生产.
    # 由 closed_loop.json 同源注入(见 _CLOSED_LOOP_ENV_MAP)。
    rephrase_guard_enabled: bool = False

    # ── 闭环 P1 提纯"认不认": energy-OOD + 温度校准 + LLM 模糊仲裁 ──
    # energy ("不认"通道): -logsumexp(logits) —— 封闭意图集外输入该值显著更"负"(分散).
    # BERT 快路径产生该分数; LLM 慢路径/无 torch 环境不产生, 由上层走置信度 + 更多信号.
    # 实测标注 (2026-08, closed_loop.json switch_rationale): 乱码样本 energy 反而更低
    # (-2.55 vs 阈值 -3.4) 被判"认得", 无区分度 → 默认关, 只作观测位; 开启前必须在
    # 部署环境用真实噪声/真实业务样本重新标定阈值, 否则是"假开关"。
    # 目标架构 L2: 向量检索意图 (规则未命中 → 种子余弦检索 → 置信不足才 LLM)
    vector_intent_enabled: bool = True
    # 五域阈值: 0.72 采信判对的 consulting (会话 2b3b2613 根治); 定义句式另由
    # domain_of_with_text 强制咨询域兜底。误判风险由下游 grounding 门把关。
    vector_intent_threshold: float = 0.72
    # OOD 落地闭环 (2026-09-02 校准, scripts/calibrate_ood_threshold.py):
    # ID(seed 191 例).p99=-2.913 / OOD(badcase 实录 27 例).p5=-3.493, 分布部分重叠
    # → 能量门一律双信号使用 (意图+能量), 单信号处已收窄 (噪声门 strong_business 放行)。
    # 阈值 = ID.p99 与 OOD.p5 中点: ID 误杀 4.2%, OOD 捕获 88.9%。换模型/扩种子集后重跑校准。
    ood_enabled: bool = True  # 开关: 用 energy 分替代裸 softmax 置信作"认不认"闸
    ood_energy_threshold: float = -3.203
    # 温度缩放: logits / temperature 后再 softmax. 1.0 = 不缩放. 用验证集一维标定,
    # 把置信度校准到与准确率匹配(>1 更保守、压低过拟合 BERT 的虚高置信).
    ood_temperature: float = 1.0
    # 模糊带宽仲裁: 多信号落在"模糊带"(energy/BERT 置信中段、无铁证)时, 把原文交 LLM
    # 判 {business|chitchat|noise} 与证据链投票. LLM 结果不单独作通过依据(弱信号).
    llm_arbiter_enabled: bool = False
    # LLM 仲裁输入也走 BERT 快路径时的 energy 模糊带宽 (|energy - 阈值| 内即模糊)
    ood_ambiguous_band: float = 1.0

    # ── 闭环 P2 中英多信号噪声闸 (InputGate) ──
    # 打开后由 InputGate 在噪声门内额外做惊讶度(中/英)多信号投票; 默认关。
    # 实测标注 (2026-08, closed_loop.json switch_rationale): 惊讶度无区分度
    # (乱码 7.2-8.3 vs 真实 6.9-8.3 区间重叠), 只作观测/框架预留, 不建议开启;
    # 未来若要启用需先回流标定分段阈值。
    noise_gate_enabled: bool = False

    # ── 多轮指代消解 (回指落实体) ──
    # 规则预扫层: 当前句命中回指(这/那张卡/那笔)时, 从历史实体池取"唯一候选"解析进实体.
    # 唯一性约束保证安全性: 候选 0 个或多于 1 个时规则层绝不乱猜. 由 closed_loop.json 同源注入.
    anaphora_resolver_enabled: bool = False
    # LLM 兜底层: 仅当规则层判定"存在回指但候选不唯一/无法确定"时按此开关触发, 供多候选难例.
    # 默认关; 与规则层独立, 需单独灰度.
    anaphora_llm_fallback_enabled: bool = False

    # ── 闭环 P3 版本化优化 ──
    # 模型注册表状态文件 (相对 agent/ 解析; 仅存版本指针, 不存权重)
    model_registry_path: str = "data/intent_classification/model_registry.json"
    # 样本回流人审 staging 文件
    backflow_review_path: str = "data/intent_classification/backflow_review.jsonl"
    # 回流并入的种子数据集
    seed_dataset_path: str = "data/intent_classification/seed_dataset.json"


class RAGSettings(BaseSettings):
    """RAG 检索配置"""

    model_config = SettingsConfigDict(env_prefix="RAG_")

    # 检索参数
    top_k: int = 5
    rerank: bool = True
    # RRF 融合参数
    rrf_k: int = 60
    # RRF 置信度阈值（RRF 分数范围 ~0.005-0.05，远低于 Reranker 的 0-1）
    rrf_confidence_threshold: float = 0.0
    # Reranker 置信度阈值（Reranker 分数范围 0-1）
    confidence_threshold: float = 0.5
    # 词法证据门 (P1): ES 已应答但 BM25 零命中(没有任何 token 匹配) -> 判定无相关知识,
    # 不把向量召回(实测无区分度: 乱码 0.55-0.63 / 真实 0.52-0.79 重叠)的文档喂 LLM.
    # 注意: 绝对分数下限被实测证伪不可用 -- 真实短查询'年费'@0.92 与乱码'额佛呢份'@2.77
    # 区间重叠, BM25 分数混合了查询词特异度与相关性, 无一刀切阈值; 零命中是唯一无歧义信号.
    # reranker 分数生效时(生产 TEI)由 confidence_threshold 主导, 本门不拦.
    require_lexical_evidence: bool = True
    # Embedding 模型
    embedding_provider: str = "ollama"  # ollama / tei
    embedding_model: str = "mxbai-embed-large"  # Ollama 开发环境模型
    tei_embedding_model: str = "BAAI/bge-M3"  # TEI 生产环境模型
    embedding_dim: int = 1024
    embedding_batch_size: int = 128  # TEI 批量大小
    tei_base_url: str = "http://localhost:8080"  # TEI 服务地址
    # 摄入批量向量化受 GPU 抢占影响大, 10s 在本地 Ollama + LLM 并发时偶发超时
    embedding_timeout: float = 30.0  # 嵌入请求超时（秒）
    # 2026-08-31: embed/reranker 固定 CPU (num_gpu=0), qwen 独占 GPU —— 统一内存
    # 带宽争抢导致分类/生成 7~10s 抖动; 小模型 CPU 推理延迟可接受 (检索场景不敏感)
    embedding_num_gpu: int = 0
    rerank_num_gpu: int = 0
    embedding_max_retries: int = 2  # 最大重试次数
    # 分块参数
    chunk_size: int = 1500  # 字符数，约 750 中文字 ≈ 1000+ tokens
    chunk_overlap: int = 200  # 字符数
    # 重排模型
    reranker_model: str = "bge-reranker-v2-m3"
    reranker_provider: str = "ollama"  # ollama / tei
    # 入库锁
    ingestion_lock_ttl: int = 600  # 分布式锁 TTL（秒）


class SafetySettings(BaseSettings):
    """安全过滤配置"""

    model_config = SettingsConfigDict(env_prefix="SAFETY_")

    # 敏感词文件路径
    sensitive_words_path: str = "config/sensitive_words.txt"
    # 脱敏规则
    enable_phone_mask: bool = True
    enable_id_card_mask: bool = True
    enable_bank_card_mask: bool = True


class SessionSettings(BaseSettings):
    """会话状态配置"""

    # 诉求跟踪 (2026-09-04 多轮会话管理): 断档(紧急诉求切话题蒸发)与
    # 带偏(旧话题影响新轮)的同根源修复。关闭即完全回滚旧行为。
    topic_tracking_enabled: bool = True
    # 同一高紧急诉求的最大回访次数 (防骚扰)
    topic_revisit_max: int = 2

    model_config = SettingsConfigDict(env_prefix="SESSION_")

    # Redis key 前缀
    key_prefix: str = "session:"
    # 会话 TTL（秒）
    ttl_seconds: int = 1800  # 30 分钟
    # 对话历史窗口大小
    max_turns: int = 20
    # 低置信度连续计数阈值（L3 触发）
    low_confidence_threshold: int = 3
    # 超时配置（秒）
    bot_idle_timeout: int = 180  # BOT 阶段空闲超时 (银行客户可能边查资料边聊, 2 分钟易误杀)
    queue_timeout: int = 60  # AG_QUEUED 排队超时（超时回退 BOT）
    ringing_timeout: int = 30  # AG_ASSIGNED 振铃超时
    session_timeout: int = 1800  # AG_ACTIVE 会话总时长超时
    review_timeout: int = 120  # AG_REVIEWING 话后小结超时


class BotSettings(BaseSettings):
    """Bot 服务配置"""

    model_config = SettingsConfigDict(env_prefix="BOT_")

    # 最大并发 Agent 数（Semaphore 槽位）
    max_concurrent_agents: int = 10
    # 消息过期时间（秒），超过此时间的消息跳过处理
    message_ttl_seconds: int = 8
    # fast_reply 冷却时间（秒），同一会话两次 fast_reply 的最小间隔
    fast_reply_cooldown: int = 5
    # 单会话在途消息队列深度上限。防止单会话短时灌入大量消息 → 无界内存队列;
    # 队列满时消息留在 Redis Stream(PEL), 由 XAUTOCLAIM/TTL 后续重投或超时兜底.
    max_session_queue: int = 20
    # P2-16: 同一客户同时进行的活跃会话数上限 (多设备/多标签页防资源耗尽)
    max_sessions_per_customer: int = 3


class AssistSettings(BaseSettings):
    """坐席辅助配置"""

    model_config = SettingsConfigDict(env_prefix="ASSIST_")

    # 分支超时（毫秒）
    script_timeout_ms: int = 500
    knowledge_timeout_ms: int = 600
    alert_timeout_ms: int = 300
    product_timeout_ms: int = 400

    # 推送节流
    throttle_window_ms: int = 800

    # 话术
    polish_model: str = "qwen2.5:7b"
    script_cache_ttl: int = 300  # Redis 缓存秒数
    max_scripts_per_push: int = 3

    # 知识
    max_knowledge_per_push: int = 3

    # 情绪趋势
    sentiment_trend_window: int = 3  # 连续负面轮数触发升级

    # 产品
    max_recommendations_per_push: int = 2


class OrchestrationSettings(BaseSettings):
    """编排层配置（对应设计文档 §3.3）"""

    model_config = SettingsConfigDict(env_prefix="ORCH_")

    # 评估器冷却轮数（对应文档 §3.3 三路评估器表）
    d1_cooldown_turns: int = 2
    d2_cooldown_turns: int = 5
    d3_always_active: bool = True

    # 评估器激活阈值
    d1_intent_confidence_threshold: float = 0.5
    d2_emotion_score_threshold: float = 0.3

    # 全局超时（对应文档 §3.5 仲裁超时兜底）
    # 5s 对 RAG 自问答过紧: 仅 ES/Milvus 检索 + embed + rerank 就约 4s, 加 LLM 生成必超标导致"回复超时"。
    # 抬到 20s, 介于前端 25s 长轮询超时之内留有余量, 典型问答仍数秒返回, 该值仅作最坏情况兜底。
    global_timeout_ms: int = 20000

    # 执行器 SLA（对应文档 §3.4 执行器 SLA 表）
    e1_sla_ms: int = 3000  # AI 服务
    e2_sla_ms: int = 500  # 营销
    e3_sla_ms: int = 100  # 风控

    # 营销延迟（对应文档 §3.3 策略: marketing_deferred）
    marketing_defer_ms: int = 500

    # PushTracker 基础推送间隔（秒）
    base_interval_ai: float = 3.0
    base_interval_marketing: float = 30.0

    # 动态间隔调整系数（坐席反馈驱动）
    adoption_shorten_ratio: float = 0.5  # 连续采纳3次→间隔×0.5
    dismiss_extend_ratio: float = 2.0  # 坐席关闭→间隔×2.0
    ignore_extend_ratio: float = 1.5  # 连续忽略3次→间隔×1.5


class CircuitBreakerConfigSettings(BaseSettings):
    """熔断器配置（各执行器独立，对应文档 §3.4 熔断器配置表）"""

    model_config = SettingsConfigDict(env_prefix="CB_")

    # AI 服务执行器
    ai_failure_rate_threshold: float = 0.5
    ai_slow_call_rate_threshold: float = 0.6
    ai_slow_call_duration_ms: int = 3000
    ai_wait_duration_open_s: float = 30.0
    ai_half_open_max_calls: int = 3
    ai_sliding_window_size: int = 20

    # 营销执行器
    mkt_failure_rate_threshold: float = 0.5
    mkt_slow_call_rate_threshold: float = 0.5
    mkt_slow_call_duration_ms: int = 500
    mkt_wait_duration_open_s: float = 20.0
    mkt_sliding_window_size: int = 20

    # 风控执行器
    risk_failure_rate_threshold: float = 0.3
    risk_slow_call_rate_threshold: float = 0.3
    risk_slow_call_duration_ms: int = 100
    risk_wait_duration_open_s: float = 10.0
    risk_sliding_window_size: int = 20


class ObservabilitySettings(BaseSettings):
    """可观测性配置：链路追踪与 OTLP 导出

    命名空间: 主用 ``OBSERVABILITY_*`` (项目统一风格)，同时支持旧名
    ``LUMIO_TRACING_ENABLED`` / ``JAEGER_HOST`` (向后兼容 conftest / docker-compose
    已注入的环境变量，避免破坏现有部署).
    """

    model_config = SettingsConfigDict(
        env_prefix="OBSERVABILITY_",
        extra="ignore",
    )

    # Python 链路追踪总开关. AliasChoices 保证旧 LUMIO_TRACING_ENABLED 仍生效.
    tracing_enabled: bool = Field(
        default=True,
        validation_alias=AliasChoices(
            "OBSERVABILITY_TRACING_ENABLED",
            "LUMIO_TRACING_ENABLED",
        ),
    )
    # OTLP 追踪后端主机. 旧 JAEGER_HOST 仍生效.
    jaeger_host: str = Field(
        default="localhost",
        validation_alias=AliasChoices(
            "OBSERVABILITY_JAEGER_HOST",
            "JAEGER_HOST",
        ),
    )
    # 可选: 显式 OTLP endpoint (含 path). 为空时按 jaeger_host 拼
    # http://{jaeger_host}:4318/v1/traces
    otlp_endpoint: str | None = None
    # 追踪采样率 (0.0~1.0). 默认 1.0 (全采样, dev 友好).
    # prod 建议 0.1; 用 ParentBasedTraceIdRatio 包装保证跨服务 trace 不被切断.
    sampling_ratio: float = Field(
        default=1.0,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices(
            "OBSERVABILITY_SAMPLING_RATIO",
            "LUMIO_TRACING_SAMPLE",  # 与 Java 端 MCP_TRACING_SAMPLE 命名习惯一致
        ),
    )


class MCPBackend(BaseModel):
    """路由模式下的单个 MCP 后端定义

    用于「多 server」拓扑：host（MCPToolClient）连接多个后端，合并各自工具目录，
    按域前缀命名空间避免撞名，并按名字分发调用。默认 ``MCPSettings.backends=[]`` →
    退回单后端（``endpoint``、``prefix=""``），名字与 schema 契约不变（零回归）。
    """

    # 后端逻辑名（用于分发索引与日志，不进入 host-facing 工具名）
    name: str
    # 后端 MCP 入口 URL（streamable-http 或 SSE 基址）
    endpoint: str
    # 传输协议: streamable-http(默认, 经 Higress 网关) | sse(直连 SSE 后端, 如 Java MCP Server)
    transport: str = "streamable-http"
    # host-facing 工具名前缀（域命名空间，如 "card."）；单后端留空即原名
    prefix: str = ""
    # 该后端的敏感工具原名白名单（与工具注解、全局 sensitive_tools 取并集）
    sensitive_tools: list[str] = Field(default_factory=list)


class MCPSettings(BaseSettings):
    """MCP 工具层配置（经 Higress AI 网关调用受治理工具）

    默认 ``enabled=False``：编排大脑行为与现状完全一致（零回归）。
    """

    # 敏感工具"核验+确认"两段式总开关 (2026-09-03 产品决策: 默认审核核实视为
    # 已通过 — 关闭时敏感写工具直接执行, 不走核验弹框/文本确认; 生产按合规
    # 要求置 MCP_SENSITIVE_CONFIRM_ENABLED=true 恢复完整核验链)
    sensitive_confirm_enabled: bool = False

    model_config = SettingsConfigDict(env_prefix="MCP_")

    # 总开关：关闭时不加载任何工具，bot 走原有 RAG/LLM 生成路径
    enabled: bool = False
    # 单后端传输协议: sse(默认, 直连 Java MCP Server :8090) | streamable-http(经 Higress)
    # 2026-09-03 定向: MCP 不过网关, 默认直连 —— Higress 链路多一跳且非必需,
    # 网关仅在生产需要统一治理 (鉴权/限流/脱敏) 时按 gateway profile 选配启用。
    transport: str = "sse"
    # Java MCP Server SSE 端点 (22 个信用卡工具, mock 数据)。生产接真实核心系统时
    # 换端点即可; 若需经 Higress 治理, transport 改 streamable-http 并指向网关入口。
    endpoint: str = "http://127.0.0.1:8090/sse"
    # 工具调用超时（秒）
    timeout_seconds: float = 10.0
    # ── 路由模式：多 MCP 后端（host 侧合并 + 分发）──
    # 默认空 → 单后端（用 endpoint、空前缀、名字契约不变，零回归）。
    # 非空时，host 连接每个后端、按 prefix 生成域命名空间工具名并合并目录，
    # 按 name→(server, raw_name) 分发调用；某后端失败仅其工具缺席，不整体报错。
    # 可经环境变量 MCP_BACKENDS 以 JSON 数组配置。
    backends: list[MCPBackend] = Field(default_factory=list)
    # 敏感工具白名单（与工具自身注解取并集，命中即需用户确认）
    sensitive_tools: list[str] = Field(default_factory=list)
    # 工具调用循环最大轮数（防死循环）
    max_tool_iterations: int = 5
    # 工具循环内每次 LLM 调用的超时（秒）。chat_with_tools 未显式传 timeout 时
    # 会回落 OpenAI 客户端默认 60s，一次慢调用即能吃掉外层 20s 编排预算 → 强制短超时。
    tool_loop_llm_timeout_seconds: float = 15.0
    # 整个工具循环的整体预算（毫秒）。多轮工具调用串联时必须有一个总上限，
    # 防止第 2/3/4 轮调用把请求拖过外层全局超时。默认小于 global_timeout_ms(20s)。
    tool_loop_timeout_ms: int = 15000
    # 待确认操作（pending_action）过期时间（秒）
    confirmation_ttl_seconds: int = 300
    # 确认窗口内无法判定（unclear）连续次数上限, 达到后自动取消 pending 并放行新消息
    # (业务: 用户确认期间发新问题不能被无限吞掉, 需逃生路径)
    unclear_auto_cancel_threshold: int = 3

    # ── 工具护栏（Python 侧纵深防御，与 Higress 治理互补）──
    # 按角色的工具授权白名单：{role: [tool, ...]}。
    # 默认空 → 不做授权限制（零回归）；一旦配置非空，未列出的角色/工具将被拒绝。
    tool_role_allowlist: dict[str, list[str]] = Field(default_factory=dict)
    # 敏感工具入参额度上限：{tool_name: max_amount}。默认空 → 不做额度校验。
    tool_amount_limits: dict[str, float] = Field(default_factory=dict)
    # 视为「金额」的入参键名，命中任一且超过对应上限即拒绝。
    amount_arg_keys: list[str] = Field(default_factory=lambda: ["amount", "target_limit", "target_amount", "limit"])

    # ── 渐进式工具暴露（Progressive Disclosure）──
    # 开关：默认关 → bot 业务路由与工具暴露完全同现状（零回归）。
    # 开启后，命中「查询类工具意图」且置信度达标时，仅向 LLM 暴露该意图对应的工具子集，
    # 降低多工具（22+）场景下的选择噪声与 token 开销；未命中/低置信时回落全量或知识问答。
    progressive_disclosure_enabled: bool = False
    # 渐进式暴露的置信度阈值：意图置信度 >= 此值才裁剪到子集，否则暴露全量交 LLM 判断。
    pd_confidence_threshold: float = 0.7
    # 意图 → 工具子集映射：{intent_label: [tool_name, ...]}。
    # 键为 IntentLabel 值（如 "bill_query"）；值为工具名（单后端时即工具原名，
    # 多后端路由模式下为带域前缀全名，见 MCPToolClient）。默认给出五类查询意图的常用子集。
    intent_tool_map: dict[str, list[str]] = Field(
        default_factory=lambda: {
            "bill_query": ["query_card_bill", "query_bill_detail", "query_annual_fee", "repay_credit_card"],
            "transaction_query": ["query_transactions", "report_transaction_dispute"],
            "limit_query": [
                "query_credit_limit",
                "query_limit_adjust_history",
                "adjust_temp_credit_limit",
                "apply_permanent_limit",
            ],
            "installment_inquiry": [
                "query_installment_offer",
                "query_installment_status",
                "apply_bill_installment",
                "cancel_installment",
            ],
            "reward_query": ["query_points", "query_card_benefits", "redeem_points"],
            # 挂失: 意图→工具唯一确定, 高置信时分派层直连 (跳过 LLM 编排循环)
            "card_loss": ["report_card_lost"],
        }
    )


class Settings(BaseSettings):
    """全局配置根"""

    model_config = SettingsConfigDict(
        env_prefix="LUMIO_",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # 服务标识
    service_name: str = "lumio"
    environment: str = "development"
    debug: bool = False
    log_level: str = "INFO"
    service_host: str = "127.0.0.1"

    # P0-3 整改: dev 旁路认证 (无 token 直接返回 admin) 必须显式开启
    # 默认 False — dev 环境不自动放行, 避免 0.0.0.0 绑定 + 漏配 LUMIO_ENVIRONMENT
    # 导致远端部署裸奔. 显式设 LUMIO_DEV_AUTH_BYPASS=true 才启用.
    dev_auth_bypass: bool = False

    # chat-svc 客户端
    chat_svc_url: str = "http://localhost:8080"

    # CORS
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173"])

    # 认证（JWT）
    jwt_secret: str = "lumio-dev-secret-change-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 30  # access token 30 分钟
    jwt_refresh_expire_days: int = 7  # refresh token 7 天

    @model_validator(mode="after")
    def _validate_production_security(self) -> Settings:
        """生产环境安全检查: 拒绝使用默认 JWT 密钥 / 弱密钥启动.

        任何环境都禁止使用占位密钥 ('lumio-dev-secret-change-in-production')
        和 <CHANGE_ME> 占位符 — 这是 P0-2 整改: 漏配 LUMIO_ENVIRONMENT
        不再让默认密钥静默通过.
        """
        # 永远禁止: 占位密钥 (历史 dev 默认 + P0-1 新占位符)
        forbidden_secrets = {
            "lumio-dev-secret-change-in-production",
            "<CHANGE_ME>",
            "<CHANGE_ME_IF_NEEDED>",
        }
        if self.jwt_secret in forbidden_secrets:
            if self.environment == "production":
                raise ValueError("生产环境必须设置 LUMIO_JWT_SECRET 环境变量, " f"禁止占位密钥 ({self.jwt_secret!r})")
            # dev/test 也不放过, 改 WARNING, 不阻断启动 (兼容现有 dev flow)
            import logging

            logging.getLogger(__name__).warning(
                "⚠️  JWT 密钥仍为占位值 %r, 部署到任何对外环境前必改. "
                "建议: export LUMIO_JWT_SECRET=$(openssl rand -hex 32)",
                self.jwt_secret,
            )
        # 长度检查仅在生产强制
        if self.environment == "production" and self.jwt_secret and len(self.jwt_secret) < 32:
            raise ValueError("生产环境 JWT 密钥长度必须 >= 32 字符")
        # P0-5 第三轮修复: 生产环境外部服务凭据默认值强制校验
        # 此前仅校验 JWT, LLM API key 默认 "ollama" / MinIO minioadmin 漏配直接可用.
        if self.environment == "production":
            if self.llm.api_key in ("", "ollama"):
                raise ValueError(
                    "生产环境必须设置 LLM_API_KEY (默认占位 'ollama' 禁止在生产使用, " "漏配会用假凭证直连外部模型服务)"
                )
            if self.minio.access_key in ("", "minioadmin") or self.minio.secret_key in ("", "minioadmin"):
                raise ValueError("生产环境必须设置 MINIO_ACCESS_KEY / MINIO_SECRET_KEY (禁止默认 minioadmin)")
            if not self.elasticsearch.username or not self.elasticsearch.password:
                raise ValueError("生产环境必须设置 ES_USERNAME / ES_PASSWORD (禁止匿名连接)")
            if not self.redis.password:
                raise ValueError("生产环境必须设置 REDIS_PASSWORD (禁止无密码 Redis)")
        return self

    # 限流
    rate_limit_enabled: bool = True
    rate_limit_default: str = "60/minute"
    rate_limit_chat: str = "30/minute"

    # 子配置
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    elasticsearch: ElasticsearchSettings = Field(default_factory=ElasticsearchSettings)
    milvus: MilvusSettings = Field(default_factory=MilvusSettings)
    minio: MinIOSettings = Field(default_factory=MinIOSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    classification: ClassificationSettings = Field(default_factory=ClassificationSettings)
    rag: RAGSettings = Field(default_factory=RAGSettings)
    safety: SafetySettings = Field(default_factory=SafetySettings)
    bot: BotSettings = Field(default_factory=BotSettings)
    session: SessionSettings = Field(default_factory=SessionSettings)
    assist: AssistSettings = Field(default_factory=AssistSettings)
    orchestration: OrchestrationSettings = Field(default_factory=OrchestrationSettings)
    circuit_breaker: CircuitBreakerConfigSettings = Field(default_factory=CircuitBreakerConfigSettings)
    mcp: MCPSettings = Field(default_factory=MCPSettings)
    observability: ObservabilitySettings = Field(default_factory=ObservabilitySettings)
    # Sprint A+ P0 增强配置
    compression: CompressionSettings = Field(default_factory=CompressionSettings)
    budget: BudgetSettings = Field(default_factory=BudgetSettings)
    guard: GuardrailSettings = Field(default_factory=GuardrailSettings)
    prompt: PromptSettings = Field(default_factory=PromptSettings)
    experiment: ExperimentSettings = Field(default_factory=ExperimentSettings)


@lru_cache
def get_settings() -> Settings:
    """获取全局配置单例"""
    return Settings()
