# V5 2Wiki Experiment Harness

本文件说明第一轮机制可行性实验的可执行入口。所有条件使用同一个
`MODEL_NAME` checkpoint、API 参数和 Manifest；Oracle Plan 只用于上界与机制诊断，不能作为
Predicted Plan 的测试结果。

## 1. 实验方法

| 方法 | 实现 | 目的 |
| --- | --- | --- |
| `direct_full_context` | 整个 Manifest 一次发送给同一模型 | 同 checkpoint 的 Direct Full Context 基线。 |
| `v5_oracle` | gold evidence 编译 Oracle Plan，启用 DEFER | 隔离 Runtime、UPDATE、VERIFY 和延迟绑定能力。 |
| `v5_predicted` | 模型仅根据问题生成 Plan | 测试完整 V5 系统。 |
| `v5_oracle_no_defer` | Oracle Plan，但未绑定候选直接 SKIP | 测量 Delayed Binding 的因果贡献。 |
| `v5_oracle_flat` | 同一 Oracle Plan，但关闭 OQG 的 DORMANT/ACTIVE frontier | 测量查询图拓扑激活相对平铺 Pattern List 的贡献。 |
| `v5_oracle_flat_no_defer` | Flat Pattern List 且关闭 DEFER | 用于拆分拓扑激活与延迟绑定两个因素。 |

`compile_oracle_plan()` 使用 2Wiki 的 `evidences` 关系链构造 Plan，并把非问题常量替换为变量。
实体别名通过显式 `question_anchor` 和 `subject_aliases` / `object_aliases` 保存。Runtime 只接受
计划中可审计地声明的别名，绝不依据字符串相似度把两个实体自动合并；comparison 中即使两个 gold
值相同也使用两个独立变量，避免把 Yes/No 答案泄漏到 Plan 拓扑。该编译器只能用于 Oracle 实验和
监督数据生成。

`answer_mode="runtime"` 是严格 target-based 消融：PLAN 必须给出
`answer_contract.target`，Runtime 直接读取绑定值。`answer_mode="evidence"` 是推荐的完整
V5 回答模式：PLAN 只提供 patterns/operators，答案类型由问题画像推断；所有 required pattern
被验证后，ANSWER 只能读取 EvidencePack 并必须返回其中存在的 `source_refs`。因此 ANSWER 可以
根据问题选择图中的最终变量，但不能重新读取全文或使用图外事实。

所有 V5 条件都将 `QueryPlan.patterns` 确定性编译为 **Open Query Graph (OQG)**：常量、共享变量、
答案变量和算子是节点，Pattern 是边。默认 `query_graph_mode="open"`：两端未知的边为 `DORMANT`，
一端绑定后为 `ACTIVE`，只有带来源的验证 Claim 才能令其 `SATISFIED`。最终 Evidence Graph 与 OQG
通过 `edge_support` mapping 连接。`flat` 仅用于消融，它保留相同 Pattern 和模型调用，但关闭由绑定
触发的 frontier 激活。

## 2. Manifest 条件

- `original`：数据集原始文档和句子顺序，即 Forward 条件。
- `reverse`：文档顺序完全反转，用于初步逆序鲁棒性测试。
- `interleaved`：不同文档按 sentence index 轮转，制造交错证据。
- `distant`：支持文档位于流两端，干扰文档位于中间。
- `shuffle`：使用固定 seed 对文档做确定性随机排列。

所有条件保持原文、`source_ref` 和 gold 标注不变，只修改 `stream_position`。

## 3. 一行运行

先把 2Wiki JSON 路径写入配置的 `input`，模型凭据继续放在
`.env.local`：

```dotenv
MODEL_BASE_URL=https://api.example.com/v1
MODEL_NAME=Qwen/Qwen3.5-9B
MODEL_API_KEY=...
```

运行 16 条样本、四种方法、Forward/Reverse：

```bash
PYTHONPATH=. python -m delaybind_core experiment \
  --config configs/experiment_smoke.json
```

使用 target-free evidence-answer 运行完整 V5 矩阵：

```bash
PYTHONPATH=. python -m delaybind_core experiment \
  --config configs/evidence_answer_smoke.json
```

进行延迟绑定主实验时，使用严格多窗口的 Oracle 对照配置：

```bash
PYTHONPATH=. python -m delaybind_core experiment \
  --config configs/oracle_streaming.json
```

该配置以 80 个词的近似窗口预算读取，且设置 `min_streaming_windows=2`。任一样本若实际只形成
一个窗口，会以 `STREAMING_PROTOCOL_TOO_SHORT` 标记为协议不合格，而不是被混入流式结果。主实验
应优先检查 `windows_processed` 和 `streaming_protocol_valid`，再解释准确率差异。

比较 Open Query Graph 与 Flat Pattern List 的四格消融：

```bash
PYTHONPATH=. python -m delaybind_core experiment \
  --config configs/oracle_query_graph_ablation.json
```

只测 Direct Full Context 时使用独立配置，不需要 Oracle Plan：

```bash
PYTHONPATH=. python -m delaybind_core experiment \
  --config configs/direct_only.json
```

师妹第一次上手只需要修改配置中的 `input`，并在仓库根目录创建 `.env.local`：

