# R2 统一 MEMORY：当前子问题与事实 → 完整受支持结果

## 启用与兼容边界

在 R2 事实成员模式中设置：

```json
{
  "protocol_version": "v5.2-r2",
  "sentence_splitting": false,
  "memory_interface": "unified_evidence_v1"
}
```

该开关同时选择完整查询桶取证、冻结授权、统一提示词和输出协议，以及无独立 RECALL 模型调用的调度。
接口版本为 `query-facts-result-v1`，内部授权策略为 `unified-evidence-v1`。
原文审阅和旧 `QueryPlanV3` 运行状态不能使用此接口；新运行的计划使用 `EvidencePlanR2`。

省略开关时保留 `legacy_bind_rebind_v1`，用于旧实验和检查点兼容。新配置将开关写入运行指纹；
同一 run ID 的检查点不能在新旧接口之间切换。旧状态、事件和没有新增字段的 MEMORY 快照仍可读取。

## 模型输入与输出

MEMORY 不接收总 Question、完整计划、Mode、旧答案、事实来源标签或查询状态。
初始 PLAN 和最终 ANSWER 继续接收总 Question；其他输入边界沿用当前实现。

```text
Current query:
Q2 | Who is Bob's mother?

Facts:
F1 | Alice's mother is Helen.
F2 | Bob's mother is Mary.
F3 | Bob's father is David.
```

有支持结果：

```text
Q2 | F2 | Mary
```

多条事实联合支持时写 `F2,F5`；多个兼容答案各写一行，返回本次材料支持的完整答案集合。
相同答案的多行由 Runtime 合并支持，不拆分专名中的逗号或 `and`。

无支持结果：

```text
Q2 | NONE | UNKNOWN
```

UNKNOWN 必须单独出现，含义是本次材料未支持答案。旧证据仍支持旧答案时，模型仍应返回该答案和支持编号。
Runtime 判断它是否与旧值和证明完全一致。UNKNOWN 保留已有绑定，但不会被记为重新确认。
本次保留非破坏性空结果合同，尚未引入“仅有反证而无替代答案时自动撤回旧绑定”的新机制。

仅当 `allow_upstream_only` 资格、上游成员路径和当前实例均有效时，输入才增加 `Inputs`。
这时允许 `Qn | NONE | 具体答案`；普通请求必须引用显示的事实编号。

输出只接受当前 QID、当前请求的短事实编号和单个具体值。错 QID、未知编号、数组、损坏行、
UNKNOWN 与答案混用都会拒绝整份响应。MEMORY_REPAIR 使用同一任务与冻结快照，要求完整替代输出。
API 的 `finish_reason` 必须为 `stop`；截断、过滤、缺失完成状态等不能把合法文本前缀发布为完整集合。

## Runtime 的取证与调度

| 来源 | 材料与触发 |
| --- | --- |
| 初次绑定 | 当前查询完整事实桶，包含历史未采纳候选和新事实 |
| 历史候选唤醒 | 上游依赖就绪后，使用新的具体子问题检查完整历史桶 |
| 复核已有绑定 | 完整桶加实际旧支持；包含新待审事实和历史未采纳候选 |

事实按持久化 ID 去重，不经模型预筛选，不把旧答案反写成事实。DORMANT 查询在依赖未就绪时只保存候选。
新模式不创建 RECALL job 或 WAITING_RECALL 会话；内部 BIND/REBIND 字段仍用于状态写入、事务和审计。
`enable_defer_callback` 控制旧接口的 RECALL 路径，新接口始终直接装载查询桶。

每个具体上游分支有单独的 Current query；事实可共享，模型不能选择要跟随的上游实体。
Runtime 冻结 `authorized_use_ids`、`fact_aliases` 和 `evidence_digest`，保证显示事实集合等于支持白名单。
获得检查权限不代表事实被接受；未选事实仍留在桶中。

依据签名包含接口版本、查询与版本、上游输入签名、事实 ID/正文，以及有完整枚举要求时的扫描闭合状态。
无关窗口、重复事实和仅有状态写入不产生新依据。相同依据的 UNKNOWN 不会被循环重问。
PLAN 修补路由、查询修改、上游变化和新事实会重新调度；复核屏障解除后也检查等待中的子查询。

新事实、PLAN 路由或必要的 EOF 闭合改变依据时，取消旧冻结会话。已发出的旧上下文不能提交。
多分支结果先暂存，全部完成后才原子发布整个查询快照；恢复和重复提交继续使用既有事务与幂等收据。
值、支持和父路径完全相同则不新建绑定；证明变化仍按既有策略使后继失效并重算。

## 预算与审计

完整候选集合受 `max_review_input_tokens` 和“实际输入＋输出预留”的 `max_context_tokens` 限制，
不按 `memory_token_budget` 裁剪，也不回退到模型筛选。超限明确报 `UNIFIED_MEMORY_INPUT_BUDGET`。
最终有效工作记忆继续受 `memory_token_budget` 约束。

请求与事件记录接口版本、来源、QID、分支、输入签名、事实及授权数量；分别统计
`unknown_result_count`、`unchanged_result_count`、`binding_changed_count`、支持事实和成员数量。
`recall_model_calls` 在新模式应为 0。输入/输出 token 同时保留供应商用量或本地估算来源；失败原始响应保留供审计。

## 新评测配置

配置继承既有 5000-token 直传 chunk、可选 PLAN 修补、Qwen3.5-9B、温度和原始文档顺序，仅选择新 MEMORY 接口并更换实验 ID / 输出目录。

- 8 题：`configs/v52_r2_smoke8_unified_memory.json`
- 完整 128 题、并发 4：`configs/v52_r2_full128_unified_memory_c4.json`
- 完整 128 题、并发 8：`configs/v52_r2_full128_unified_memory_c8.json`

```sh
.venv/bin/python -m delaybind_core.cli experiment --config configs/v52_r2_smoke8_unified_memory.json
.venv/bin/python -m delaybind_core.cli experiment --config configs/v52_r2_full128_unified_memory_c4.json
.venv/bin/python -m delaybind_core.cli experiment --config configs/v52_r2_full128_unified_memory_c8.json
```

以上命令会发起真实 API 请求。本次实现验证使用本地机制测试和固定响应客户端，未启动付费评测，
因此不对准确率或总 token 改善作结论。后续完整 128 题应同时比较新增正确与原正确转错，不能拼接旧正确结果。

## 本地验证

```sh
.venv/bin/python -m pytest -q tests
```

专用测试分布：

- `test_r2_unified_memory_protocol.py`：来源变化时视图一致、完整结果格式、别名、免证资格与格式修复。
- `test_r2_unified_scheduler.py`：历史候选、组合证据、重复调度、PLAN 修补、复核屏障、EOF、恢复和旧回放。
- `test_r2_unified_memory_application.py`：UNKNOWN、相同结果、证明变动、多分支原子发布及授权校验。
- `test_r2_unified_runner.py`：三跳端到端 0 RECALL、预算、配置隔离和真实 API 适配层的截断响应拒绝。
