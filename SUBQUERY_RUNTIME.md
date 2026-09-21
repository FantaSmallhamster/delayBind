# V5.1：双 Agent、自然语言计划与事实工作记忆

实现依据是用户上传的 [V5.1 设计文档](docs/V5.1_design.md)。以下四项冲突已经用户确认，
执行时以本轮要求为准：采用高层/低层两个 Agent；VERIFY 仅用于新绑定激活下游 query 后
选中的 defer 候选；文本结束后不做充分性检查、直接 ANSWER；事实匹配且足以形成完整绑定时
才将 query 标为已解决。公开状态采用文档的 `RESOLVED`，对应用户所说的 satisfy。

## 职责和实际调用顺序

| 执行方 | 接口 | 责任 |
| --- | --- | --- |
| 高层 Agent | PLAN | 仅根据问题生成自然语言证据需求与依赖 |
| 高层 Agent | MEMORY | 维护活跃事实视图、提出绑定、纠错和必要的局部计划修改 |
| 高层 Agent | ANSWER | 根据现有工作记忆进行最终推理和回答 |
| 低层 Agent | UPDATE | 带着问题、计划和活跃事实读取一个窗口，抽取完整事实并标记归属 |
| 低层 Agent | RECALL | 新绑定产生后，在对应 defer 候选桶中寻找相关事实 |
| 低层 Agent | VERIFY | 仅核验 RECALL 选中的 defer 事实及其对应已读原文 |
| Runtime | 无语义推断 | 校验来源、应用状态变化、保存事实、维护版本及支持连接、调度调用 |

两种 Agent 是具有不同接口权限的对象。默认共用同一个模型服务，也可分别指定 `high_api`、
`low_api`，或给 `V5Runner` 传入不同的 `client` 和 `reader_client`。
高层不能调用 VERIFY/RECALL，低层不能调用 PLAN/MEMORY/ANSWER；低层 UPDATE 中出现 BIND
会被拒绝并触发格式修复，而非绕过高层直接修改绑定。

```text
问题 → 高层 PLAN

每个窗口：
  低层 UPDATE：完整事实 + ACTIVE / DORMANT 标记
  Runtime 先登记全部事实，再分别 COMMIT / DEFER
  有活跃记忆变化或计划提示时，高层 MEMORY 提出 BIND 等增量操作

新 BIND：
  实例化下游 query
  低层 RECALL 扫描该 query 的 defer 候选桶
    没选中候选 → 保持 ACTIVE，继续阅读
    选中候选 → 只恢复这些事实的原文 → 低层 VERIFY
             → MATCH 事实 PROMOTE 进入工作记忆
             → 高层 MEMORY 判断能否形成完整 BIND
             → RESOLVED，并继续激活下一跳

文本结束：现有工作记忆 → 高层 ANSWER
```

没有逐窗口定向补抽，也没有用新实体去全文 Archive 自由检索的路径。
RECALL 只能看到该 query 的 defer 事实文本；在它选定 fact ID 后才发生原文 FETCH。
默认只取候选明确引用的来源；`verify_source_neighborhood` 可增加同一已读文档内的邻域。
不调用全文 `search_mentions` 或 `expanded_context`。

## 计划形式

PLAN 使用短文本块：

```text
Q1
query: Cindy 的老师是谁？
output: ?teacher
depends_on: NONE

Q2
query: ?teacher 的母亲是谁？
output: ?mother
depends_on: Q1
```

内部 `QueryPlan.queries` 存储 `id / template / output / depends_on`，并允许
`requires_complete_set: true`。JSON 仍可作为人工计划的导入形式；此前的 `query / binds`
字段可导入为 `template / output`。无须三元组、关系本体或算子注册表。

Runtime 维护各 query 的 `status / result / support_fact_ids / version / binding_version`。
传给模型时附上 `rendered_query` 与 `support_refs`，不覆盖原始模板。
根 query 输入已知，因此从 ACTIVE 开始；输出未知不会令它 DORMANT。
只有依赖完成才激活下游；仅收到 COMMIT 不会自动 RESOLVED。

