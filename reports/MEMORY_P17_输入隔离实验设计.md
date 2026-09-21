# MEMORY P17 输入隔离实验

## 修改边界

本轮保留生产 A=text-15、B=text-16，生产默认仍是 B。UPDATE、PLAN、基数、REBIND、绑定事务及原文/分句开关不变。新增的 A-neutral、P17-full、P17-local 和候选诊断只由离线实验脚本调用，不接入生产 runner。

A-neutral 只统一替换 A 中示例人名；规则、句式、关系、示例数量和顺序、系统提示、动态数据、协议标签不变。P17-full 沿用 B 的系统提示和全部动态输入，只替换指令；P17-local 再把动态输入缩为 Current query、cardinality、Effective upstream bindings、Facts。实验版本号记入实验记录，不向提示词加入随机字串。

独立候选诊断返回结构化候选、支持、排除和反证，不询问旧 NOOP 的原因，不要求思维链，绝不调用生产解析器或提交绑定。它是另一条新请求，不能用其成功倒推原 NOOP 请求内部已经找到了答案。

## 第一阶段：预先冻结的条件

78 个条件，各固定重复 3 次，共 234 个模型回答。所有重复均计入；不对已得到的模型回答进行语义重试，不更换失败回答。

- 19 个核心快照 × B/P17-full/P17-local。包含 dev_4961 原始、反序、等价措辞、错实体/错关系噪声；dev_5343 带头衔/裸名；dev_7920 原始/反序；dev_10616 的 parent、father 缺角色、补男性角色、明确母亲、直接父亲等对照；保留错实体、错关系、真实多值 NOOP 控制。
- 6 个重点快照额外比较 A/A-neutral，保留与核心 B 的配对比较。
- dev_10616 parent 和 dev_4961 director 各补 D10/D01/D11；D00 是核心 B。只修改展示，不修改图中变量或事实。
- dev_4961 的 I00=原序/正确 F2，I10=反序/正确 F2 已在核心 B；另补 I01=原序/正确 F1、I11=反序/正确 F1。
- 一个独立的词面重叠合成对照，只将错电影 Fanny and Alexander 改为 Silver Harbor，保持正确事实 Alexander Korda 不变。

dev_5343 带头衔条件在调用前标注同一对象可接受全名或 Richard of Shrewsbury；裸名条件只接受裸名。旧 55 项回归的原标注不追溯修改。

事实顺序与短 ID 分别操作。插入噪声保留旧 F1/F2，新事实使用 F3；引用在评分前严格通过本条件映射还原持久 ID，再调用现有文本解析器及隔离快照上的真实运行时事务。未知、重复 ID 不进行自动修复。

短版仍漏绑时，针对任一短版未正确绑定的正例以及三个预定 NOOP 控制各执行 3 次独立候选诊断。诊断分离候选缺失、无关候选、伪反证、正确候选却 NOOP 和候选/决策均正确；诊断条件根据第一阶段结果自适应选择，但调用前固定，不回溯改动第一阶段。

## 第二阶段：冻结选择后再验证

选择标准在第一阶段调用前固定：优先选择未观察到误绑的短版，再按核心条件的题目家族宏平均完全正确率排序；同分选 P17-local。单独记录它是否超过 B。若两个短版均有误绑，则仅选表现较好者用于诊断性回归，不批准上线。选择后不再调提示词。

第二阶段比较 B 与冻结版本：旧 55 项开发/回归集，以及调用前已标注的 30 项新验证条件；每项每版本重复 3 次，共 510 次调用。

新条件来自 12 个不属于旧八题的问题家族，以及一个完全合成的作品名称家族。历史来源是 V5.1 128 题运行的真实 MEMORY 输入：只抽取原问题、明确的当前查询和当时可见事实，投影到独立单查询 R2 快照，不读取历史模型答案或最终金标。它们不是原生 R2 历史轨迹，也不能据此声称 R2 已在这些题上端到端成功。

新验证覆盖直接事实、被动/逆表达、同一答案多事实支持、两事实角色组合、错实体/错关系、角色缺失、反证、带头衔姓名、包含 and 的单个作品及真正多个作品。所有合成补充与语义修改均在 provenance 中记录。

为三个新问题家族另设反序、纯换 ID、插噪声条件。稳定性比较使用还原后的答案与持久支持集合，并同时报告两边都正确的比例；稳定的错误 NOOP 不计为正确。

## API 和评分

模型及线上参数固定为历史调用参数：Qwen/Qwen3.5-9B、temperature=0、top_p=0.95、enable_thinking=false、max_tokens=10000。未开展 thinking 参数实验。

新建结果目录与 API 日志库。每个预定重复使用不同本地 run_id，wire prompt 不变，确保绕过本地响应缓存；每条记录审计 cache_hit。只允许补跑没有模型回答的传输失败，全部 HTTP 失败和补测历史保留。并发上限 3。

完全正确要求协议、隔离运行时、决定、答案值及全部直接支持集合均正确。API/协议错误不能算正确 NOOP。报告题目家族宏平均、漏绑、误绑、重复一致性，以及顺序/ID/噪声稳定性，不能把同一题多个扰动当独立题数。

本轮不以八题最终 EM 选提示词。第三阶段需要新快照净收益这一前提；只有条件成立，才考虑另行推进冻结 PLAN/UPDATE 的 RECALL→MEMORY→激活→ANSWER 联调。禁止事后替换旧 MEMORY 输出冒充端到端轨迹。

## 复现

```bash
.venv/bin/python scripts/evaluate_r2_memory_isolation.py prepare --output results/新的实验目录
.venv/bin/python scripts/evaluate_r2_memory_isolation.py run --output results/新的实验目录 --stage 1
.venv/bin/python scripts/evaluate_r2_memory_isolation.py diagnose --output results/新的实验目录
.venv/bin/python scripts/evaluate_r2_memory_isolation.py run --output results/新的实验目录 --stage diagnostic
.venv/bin/python scripts/evaluate_r2_memory_isolation.py select --output results/新的实验目录
.venv/bin/python scripts/evaluate_r2_memory_isolation.py run --output results/新的实验目录 --stage 2
.venv/bin/python scripts/evaluate_r2_memory_isolation.py report --output results/新的实验目录
.venv/bin/python scripts/evaluate_r2_memory_isolation.py audit --output results/新的实验目录
# 新快照有收益后，进行隔离下游联调（不切换生产默认）：
.venv/bin/python scripts/evaluate_r2_memory_chain.py --parent results/新的实验目录
```

对未取得模型回答的 API 失败可加 `--retry-api-errors`。默认输出目录为 `results/r2-memory-isolation-p17-20260921`，不会覆盖上一轮 A/B 结果。
