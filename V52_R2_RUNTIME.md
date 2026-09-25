# V5.2-R2：原文/事实双模式绑定与重绑定

本实现按《DelayBind_V5.2-R2_MEMORY绑定与重绑定_全局修改设计》接入。用户明确选择：
**R2 也保留文本模型输入输出，只采用新职责与状态机，不切换为整份 JSON 模型协议。**
内部计划现使用 `schema_version: r2-members-1`；旧 `v3` 轨迹仍可读取。DTO、SQLite 与配置文件的 JSON 是内部表示，不是给模型的输入输出合同。

旧 V5.1 / graph 和 V5.2 路径仍保留，默认 `RunnerConfig` 仍是 V5.1。
新运行须显式设置 `protocol_version="v5.2-r2"`，使用新的 run ID / 输出目录。
不得把旧轨迹或原来的八题结果重新标成 R2。

## 统一 MEMORY 接口

新增 `memory_interface="unified_evidence_v1"`：在 fact-only 成员模式中，MEMORY 只接收具体子问题和完整候选事实，
输出 `Qn | 支持事实短编号 | 答案`。Runtime 负责绑定与复核，无独立 RECALL 模型调用。
输入、权限、UNKNOWN、预算、恢复及新 128 题配置见 [V52_R2_UNIFIED_MEMORY.md](V52_R2_UNIFIED_MEMORY.md)。
省略该开关时保留 `legacy_bind_rebind_v1`；本页以下 BOUND/NOOP、RECALL 准入描述属于该兼容接口。

## 当前默认：无基数预设的成员依赖图

新建 R2 运行默认采用成员图；PLAN 不定义 SINGLE/SET。`EvidencePlanR2` 中只有查询、输出变量、输入依赖及可选的完整枚举需求。
传入旧 V3 计划创建新运行时，runner 转换为新计划；恢复已有旧轨迹则保持其旧合同，不原地改写历史。

查询模板与成员节点分开：一个查询可得到零个、一个或多个结果。一个查询的当前 binding 是原子发布快照，
其 `members` 才是 Working Memory 中的实体/值节点。每个成员保存自己的 `direct_fact_ids`、`parent_member_ids` 和 `lineage`。
相同值来自不同上游路径时保留不同证明节点，不能仅按值合并而丢掉归属。

例如 Q1 得到 Team Red，Q2 查询其成员，Q3 查询每个成员出生地：

```text
Q1 成员节点：Team Red（由 F_team 支持）
  ├─ Q2 成员节点：Alice（由 F_alice 支持）
  │    └─ Q3 成员节点：Paris（由 F_paris 支持）
  └─ Q2 成员节点：Bob（由 F_bob 支持）
       └─ Q3 成员节点：Rome（由 F_rome 支持）
```

Alice、Bob 都依赖同一个 Team Red 节点及其证明；Paris 只继承 Alice 的路径，不能接到 Bob。
`state.member_graph` 和最终 evidence pack 中的 `members/member_edges` 显式导出这些节点及边。
旧的 query edge/binding port 只表达查询级路由；`navigation_links` 同样改为成员级精确证明，不再把全部父事实接给每条子事实。

主要流程：

1. UPDATE 按查询模板 QID 路由有来源支持的事实；成员图事实模式保留最小自足的原文句子或子句，不猜结果个数，分句实现仍是 syntok。
2. RECALL 对查询模板扫描完整候选桶，涵盖所有成员；后续 MEMORY 再做具体实体匹配。
3. Runtime 从有效上游成员生成具体分支，每个 MEMORY FINAL 请求只代入该分支的实体，绝不把名字列表拼进一个实体槽。
4. 多父汇合使用 lineage 相容性连接：同一祖先必须是同一成员，防止甲的城市与乙的学校交叉配对；独立祖先才做组合。
5. MEMORY 每个结果一条 BOUND，逐条引用自己的支持事实。Runtime 绑定父成员边，模型无需输出内部成员 ID。
6. 分支结果持久暂存；本次查询各分支完成后，原子发布成员快照。API/协议失败不发布半张结果图，恢复时继续未完成分支。
7. 新增或纠正成员创建新 binding revision，退休旧快照及受影响查询的后继，再 RECALL/绑定；未依赖它的独立查询不受影响。
8. ANSWER 读取全部有效工作记忆及成员依赖边，完成原问题的比较、计数等最终推理，不只返回某个叶子。