只用于最终回答的比较与计算交给 ANSWER。决定下一步寻找对象的中间推理由高层 MEMORY
生成有支持事实引用的 BIND，不调用额外 VERIFY，也不伪造“派生原文事实”节点。
完整集合 query 使用读完给定范围的保守闭合条件，范围未结束时不接受完整集合 BIND。

## UPDATE 与候选保存

正式输出为：

```text
Q2 | c02:s1 | Alice 的母亲是 Mary。 | DORMANT
Q1 | c20:s1 | Cindy 的老师是 Alice。 | ACTIVE
```

解析器将 ACTIVE 映射为 COMMIT、DORMANT 映射为 DEFER，同时兼容文档的 COMMIT/DEFER
动作名和 `[Q2 @ c02:s1] Alice 的母亲是 Mary。（defer）` 写法。只解析规定位置的标记，
事实正文中出现单词 defer 不会改变路由。正文中的换行或竖线可写成 `\n`、`\|`。

占位符 `?xx` 可以代表任意名字、词语或值，但关系方向、时间、否定和范围限制必须保留。
事实中的具体姓名不替换成变量。代词补全依赖前文时，用逗号列出全部必要 `source_refs`。

ACTIVE 事实通过 ID 和来源合法性检查后直接进入工作记忆，不调用 VERIFY。
即使低层误将 DORMANT query 的事实标成 ACTIVE，Runtime 也会降为候选，禁止提前绑定。
一窗口的所有事实先登记，再处理高层 BIND，同窗口输出先后不影响可见候选。

`PLAN_HINT | source_refs | 事实及缺失需求` 保存无法路由的相关证据，供高层局部修改计划。
无相关事实时输出 NONE。格式错误有日志及有限修复重试，调用成本计入统计。

### 文档编号与来源引用

对于 ReMemR1 的 `Document N:` 长文本，每个原始 token 窗口在读到后再按文档边界登记来源片段。
窗口大小和正文不变，低层会看到如下标记：

```text
[D44@C0] Document 44
Document 44:
Without the King
Without the King is a 2007 documentary film.
```

推荐输出 `Q1 | D44@C0 | Without the King is a 2007 documentary film. | ACTIVE`。
`D44@C0` 对应内部不可变来源 `<sample_id>:c00000:D44`；同一文档跨窗口时，后续片段使用
`D44@C1`。VERIFY 只恢复候选引用的已读片段，不将整个窗口中的其他文档混作该来源。

解析器兼容 `Doc44`、`doc_44`、`Document 44`、`D44`、`44` 等写法，但只在本次 UPDATE
的窗口与显式提供的工作记忆来源中查找。裸文档号若对应多个可见片段，会保留这些片段的全部引用，
不会扩大到未读部分或未呈现的历史原文。BIND 只能将文档别名转换为已接受的支持事实引用。
JSON ANSWER 的来源引用也只在证据包范围内转换。

无效或不存在的来源会触发带可用编号列表的格式修复重试；`NONE` 不能成为事实的来源。
已知的复制格式标题行、没有 query 和来源的 `NONE | | ...` 占位行会被忽略并记录
`UPDATE_LINE_IGNORED`，不会让同一输出中的有效事实全部解析失败。真实事实行仍必须使用单一动作。
引用格式正确不代表事实语义正确；ACTIVE 事实仍遵循已有的直接接受规则，defer 候选仍按原流程 VERIFY。

### 协议兼容和局部错误处理（protocol-v4）

- RECALL 支持 `SELECT F1,F2`、`SELECT | F1,F2`、纯 ID 列表、JSON ID 数组、代码块和列表标记。
  只按明确语法解析，不从解释性长文里搜索并猜测 fact ID。输入已显示的上游绑定支持 ID 若被混入，
  会记录 `RECALL_CONTEXT_IDS_IGNORED` 并排除；只有当前批次的 defer 候选可以进入 VERIFY。
  拼错、不存在、属于其他批次的 ID 不会自动纠正或当成有效证据。
