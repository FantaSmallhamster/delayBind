# MEMORY BIND 两阶段 A/B 实验

本轮只改变 Fact-only `mode=BIND` 的 system message 与 user instruction。BIND 采用独立提示词版本 `v5.2-r2-memory-bind-text-16-query-answer`；其他接口继续使用原 text-15。现有 REBIND 提示词逐字保留。BIND 的格式修复沿用同一个 BIND 提示词和冻结快照。

生产修改在 `delaybind_core/prompts_r2.py`，调用审计版本在 `delaybind_core/subquery_runner_r2.py`。新增提示词定义“允许引用”不等于“语义适用”，先过滤错实体、错关系，再判断答案唯一性；按语义角色匹配主动、被动及无歧义逆表达。虚构正例包含“错误父亲关系＋正确丈夫事实”和“错误电影＋正确导演事实”。没有把评测八题的姓名或答案写入生产 BIND 提示词。

UPDATE、PLAN、基数、REBIND、协议解析、状态机和动态输入 renderer 的冻结哈希保存在 `tests/fixtures/r2_memory_ab/frozen_contracts.txt`。回归测试及实验准备都会核对这些哈希。

## 第一阶段：精确复放

来源为历史 run7 的 14 次真实 MEMORY 请求。源 SQLite 以只读方式打开，按 `context_id` 和 `state_revision` 恢复调用前状态。A 的完整请求逐字匹配历史请求；B 只替换提示词，数据区逐字相同。

每个用例保存 mode、实例化查询、cardinality、有效上游绑定、旧绑定、全部可见候选、短 ID 与永久 ID 映射、旧输出、预标注和原始快照。14 个请求都是 BIND，必要输入变量均已实例化。

标注只依赖可见事实，包含 9 个应绑定请求、5 个应 NOOP 请求。

- `dev_7920.Q1.2`：错误 father 事实不影响正确 husband 事实，期望绑定 Frederik，仅引用正确事实。
- `dev_4961.Q1.2`：排除其他电影导演，期望绑定 Alexander Korda；不要求同时知道死亡地点。
- `dev_10616.Q1.1`：仅有 son-of，没有确定父亲角色的依据，期望 NOOP。父母角色、补充男性依据及女性边界放在第二阶段作合成对照。
- `dev_954.Q1.1`：两个真实国籍与 SINGLE 不兼容，期望 NOOP，不改基数。
- `dev_4784.Q1.1`：复合事实给出两名导演，SINGLE 无法唯一绑定，期望 NOOP。
- `dev_91.Q2.1`：当前事实只支持 Norwegian government，不能用最终金标要求模型凭空恢复 United Nations。

## 第二阶段：受控扰动

在第一阶段预标注可绑定的 9 个请求上构造 38 个扰动：2 个多事实快照交换顺序；每个请求分别插入错实体事实、插入错关系事实、改写支持事实为语义等价表达、加入直接反证。再为 dev_10616 添加 3 个角色对照，共 41 项。

单条事实的快照不做无效的单元素交换。插入噪声时将噪声放在最前面，检查正确支持不位于首行时的稳定性。角色对照分别为：改问 parent；保留 father 查询并增加明确男性事实；保留 father 查询并增加明确女性事实。这些是显式合成条件，不表示历史原文已经提供了这些证据。

所有标签与扰动在新 A/B API 请求前冻结，A/B 共享同一份数据和短 ID 映射。每个测试项只取一个回答，不进行语义重试，不对 NOOP 强制要求 BOUND。保留现有 API 传输重试与全部请求日志。

## 评分

正确结果要求：决定正确、值正确、支持事实集合正确、协议可解析，并能在隔离的调用前 runtime 快照上通过真实事务校验。每条回答独立应用，绝不修改历史运行或串联新下游调用。

报告绑定召回率、错误绑定数、应弃答样本的正确 NOOP，以及扰动下答案和支持的稳定性。错误绑定包括不应绑定时绑定、绑定错值、引用无关事实。API 或协议错误不计为正确 NOOP。

结果文件位于 `results/r2-memory-bind-ab-text15-text16/`：

- `cases.jsonl`：全部 55 个用例、预标注、A/B 请求和 runtime 快照。
- `manifest.json`：来源、配置、套件及冻结接口哈希。
- `api_calls.sqlite`：真实 API 请求与响应。
- `results.jsonl`：逐项输出及评分。
- `summary.json`、`perturbation_summary.json`、`report.md`：两阶段汇总及明细。

## 复现

```bash
# 准备新目录；文件已存在时拒绝覆盖冻结实验。
.venv/bin/python scripts/evaluate_r2_memory_ab.py prepare --output results/memory-ab-new
.venv/bin/python scripts/evaluate_r2_memory_ab.py run --output results/memory-ab-new --stage 1
.venv/bin/python scripts/evaluate_r2_memory_ab.py run --output results/memory-ab-new --stage 2 --concurrency 3
.venv/bin/python scripts/evaluate_r2_memory_ab.py report --output results/memory-ab-new
```

此次不是端到端八题测试，不报告最终 EM。BIND 变化可能产生历史轨迹中不存在的下游调用，需要后续独立端到端评估。旧结果目录不会混入新 BIND 提示词；新版本的运行配置审计会识别 BIND 提示词版本变化。