当前失效策略仍是**查询及其后继的保守重算**，不是只重算变化成员的最小子图。未变化的成员及证明使用稳定节点 ID，
但其下游可能重新调用 MEMORY；不宣称已有细粒度分支缓存优化。

“找到成员”与“已完整收集”分开：支持充分的成员无需等 EOF 即可绑定；`collection_scope_closed` 标记输入扫描完成。
缺证据的分支标为 `UNRESOLVED_MEMBER_BRANCH`，不是空集合。计数、全部成员、否定等结论仍需完整性依据；不能因 query RESOLVED 就假定成员已穷尽。

单查询分支展开上限 1024，超过则明确 `MEMBER_BRANCH_BUDGET`，不静默截断；仍受既有调用次数、轮数、token 预算限制。
新建 fact-only 运行使用 `strict-recall-v1` 准入策略和 `v5.2-r2-member-graph-text-26-current-query-only` MEMORY 提示词。历史 B/P17 提示词、快照及测试结果保留，不等于当前默认提示词。

## 新运行的事实准入与空工作规则

fact-only 的事实保存、获准进入当前 MEMORY、被绑定接受分别记录。ACTIVE/RESOLVED 的新 UPDATE 事件为当前 `(query_id, query_version, input_signature)` 授予一次待审准入；DORMANT 只入候选桶。RECALL 扫完全部批次且成功提交后，仅选中事实获得准入。未选候选留在桶中，不显示给 MEMORY，也不进入直接引用白名单。后续新证据到来时可重新回查历史候选。

MEMORY 的直接白名单由本会话未消费的 UPDATE/RECALL 准入和当前绑定必要旧支持构成。旧支持在 REBIND 屏障期间仍可用于复查。多成员分支共用查询级冻结授权集合，全部分支完成后才消费令牌并原子发布。NOOP 保留旧绑定及证明，将未采纳的新待审事实留作候选。API 或协议失败不会消费令牌或解除真实的重绑定屏障。

无候选可扫、无直接准入和旧支持、且没有合法纯上游推理资格时，Runtime 记录 `MEMORY_SKIPPED`，继续其他任务，不发模型 NOOP 请求。仅被下游消费、有有效父成员、且 PLAN 明确声明 `allow_upstream_only: true` 的中间查询可在无直接事实时尝试 MEMORY；提交无直接支持的 BOUND 仍需通过该资格校验。普通下游仅有 `inputs` 不满足资格。

新 fact-only 状态拒绝按旧策略恢复。旧记录仍可读取和离线审计；新运行需使用新 run ID。原文模式保留 REVIEW、HOLD、CONTEXT 和 UNBOUND 证据合同，不使用 fact-only 空工作门控。

## 各部分职责

| 部分 | 职责 | 权限限制 |
| --- | --- | --- |
| HIGH PLAN | 建立无基数 EvidencePlanR2；可选 REPAIR 修复需求、路由 | 不直接写绑定 |
| LOW UPDATE | 从当前窗口抽取事实并逐 query 路由 | 不接受事实、不绑定、不输出运行时状态 |
| LOW RECALL | 扫完冻结桶所有批次，选择潜在线索 | 不核验；失败不等于空选择 |
| HIGH MEMORY | 单 query 的 BIND / REBIND；分句模式审阅原文，事实模式直接选择事实证明 | 不 PATCH / ROUTE / FOCUS，不修改下游 |
| Runtime | 版本、权限、事务、双图、失效范围、补取和调度 | 不代替模型判断自然语言蕴含 |
| LOW ANSWER | 仅根据 question + 有效 working memory 作答 | 不读完整计划、Archive 或候选桶；有独立 Reader 客户端时走该客户端 |

