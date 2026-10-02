# V04：UPDATE 三列输出与 Runtime 状态路由

日期：2026-10-01。基于清理后的 V04（359d171）。本轮没有迁移 JSON。

## 修改范围

- UPDATE 事实行改为 `Qn | source_ids | fact`；移除末尾 ACTIVE/DORMANT/COMMIT/DEFER。
- 保留来源编号允许集合、PLAN_HINT、自然语言事实和无证据时的 NONE。
- FactEvent 不再有 relevance 字段；Runtime 按本窗口入库开始时的 query 状态路由：DORMANT 入候选池，ACTIVE/RESOLVED 接收为待 HIGH 判断的事实。
- 事实接收不等于完成绑定；已有来源核验、pending review、依赖版本及 BIND/REBIND 检查保留。
- 修复提示同步改为三列；四列旧格式被记录为拒绝，不作为第四列文本写入事实。
- MEMORY_GROUNDED、RECALL、PLAN、ANSWER 的提示词未改；没有添加 rescue 或 raw fallback。

## 验证与运行

- 本地相关测试：64 passed；覆盖同窗口先登记后绑定、空桶回查后新增事实、RESOLVED 更正事实、重复候选、来源/ID 校验和转义字符。
- compileall 与 git diff --check 通过。
- Qwen/Qwen3.5-9B，SiliconFlow，seed=4，并发=8；完整 128 个唯一 ID，同一题集顺序，耗时 25.69 分钟。
- 题集、tokenizer 和逐题 gold 与对照一致；重新计算评分一致；本轮生产源码 SHA256 与运行开始时一致，没有刷新历史运行指纹。
- 126 个具有完整最终状态的轨迹重放一致；其余 2 题为 ERROR。
- 全量核对 518 条入库/延迟事件，全部与 Runtime query 状态一致；没有角色或接口越权。

## 单轮结果

| 指标 | 最近 V04 | 三列 UPDATE |
| --- | ---: | ---: |
| 正确题数 | 92/128 | 96/128 |
| EM | 71.875% | 75.000% |
| 答案 token F1 | 0.740972222 | 0.776748512 |
| ERROR | 1 | 2 |
| 逻辑调用 | 983 | 955 |
| 网络请求尝试 | 989 | 962 |
| 网络重试 | 6 | 7 |
| 缓存命中 | 0 | 0 |
| 输入 token | 2227096 | 2178302 |
| 输出 token | 308698 | 287348 |
| 每题延迟 p50（秒） | 66.99 | 48.00 |
| 每题延迟 p95（秒） | 203.59 | 216.72 |

EM 沿用项目现有 answer_exact（含既有归一化），失败、空答案都在 128 分母中；未改变评分或挑题重跑。网络 usage 是已记录值，不等同服务商账单。

逐 ID 对比：12 道错→对、8 道对→错，净增 4 题。

| 题型 | V04 | 三列 UPDATE |
| --- | ---: | ---: |
| bridge_comparison | 19/24 | 21/24 |
| comparison | 34/35 | 34/35 |
| compositional | 26/53 | 30/53 |
| inference | 13/16 | 11/16 |

## 协议与剩余问题

UPDATE 共 240 条模型响应，解析得到 732 条事实行、2 条 hint。30 条行在解析阶段被拒绝（含无来源占位、截断行等）；进一步来源/ID 归一化后，UPDATE_LINE_REJECTED 共 51 条事件。不将这些坏行伪装为格式完全通过。

- dev_2065、dev_4865 的 ANSWER 达到 4096 token 上限，原重试后仍缺 boxed answer，计为 ERROR。
- dev_5534 已绑定正确出生地，但 ANSWER 截断且解析结果为空，计为答错（既有解析行为，本次未改）。
- 状态误路由的机制已移除，但抽取遗漏、身份/关系误判和 ANSWER 输出问题仍可能导致失分。

## 门禁结论

**BLOCKED（严格验收）**：EM/F1 高于最近 V04，但 ERROR 从 1 增至 2；comparison 仍为 34/35，未达到方案保护集 35/35；inference 从 13/16 降到 11/16，需要关注。预定三轮候选/父版本配对验收尚未执行。

保留本轮改动与全部结果，停在本次修改，不宣称已晋升为通过所有门禁的版本。

## 产物

- `results/ultimate/v04_three_column_update_full128_r1/`：完整结果、模型轨迹、SQLite、运行日志、配置和源码清单。
- `paired_diff.csv`：128 题逐 ID 对照。
- `regressions.md`：全部退步题的轨迹观察。
- `gate.json`：分题型、分接口、耗时、成本和门禁。
- `parent_snapshot.json`、`unit_tests.log`：修改前源码指纹与本地测试。
