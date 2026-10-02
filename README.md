# DelayBind V08

当前工作树是 V08 精简版：保留子问题规划、JSON 事实抽取、原文与记忆联合核验、确认绑定、原文回看和最终证据包。旧 graph/oracle 执行器、ReMemR1/verl 训练工程、历史阶段脚本、配置、题集及运行产物已移除。

## 保留内容

- `delaybind_core/`：V08 核心及 API、存储、判分、离线重放。
- `data/test/filtered_seed4_128_refresh_v08/`：最新冻结的 128 题，每题 50 篇文档，以及来源记录、筛选决策和替换映射。
- `models/Qwen3.5-9B-tokenizer/`：切块和预算计算所用 tokenizer。
- `configs/ultimate/`：V08 和完整上下文直接回答的运行配置。
- `scripts/ultimate/run_experiment.py`：V08 批量运行；`run_direct_raw_context.py`：完整上下文直接回答。
- `scripts/verify_v08.py`：独立离线核验当前题集、筛选记录、两批结果及 V08 轨迹。
- `tests/`：当前功能测试。JSON、确认绑定等早期阶段引入的功能已纳入 V08，测试按功能命名。
- `results/ultimate/`、`reports/`：最新题集的两次评测和题集构建依据。

## 当前题集上的结果

模型均为 **Qwen/Qwen3.5-9B**，SiliconFlow API，并发 8，temperature=0，top_p=0.95，seed=4，enable_thinking=false，输出上限 4096。

| 方式 | EM | F1 | OK / ERROR |
| --- | ---: | ---: | ---: |
| V08 | 112/128（87.50%） | 0.896425 | 125 / 3 |
| 完整上下文直接回答 | 74/128（57.8125%） | 0.653487 | 128 / 0 |

[最新题集](data/test/filtered_seed4_128_refresh_v08/README.md) · [V08 测试报告](reports/ultimate/v08_refreshed_benchmark/README.md) · [直接回答报告](reports/ultimate/direct_raw_refreshed_full128_t0/README.md) · [清理核验](reports/v08_cleanup/README.md)

这些是清理前已完成的模型评测结果。清理后进行了离线验证，没有再次调用模型跑 128 题；结果目录内的历史代码 manifest 保持原样。核心提示词、子问题运行、事实/记忆协议和答案判分函数保持原有内容，具体文件及函数核对记录见清理报告。

## 安装与离线检查

Python 3.11+。已有 `.venv` 可继续使用；新环境安装：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test,tokenizer_json]'
.venv/bin/python -m pytest -q
.venv/bin/python scripts/verify_v08.py
.venv/bin/python scripts/ultimate/run_direct_raw_context.py \
  --config configs/ultimate/direct_raw_refreshed_full128_t0_r1.json --prepare-only
```

以上核验不需要 API 密钥，不发模型请求。

## 后续运行

V08 配置入口为 `configs/ultimate/v08_refreshed_benchmark_full128_r1.json`。
每次新运行先复制配置并修改 `experiment_id` 和 `output_dir`；现有输出目录已有结果，运行器会拒绝覆盖。随后执行：

```bash
.venv/bin/python scripts/ultimate/run_experiment.py \
  --config configs/ultimate/v08_next_run.json --expected-samples 128
```

直接回答使用对应配置副本及 `scripts/ultimate/run_direct_raw_context.py --config <配置路径>`。API 密钥通过隐藏输入或 `MODEL_API_KEY` 环境变量提供，不写入配置。

`V5Runner`、`v5_predicted` 等保留标识用于现有轨迹和存储格式；当前入口只运行 V08 子问题流程。

## 许可

本项目使用 [MIT License](LICENSE)。继承代码的 Apache 许可与声明保存在 [THIRD_PARTY](THIRD_PARTY)。