```dotenv
MODEL_BASE_URL=https://你的中转站地址/v1
MODEL_NAME=Qwen/Qwen3.5-Flash
MODEL_API_KEY=你的密钥
```

然后安装轻量 V5 依赖并运行：

```bash
python3 -m pip install -e .
PYTHONPATH=. python -m delaybind_core experiment \
  --config configs/direct_only.json
```

Direct 会把当前样本的整个 Manifest 一次发送给模型，结果写入
`results/2wiki-direct-only/`。它不是 V5 流式流程，只用于同一模型的 Full Context 基线；
如果完整上下文超过服务端窗口，需要先缩小样本上下文或明确记录为 truncated/extended 条件。

也可以提前生成并人工审核 Oracle Plan：

```bash
PYTHONPATH=. python -m delaybind_core compile-oracle-plans \
  --input data/2wiki/dev.json \
  --output runs/oracle_plans.json \
  --sample-count 16
```

然后在实验配置中设置 `"oracle_plans": "runs/oracle_plans.json"` 并关闭
`compile_oracle`。

## 4. 输出产物

每次实验在 `output_dir` 下生成：

| 产物 | 内容 |
| --- | --- |
| `results.jsonl` | 每个 `(sample, method, order)` 的完整评分行。 |
| `results.csv` | 适合筛选和画图的扁平核心指标。 |
| `summary.json` / `summary.csv` | 按 method/order 聚合的主指标及 Reverse-Forward Gap。 |
| `experiment.sqlite` | Runtime events、model calls、raw archive 和 snapshots。 |
| `trajectories/*.json` | 单条件完整事件、模型调用、最终状态和 EvidencePack。 |
| `manifests/*.json` | 每个样本与顺序条件的不可变输入清单。 |
| `oracle_plans.compiled.json` | `compile_oracle=true` 时生成的可审查 Oracle Plan。 |
| `config.resolved.json` | 不含 API key 的冻结实验配置。 |

`resume=true` 会跳过已成功条件；默认 `retry_errors=true`，所以 API 暂时失败或 Plan 无效的条件
会在下一次命令中重试并覆盖旧错误行。
`config.resolved.json` 还包含模型与语义配置的 SHA-256 fingerprint；同一 `output_dir` 更换
checkpoint、方法矩阵或 Runtime 参数后会拒绝续跑，防止新旧结果被静默混合。

## 5. 已实现指标

- Answer Exact Match 与 token F1。
- Supporting-Fact precision/recall/F1，按稳定 `source_ref` 计算。
- Triple Event F1，基于模型提出的 `TRIPLE_EVENT_PROPOSED` 与 gold evidence triples。
- Graph Triple F1，基于最终 EvidencePack 中的 verified Claim。
- Verifier precision/recall/F1：仅在已经送入 VERIFY 的候选 Claim 上评估其终态 ACCEPT，和 UPDATE
  的事实抽取召回分开报告；扩展上下文后的最终决定覆盖先前 `NEED_MORE_CONTEXT`。
- Plan Validity 与 Oracle relation-family recall。
- Early Latent Evidence Recall：首次 grounded Claim 前出现的 gold evidence 中，被正确 DEFER 的比例。
- Deferred-to-Promoted conversion 与 exact callback hit rate。
- Cross-window Deferred Promotion：仅当 deferred Claim 的 source 严格早于触发 binding 的 source
  时计数；同一句或较晚事实的提升单列为 non-early，不能作为延迟回查机制证据。
- `INSUFFICIENT`、`CONFLICTED`、`UNSUPPORTED`、`RESOURCE_LIMIT` 和运行错误。
- logical model calls、实际尝试、cache hit、输入/输出 token、模型延迟和端到端延迟。
- Reverse-Forward Accuracy Gap。

当前 token 指标来自 API `usage`；若服务不返回 usage，相应数值为 0。货币成本需要根据供应商
价格表另行换算，不能从词数近似值推断。

VERIFY 默认先读取候选句的同文档邻域；若返回 `NEED_MORE_CONTEXT`，Runtime 最多执行一次受控
扩展，加入该已读文档和主体/客体在 read prefix 中的 mention。对应配置为
`max_verify_expansions` 和 `verify_expansion_limit`，扩展仍不能访问未来后缀。
VERIFY 的模型接口只包含 `claim_id`、三态 `status`、必填 `reason`、支撑 `source_refs` 和连续
`supporting_text`；其内部 Claim 状态由 Runtime 维护，避免把完整嵌套 Claim schema 反复放入验证
提示词。VERIFY 只判断原子 Claim 是否被本地来源支持，不得要求后续 hop 或完整问题答案。

## 6. 推荐执行顺序

1. 先运行 `sample_count=16`、`original/reverse`，人工检查所有 error trajectory。
2. 先运行 `v5_oracle`、`v5_oracle_flat`、`v5_oracle_no_defer`，并只用 cross-window 指标解释
   图拓扑与延迟绑定信号。
3. 确认 Oracle 图可运行后比较 `v5_predicted` 与 `v5_oracle`，决定是否优先训练 PLAN。
4. 扩至 hard-128，并加入 `interleaved/distant`。
5. 最后扩展到 50/200/800/1600 documents；超出 checkpoint 原生窗口时必须将 Direct 标为
   extended 或 truncated，不能继续写作 Full Context。