- UPDATE/MEMORY 兼容已声明 query 的 `Q1@NONE`、大小写和引用符号变体，以及
  `Q1 [ACTIVE]` / `Q1 [DORMANT]` / `Q1 [RESOLVED]`。括号只作为编号装饰移除，
  不能修改 runtime 的真实状态。UPDATE 的编号转换记录为 `QUERY_IDS_NORMALIZED`。
  不会因为出现 `Q2@NONE` 就创建不存在的 Q2，也不改变 query 的依赖与状态。
- 来源/支持引用支持逗号列表和 JSON 字符串数组。MEMORY 中的 NOTE 说明块与合法命令分开处理，
  无支持的 BIND 会被拒绝；JSON 数组外壳不会被误当成 fact ID 的一部分。
- UPDATE 按行解析和校验，保留合法事实；混入的 `Q3|NONE|`、无来源或未知 query 行独立记录为
  `UPDATE_LINE_REJECTED`。有限修复重试后仍有错误时，保留此前通过校验的事实继续读取，不伪造缺失引用。
- 完整四栏的 `Q2|NONE||DORMANT` 等明确空占位，以及没有来源、符合有限句式的
  `No document provides ...` / `Cannot determine ... without ...` / `Cannot determine ... because ... are unknown`
  无结果说明，记录 `UPDATE_LINE_IGNORED` 后跳过，不触发模型重试。带具体断言但缺来源的行仍被拒绝；
  有来源的否定事实不受此规则影响。阅读模型只需输出发现的事实，不必给每个 query 填一行。
- UPDATE 来源栏只接受本次可见原文的来源编号。`F...` 是记忆事实编号，不能自动转换为原文来源。
  一行混有合法文档编号和错误事实编号时整行拒绝，不删除错误项后接受剩余引用。
- UPDATE 使用局部修复提示词 `UPDATE_REPAIR`，仅列出上次被拒绝的行、逐行原因、允许的 query/来源编号，
  以及本窗口和显式可见记忆来源的原文。不会推进阅读窗口或搜索额外 Archive。
  模型只返回可由这些证据支持的修正行；无法修复的行省略，全部无法修复则返回 NONE。
  合法事实跨尝试保留，按 query、正文、来源集合和路由标记去重；合法 PLAN_HINT 也去重。
  修复请求记录 `UPDATE_REPAIR_REQUESTED`，包含待修行、已保留事实数和窗口编号。
  后续仍失败时，只针对本次剩余坏行继续有限重试；其余接口保留原有重试方式。
- MEMORY 重试后仍部分出错时，只应用最后一次响应中通过语法和 query 检查的命令，仍受 Runtime
  的支持引用检查约束。未满足依赖或还在等待候选核验的 BIND 记为 `BINDING_HELD`；之后由高层
  根据最新证据重新提出，不会跳过 VERIFY 自动补绑。
- VERIFY 接受分隔符与空格的简单变体；必须覆盖实际选中的候选，不能使用未知 ID 或相互矛盾的判定。
  若重试仍无法解析，该批候选保留为 UNCERTAIN，判定来源标记 `PROTOCOL_FALLBACK`，不会提升事实。
- RECALL 重试仍无法得到合法候选列表时，本批按未选中处理，原候选继续保存；后续流程可继续。

上述重试耗尽统一记录 `PROTOCOL_REPAIR_EXHAUSTED`。结果包含 `protocol_valid=false` 和详细
`protocol_repair_failures`；评分同时报告协议失败题数、拒绝行数和暂缓绑定次数。
能够继续返回答案不等于协议无错误，也不等于证据完整。PLAN 没有合法计划、模型 API 失败和资源耗尽
仍沿用原有错误处理，不会通过这些格式兼容规则隐瞒。

