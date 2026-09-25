# R2 分 chunk 直传与可选 PLAN 修补拒绝：实施记录

## 修改范围

- `update_input_mode=plain_token_chunks`：事实成员模式下使用配置 tokenizer 对 `context.strip()` 编码一次，每 `chunk_size` 个 token 解码后直接进入 UPDATE / UPDATE_REPAIR 的 `<section>`。新分支不创建 SentenceArchive，也不为正文生成来源引用。默认 `archive_windows` 保留原行为。
- `pending_chunk`、游标 token 位置与 `update_repair_progress` 随 R2 状态事务保存。恢复优先复用原 UPDATE 快照、已采纳项、剩余修复范围和重试次数；完成 UPDATE 后才清除待处理 chunk。
- `plan_repair_failure_policy=continue_valid_plan`：仅捕获 `PlanRepairProposalError`，有限重试耗尽后原子保存独立拒绝记录，并按语义依据去重。上下文、提交、SQLite、API、预算及未知程序错误保持原失败路径。默认 `abort` 保留原行为。
- 新配置 `configs/v52_r2_full128_member_graph_plain_chunks_optional_repair.json` 使用独立实验 ID 与输出目录；没有启动真实模型评测。

## 实际验证

| 检查 | 命令 / 输入 | 结果 |
| --- | --- | --- |
| 新增专项测试 | `.venv/bin/python -m pytest -q tests/test_r2_plain_chunk_input.py tests/test_r2_optional_plan_repair.py` | 25 passed |
| 项目测试目录 | `.venv/bin/python -m pytest -q tests` | 588 passed |
| 差异格式 | `git diff --check` | 通过 |
| 冻结 128 题参考 chunk 对照 | `.venv/bin/python -m scripts.audit_r2_plain_chunks --dataset data/test/filtered_seed4_128/eval_2wikimultihopqa_50.json --tokenizer models/Qwen3.5-9B-tokenizer/tokenizer.json --chunk-size 5000 --output reports/r2_plain_chunk_equivalence_128_20260925.json` | 128 题、240 片、0 处差异，正文一致率 1.0 |
| 历史五题修补提案归类 | 读取用户提供的 wrong69 包中 SQLite，按每次 `PLAN_REPAIR_SNAPSHOT.state_revision` 恢复事务状态；对记录的 `parsed_output` 运行新提案准备函数 | 5 题、10 次回复：6 次格式解析错误、4 次计划 DTO 错误；均为模型提案错误 |

专项测试使用真实解析器、Runtime、SQLite 和脚本客户端，覆盖真正的模型消息正文、无 Archive 分支、输入预算、中断后重新读取原片、部分 UPDATE 后继续原修复范围、UPDATE 完成后继续下一片、旧来源窗口、语义去重、拒绝事务回滚及事件回放。

参考 chunk 的逐片 token 起止、正文 SHA-256、样本 ID 和一致性结果在 `reports/r2_plain_chunk_equivalence_128_20260925.json`。历史回复的 call ID、原状态 revision、回复 SHA-256 与分类在 `reports/r2_plan_repair_historical_classification_20260925.json`；没有复制原始回复或修改历史数据库。

## 未执行与边界

- 本次真实模型调用为 **0**。G0–G3 配对评测、完整 128 题 EM/F1、token 费用与语义准确率未测；离线一致性不能证明答案质量提升。
- 无筛选的仓库根目录 `pytest` 收集会遇到训练侧可选依赖缺失（`torch`、`datasets`、`numpy`）；本次完整回归范围为项目 `tests/`。
- 历史十次提案分类只说明新错误边界会如何处理既有回复；没有将旧 `RUNTIME_ERROR` 状态改写为新运行结果。
