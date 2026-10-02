# V06a 逐题退化摘要

候选相对父版本共有 17 道 LOSS，10 道 GAIN；完整配对表见 `paired_diff.csv`。下面列出已检查的主要退化样例。描述只依据实际轨迹，不把父版本偶然答对视为证据链正确，也不把没有 JSON 校验失败的题归为语法故障。

| sample_id | 现象 | 轨迹定位 |
|---|---|---|
| `dev_4961` | Yamata 导演死亡地点证据缺失，两版在缺证据时猜测不同：父版 London，候选 Hungary。 | UPDATE JSON 解析通过；无明确死亡地点绑定。 |
| `dev_3890` | 行刑队处决事实进入 hints，未进入死因查询链，候选只答 execution。 | facts/hints 分配与下游可见性；无 JSON 解析失败。 |
| `dev_10191` | Robert Rossen 身份/日期进入 hints，Q2 未绑定，候选最终猜 Los Angeles；父版利用了更多人物背景猜 Hollywood。 | 证据覆盖与答案上下文差异。 |
| `dev_12309` | UPDATE 正确抽取 Lőcse/Levoča，但 MEMORY 使用了拼错的事实 ID，Q2 未激活，候选答 Pest。 | 未迁移的 MEMORY 文本协议。 |
| `dev_4298` | Victoria Aitken (née Lockwood) 被当作另一位亲属，正确生日放入 hints，候选回答母亲姓名。 | 别名身份判断和 facts/hints 分配。 |
| `dev_1566` | Cursed 导演事实关联到错误 Qn；一次非法来源 ID 修复后仍未完成比较分支，候选答 no。 | query 分配/比较链；不是 JSON 语法故障。 |
| `dev_5351` | Joseph I 的父子关系两次都放入 hints，Q1/Q2 未激活，候选答 Leopold I。 | facts/hints 分配；无 JSON 校验失败。 |
| `dev_4060` | 两版都缺 Jim Wynorski 出生地绑定，候选把人物名当答案，父版用外部猜测答 New York City。 | 最终证据缺口与答案行为差异。 |
| `dev_6901` | near Coutances 的人物资料进入 hints，候选猜 Normandy；父版把背景位置作为事实答 Coutances。 | 人物地点背景可见性差异。 |
| `dev_7403` | UPDATE 正确抽取丈夫和生日，但 MEMORY 两次拼错事实 ID，生日未绑定，候选答丈夫姓名。 | MEMORY 长 ID 错误。 |
| `dev_4237` | 原文明确包含 Fulbright scholarship，候选漏抽，最后答 None。 | UPDATE 内容覆盖不足；JSON 结构合法。 |
| `dev_5853` | 候选把 Antonio Margheriti 与另一部影片混淆，并赋予与 Edmond T. Gréville 相同生日，答 Neither。 | 实体/影片混淆。 |
| `dev_10047` | 候选区分 Mary Mazzio 与 Alfred Hitchcock 后答 no；父版把两部片都归给 Hitchcock 后答 yes。 | 父版有身份混淆，候选有国籍推断。 |
| `dev_5002` | Júdás / Michael Curtiz 事实进入 hints，另一侧缺失，候选答 no；一次 Q37 非法 ID 触发局部修复。 | query 关联不足；不是 repair exhaustion。 |
| `dev_5547` | 导演和出生地都被抽取但全部进入 hints，正式工作记忆为空，候选答 None。 | facts/hints 分配与下游可见性。 |

其它 LOSS：`dev_3448`、`dev_4478`。它们已包含在 `paired_diff.csv`，应在后续 V06b 前先做局部回放，不要用下一接口迁移来覆盖本轮退化。
