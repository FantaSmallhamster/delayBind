# MEMORY BIND 两阶段 A/B 测试结果

本轮已完成提示词修改及两个阶段的真实 API 对照，共 55 个用例、110 个有效回答。B 修复了 dev_7920 的直接绑定，但有回退；两个阶段的总分均与 A 持平，尚未证明 B 整体更好。

## 本轮修改

A 为原 text-15 MEMORY 提示词。B 将 Fact-only BIND 改为回答当前子问题的接口，先筛除错实体、错关系和无关属性，再按支持的结果值判断唯一性；允许无歧义的等价／逆表达，同时保留反证与多值约束。生产提示词加入 Robin/Morgan、Mira/Noah、Film Cedar/Film Pine 等虚构对照例，没有加入八题姓名和标准答案。

B 只在 Fact-only `mode=BIND` 及其格式修复调用中启用，独立版本为 `v5.2-r2-memory-bind-text-16-query-answer`。UPDATE、PLAN、基数、REBIND、动态输入及输出协议被冻结并核对哈希。当前默认 Fact-only BIND 已使用 B，旧 A 仍在代码和冻结实验请求中保留用于复现。

代码：`delaybind_core/prompts_r2.py`、`delaybind_core/subquery_runner_r2.py`。实验入口：`scripts/evaluate_r2_memory_ab.py`。人工可核对的预标注及扰动定义：`tests/fixtures/r2_memory_ab/annotations.py`。

## 实验设置

来源是 run7 的 14 个真实 MEMORY 调用前快照，全部为 BIND。核对结果表明所有当前查询都已正确实例化，dev_7920 和 dev_4961 的后一轮请求确实同时包含旧噪声与正确直接事实。A 的第一阶段完整请求与历史请求逐字一致；B 的输入数据区与 A 逐字一致。

模型为 Qwen/Qwen3.5-9B，temperature=0、top_p=0.95、enable_thinking=false，最大输出为原来的 10000 tokens；请求不附加 seed。单个用例每版保留一个模型回答。第一阶段顺序调用，第二阶段最多三个独立用例并行，每个用例内按 AB/BA 交替顺序请求。没有对有效 NOOP 重采样或提示其改为 BOUND。

每个回答除检查文本协议，还在独立恢复的 runtime 快照上运行真实 MEMORY 事务校验。正确要求决定、值、支持事实集合同时正确。基数为 SINGLE 但事实确实多值，以及父亲角色依据不足的快照，按 NOOP 评分。dev_91 依据当前可见工作机构事实评分，未注入最终金标。

## 第一阶段：14 个真实快照

| 指标 | A：原版 | B：新 BIND |
|---|---:|---:|
| 完全正确 | 12/14 | 12/14 |
| 可绑定样本的正确绑定 | 7/9（77.8%） | 7/9（77.8%） |
| 应 NOOP 样本正确弃答 | 5/5 | 5/5 |
| 错误绑定 | 0 | 0 |
| 最终未解决 API 失败 | 0 | 0 |

重点样例：

| 请求 | A | B | 判断 |
|---|---|---|---|
| dev_7920.Q1.2，father 噪声＋husband 事实 | NOOP | BOUND Frederik，仅 F2 | B 修复 |
| dev_4961.Q1.2，错误电影＋Yamata 导演 | NOOP | NOOP | 均仍漏绑 Alexander Korda |
| dev_5343.Q1.1，明确 husband 事实 | BOUND Richard of Shrewsbury, 1st Duke of York | NOOP | B 回退，抵消 dev_7920 的收益 |
| dev_10616.Q1.1，仅 son-of 事实 | NOOP | NOOP | 本次可见事实未确定父辈性别，不能据金标强行要求 father 绑定 |
| dev_954.Q1.1，两种国籍＋SINGLE | NOOP | NOOP | 均符合冻结的 SINGLE 合同 |
| dev_4784.Q1.1，两名导演＋SINGLE | NOOP | NOOP | 均没有任意挑选一位导演 |