LOW 新路径不能调用 VERIFY。`enable_defer_callback=false` 只关闭历史候选回查，不关闭 REBIND。

### 总 Question 的输入边界

初始 H·PLAN 接收总 Question 以生成 query plan，最终 L·ANSWER 接收总 Question 和有效工作记忆。
中间调用均不显示总 Question：UPDATE / UPDATE_REPAIR、RECALL、MEMORY / MEMORY_REPAIR（BIND 与 REBIND），以及 PLAN 的 REPAIR 模式。
此规则覆盖直传 chunk、Archive 窗口、事实模式和原文审阅模式。UPDATE 以提取目标和当前文本为输入；
RECALL 与 MEMORY 以当前子查询及各自获准的事实、绑定为输入；PLAN REPAIR 依据已有计划、hint 和事实修补。
内部审计快照仍可保存 `question`，该字段不会被上述中间调用的模型输入视图渲染。

## 文本模型协议

PLAN 沿用 `Q1 / query: / output: / depends_on:` 查询块。
`sentence_splitting=true` 是原文模式。UPDATE 每行一个路由；同事实关联多个 query 时重复正文和来源，Runtime 合并不可变正文：

```text
Q1 | D3:S1 | Cindy 的老师是 Alice。
Q2 | D4:S2 | Alice 的母亲是 Mary。
Q1 | D5:S0 | 新原文限定本题 Cindy 的老师是 Bob。
PLAN_HINT | D6:S1 | 与问题相关但计划没有覆盖的证据需求。
```

无相关项输出 `NONE`。模型只输出 query ID、来源和事实三个字段；
ACTIVE / DORMANT / RESOLVED 由 Runtime 按不可变 UPDATE 快照自动附加，模型不能输出或修改状态。
局部修复只处理原失败条目，不借修复重新抽取或改写事实。

MEMORY 对必需证据先分批 REVIEW，mode、query、phase 与 reason code 均由 Runtime 注入。
REVIEW 输出只包含模型必须判断的内容：

```text
REVIEW | F_old | REJECT | D3:S1,D5:S0
REVIEW | F_new | ACCEPT | D5:S0
```

需要时紧跟 `CORRECTION` 或 `CONTEXT`。Runtime 按 verdict/附加动作生成内部理由、代码和 proof kind。
FINAL 按具体上游分支调用，每个受支持成员输出一行：

```text
BOUND | Bob | F_new | D3:S1,D5:S0
BOUND | Alice | F_other | D4:S1
```

也可 `UNBOUND | D3:S1,D5:S0`。
旧五/六字段 BOUND、带理由的 REVIEW/UNBOUND 仍可读取，但多余的 kind/reason 只作兼容输入，
不进入运行时权威状态。REVIEW 阶段混入的 BOUND/UNBOUND，以及 FINAL 阶段重复的 REVIEW，
会被忽略并记录 `MEMORY_LINES_IGNORED`，不会执行越权动作或丢弃同响应中的合法行。
不再输出 BIND/REBIND 头、query ID、PENDING、RESULT、FINAL、END、reason 或 reason code。可追加：

```text
CORRECTION | F_old | N1 | D3:S1 | 修正后的完整事实
CONTEXT | F_held | D3:S1 | 1 | 1
```

CORRECTION 必须属于对应 ACCEPT，CONTEXT 必须属于对应 HOLD；它们不是独立顶层动作。
N1 仅在本提案有效；后续 FINAL 使用暂存结果显示的真实 corrected_fact_id。
当前成员模式一行一个具体值，不接受数组/对象；整份 JSON、围栏、旧顶层命令均拒绝。
旧轨迹保留旧标量/数组合同。相同分支的同值多条 BOUND 合并支持，不创建重复成员。
boxed 答案继续使用 `\boxed{...}`；无模型引用时不补造来源。

`sentence_splitting=false` 是事实模式，不只是“改用块锚点”。UPDATE 当次只读取 V5.1 窗口正文，
模型看不到 Archive ID，也不输出来源字段：

