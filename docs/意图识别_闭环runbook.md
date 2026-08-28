# 意图识别四步闭环 Runbook（采集 → 归因 → 人审 → 优化）

> 版本：v1.0（2026-08-25）
> 关联代码：`trap_collector.py`、`trap_eval.py`、`sample_backflow.py`、`scripts/intent_classifier_spike.py`、`eval_gates.py`、`model_registry.py`、`router.py:/admin/model-registry`
> 状态：**各步骤均有单测覆盖（1802 passed），完整一轮尚未在真实环境跑过**；首次执行时按本 runbook 逐段验收。

闭环的四步是：**感知**（trap 采样）→ **评估/归因**（attribute）→ **人审回流**（staging/approve/merge）→ **优化**（重训 → 四门 → promote）。监管约束：训练数据变更必须人审，不自动重训、不自动上线（银行场景底线）。

---

## 0. 前置条件

- PostgreSQL 可用（`classifier_sample` 表由 alembic 迁移建好）
- 训练机有 torch/transformers（spike 脚本用系统 python3 独立运行，不进 poetry venv）
- 已有一版 active 模型注册在 `data/intent_classification/model_registry.json`
- **快速干跑验证**（无 PG/GPU 亦可）：`poetry run python scripts/closed_loop_dryrun.py --auto-approve` 在临时目录端到端走一遍 归因→精选→人审→并入→四门→promote 门控（`--auto-approve` 仅干跑模拟人审, 真实回流禁止自动批准）。

---

## 1. 感知：trap 采样

**机制**：每次分类结束时 `classifier._emit_sample` 把快照交给 `TrapCollector.capture`，按带宽采样落 PG `classifier_sample` 表；`trap_ambient_rate` 负责随机采样兜底（打破"只采异常"选择偏差）。

**开关**（`data/intent_classification/closed_loop.json`）：

```json
"trap_enabled": true,
"trap_sampling_band": 0.15,
"trap_ambient_rate": 0.02
```

**验收**：`SELECT count(*) FROM classifier_sample WHERE created_at > now() - interval '7 day'` 有持续增量；`TrapCollector.aggregate()` 能看到按意图的样本数与平均置信度（漂移观测消费端：建议 crontab 周期调用）。

## 2. 归因：AttributeEngine 定罪

**机制**：`trap_eval.AttributeEngine.attribute(sample) -> AttributionVerdict`，按层（classification/retrieval/generation）与证据给出 HEALTHY / FAILURE / 等 verdict；只有 **classification 层**的失败样本进回流（其余层需 P3 下游信号定罪，不入回流）。

**验收**：单测 `tests/test_trap_p3.py::TestSelectCandidates`（失败样本入选、healthy 跳过、text+intent 去重）。

## 3. 人审：staging → approve

```bash
cd agent
# 3.1 精选候选并写入人审 staging 文件（文本已 PII 打码）
poetry run python - <<'EOF'
from lumio.services.common.sample_backflow import select_candidates, write_staging
# pairs 来自第 2 步: [(AttribSample, AttributionVerdict), ...]
cands = select_candidates(pairs, max_n=50)
write_staging(cands, "data/intent_classification/backflow_review.jsonl")
EOF

# 3.2 人工逐条批改 backflow_review.jsonl 的 approved: false -> true
# 3.3 批准条目并入 seed_dataset.json（版本自动 +0.0.1, counts.examples 同步递增）
poetry run python - <<'EOF'
from lumio.services.common.sample_backflow import finalize_confirmed
n, ver = finalize_confirmed(
    "data/intent_classification/backflow_review.jsonl",
    "data/intent_classification/seed_dataset.json",
)
print(f"并入 {n} 条, 新版本 {ver}")
EOF
```

**验收**：`seed_dataset.json` 的 `meta.version` 递增、`meta.counts.examples` 与 `examples` 数组长度一致（此前写错 meta 键的 bug 已修，见 `sample_backflow.finalize_confirmed` 注释）。

## 4. 重训：spike 脚本（三路切分 + 早停 + 温度校准）

```bash
cd agent
python3 scripts/intent_classifier_spike.py --epochs 6 --patience 2
```

产出：
- 权重 + `config.json`（含 `id2label`/`label2id`/`temperature`，推理侧自动读取，不再手工同步标签顺序）
- `out_intent_clf/calibration.json`（温度、校准前后 dev NLL、dev 精度）
- 终端打印「规则逐类命中率（建议置信度）」——对照 `classifier.py:_RULES` 的 confidence。**首次标定已于 2026-08-25 应用**（seed v0.3.2 实测：bill .84 / txn .56 / limit .95 / inst .71 / reward .94 / faq .47 / card_loss .56 / complaint .62 / transfer .90 / chitchat .35；标定同时暴露并修掉了 limit 关键词「信用」误伤「信用卡」的歧义、加了平局敏感意图优先）。之后每次种子集扩容/回流并入，由人工对照报告复核是否回写。

**验收**：`calibration.json` 存在且 `dev_nll_calibrated <= dev_nll_temperature_1`；易混淆对（15 对）的模型命中率对比上一版不下降。

## 5. 四门评估：EvalGates

golden（12 内置 + 15 易混淆对 = 27 条）/ sensitive / safety / rag 四门，通过 `model_registry` 的 promote 流程在事件循环内对候选模型执行（`router.py` `/admin/model-registry`）。rag 门未配置 judge 时标记 offline 放行。

**验收**：四门全 PASS 才允许 promote（`EvalGates.all_pass`）。

## 6. promote / rollback：ModelRegistry

```bash
# 状态文件仅存版本指针与裁决记录, 不搬运权重
cat data/intent_classification/model_registry.json

# promote: canary 过四门 -> active（走 /admin/model-registry 端点, 银行场景不自动上线）
# rollback: 保留上一 active 指针, 一键回退
```

**验收**：promote 后 `model_registry.json` 的 active 指针指向新版本路径，gate_report 留痕；回滚演练一次。

## 7. 首次完整跑一轮的验收清单

- [ ] PG `classifier_sample` 有 7 天窗口内样本
- [ ] 归因产出 ≥1 条 classification 层失败候选
- [ ] staging 人审批准并并入 seed（版本 +0.0.1、counts 一致）
- [ ] 重训产出 calibration.json，dev NLL 校准后不劣化
- [ ] 四门全 PASS（golden 27 条含易混淆对）
- [ ] promote 后 active 指针切换；线上分类抽样无回归（decision_log 对账）

---

## 当前欠账（如实记录）

- **漂移监控无消费端**：`aggregate()` 的 Grafana/告警未接。
- **无自动重训触发**：人审并入后不会自动调度 spike——按监管约束保留人工触发，本 runbook 第 4 步即触发点。
- **惊讶度/energy 两通道实测无区分度**（closed_loop.json switch_rationale），保留为观测位，启用前必须重新标定阈值。
