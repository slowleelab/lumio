# 意图分类种子数据集 — 银行信用卡客服 (Intent Classification Seed)

BERT 系轻量意图分类器微调的首批训练输入。标签口径与 `lumio/services/common/classifier.py` 的
`RuleClassifier._RULES` 对齐，避免自造一套分类口径。

## 标签 (封闭 10 类单标签)

| id | 中文 | 决策路径 | 备注 |
|---|---|---|---|
| `faq` | 通用知识问答 | knowledge (RAG) | 覆盖面广，靠检索回答 |
| `bill_query` | 账单查询 | business | 问总额/还款额 |
| `transaction_query` | 交易明细查询 | business | 问逐笔/流水 |
| `limit_query` | 额度查询/管理 | business | 提额/降额为敏感操作 |
| `installment_inquiry` | 分期咨询/办理 | business | 含分期费率词 |
| `reward_query` | 积分/权益查询 | business | 涉及积分时效/兑换 |
| `card_loss` | 挂失/补卡 | business (敏感) | 全站唯一敏感写意图，高优先级 |
| `complaint` | 投诉 | business (投诉处理) | 伴随负面情绪 |
| `transfer_agent` | 转人工 | transfer | 诉求重点是"找人工" |
| `chitchat` | 闲聊/非业务 | fallback (模板) | 承担封闭集外溢出 |

## 文件

- `seed_dataset.json` — 主数据集，含 `meta / labels / examples / confusable_pairs` 四段。
  - `examples`：129 条种子句，`text` + `intent` 单标签；每类 ≥12 条，`card_loss`/`transfer_agent` 等
    重点类略多于均值。
  - `confusable_pairs`：12 条易混淆对，`text` + `correct` + `trap` + `note`（区分要点），
    用于硬负样本/评测集，不直接当正样本训练。

## 数据用法

- **微调**：`examples` 按 8:2 切 train/val 训单标签分类器（如 DistilBERT / ALBERT，ONNX 导出）。
- **评测**：`confusable_pairs` 用于验证易混淆类（尤其 bill/transaction、transfer/card_loss、
  complaint/transaction）的判别力。
- **弱标签引导**：可先让现有 `RuleClassifier` 打伪标签扩充正样本，再人工抽改，以弥补 150 条规模的不足。
- **重点难类**：`card_loss` 与 `transfer_agent` 需判准（影响安全和转人工），出现频次应高于均数。

## 与运行时解码的对齐

落地的三级阶梯建议与当前 `_FAST_PATH_THRESHOLD=0.7` 决策点一致：

```
规则/轻量分类器(≥阈)→ 命中
低置信/封闭集外 → LLM 兜底 或 chitchat
```

即：分类器对 `chitchat` 之外的低置信结果不强行给一个标签，而是让给兜底，避免封闭集误判。

## 版本

- 0.3.1 — 造数扩充 + 重训。手工补 ~55 条真实变体问法(每类+填槽分支/易混淆边界),
  新增 2 条 bill↔transaction 易混淆对, 补 4 条"转人工+挂失/补卡"同现样本(修 transfer 被 card_loss 吞)。
  新增独立 OOD/噪声评估池 `ood_pool.json`(29 条, 只评测不训练), 用于 energy-OOD 阈值标定。
  训练用 `scripts/intent_train_pipeline.py`(确定性 paraphrase 扩充 + 规则弱标签扩量,
  微调 24M 中文 RoBERTa)。产出模型 `out_intent_clf_v030/` 并轮换进 `out_intent_clf`:
  留出集 acc 0.973 / macroF1 0.979, 易混淆 15 对全对, 域内误伤≤1% 档位下 OOD 命中 20.7%→58.6%。
  标定阈值 ood_energy_threshold=-3.4 / band=0.5 写入 `closed_loop.json`(开关仍默认关, 供灰度启用)。
- 0.2.0 — 补 15 条卡权益/产品真伪校验类 FAQ 正样本 + 1 条 faq↔card_loss 易混淆对，
  faq 类种子 15→30。修复微调 BERT 将「卡权益/功能真伪问题」误判为 card_loss 的偏置
  （如「我的卡是不是终身免年费还带接送机」，重训后 → faq 0.905；留出集 acc 0.929 / 易混淆对 acc 0.923）。
- 0.1.0 — 首版：rule-aligned 种子句 + 易混淆对。

## 重训 / 标定

```bash
# 造数 + 微调 + 评测 + energy 标定(落盘到 out_intent_clf_v030/, 含 ood_calibration.json)
python3 scripts/intent_train_pipeline.py --out data/intent_classification/out_intent_clf_v030
# 只看已存模型在留出集/易混淆对/OOD 池上的表现
python3 scripts/intent_train_pipeline.py --out data/intent_classification/out_intent_clf_v030 --val-only
```

模型目录 `out_intent_clf_v030/` 保留版本化权重; 确认达标后把其权重轮换进 `out_intent_clf`(生产读取路径,
现配置 `bert_model_path`)。`ood_calibration.json` 记录 in vs OOD 能量分布与推荐阈值。