```text
Q1 | Cindy 的老师是 Alice。
Q2 | Alice 的母亲是 Mary。
```

UPDATE 输入在不同文档间显式加入 `<DOCUMENT_BOUNDARY>`；成员图事实模式的每行保留最小自足的来源句子或子句，
必要的角色、时间和限定不能因拆分而丢失。独立成员在保留支持含义的前提下拆行，不得跨文档拼接；单条事实中的换行仍由协议拒绝。
当前事实模式提示要求逐一匹配 Plan 中所有问题：未实例化的 `?变量` 可匹配任意具体实体或值，
其余关系、方向和限定条件必须与原文一致，已实例化的具体值仍须匹配。不得为了匹配问题改写原文关系。
成员图 UPDATE 在已有上游成员时，同时展示具体成员目标与原查询模板：具体目标指引当前分支，
模板只用于保留未来新增或纠正成员的下游候选事实，不创建成员绑定，也不扩大 MEMORY/RECALL 的具体分支。
显式无兼容分支的空自然连接仍不退回模板。`plan_repair_mode=on_hint` 时，事实模式还允许
`PLAN_HINT | 原文支持的事实及缺少的证据需求`；已有目标缺少答案不算计划缺项，最终比较或计数
也不应新增为计划查询。UPDATE_REPAIR 只能修复冻结的失败条目。

事实写入后，MEMORY、RECALL、PLAN REPAIR 和 ANSWER 均不恢复 Archive 原文。MEMORY 没有 REVIEW / CONTEXT / CORRECTION
阶段，只在当前 query 的具体分支中逐成员输出：

```text
BOUND | Alice | F1
BOUND | Bob | F2
```

或 `NOOP`。`NOOP` 不改变事实状态、当前绑定或下游链；兼容收到旧格式 `UNBOUND` 时也作为 `NOOP` 处理。
`BOUND` 只将所选 fact IDs 标成 ACCEPTED，其余事实保持 CANDIDATE，不因本轮未选而标成 REJECTED。模型不输出
原文索引、状态、reason、proof kind 或绑定版本。ANSWER 仍读取完整的有效工作记忆并完成最终推理，不直接返回叶子绑定。
新运行无 SET/SINGLE：多个值分别输出多行，不能用数组或拼接字符串冒充一个成员；带 and/逗号的单个专名不因此拆开。

成员图事实模式的 MEMORY/MEMORY_REPAIR 输入显示当前子问题、Eligible facts、有效上游绑定和现有绑定，不传总问题的 `Question` 区块；
不显示 FactUse 的 PENDING/CANDIDATE 内部状态、完整 barriers、`Decision authorized` 或 `scope_closed=false`。
Eligible facts 使用本次请求内的短 ID `F1/F2`，解析后由 Runtime 映射回不可变的持久化 fact ID；未知或重复短 ID
仍在协议边界拒绝。MEMORY 按固定实体、变量绑定、关系方向及限定条件逐项判断，多个可同时成立的结果不因数量而视为冲突。
REBIND 返回当前分支完整的受支持成员列表，包括应保留的旧成员；新成员与旧成员并存时应都列出。
仅在新证据确实纠正旧成员时删去或替换该成员；不能仅因出现更新的事实而舍弃旧成员。
该语义判断仍由模型负责，Runtime 校验证明权限及依赖结构，不自动判断自然语言真假。本路径不因 NOOP 自动追加二次模型复核。

事实模式的绑定转移固定为：

```text
无绑定 + 新事实不足 -> NOOP
无绑定 + 新事实成立 -> 建立绑定
已有绑定 + 没有更好事实 -> NOOP，保持旧绑定
已有绑定 + 新事实成立 -> 原子建立新绑定并退休旧链
```

## 状态机与一致性

