# V5.1 protocol-v4：2Wiki 128 题完整结果

本目录是实际完成的 seed=4、128 题、每题 50 文档、Qwen/Qwen3.5-9B 实验归档。API 最多重试 5 次，128 题均完成，EM 52.34%（67/128），F1 59.21%。

- [实验报告](REPORT.md)：配置、分类型评分、重试、协议问题与局限。
- [逐题轨迹索引](TRAJECTORIES.md)：按冻结题目顺序浏览全部 128 题。
- [逐题评分 CSV](baseline_comparison.csv) / [JSON](baseline_comparison.json)。
- [冻结输入](../../data/test/seed4/eval_2wikimultihopqa_50.json) / [输入生成记录](../../data/test/seed4/eval_2wikimultihopqa_50.provenance.json)。
- [运行配置](../../configs/v51_2wiki50_full128_retry5.json) / [最终状态](execution_status.json)。
- [代码与输入指纹](benchmark.audit.json) / [离线审计](offline_audit.json)。
- `code_snapshot/` 保存执行时的源码；`experiment.sqlite` 保存模型调用、事件、原文 Archive 和状态快照。

轨迹里的 `model_calls[].raw_request` 是模型收到的完整提示词，`raw_response` 是 API 原始响应，`attempt` 和 `error` 可用于检查重试。`events` 按 `event_seq` 排序，展示抽取、绑定、回查、VERIFY 和事实提升。

本目录保留原始运行记录中的机器路径和执行前 Git HEAD；实际源码身份以 `benchmark.audit.json` 的 `code_hashes` 与 `code_snapshot/` 为准。API 密钥和 SQLite 临时锁文件不在归档中。离线审计可在仓库根目录执行：

```bash
python results/v51-2wiki50-seed4-full128-node199-protocol-v4-retry5/audit_full_run.py
```

评分保留源数据的 `answers[0]`，包括已知的 `dev_1476` 金标异常。当前没有相同 seed4 输入的 baseline 结果，不将本分数视为论文精确复现或完整证据链正确率。复跑应使用新的实验 ID 和输出目录；本归档不应被覆盖。模型调用还需自行准备凭据、本地 tokenizer，并调整配置中的本地参考路径。
