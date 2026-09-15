# 2Wiki 128 题 × 50 文档：seed=4 数据

按用户要求，题目抽样与干扰文档采样 seed 从 42 改为 4，文档排列 seed 保持 4。
本次只生成数据、更新配置和执行离线预检，模型 API 测试保持暂停。
模型请求的 `model_seed` 仍为 `null`；它与 benchmark 的采样 seed 是两个参数。

## 冻结文件

- 数据：`data/test/seed4/eval_2wikimultihopqa_50.json`。
- 数据 SHA-256：`24bf5d18ce3911d05e4ad3666e9836adfbb1e73e436a146e55d436cec2a8e576`。
- 生成记录：`data/test/seed4/eval_2wikimultihopqa_50.provenance.json`。
- 原始数据：FlashRAG 2Wiki dev JSONL，12,576 条；原文 SHA-256
  `b4d74d73d1cb3c0632c665a85e1e5060e3411968b807721618cfd2464fc905e3`。
- 规范化语料库：56,707 篇去重文档，按原生成器的文本排序规则排列。

新文件包含 128 个不同题目，每题 50 个连续编号文档，保留来源问题、答案及支持文档正文。
两次独立运行的输出字节完全一致。旧 seed=42 文件与已暂停的 API 记录保留在原目录。

## 可复现生成

```bash
python3 scripts/build_2wiki_benchmark.py \
  --source /root/delayBind/artifacts/rememr1_eval/data/source/2wikimultihopqa_dev.jsonl \
  --output data/test/seed4/eval_2wikimultihopqa_50.json \
  --seed 4 --sample-count 128 --num-docs 50
```

生成器保留公开 ReMemR1 的问题/文档转换、先抽 `5 × 128 = 640` 个候选再取前 128 个、
50 文档拼接及 seed=4 文档排列规则。题目和干扰文档使用同一个局部 `Random(4)`，按固定顺序
消费随机数，避免原多进程 worker 的随机状态造成重复运行结果不同。

`scripts/0_run_data_process.sh` 已显式传 `--seed 4`，原 `process_test.py` 的默认值也改为 4。
当前冻结文件应以上面的独立生成命令复现，不依赖通用多数据集、多进程脚本的随机数消费顺序。

## 更新后的 8 题冒烟

配置仍为 `configs/v51_2wiki50_smoke8.json`，现在指向 seed4 新文件和独立输出目录
`results/v51-2wiki50-seed4-smoke8/`。仍取新 128 题集合的前 8 题。

| 题目 ID | Qwen3.5 tokenizer tokens | 5000-token 窗口数 |
| --- | ---: | ---: |
| dev_3867 | 5711 | 2 |
| dev_4969 | 5612 | 2 |
| dev_1690 | 6040 | 2 |
| dev_11816 | 6768 | 2 |
| dev_6489 | 5043 | 2 |
| dev_7845 | 5811 | 2 |
| dev_2539 | 6273 | 2 |
| dev_1476 | 5435 | 2 |

本次仅执行以下预检，实际模型请求为 0：

```bash
/tmp/delaybind-subqueries-venv/bin/python scripts/run_v51_smoke.py --prepare-only
```

预检验证了文件指纹、128 题/50 文档、题目 ID 和 token 切块后原文还原；共 16 个阅读窗口。

## 与旧 baseline、论文的关系

seed42 的历史 baseline 输出不适用于这批新题。配置已移除旧结果配对，只将旧实验配置作为
模型参数参考；新数据尚未运行对应 baseline，比较分数应为 `null`。冒烟脚本支持没有历史
baseline 结果的独立评测，仍使用原始评分函数和 `answers[0]`。

seed=4 与论文 C.3 的采样参数对齐。由于没有作者当时最终使用的题目清单、数据版本及干扰文档
文件，不能仅凭相同 seed 保证逐题逐文档等同于论文，`paper_exact_sample_match` 仍标为 false
（尚未证实）。新数据可以用于后续在相同输入上重新比较 V5.1 和 baseline。