## 工作记忆图与版本

事实表正文只保存一份：

```text
F1: Cindy 的老师是 Alice。 [c20:s1]
F2: Alice 的母亲是 Mary。 [c02:s1]
```

query 的使用记录单独保存。一条事实可以被 Q1 接受，同时仍是 Q3 的候选。
`defer_workspace` 是 `query_id → fact_id 列表`；候选不会自动占用每次 UPDATE 的活跃上下文。

工作记忆包含 `facts` 和 `links`。连接记录：

```text
upstream_fact_ids: [F1]
downstream_fact_id: F2
upstream_query_id: Q1
query_id: Q2
variable: ?teacher
value: Alice
```

它表达任务中的绑定衔接，不表达现实因果或“F1 蕴含 F2”。多个上游支持事实按一个集合引用，
不拆成“其中任一事实即可独立证明”的多条边。连接只由已确认的绑定和接受记录生成，不按同名连边。
BIND 的支持还包含已声明的依赖依据，避免只留下一个答案名字而丢掉桥接证据。

高层可以用 BIND、KEEP、RETRACT、UNBIND、CLEAR_CONFLICT、PATCH、ROUTE 做增量维护。
上游绑定改变后，旧下游结果、接受记录和连接失效；事实正文保留。
复核结果按 query 版本、输入绑定版本、fact ID 记录，旧版本判定不会阻止新绑定下重新核验。
PATCH 校验整个更新后的计划，但只使受影响部分失效。ROUTE 仅重分配候选，不能直接提升。

## VERIFY 的严格边界

低层先分批扫描当前已知 defer 桶，再分批核验选中候选。合并完各批判定后，高层才形成绑定，
不会第一批命中就忽略后面已有的冲突。候选数量超过显式资源上限时记录资源失败，不静默取 top-k。

VERIFY 逐条返回：

```text
F2 | MATCH
F4 | MISMATCH
```

另支持 UNCERTAIN、CONFLICT。MATCH 只表示事实可用，完整结果仍由高层 BIND 提出。
高层只能引用已经接受的事实及依赖支持，无法用一个尚未提升的候选直接绑定变量。

按用户确认的严格范围，以下情况不会额外调用 VERIFY：

- 新读到的 ACTIVE 事实，包括对已解决 query 的明确反证；交给高层维护和纠错。
- 已激活后才新增的 DORMANT/DEFER 标记事实，若没有新的绑定激活事件，就继续留在候选区。
- 单独的计划 PATCH、没有候选的中间推理，以及最终充分性判断。

因此原设计附录 C 中 T13、T24、T25 的调用方式已按本轮要求调整。
上游纠错产生新绑定时，受影响的下游仍可重新检索并核验旧候选。

## 结束、预算与输出

文本结束后直接调用 ANSWER，输入仅为原问题、现有可用事实和必要连接。
不要求全部 query 已解决，也不做原问题充分性 VERIFY。
空工作记忆、未解决 query 或显式冲突不会成为程序拒绝调用 ANSWER 的条件；它们仍记录在结果中。

`answer_format="auto"` 对 ReMemR1 长文本输入使用原有 boxed 格式，对原 2Wiki 文档输入
沿用已有 AnswerResponse JSON 合同。也可明确选 boxed、text、json。
`raw_answer` 保留模型原始答案，`evidence_pack.answer_value` 用于评分。

活跃视图与完整已接受事实分开。高层 KEEP 选择显示内容，Runtime 保护绑定支持。
最终证据可恢复已接受但暂未显示的事实；恢复后的输入大小也会记录。
传入模型 tokenizer 时，`memory_token_budget` 按序列化记忆的实际 tokenizer token 数计量，
默认 8192；无 tokenizer 的轻量运行明确采用 `memory_char_budget`（默认 24000 个字符），
不会把字符数冒充 token 数。必要绑定支持或最终完整记忆超过预算时记录 RESOURCE_LIMIT，
不无日志删除桥接事实。模型调用预算耗尽也可能使 ANSWER 无法执行，这是资源限制，不是充分性检查。

