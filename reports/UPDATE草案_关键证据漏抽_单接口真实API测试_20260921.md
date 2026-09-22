# UPDATE 草案：关键证据漏抽的单接口真实 API 测试

## 结论

使用对话中展示的英文 P1–P5 改写草案，仅调用真实 UPDATE API。

- 两个具有完整关键证据、输入与历史请求完全一致的漏抽窗口中，修复 1 个：`dev_2065` 抽到父亲出生日期；`dev_2955` 仍输出 `NONE`。
- `dev_1850` 的关键婚姻事实实际跨越两个窗口，不能简单归为“完整事实已展示但漏抽”。原始第二窗口与补齐同文档前文的诊断对照都输出 `NONE`。
- 四次请求全部成功，零超时、零 API 错误、零缓存命中、零协议解析错误；所有返回的 finish_reason 均为 stop，不是输出长度截断。
- 没有运行 PLAN、MEMORY、RECALL、UPDATE_REPAIR 或 ANSWER。生产代码中的 UPDATE prompt 未修改；没有改变窗口切分或原文回看逻辑。

这不是端到端正确率测试，也不是新旧 prompt 同期重复采样的 A/B。旧结果来自历史日志，新草案每个输入只请求一次。

## 参数与冻结边界

- 模型：`Qwen/Qwen3.5-9B`
- 地址：`https://api.siliconflow.cn/v1`，普通 DNS，无固定连接 IP
- temperature：0；top_p：0.95；seed：未指定；thinking：关闭
- max_tokens：10000；timeout：120 秒；API 错误最多重试 2 次，本次均首次成功
- 保留历史 system prompt；仅替换 UPDATE 指令及版本标记
- 精确回放条件：Question、Extraction targets、Current window 保持历史内容；没有缩短原文、只挑支持段落或注入标准答案
- 独立对照条件：只在 `dev_1850` 第二窗口前恢复同一个 Document 44 的已读前缀；完整拼接段逐字存在于原始数据。该条件不能计入同输入 prompt 改善率
- 所有评价目标在请求前保存；不向模型发送评价标签

`dev_8904` 未调用 API：草案直接用 James Franklin 的真实日期、Boston 和 Newport 作示例，已包含该题答案，不能用它验证独立泛化能力。

## 逐项结果

| 请求 | 输入条件 | 应关注的关键证据 | 历史输出 | 新草案输出 | 判断 |
|---|---|---|---|---|---|
| dev_2065 / 窗口 1 | 原始完整窗口，包含 Document 37 的出生日期 | James Power，6 December 1800 | 只有父子关系，生日查询收到父子关系 | 正确父子关系＋出生日期 | 关键证据召回改善 |
| dev_2955 / 窗口 1 | 原始完整窗口，包含 Document 18 的出生地 | Otakar Vávra，Hradec Králové | NONE | NONE | 仍漏抽 |
| dev_1850 / 窗口 2 | 原始窗口，仅包含婚姻句的尾部 | Constantin J. David 与 Käthe von Nagy 的婚姻 | NONE | NONE | 完整关系未展示，不能作为该事实的纯漏抽样本 |
| dev_1850 / 窗口 2＋同文档前缀 | 诊断对照，补齐 Document 44 | Constantin J. David 与 Käthe von Nagy 的婚姻 | 无同输入历史结果 | NONE | 完整关系已展示，仍漏抽 |

### dev_2065：实际输出

```text
Q1 | Sir John Talbot Power, 3rd Baronet is the son of James Power.
Q2 | James Power was born on 6 December 1800.
```

关键日期与原文一致。原文完整身份为 Sir James Power, 2nd Baronet，输出省略了爵位限定；本次按关键日期和主体关系召回计成功，不代表所有身份限定都完整保留。没有运行下游绑定或最终答案，不计算 EM。

### dev_2955：证据完整，但仍输出 NONE

原始窗口中的 Document 18：

```text
Otakar Vávra
Otakar Vávra (28 February 1911 – 15 September 2011) was a Czech film director, screenwriter and pedagogue.He was born in Hradec Králové, Austria-Hungary, now part of the Czech Republic.
```

提取目标仍为：`Q2 | What is the place of birth of ?director?`。

该具名出生地事实可以作为未绑定变量的下游候选，模型仍未报告。

### dev_1850：输入边界问题与漏抽问题同时存在

历史第一窗口的 Document 44 结束于：

```text
Constantin J. David (18 February 1886 – 19 February 1964) was a German journalist, film director, film producer, and actor.He is also known for his marriage to the
```

历史第二窗口的开头仅为：

```text
 Hungarian actress, singer, and model Käthe von Nagy.
```

所以第二窗口单独没有这段婚姻事实的完整主体和关系上下文。此前“第二窗口有明确完整婚姻事实但没抽到”的归因需要纠正。

诊断对照只恢复 Document 44 的前缀，形成：

```text
Document 44:
Constantin J. David
Constantin J. David (18 February 1886 – 19 February 1964) was a German journalist, film director, film producer, and actor.He is also known for his marriage to the Hungarian actress, singer, and model Käthe von Nagy.
```

仍然输出 `NONE`。这说明句子跨窗口不是全部原因：完整婚姻关系展示后，当前草案与模型组合仍未抽出这个下游候选。原始窗口返回 NONE 也不等于其余所有候选都已正确处理；本次只标注目标婚姻证据。

## 调用统计

| 请求 | 输入 tokens | 输出 tokens | API 延迟 |
|---|---:|---:|---:|
| dev_2065 原始窗口 | 6,723 | 39 | 2.03 秒 |
| dev_2955 原始窗口 | 6,622 | 1 | 3.28 秒 |
| dev_1850 原始窗口 | 1,918 | 1 | 0.80 秒 |
| dev_1850 同文档补齐 | 1,977 | 1 | 0.68 秒 |
| 合计 | 17,240 | 42 | 6.80 秒 |

两个严格同输入且完整证据可见的漏抽样本中，历史关键证据命中 0/2，本次为 1/2。样本很少，且旧结果未重跑，不能外推整体准确率。

## 对下一步的启示

新草案尚不足以稳定解决漏抽，不宜直接认定成功并替换生产 prompt。

仍值得检验的假设是：模型受原问题影响，要求先知道电影与导演的完整链路，而没有按未绑定变量的语义保留当前具名候选。这与 dev_2955、dev_1850 的输出一致，但本轮没有通过专门对照证明因果关系。

后续可固定上述输入，单独对照“保留原问题”与“只显示抽取目标”，检验是否存在原问题干扰；窗口跨句问题应单独处理，不与 prompt 收益混算。本轮未执行这些额外请求。

## 产物

- 草案：`experiments/update_p1p5_draft/prompt.txt`
- 独立测试脚本：`scripts/evaluate_r2_update_draft.py`
- 冻结输入、预声明目标及哈希：`results/r2-update-p1p5-draft-20260921-193805/cases.json`
- 原始 API 请求、响应和使用量：同目录 `experiment.sqlite` 与各 case JSON
- 全部输出：同目录 `results.json`
- 不含密钥的参数审计：同目录 `audit.json`
