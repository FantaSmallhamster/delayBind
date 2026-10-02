# V08 刷新后的 128 题集

本题集保留旧版 98 道题的整条记录，包括原有 50 篇上下文及题序位置；
替换 30 道，新增 25 道 comparison 和 5 道 inference。

| 类型 | 旧题集 | 移除 | 新增 | 新题集 |
| --- | ---: | ---: | ---: | ---: |
| compositional | 53 | 27 | 0 | 26 |
| comparison | 35 | 0 | 25 | 60 |
| inference | 16 | 0 | 5 | 21 |
| bridge_comparison | 24 | 3 | 0 | 21 |
| 合计 | 128 | 30 | 30 | 128 |

## 替换规则

移除全部 24 道 V08 答错的 compositional，以及错题中另一个存疑题
dev_10047。另移除 5 道虽然 V08 答对、但正文仍有证据缺失或人物链接问题的题：
dev_4961、dev_10191、dev_6901、dev_9742、dev_10404。
具体的一对一对应见 [replacement_mapping.json](replacement_mapping.json)。

保留的原题并非全部答对：dev_5002、dev_5853、dev_6785、dev_5416、dev_7046
在 V08 中答错，但原文证据足以回答，且不属于本次指定移除的 compositional。

## 新题筛选

沿用 MemAgent 的无上下文提示：仅提供问题和 `Your final answer in \boxed{}.`，
不提供检索段落、支持事实或结构化 gold 三元组。
每道入选题必须四次有效最终回答的官方归一化 EM 均为 0；
超时、HTTP 错误、截断或没有有效 boxed 答案不计作错误答案。

本批最终使用 **Qwen/Qwen3.5-9B 独立回答四次**，temperature=1.0、top_p=0.7、
max_tokens=512、enable_thinking=false、n=1，并发 8。
原先 4B/9B 各两次的尝试因 4B 服务反复 503/超时中断；这项模型调整已在执行时说明。
21 条参数、题目与来源完全匹配的既有 9B 独立回答被复用，其余 99 条为追加请求；
最终 120 条回答有 120 个不同请求 ID，不混入 4B 回答或错误请求。

MemAgent 上游还有全文包含答案的检查；本题集沿用旧题集既定的官方 EM 筛选口径，
全文包含仅作诊断，不把推理中提到候选答案视为最终答对。
此外逐题查看完整支持段落，排除证据不足、同名人物、合拍片语义歧义以及
答案缩写/头衔造成的疑似 EM 假错误。入选 30 道均有明确原文证据链。

从官方 dev 数据以 seed=4 固定候选顺序，排除旧 128 道题，分别取人工复核
通过候选中最早的 25 道 comparison 和 5 道 inference。
新增题保留原始文章文本，以 seed=4 补入干扰文章到 50 篇、打乱文档顺序并重映射 evidence_idx。
dev_2989 的原始干扰文章有一篇完全重复文本，本次去重并补一个干扰文章；所有支持文章不变。

## 文件与复核

- [eval_2wikimultihopqa_50.json](eval_2wikimultihopqa_50.json)：可运行的 128 道、每题 50 篇文档。
- [selected_official.json](selected_official.json)：对齐的原始官方记录。
- [selected_source.jsonl](selected_source.jsonl)：对齐的 FlashRAG 来源记录。
- [new_selected_filter_decisions.json](new_selected_filter_decisions.json)：30 道新题的四次最终回答、EM 与 gold 别名。
- [frozen_manifest.json](frozen_manifest.json)：来源、筛选参数、保留/替换 ID 及 SHA-256。
- [复核总报告](../../../reports/dataset_refresh_v08/README.md)。

数据 SHA-256：`90cb0c3d4e10513083f15baa2e9d767f659a632348691bd9f47c7776fdcbd8b7`。

## 后续运行

已生成配置 `configs/ultimate/v08_refreshed_benchmark_full128_r1.json`。
除题集路径、哈希、实验 ID 和输出目录外，参数与旧 V08 运行一致。
已于 2026-10-02 完成新题集 V08 全量评测：EM 112/128（87.50%），
F1 0.896425，125 OK、3 ERROR；详细统计和错题见
[测试报告](../../../reports/ultimate/v08_refreshed_benchmark/README.md)。

本次运行命令：

```bash
.venv/bin/python scripts/ultimate/run_experiment.py \
  --config configs/ultimate/v08_refreshed_benchmark_full128_r1.json \
  --expected-samples 128
```

API 密钥由隐藏输入提供，未写入配置或数据包。