结果保留 query 状态、事实使用记录、版本、defer 候选、工作记忆、原始答案、各 Agent/接口调用数。
SQLite 记录每次选择、来源恢复、验证、提升、绑定、失效及模型请求，支持离线 replay。
答案和支持来源指标继续计算；自然语言事实不强行转回三元组评分。原始 ReMemR1 数据没有支持句标签时，
相应支持事实分数为 null。协议规定最少窗口数时，仅记录 streaming_protocol_valid，不据此拦截 ANSWER。

## 直接使用 ReMemR1 输入

无需用户构建 Manifest：

```python
result = await V5Runner(client, tokenizer=tokenizer).run(
    run_id="example",
    item={
        "_id": 0,
        "input": "Cindy 的老师的母亲出生在哪个城市？",
        "context": "Mary 出生于苏州。\nAlice 的母亲是 Mary。\nCindy 的老师是 Alice。",
    },
    store=SQLiteEventStore(),
)
```

也可直接传 `question="...", context="..."`。
TextReadCursor 复用传入 tokenizer 的 encode/decode 切 token 窗口。普通长文本按窗口生成
`0:c00000`；带 `Document N:` 标题的文本按已读文档片段生成 `0:c00000:D44`，给模型显示
短引用 `D44@C0`。这些来源仅在窗口读到时加入 Archive。无 tokenizer 时按字符分块，输出显式报告单位。
原 2Wiki 的文档列表和 Manifest 保留为兼容输入及历史顺序消融工具。
已经拼接好的 raw context 只能使用 original 顺序，不能假称进行了文档 reverse 实验。

```bash
python3 -m pip install -e '.[test]'
python3 -m delaybind_core run --config configs/rememr1_input_example.json
python3 -m delaybind_core experiment --config configs/subqueries_smoke.json
python3 -m pytest -q tests
```

模型凭据仍放在 `.env.local`。需要 CLI 加载实际 tokenizer 时安装 `.[tokenizer]` 并配置
`tokenizer_path`，例如本地模型目录。实验默认使用两个角色共享的模型配置；不同服务可以分别写
high_api、low_api。配置和结果会记录两边的模型参数，API key 不写入冻结配置。

若同一 API 域名解析出的地址响应差异明显，可在 `api` 中设置 `connect_ip` 选择连接地址。
URL、HTTP Host 和 TLS 证书校验仍使用原域名，不修改系统 DNS，也不会把 IP 作为模型参数发送。
成功响应的 `_client_transport` 记录实际 `peer_ip`、TLS 主机名及服务端 trace ID，供审计。
这只控制连接路线，不能保证后端不再繁忙或超时。

原 ReMemR1 评测脚本增加了 `--api v51`，沿用 `_id / input / context / answers` 数据格式、
既有 tokenizer 和 boxed 答案提取器。`nocallback` 条件只关闭历史候选回查，仍保存 DEFER；
`defer_unbound=false` 是另外的无候选保存消融。此适配不改原 ReMemR1 的训练或其他评测方法。

历史图实验仍显式使用 `plan_format="graph"`；本页说明的是默认 V5.1 路径。
测试覆盖流程正确性，不代表已经用真实模型证明准确率或成本改善。
# V5.2 extension

This document describes the retained V5.1 runtime. For the opt-in raw-first
V5.2 protocol, see [V52_RUNTIME.md](V52_RUNTIME.md). V5.2 uses separate versioned
modules and does not silently reinterpret V5.1 plans or recorded runs.

The separately versioned `v5.2-r2` path uses text BIND/REBIND blocks and durable
downstream invalidation. Its segmentation switch selects either staged raw review
or a source-free fact-only path. See [V52_R2_RUNTIME.md](V52_R2_RUNTIME.md).