## 第二阶段：41 个扰动

在预标注可绑定的 9 个快照上构造顺序、错实体、错关系、等价改写和反证扰动，再加入 3 个 dev_10616 角色条件对照。单事实列表不做无效的顺序交换，因此顺序类只有 2 项。

| 扰动类型 | 用例数 | A 完全正确 | B 完全正确 |
|---|---:|---:|---:|
| 交换事实顺序 | 2 | 0/2 | 1/2 |
| 插入错实体事实 | 9 | 9/9 | 9/9 |
| 插入错关系事实 | 9 | 9/9 | 9/9 |
| 等价表达改写 | 9 | 7/9 | 6/9 |
| 加入真正反证 | 9 | 9/9 | 9/9 |
| 改问 parent 的逆表达对照 | 1 | 0/1 | 0/1 |
| 保留 father 并增加明确男性依据 | 1 | 0/1 | 0/1 |
| 保留 father 并增加明确女性依据 | 1 | 1/1 | 1/1 |
| 合计 | 41 | 35/41 | 35/41 |

其中 31 项应绑定，A/B 均正确绑定 25 项（80.6%）；10 项应 NOOP，双方都正确弃答。两个版本均未产生错误绑定。

B 相比 A 的局部收益：

- dev_7920 交换顺序后仍可正确绑定。
- dev_4961 将正确事实改成 “Film Yamata was directed by Alexander Korda.” 后可绑定，原表述及反序表述仍 NOOP。
- dev_91 的雇佣关系等价表达可绑定。

B 相比 A 的局部回退：

- dev_11393 的 performer 等价改写返回 NOOP。
- dev_5343 的 mother 逆序表达返回 NOOP。
- dev_91 的 husband 逆序表达返回 NOOP。

两种提示词在 dev_10616 的 parent 对照以及补充男性依据的 father 对照中都返回 NOOP，角色匹配仍未稳定。parent 对照保持原始总问题和输出变量名称 `?father`，只改 Current query 的父母角色措辞；因此它同时检验能否以当前子问题为准，不能据此单独证明失败仅由逆向关系识别造成。

另外，dev_4961 在插入额外错实体或错关系事实后，A/B 都能绑定，而原始快照两者都失败。这说明模型对候选组合、措辞或呈现形式敏感，不能简单归因为“旧错误候选必然导致冲突”。新增噪声也会改变局部事实 ID，现有实验不能进一步区分这些因素。

## API 与审计

总计 146 次 HTTP 尝试，其中 110 次成功、36 次失败（500：1 次；503：27 次；读取超时：8 次）。9 个测试请求在原传输重试耗尽后进行了一次补测；仅补测无响应的请求，所有已经返回的 BOUND/NOOP 均保留首次结果。

最终 110 个 A/B 回答全部获得，均通过协议和 runtime 校验，未解决 API 失败为 0。已返回 usage 的调用合计输入 141264 tokens、输出 711 tokens。传输失败的服务端 token 消耗没有被可靠报告。

第一阶段 A 的结果与历史 text-15 的 14 个输出结论一致。所有错误输出均为漏绑产生的 NOOP，没有用协议失败替代语义结果。

完整冻结输入、映射、旧输出、预标注、A/B 请求、响应和 runtime 校验在：

`results/r2-memory-bind-ab-text15-text16/`

- `report.md`：55 项逐项 A/B 明细。
- `cases.jsonl`：每项完整输入与状态快照。
- `results.jsonl`：完整评测尝试历史，保留 API 失败与补测。
- `api_calls.sqlite`：逐次 API 请求／响应。
- `summary.json`、`perturbation_summary.json`、`telemetry.json`：语义结果、扰动分类与服务错误分别统计。

本轮只比较 A/B，不含 C 的基数修正，也未重新运行 PLAN、UPDATE、RECALL、ANSWER 或计算八题最终 EM。当前证据支持“局部绑定改善，同时存在回退”，不支持宣称新提示词已提高整体效果。