- 使用键为 `(query_id, query_version, input_signature, fact_id)`；同事实在各实例独立判断。
- BindingStore 是两图唯一绑定来源；query edge 与 WM port 引用同一 binding ID。
- RESOLVED 新线索先登记 PENDING/inbox，再建 REBIND 屏障；旧绑定仍可审计，但该链暂时不 effective。
- 事实模式的 RESOLVED 新事实触发同一 REBIND 屏障；选择新证明时 Runtime 原子退休旧绑定及下游绑定，建立新 revision，并重新激活/RECALL 下游。
- REVIEW 只写会话暂存；所有批次完成后 FINAL 原子合并，不能提前破坏旧证明。
- 同值同证明只 reaffirm，不重算后继；值或证明变化都新建 revision，原子失效旧后继。
- 仅原文审阅模式保留 UNBOUND 的撤回语义。事实模式的 NOOP（及兼容的旧 UNBOUND）绝不撤回绑定或拒绝候选；retry_gate 只防止相同事实立即重试。
- route_index 保存所有历史路由事实，包括旧 ACCEPTED / REJECTED；新输入可以重新召回。
- 多父全部 effective 才就绪；菱形后继去重；独立分支不被无关重绑清空。
- HOLD 只补取同文档已读邻域；未来窗口未到达时 WAITING_CONTEXT，EOF 可明确 UNBOUND。
- 新成员模式把枚举完整性与已知成员的绑定分开；旧 V3 轨迹仍保留 `requires_complete_set=true` 的 WAITING_SCOPE 行为。
- BOUND 提交后 Runtime 更新子查询输入签名、从 DORMANT 激活并调度历史候选 RECALL；流程完成或证据耗尽前不调用 ANSWER。
- PLAN 保持 V5.1 的证据收集职责：最终比较、计数和集合运算由 ANSWER 使用全部 Working Memory 完成，不作为 query 节点。模型误加的终端计算叶子仅删除该叶子，不改写上游关系或依赖；PLAN REPAIR 也不得重新加入或设置基数。
- API / 协议 / 预算失败保持失败状态，不代写语义 UNBOUND 或假装复查完成。

事务顺序：快照校验 → 克隆上暂存、校验 → 单次 SQLite CAS 事务写状态、事件、审阅、outbox、receipt → 提交后发布内存。
相同 context + 相同响应返回 receipt；不同响应报 CONTEXT_CONSUMED。迟到输出不可用旧快照修复。
持久化任务支持重新领取；RECALL 批次有幂等进度；日志回放只发布完整提交 envelope 并验证双图。

## 分句、视图和预算

`sentence_splitting=true` 继续使用 **syntok 1.4.4 analyze()**、原始字符偏移及句/片段锚点；没有新增缩写表或分句正则。
`sentence_splitting=false` 使用原 V5.1 窗口读取正文，但 `D18@C0` 等块坐标只留在 Archive、游标恢复和完整性审计中，
不进入事实、MEMORY 或 ANSWER 的模型视图。tokenizer 不通过 decode 改写原文。

原文模式固定保留绑定证明、新证据和诊断原文；预算紧张时可隐藏旧的非证明事实，隐藏不等于删除。
必要原文超限报 REVIEW_INPUT_BUDGET / FINAL_RAW_MEMORY_BUDGET，不能退回仅摘要。事实模式没有原文包，
工作记忆超限报 FINAL_FACT_MEMORY_BUDGET；成员模式每个具体分支一次 FINAL 决策调用，查询快照统一发布。
预算计量完整序列化 prompt，并预留 ANSWER 调用。本地日志仍保留内部窗口哈希供事务审计，但事实模式的模型请求
`visible_source_refs` 为空。

## 验证与新八题配置

结构回归位于 `tests/test_r2_member_graph.py`，覆盖多成员同父、精确事实边、菱形自然连接、同值不同路径、部分分支 NOOP、
新增与纠正、迟到响应、SQL 回滚、跨进程恢复、文本协议、原文模式和完整 runner 联调；这些是确定性测试，不是模型正确率。

2026-09-21 本地验证：`tests/` 共 470 项通过，其中新增成员图测试 26 项；三种离线重绑定场景通过，事务回放一致。
`delaybind_core` 编译检查通过。仓库其他训练模块的全目录 pytest 收集需要当前环境未安装的 torch/datasets/numpy，
因此不将它们计入上述验证，也未为本次框架修改安装训练依赖。

