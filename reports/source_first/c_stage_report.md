# C 阶段：前置原文核验

结论：**BLOCKED**。冻结 128 题、并发 8 的主运行中，C 为 **92/128**，低于已验收 B 的 **94/128**；按执行方案的整数正确题数不下降门槛，B 仍是稳定版本。本次 C 代码与配置保留作实验分析，不将其结果标为通过。

| 指标 | B | C |
| --- | ---: | ---: |
| 正确题数 | 94/128 | 92/128 |
| 答案 token F1 | 0.7625 | 0.7355 |
| 错误 / 资源限制 | 3 / 0 | 1 / 0 |
| 模型调用 | 1295 | 1340 |
| 输入 / 输出 token | 2,540,404 / 247,015 | 2,805,224 / 304,370 |
| 墙钟耗时 | 27.0 分钟 | 40.8 分钟 |

C 相对 B 有 **4 道错变对、6 道对变错**；翻转分布在多个题型，净下降 2 题体现在 bridge_comparison（B 21/24，C 19/24；其他题型净正确数不变）。128 个唯一 ID、题目、gold、原评分、数据与 tokenizer 指纹均通过复核；有效配置除 `memory_source_mode` 和运行标识外一致。离线 106 项 unittest 通过，严格契约 r3 的固定冒烟为 7/8。

工程轨迹审计无违规：377 次完成的核验全部为 `source_precheck`，每次 `MEMORY` 前有当前证据包的前置核验；没有活动 `proposal_postcheck`、低层 `VERIFY`、raw fallback 或救援事件。核验接口有 396 条调用记录（含 19 次重试），`MEMORY` 有 362 条；有效结果标签为 SUPPORTED 1052、SOURCE_DIFF 222、UNRESOLVED 170。另有 414 条不符合前置行协议或引用范围的输出行被拒绝，71 条 MEMORY 提议因来源/快照契约被拒绝。后两项是保守门槛的实际运行代价，不等同于 414 个独立事实或 71 道错题。

## 退步题观察

| ID | C 轨迹中的可核查观察 | 归因边界 |
| --- | --- | --- |
| dev_10616 | B 的 UPDATE 抽出“Jobst 的父亲是 Hermann Georg”；C 的 UPDATE 只抽出 Georg Ernst 的父亲与一条无关血缘事实，后续 MEMORY 两次 NONE。 | 证据抽取差异；不能单凭此题证明前置核验误拒。 |
| dev_8009 | 前置核验将“Cochin Haneefa 生于 1986”标成 SOURCE_DIFF，指出 1986 是电影年份；最终 ANSWER 仍把 1986 用作导演出生年，选错电影。 | ANSWER 仍可见旧摘要且不接收核验 note，是方案已记录的首版限制；同时有提议越界/动作错误被拒。 |
| dev_7403 | 两次前置输出把允许的 fact ID 拼错，均被拒绝，唯一事实变成 UNRESOLVED；没有 MEMORY 写入，ANSWER 回答丈夫姓名而非生日。 | 核验输出 ID 错误触发保守拒绝；这是明确的 C 协议失败样例。 |
| dev_7146 | 两轮核验都未给出合法 source_ref，相关事实全为 UNRESOLVED；ANSWER 在冲突导演与缺少可靠国籍时猜成 Canadian。 | 核验引用格式失败与 ANSWER 推断并存；不能把最终猜测当作原文支持。 |
| dev_2261 | C 写入两位导演和 Polanski 的生日，但 Cahn 的生日未建立；ANSWER 在缺值下猜选 Knife In The Water。B 轨迹同样未获得 Cahn 生日，最终却猜中 Born To Speed。 | 比较证据不足与模型猜测波动；不归为前置核验单独造成。 |
| dev_432 | C 的 Q3 绑定提议因完整上游支持未全部通过该查询本轮核验而被拒；Q4 Germany 已写入，另一导演国籍未写入，ANSWER 把“未知”推成“不同”。 | C 的完整支持门槛与抽取差异叠加；需固定中间状态重放才能细分贡献。 |

首次全量尝试因默认沙箱禁网，在 24 条首次 API 请求失败后中止，保留在 `results/source_first/c-source-verify-c8/`，不作为评分或挑选性重跑。主运行在可联网环境重新从全部 128 题开始，使用新目录 `results/source_first/c-source-verify-c8-r2/`，没有只重跑错题。

可复核文件：`c_gate.json`、`paired_diff.csv`、`c_regressions.md`、主运行目录中的 `results.jsonl`、`summary.json`、`trajectories/`、`config.resolved.json`、`code_manifest.json`、`run.log`。答案评分沿用项目 `answer_exact` 归一化，不宣称为未经修改的 2Wiki 官方 EM。
