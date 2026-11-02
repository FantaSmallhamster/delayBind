# DelayBind-MWR 开发副本

代码位于 `/Users/sunning/Documents/ChatGPT/delaybindsota`。
用户提供的 `/Users/sunning/Documents/delaybind/v08_98of128_verify_step1` 未修改。
原始核心代码、配置、测试和脚本的冻结副本在 `baselines/legacy_v08`；数据/tokenizer 等逐文件哈希在该目录的 `sha256.json`。

## 本地验证

在本项目目录运行：

```bash
.venv/bin/python -m pytest tests -q
```

最新结果：255 passed，23 subtests passed，日志见 `reports/mwr/phase2_20261028/final-tests.txt`。首版为 223 passed；原始测试为 147 passed、39 failed，迁移原因和保留断言见 `TEST_MIGRATIONS.md`。
本机 `.venv` 使用 Python 3.12；其他机器可用 Python >=3.11，安装 `pip install -e '.[test,tokenizer_json]'`。

## 真实模型验证顺序

用户已选择沿用 SiliconFlow `Qwen/Qwen3.5-9B`。请在本机环境变量 `MODEL_API_KEY` 或本项目 `.env.local` 配置密钥；不要把密钥提交到仓库。
已完成两阶段真实语义诊断、两题固定三轮 smoke 和 16 题三档容量 pilot，最新结果与限制见 `reports/mwr/phase2_20261028/REPORT.md`；512 题新集合仅完成离线接入与来源窗口定位。

先验证语义判断，再运行小样本端到端检查：

```bash
.venv/bin/python scripts/mwr/semantic_check.py --config configs/mwr/smoke.json --output reports/mwr/semantic-real.json
.venv/bin/python scripts/ultimate/run_experiment.py --config configs/mwr/smoke.json --expected-samples 2
```

smoke 为 2 题 × 3 方法 × 1 顺序 = 6 条结果。只对通过工程/语义检查的版本继续 pilot；不要先执行全部 128 题并宣称性能改善。

```bash
.venv/bin/python scripts/ultimate/run_experiment.py --config configs/mwr/pilot_2k.json --expected-samples 16
.venv/bin/python scripts/ultimate/run_experiment.py --config configs/mwr/pilot_4k.json --expected-samples 16
.venv/bin/python scripts/ultimate/run_experiment.py --config configs/mwr/pilot_8k.json --expected-samples 16
```

比较输出使用统一计分并保留失败题；新 engine 的 `max_live_state_tokens` 和 `max_prompt_tokens` 在 runner.mwr 中分别指定。
每次改变源码、模型合同、数据或 tokenizer，必须使用新实验 ID/输出目录。恢复只接受完全相同的合同。
单次 `run` 命令同样可在 `runner` 中指定 engine 和 mwr 配置。

## 四组诊断与离线过程指标

`diagnose.py` 输入显式冻结 case：`sample`、`predicted_plan`、`proposal_stream`（按窗口的 EvidenceRecord 数组）、`support_spans`、可选 `legacy_bindings`。
Gold 只在该诊断入口使用，正常 engine 只接收 InferenceInput。候选流来自固定 extractor，不得以 gold 改写正常候选流。
四组诊断为支持原文 oracle、同原文有/无旧绑定、仅首次当前窗口注入支持句的抽取 oracle、相同预测候选/预算下的 FIFO 来源记忆。
缺少旧绑定时会明确报告该诊断不可用，不编造旧结果。

```bash
.venv/bin/python scripts/mwr/diagnose.py --case /absolute/path/frozen-case.json --config configs/mwr/smoke.json --output reports/mwr/diagnostic-real.json
.venv/bin/python scripts/mwr/retention_replay.py --stream /absolute/path/frozen-stream.json --tokenizer models/Qwen3.5-9B-tokenizer/tokenizer.json --budgets 2000 4000 8000 --output reports/mwr/retention-real.json
.venv/bin/python scripts/mwr/analyze.py --results results/mwr/pilot_4k/results.jsonl --output reports/mwr/paired-real.json
```

过程指标另传 `--annotations`。文件为 `{sample_id:order: [annotation...]}` 的 JSON 映射（键示例 `q123:original`）。字段见 `delaybind_core/mwr/analysis.py::DelayedAnnotation`。
无可靠标注时，仅计算配对结果区间，不输出虚构的捕获/保留/召回准确率。

## 文件入口

- `delaybind_core/mwr/engine.py`：前向阅读、暂存、绑定、保留与提交。
- `source_access.py`：只按存活证据授权来源访问。
- `evidence.py` / `demands.py` / `retention.py`：统一证据池、需求视图和真正淘汰。
- `recall.py` / `binding.py` / `packing.py`：有限选择、实例支持与完整证据组。
- `answering.py` / `budget.py`：统一答案与请求预算。
- `analysis.py` / `experiments.py` / `diagnostics.py`：离线评价、消融与 oracle，正常推理不导入它们。

每一步当前状态见 `reports/mwr/PROGRESS.md`。实验限制和正式预注册待办见 `EXPERIMENT_PROTOCOL.md`。

## 新增 512 题测试集

导入、来源核验和复现命令见 `reports/mwr/bundle_512/REPORT.md`。推理只读取 `data/test/2wiki_512_complete_eval_bundle_20261026/test/eval_2wikimultihopqa_50.json`；gold 与官方句子副本仅用于离线评分和过程标注。新入口分别是 `scripts/mwr/import_eval_bundle.py`、`map_support_sentences.py`、`grade_eval_bundle.py`。候选运行配置为 `configs/mwr/heldout_512_4k_candidate.json`，正式容量仍待开发 pilot 确定。

2026-10-27 的真实调用、接口修复、语义失败和固定三轮重复结果见 `reports/mwr/REAL_VALIDATION.md`。离线重建汇总使用 `.venv/bin/python scripts/mwr/summarize_validation.py`，不会调用模型。已有 `smoke`、`smoke-r2`、`smoke-r3` 输出属于不同代码合同；改代码后必须换实验 ID 和输出目录，不能覆盖早期失败记录。