新八题配置：`configs/v52_r2_smoke8_member_graph.json`。它使用单独输出目录，不复用旧八题结果。运行命令：

```sh
.venv/bin/python -m delaybind_core.cli experiment --config configs/v52_r2_smoke8_member_graph.json
```

该命令会产生真实 API 请求。本次架构修改仅执行本地测试，未运行这轮真实八题。
历史 `scripts/run_r2_p17_smoke.py` 增加冻结合同预检，当前架构下会在发送请求前拒绝运行，避免把新成员图误标为旧 P17 对照。

## 计划修复与入口

`plan_repair_mode=disabled` 用于固定计划实验；`on_hint` 保留原 PATCH/ROUTE 能力，归属独立 HIGH PLAN。
新八题配置选择 **on_hint**，避免迁移时关闭此能力。
文本为 `REPAIR | 依据 IDs`、`UPSERT | Qn` 查询块、`ROUTE | Qn | fact IDs`、`END REPAIR`。
按旧/新 DAG 影响范围并集失效，路由到已就绪 query 立即调度。

离线验证，不读取 key、不发送 API：

```bash
python -m pytest -q tests
python -m scripts.run_r2_smoke
```

真实八题配置可继续使用；切换事实模式时必须使用新的 run ID / 输出目录：

```bash
python -m delaybind_core experiment --config configs/v52_r2_raw_rebind_smoke.json
```

凭据仍读取现有 `.env.local`。事实模式加 `--no-sentence-splitting`，不要在已有 run ID 上切换模式。
评测方法：`v52_r2_predicted` / `v52_r2_no_callback` / `v52_r2_no_defer`；原评测脚本适配器：`--api v52_r2`。
CLI run 支持 `--protocol-version v5.2-r2`；experiment 同时使用匹配的 `v52_r2_*` 方法名。

## 实现与验证索引

### 紧凑原文展示

V5.2 / R2 原文模式发送给模型的原文统一为 `[来源ID] 原文`，不再逐条附加
`kind`、`complete`、`chars`。原文字符、来源 ID、分句开关及 syntok 不变；
类型、完整性、偏移和哈希仍保留在内部档案/上下文中，用于引用、完整性和事务校验。
独立的 Citable IDs 清单不再发送；模型从当前窗口及工作记忆的原文锚点引用，
内部来源白名单和逐条权限校验不变。查询的渲染文本与模板相同时只展示一遍，
代入变量后不同时仍保留模板、依赖及绑定信息。
`:P` 后缀仍表示不完整片段，`@C` 文档块仍不保证句子完整。
窗口展示与游标预算采用相同紧凑格式；完整请求仍额外包含系统指令、问题、计划和工作记忆，
所以 5000-token 窗口不代表整个请求最多 5000 tokens。
提示词版本已更新，历史日志不改写；不要在原 run ID 上续跑不同版本。

| 文档领域 | 实现模块 | 测试 |
| --- | --- | --- |
| P01–P13、P18：协议、路由、隔离、阶段、来源、修订 | schema_r2 / protocol_r2 / memory_r2 | test_r2_core / test_r2_context |
| P14–P17：跨窗、邻域、权限、同窗顺序 | runtime_r2 / context_r2 / prompts_r2 | test_r2_context / test_r2_runner / test_r2_core |
| D01–D18：重绑三结果、后继、多父、菱形、独立分支、回查 | navigation_r2 / review_jobs_r2 / memory_r2 | test_r2_core / test_r2_reliability / test_r2_runner |
| R01–R08、R10–R12、R17：事务故障、恢复、幂等、缓存、计划修复 | storage_r2 / runtime_r2 / plan_repair_r2 / api / replay | test_r2_reliability / test_r2_core / test_r2_runner |
| R09、R13–R16、R18：预算、ANSWER、旧路径、入口、原文 | subquery_runner_r2 / context_r2 / runner / cli / adapters | test_r2_runner / test_r2_context / 原有 306 项回归 |

