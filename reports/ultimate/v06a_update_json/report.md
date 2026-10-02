# V06a：UPDATE JSON 迁移报告

日期：2026-10-01

## 阶段结论

V06a 只把 LOW `UPDATE` 从三列文本协议迁移为严格 JSON；MEMORY、RECALL、PLAN、ANSWER 均未迁移。本轮固定 128 题完整运行已完成，但候选没有通过无退化门禁，状态为 **BLOCKED**，停在 V06a，不进入 V06b。

父版本是上一轮三列 UPDATE 的未提交源快照，对应 `results/ultimate/v04_three_column_update_full128_r1/`，成绩 96/128、Accuracy 75.0000%、token F1 0.7767485119。候选是 `results/ultimate/v06a_update_json_full128_r1/`，成绩 89/128、Accuracy 69.53125%、token F1 0.7285714286。相对父版本下降 7 题、5.46875 个百分点和 0.0481770833 F1。

候选的 35 道 comparison 题为 35/35；128 题全部状态为 `OK`，没有 `ERROR` 或 `RESOURCE_LIMIT`。这不能抵消总分和 compositional / bridge_comparison 的退化：compositional 为 24/53（父版本 30/53），bridge_comparison 为 18/24（父版本 21/24）。

## 实施范围

- 新增 `delaybind_core/update_json.py`。
- `UPDATE` 输出对象固定为：

  ```json
  {"facts":[{"query":"Q2","sources":["D18@C0"],"text":"Natural-language fact."}],"hints":[]}
  ```

- `facts` 只允许 `query/sources/text`；`hints` 只允许 `sources/text`。
- `additionalProperties=false`、必填字段、非空字符串/来源数组、来源和 query 本地允许集合校验均启用。
- 拒绝重复键、NaN、截断 JSON、Markdown 包裹、外部 prose、错误字段和状态/动作字段；不回退到文本协议。
- 保留最多一次 UPDATE JSON 修复；完整截断/拒答不会从半截文本猜事实；独立有效项目可在坏项目被拒绝时保留。
- SiliconFlow `Qwen/Qwen3.5-9B` 已通过供应端严格 `json_schema` 探针；正式评测固定使用 `json_schema`，没有自动降级。
- 其它接口没有加入 `response_format`；运行时状态路由、HIGH/LOW 角色、来源边界、MEMORY 语义均保持原接口。

相对父源快照实际变化的生产文件为 `agent_prompts.py`、`evaluation.py`、`runner.py`、`subquery_runner.py`，另新增 `update_json.py`。已有 PLAN、MEMORY、RECALL、ANSWER prompt 函数的 AST 未改变；范围和哈希核验见 `scope_check.json`。

## 测试与审计

- 单测与集成测试：100 passed，日志见 `unit_tests.log`。
- 128 题：128/128 完成，轨迹 128 份，运行退出码 0。
- JSON 请求范围：245 次 UPDATE 响应记录均使用配置的 JSON schema；其它接口无 `response_format`，无 transport violation。
- JSON 响应：245 次 UPDATE 记录中 244 次成功解析；1 次 `finish_reason=length`，被标为 `TRUNCATED` 并修复。另有 4 次项目级非法来源/ID，均触发一次局部修复；没有 repair exhaustion。
- 状态回放：128/128 通过最终状态重放；运行时接口与角色不变量通过。
- `git diff --check`、Python compile 检查通过。

审计门禁见 `gate.json`，逐题配对见 `paired_diff.csv`，逐题退化说明见 `regressions.md` 与 `regression_notes.json`。

## 成本

| 指标 | 父版本 | V06a | 变化 |
|---|---:|---:|---:|
| 逻辑调用 | 955 | 922 | -33 |
| 网络记录 | 962 | 952 | -10 |
| 重试 | 7 | 30 | +23 |
| 输入 token | 2,178,302 | 2,223,904 | +45,602 |
| 输出 token | 287,348 | 261,025 | -26,323 |
| p50 延迟 | 48.0s | 60.4s | +12.4s |
| p95 延迟 | 216.7s | 280.2s | +63.5s |

JSON 本身没有造成解析耗尽，但 UPDATE prompt / repair 负担和本轮网络超时使重试与延迟上升；输入 token 增加约 2.1%。

## 逐题根因边界

本轮新增错误不能统一归因于 JSON 语法。已观察到的主要类别是：

1. 相关证据被模型放入 `hints`，没有关联到对应 Qn，导致正式事实链和依赖激活缺失。
2. UPDATE 已抽取正确事实，但未迁移的文本 MEMORY 接口写错事实 ID，导致检查/绑定失败。
3. 事实或导演/人物身份被错误关联到另一个 Qn，比较或组合分支因此未完成。
4. 原文明确出现的信息被 UPDATE 漏抽，例如 Fulbright scholarship。
5. 原文没有明确回答地点时，两版 ANSWER 的猜测不同；这种题不能被简单标记为“JSON 漏字段”。

这些逐题证据已经记录在 `regression_notes.json`。V06a 的结论是：结构化输出和本地校验机制工作，但在当前 9B 模型和现有下游文本 MEMORY 下，JSON 迁移改变了事实覆盖、hint/fact 分配和依赖链，造成明显质量退化。

## 门禁状态

`gate.json` 的单轮检查为：

- Accuracy 相对父版本：FAIL
- token F1 相对父版本：FAIL
- 原始 C floor：FAIL
- comparison 35/35：PASS
- ERROR / RESOURCE_LIMIT 不增加：PASS
- 运行时与接口不变量：PASS

严格方案还要求三次独立 paired full runs；本交付只完成预声明的第一轮，因此即使单轮没有下降也不能宣称阶段 PASS。当前应保留父版本作为主线，V06a 作为 BLOCKED 实验版本。