此为按领域对应的索引，不把模拟材料当作真实模型语义正确率证明。
事务故障测试在重绑事务每个 SQL 写入点注入失败；脚本化端到端覆盖原文模式的维持/替换/UNBOUND，以及事实模式的替换/NOOP。
真实准确率、协议首次有效率、遗漏率、token 和成本仍需独立测量。

本次离线验证：`python -m pytest -q tests` 为 **425 passed**；
`python -m scripts.run_r2_smoke` 三条轨迹通过，VERIFY=0，事务回放一致性均为 1。
另有事实模式端到端用例验证两字段 UPDATE、零原文 MEMORY/ANSWER，以及后续 RESOLVED 新事实导致
Q1/Q2/Q3 旧链全部退休并重绑为新链。
无筛选的仓库级 pytest 仍需要训练侧可选依赖 `torch`、`datasets`、`ray`。

## 可选 token 片直传与 PLAN 修补拒绝

R2 事实成员模式可显式设置 `update_input_mode=plain_token_chunks`。读取入口对
`context` 只做一次 `strip()`，用配置的真实 tokenizer 编码一次，再按 `chunk_size`
顺序切分 token ID 并逐片解码。解码结果直接放入 UPDATE / UPDATE_REPAIR 的
`<section>`，不经来源归档、文档重排或句子拼接，也不插入 `DOCUMENT_BOUNDARY`。
此模式必须提供上下文和 tokenizer；完整请求仍受现有 token 与调用预算约束。
默认 `archive_windows` 保留旧事实模式行为。

每片的 token 起止位置、正文和下一位置随 `CHUNK_OBSERVED` 事务保存。
UPDATE 成功后才清除 `pending_chunk`；中断恢复复用已登记的 UPDATE 快照，
部分采纳后的修复范围、保留条目和剩余重试次数也随状态提交。请求审计中的
`raw_input_tokens` 和哈希覆盖实际片正文。版本为 `baseline-token-chunks-v1`。

`plan_repair_failure_policy=continue_valid_plan` 仅作用于 `mode=REPAIR` 的模型提案。
合法提案仍按原事务提交；格式、权限或计划 DTO 错误按现有次数重试，耗尽后
写入 `plan_repair_failures` 和 `PLAN_REPAIR_REJECTED` 事件，再继续原调度。
同一计划、待处理 hint、授权事实、有效绑定和查询语义视图不重复请求；新依据
允许再次尝试。失败不会把 hint 标记为已处理，也不改写绑定或证据。
上下文过期、存储/不变量、API 和预算错误仍沿原失败路径传播。默认 `abort`
保留旧行为；控制版本为 `optional-repair-reject-v1`。

完整实验配置在
`configs/v52_r2_full128_member_graph_plain_chunks_optional_repair.json`，使用独立
实验 ID 和输出目录。本次修改只运行本地确定性测试，没有发送模型请求；
离线测试覆盖真实 tokenizer 的参考切片、模型请求正文、快照恢复、局部修复拒绝、
事件回放及旧模式回归。

2026-09-25 离线实现验证时 `.venv/bin/python -m pytest -q tests` 为 **588 passed**。
冻结 128 题的实际 tokenizer 离线审计共检查 240 个 chunk，cursor、注册快照及
UPDATE `<section>` 与参考 decode 均相同，差异数为 0。历史五题的十次修补回复
用其原有事务 revision 重建提案准备状态，十次均归类为模型提案错误。
详见 `reports/r2_plain_chunk_equivalence_128_20260925.json` 和
`reports/r2_plan_repair_historical_classification_20260925.json`。
未执行 G0–G3 真实模型评测，不能从上述离线验证推断 EM/F1 改善。

随后清理了不再被 R2 使用的 V5/V5.2 运行模块及其专用测试。清理后的
`.venv/bin/python -m pytest -q tests` 为 **329 passed**，现有 128 题实验的
配置指纹保持一致，可继续 resume。旧实验配置和既有结果保留作历史记录。
