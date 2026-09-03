# V5 延迟绑定证据运行时：设计与实现说明

> 文档版本：2026-09-02。代码范围为当前工作区的 `delaybind_core/`，基线版本为
> ReMemR1 提交 `cc514c092ca968a50c52cdcc2e2ba96362fce25a`。本文件描述的是已提交的
> 可执行行为，不把规划中的能力写成已完成的能力。

## 1. 范围、定位与设计结论

V5 是一个面向长上下文、多跳问答的**训练无关（training-free）**证据运行时。它将
ReMemR1 的“分块处理、可回看记忆”思路改造成可审计的结构化流程：模型提出计划与事实
候选，确定性运行时决定候选是否可用、何时绑定变量、何时回查并将事实纳入最终证据。

这里的**延迟绑定（delayed binding）**是指：事实先出现但其实体身份尚未由问题链路确定
时，先保存为待定事实；待后续证据确定实体后，再精确找回并验证该早期事实。它避免仅因
文档顺序与推理顺序不一致而丢失关键证据。

V5 与原始 ReMemR1 的职责边界如下：

| 范围 | 当前实现 | 作用 |
| --- | --- | --- |
| `recurrent/`、`verl/`、`taskutils/` | 上游基线，未由 V5 修改 | 原始循环记忆 Agent、GRPO 训练、评估与分布式基础设施。 |
| `delaybind_core/` | V5 独立包 | 数据规范化、计划校验、证据状态机、SQLite 审计、API Runner。 |
| `V5_IMPLEMENTATION.md` | V5 状态说明 | 给出已完成范围及尚未完成的接口边界。 |
| ReMemR1/verl 适配器 | 未实现 | 尚未把 V5 的结构化状态机接入训练 rollout。 |

### 1.1 要解决的问题

长文档按固定顺序读取时，支撑多跳答案的属性事实可能早于其主体识别事实出现。例如先读到
“Martin Lee 生于 1948 年”，后读到“Film A 的导演是 Martin Lee”。传统只维护当前
链路的流式 Agent 在第一句时无法知道 Martin Lee 是否相关，容易遗忘或错误地立即采用。

V5 把此问题拆成五个明确责任：

1. **计划（PLAN）**：把问题转换为带变量的关系模式与答案契约。
2. **读取（READ/UPDATE）**：只暴露当前分块，让模型提取原子三元组候选。
3. **延迟（DEFER）**：无法与已知实体对齐的候选进入待定区，不直接成为答案证据。
4. **绑定与回查（BIND/LOOKUP）**：后续匹配的关系将变量绑定为实体，再按“实体 + 关系族”
   精确筛选待定事实。
5. **验证与提升（VERIFY/PROMOTE）**：仅对被回查命中的待定事实读取已见原文邻域，接受后
   将其提升为可用证据；最终由运行时做完整性检查。

### 1.2 不在当前实现范围内

- 不训练模型，不修改 ReMemR1 的 GRPO、多层奖励或 `MemoryAgent`。
- 不执行通用知识图谱检索，也不访问尚未读取的原文。
- 不实现任意开放式规则的通用算子执行图；Oracle Plan 编译和首轮 2Wiki artifact 导出已经完成。
- 当前实现了逻辑模型调用数和窗口数上限；精确 tokenizer token/cost 预算仍需接入实际服务
  价格与 tokenizer，不能把词数近似当成精确成本。
- 不提供自动合并冲突、跨样本共享记忆或并行事务恢复。

### 1.3 208 个 Grill 问题收敛出的最终决策

下面是讨论中已经冻结、并在当前实现或实验协议中体现的决策摘要。它不是逐题聊天记录，
而是后续开发应遵守的约束；若代码与本表冲突，以代码测试和最新实验配置为准，并同步更新
本文件。

| 问题范围 | 已确定的方案 | 对实现的直接影响 |
| --- | --- | --- |
| Q1-Q10 | 以 ReMemR1 为工程基线；优先完成正确的 V5 流程，不把人力或暂不相关的长期目标作为限制；模型 API 由用户提供。 | ReMemR1 commit 被固定；V5 代码放在独立 `delaybind_core/`，不改上游训练代码。 |
| Q11-Q19 | 第一篇只研究当前问题的 Graph Working Memory；长期记忆图是后续工作，不能反向污染第一篇框架。 | 当前状态不跨样本共享图；长期 consolidation 只保留概念接口。 |
| Q20-Q24 | 保留完整早期候选，但只在变量绑定后精确检索；先从 Deferred Workspace 找最相关候选，再回到该候选附近的已读原文做精确核验。 | `DEFER -> BIND -> LOOKUP -> FETCH -> VERIFY -> PROMOTE`；Runtime 是事实裁决者，模型只提案。 |
| Q25-Q29 | 先用通用 Qwen3/Qwen3.5 做 predicted Plan；效果差时用强模型诊断；2Wiki Oracle Plan 可用于后续监督训练。 | `PLAN` 有 Schema/语义校验和一次纠错重试；Oracle plan 文件可通过 CLI 注入。 |
| Q30-Q36 | 先保留模型生成计划，再根据 trajectory 做修正；统一一个 Agent 的多个接口；比较需同一 checkpoint。 | `PLAN/UPDATE/VERIFY/ANSWER` 共用 `OpenAICompatibleClient`；Direct Full Context 是主公平基线。 |
| Q37-Q46 | 保留 Direct Full Context；先评估 Runtime 直接给答案，效果不足再启用 ANSWER 模型；所有方法固定 thinking 和输出预算。 | `answer_mode=runtime` 保留为严格 target 消融；`answer_mode=evidence` 使用问题类型和已验证图回答，不要求 PLAN target。 |
| Q47-Q60 | 不复制完整世界状态；Graph 只留当前题已核验事实，未绑定候选留在外部；否定、冲突和事实更新必须可追踪。 | Claim 带 polarity/modality/disposition/source；冲突终止为 `CONFLICTED`，不覆盖旧事实。 |
| Q61-Q80 | 关系/规则由 Plan 显式提出，运行时只执行注册的确定性算子；找不到足够证据就拒答。 | Operator registry、EOS Sufficiency Gate 和 `INSUFFICIENT/UNSUPPORTED` 状态。 |
| Q81-Q100 | 先做结构、证据、顺序、成本和鲁棒性测试；同窗口和跨窗口均按 Manifest 原文流位置排序；保留 Snapshot。 | Manifest 是不可变输入契约；Runtime 重排模型事件；Runner 按窗口写 SQLite Snapshot。 |
| Q101-Q120 | 不同样本可并行，单样本内部仍严格串行；Graph 只序列化紧凑相关子图，不能因“图小”而取消预算。 | 并行边界在样本/`run_id`，不共享 SQLite 事务；`chunk_size`、`max_graph_claims`、`max_model_calls` 和 `max_verify_candidates` 可配置。 |
| Q121-Q140 | Snapshot 保存恢复/审计所需的状态、游标、Manifest ID 和调用计数；最终 ANSWER 模型未来可同时看证据图与外部 Open-Set 累加器的闭合结果。 | `snapshots` 表和 `EvidencePack.operator_trace` 已有接口；Open-Set 累加器与自动断点续跑仍是后续工作。 |
| Q141-Q160 | 第一阶段只跑 2Wiki；新 benchmark、长期图和更复杂集合任务后置；2Wiki 的 SET/COUNT 先由计划和确定性算子覆盖。 | `canonicalize_record()` 优先兼容 2Wiki/FlashRAG；批量 Direct/V5/Oracle/no-DEFER 评测已接入。 |
| Q161-Q175 | 依赖按可复现版本管理；ReMemR1 无稳定 release 时锁 commit；API 使用 Pydantic v2 结构化 Schema。 | 基线 commit 为 `cc514c092ca968a50c52cdcc2e2ba96362fce25a`；所有模型输出 `extra=forbid`。 |
| Q176-Q208 | 配置写入 JSON，命令保持一行；API 密钥不进代码和 Git；先完成 CPU/API 流程，再接训练和 GPU。 | `python -m delaybind_core run --config ...` 自动读 `.env.local`；SQLite、JSON 结果和测试均可复现。 |

## 2. 总体架构

```text
2Wiki / FlashRAG 原始样本
          |
          v
canonicalize_record -> CanonicalSample -> build_manifest -> Manifest
                                                       |
                                                       v
                                                    ReadCursor
                                                       |
                 +-------------------- RawArchive <---+--- 仅追加当前读窗
                 |                         |
                 |                         v
问题 --> PLAN 模型 --> QueryPlan --> 计划校验 --> EvidenceRuntime <--- SQLiteEventStore
                                          |               |
当前窗口 --> UPDATE 模型 --> TripleEvent --+               |
                                                          v
            DEFER -> BIND -> 精确回查 -> VERIFY -> PROMOTE -> EOS 完整性检查
                                                          |
                                                          v
                                         EvidencePack -> （可选 ANSWER 模型）
```

系统遵循两个硬边界：

- **模型是提案者，不是裁决者。** `TripleEvent.proposed_action` 只保留模型建议；
  `EvidenceRuntime` 按当前绑定和模式匹配结果自行决定 `COMMIT`、`DEFER` 或 `SKIP`。
- **原始文本按读取前缀（read prefix）隔离。** 只有被 `ReadCursor` 当前或此前窗口追加到
  `RawArchive` 的句子才能作为 `source_ref` 引用或回查。未来句子会抛出
  `FutureSourceAccessError`。

## 3. 核心术语

| 术语 | 含义与用途 |
| --- | --- |
| 规范化样本（CanonicalSample） | 将 2Wiki、FlashRAG 的不同 JSON 字段统一为问题、文档、答案和标注。 |
| 清单（Manifest） | 不可变的句子级输入顺序，记录稳定 `source_ref`、哈希和 `stream_position`。 |
| 读取游标（ReadCursor） | 按清单正向切出预算内窗口，不能回退或跳读。 |
| 原始档案（RawArchive） | 已读取句子的持久化副本，是所有证据引用的访问控制层。 |
| 查询计划（QueryPlan） | 由关系模式、算子和答案契约组成的结构化推理目标。 |
| 关系模式（Pattern） | `subject -> relation -> object` 的三元组模板，`?x` 表示待绑定变量。 |
| 关系族（relation family） | 将具体表述如 `birth_year` 归到计划语义关系如 `TEMPORAL_ORDER_KEY`。 |
| 绑定（binding） | 将 `?director` 这类变量确定为实体值的运行时状态改变。 |
| Claim | 带来源、极性、模态、处置状态的候选事实；不是无来源的模型结论。 |
| 延迟 Claim | 关系相关但尚未可落到已知实体的 Claim，存入 `deferred`。 |
| 验证（verification） | 对候选 Claim 与原文邻域是否精确支持进行 `ACCEPT/REJECT/...` 判断。 |
| 证据包（EvidencePack） | 终止时导出的可用 Claim、算子轨迹和候选答案值。 |
| 事件溯源（event sourcing） | 将状态变化写成有序事件，之后可从日志重新投影状态。 |

## 4. 数据层：从原始记录到受控读取流

### 4.1 记录规范化

实现位于 `delaybind_core/data.py`。`canonicalize_record()` 接受 JSON/JSONL 中的 2Wiki 或
FlashRAG 变体：

- 上下文既支持 `[[title, sentences], ...]`，也支持
  `{"title": [...], "content"/"sentences": [...]}`。
- 文档内部编号固定为 `d00000`、`d00001` 等；这不是标题的哈希，因此可在同名标题下保持
  唯一性。
- 支持事实从列表或 `{title, sent_id}` 字典两种形态读取。
- `answer` 与 `golden_answers` 统一为 `answer`（首选答案）和 `answers`（全部候选）。
- 保留未识别字段，但将其放入 `metadata`，以兼容数据集的附加标注。

`load_records()` 只接受 `.json` 与 `.jsonl`；其他后缀立即报错，不进行猜测式解析。

### 4.2 句子级 Manifest

`build_manifest()` 以文档内句子为最小读取单元，构造：

```text
source_ref = <sample_id>:<document_id>:s<sentence_id>
```

每个 `ManifestEntry` 含原句、文档标题、原始句号、流位置、字符范围及 `text_sha256`。
Pydantic 校验会重算文本 SHA-256；外部传入的哈希若不一致会拒绝清单。`Manifest` 还要求：

- 所有 `stream_position` 递增且唯一；
- 每条 entry 的 `dataset_id`、`sample_id` 与所属 Manifest 一致；
- `manifest_id` 在未指定时由 `sample_id + seed + order + source_refs` 的 SHA-256 截断生成。

清单支持三种展示顺序：`original`、`reverse`、`shuffle`。`shuffle` 固定使用局部
`random.Random(seed)`，不污染全局随机数；无论展示顺序如何，`source_ref` 与句子文本都不变。
这使“证据倒序到达”可被稳定复现。

### 4.3 ReadCursor 与 RawArchive

`ReadCursor.next_window(token_budget=5000)` 从当前位置连续累积句子，估算长度为
`len(text.split())`，至少按 1 计；累计超过预算时在句间边界停止。它随后将整个窗口追加到
`RawArchive`，返回带起止流位置的 `ReadWindow`。这里的 token 是词数近似值，不是模型
tokenizer 的精确计数，实际 API 上下文仍可能与该预算有偏差。

`RawArchive` 并不保存整份 Manifest，而是通过 SQLite 的 `raw_archive` 表只保存已经交给
读取游标的 entry。对不存在的 `source_ref` 调用 `entry()` 或 `fetch()` 会拒绝访问；
`fetch(source_ref, neighborhood=1)` 仅返回已归档、且属于同一 `document_id` 的相邻句子，
绝不会为了凑足邻域去读未来或把相邻文档的内容混入 VERIFY。

### 4.4 Graph Working Memory 与提示词投影

Graph Working Memory 是当前问题唯一进入 UPDATE 上下文的持久事实状态。它由已通过验证门的
RAW Claim 组成；开放 Pattern、pending/deferred Claim、完整原文和未执行算子都不属于图。
工程上，`RuntimeState.verified` 保存带完整 provenance 的 Claim，而
`RuntimeState.graph_projection(max_claims=N)` 只向模型暴露以下字段：绑定、主体、关系、客体、
限定条件、极性、模态和匹配 Pattern ID。这样模型能够判断下一窗口的相关性，同时不会把
`source_ref`、验证断言等审计冗余字段在每一轮重复展开；完整数据仍保留在 SQLite 与最终
EvidencePack 中。`max_graph_claims` 只限制提示词投影，不删除运行时事实。

图的生命周期是单题单运行：`G_0=empty`，每个 VERIFY `ACCEPT` 才新增边，运行结束导出
EvidencePack；不同样本使用不同 `run_id`，不共享当前问题图。后续长期图工作只能消费这些已
验证局部图，不能在第一篇 V5 中反向改变读取或绑定规则。

## 5. 计划层：QueryPlan、答案契约与确定性校验

### 5.1 模型输出契约

所有模型可见 schema 位于 `delaybind_core/schema.py`，当前结构版本为 `v1`，并启用
Pydantic `extra="forbid"`，即未知字段不能静默进入运行时。

`QueryPlan` 的关键字段是：

| 字段 | 实现约束 | 用途 |
| --- | --- | --- |
| `plan_id`、`plan_version` | 计划标识与版本 | 区分计划实例和未来演进。 |
| `patterns` | 1 至 16 条 | 定义所需关系跳数与变量。 |
| `relation_specs` | 最多 16 条 | 描述关系方向、实体类型、基数和可接受表述。 |
| `operators` | 最多 8 条 | 声明比较、集合或投影等派生步骤。 |
| `answer_contract` | runtime-answer 时声明目标变量；evidence-answer 时由问题画像提供类型 | 规定答案类型、基数与规范化方式。 |

三元组方向始终是 `subject -> relation -> object`。例如“Film A 的导演的母亲是谁”应包含：

```json
{
  "patterns": [
    {"id": "director", "subject": "Film A", "relation": "director", "object": "?director"},
    {"id": "mother", "subject": "?director", "relation": "mother", "object": "?mother"}
  ],
  "answer_contract": {"target": "?mother", "type": "ENTITY"}
}
```

### 5.2 防止“看似合法、语义失效”的计划

JSON Schema 只能检查字段形状，不能保证计划能从问题出发。因此
`plan_validation.validate_plan()` 还执行语义检查：

- 模式 ID 不能重复，关系不能为空。
- 变量必须是如 `?director` 的合法名字；不能使用 `unknown_person_1` 这类模型虚构占位符。
- 同一模式不能把同一变量放在主语与宾语两端，避免无意义的自环。
- 共享变量构成的每个模式连通分量，必须能由问题中出现的常量锚定；否则会报
  `UNANCHORED_PATTERN_COMPONENT`，防止无关扫描。
- 非变量常量必须实际出现在问题中，避免模型把答案或外部知识偷塞进计划。
- 算子必须在 V5 允许词表中，算子输入及答案目标变量必须已被模式或算子输出声明。
- runtime-answer 中缺少 `answer_contract`、或非布尔答案缺少 `target` 会失败；
  evidence-answer 中由问题画像补齐 type，不要求 Plan 指定 target。

Runner 对无效的计划最多执行一次纠正重试（`max_plan_retries=1`）：将结构或语义错误格式化
后放回 PLAN 提示词，要求模型返回完整替代计划；重试仍失败则抛出 `PlanValidationError`。

## 6. 延迟绑定状态机

### 6.1 Claim 的状态与动作

模型的 `TripleEvent` 提供 `source_ref`、实体、具体关系、可选关系族、对象、来源顺序和
置信度等信息。运行时把它转换为带原始来源 `EvidenceAssertion` 的 `Claim`。

```text
模型 TripleEvent
       |
       +-- 无匹配 Pattern -------------------------> SKIPPED
       |
       +-- Pattern 已可落地 --> COMMITTED --（可选验证）--> PROMOTED
       |
       +-- Pattern 尚不可落地 --> DEFERRED -- BIND/LOOKUP --> VERIFY
                                                       | ACCEPT
                                                       v
                                                    PROMOTED
                                                       |
                         REJECT ------------------> REJECTED
                         CONFLICT ---------------> REJECTED + 冲突记录
                         NEED_MORE_CONTEXT ------> 保留原状态
```

术语上，`COMMITTED` 表示运行时判断候选匹配且可落地；在 Runner 默认的统一验证模式下，
它先进入 `state.pending`，收到 `VERIFY ACCEPT` 后才进入 `state.verified` 并触发绑定。
`PROMOTED` 表示该 Claim 收到过 `VERIFY ACCEPT`。将 `verify_committed=false` 用于低成本消融时，
COMMITTED Claim 才会直接进入 `verified`，因此该开关必须在实验报告中明确记录。

### 6.2 `apply_event()` 的精确决策顺序

`EvidenceRuntime.apply_event()` 的执行顺序是：

1. 通过 `archive.entry(source_ref)` 确保来源已读；`context_only=True` 的来源也会拒绝。
2. 将模型报出的 `source_order` 与 Manifest 真值比较；不一致时写
   `EVENT_SOURCE_ORDER_MISMATCH` 并用 Manifest 的流位置覆盖它。
3. 用 `proposal:<event_id>` 查询事件库，实现同一模型事件的幂等处理。**幂等**指重试不会
   产生第二份状态变更。
4. 仅按关系/关系族和已知常量、已绑定变量筛选匹配 Pattern；多条命中时当前实现取第一条。
5. 无 Pattern 命中时生成 `SKIPPED` Claim；Pattern 至少有一端是常量或已绑定变量时为
   `COMMITTED`，否则为 `DEFERRED`。
6. `COMMITTED` 时，填充此前未绑定的变量、写入 `ENTITY_BOUND`，并查找可能因新绑定而
   可回查的 deferred Claim。

`apply_events()` 无条件按 Archive 中的真实 `stream_position`、`span_hint`、`event_id` 排序。
因此 UPDATE 返回的 JSON 数组即使乱序，运行时也会写 `EVENTS_REORDERED` 并按输入阅读顺序
处理。

### 6.3 完整示例：属性先于身份

问题为“Film A 的导演出生于哪一年？”，计划有两条模式：

```text
T1: (Film A, DIRECTOR, ?director)
T2: (?director, TEMPORAL_ORDER_KEY, ?year)
答案：?year
```

| 流位置 | 句子与 UPDATE 候选 | 运行时结果 | 原因 |
| --- | --- | --- | --- |
| 0 | `Martin Lee was born in 1948.` -> `(Martin Lee, birth_year, 1948)`，关系族为 `TEMPORAL_ORDER_KEY` | `DEFERRED` | `?director` 尚未绑定，不能证明 Martin Lee 就是问题所问导演。 |
| 1 | `Film A was directed by Martin Lee.` -> `(Film A, director, Martin Lee)` | `COMMITTED`，绑定 `?director=Martin Lee` | T1 有来自问题的常量 `Film A`，可安全落地。 |
| 1 后 | `DEFERRED_LOOKUP` | 命中位置 0 的 Claim | 当前实现以 `(Martin Lee, TEMPORAL_ORDER_KEY)` 精确匹配。 |
| 回查 | 向 VERIFY 提供位置 0 及已读邻域 | `ACCEPT` 后 `PROMOTED`，绑定 `?year=1948` | 原文支持该精确候选。 |
| EOS | 必需 T1、T2 均满足 | `ANSWERED`，证据包答案为 `1948` | 运行时输出可以不再请求答案模型。 |

`_lookup_deferred()` 的匹配键是“新绑定变量所在的一侧（主语或宾语）+ 实体 + `relation_key`”。
它覆盖单变量的正向和反向落地；需要多个变量共同决定的关系、通用 join 或复杂集合索引，
仍应视为后续扩展项。

### 6.4 验证及冲突处理

Runner 默认会对 grounded 的 pending Claim，以及被 `deferred_matches` 找回的 Claim 发起 VERIFY。
验证提示词只提供：问题、候选 Claim、以原始来源为中心的已读 `neighborhood=1` 句子；它明示
模型不可引用外部事实。

- `ACCEPT`：Claim 变为 `PROMOTED`，从 deferred 移除，写入可用集合，并尝试由其再次绑定变量。
- `REJECT`：Claim 变为 `REJECTED`，从 deferred 移除并写入 rejected 集合。
- `CONFLICT`：记录冲突原因并终止时标记 `CONFLICTED`；当前实现还保留原集合成员，故不应在
  冲突状态下使用 `EvidencePack`。
- `NEED_MORE_CONTEXT`：只记录事件，不自动扩展窗口、重试或改变 Claim 处置状态。

默认配置下，grounded 和 deferred 两条入图路径都必须经过 VERIFY；关闭
`verify_committed` 只应用于低成本消融，不应作为高风险证据模式。

## 7. 终止、算子与答案

### 7.1 EOS 完整性门

EOS（end of stream，输入流读完）时调用 `EvidenceRuntime.finalize()`：

1. 收集 `verified` 集合中命中的 Pattern ID，并与所有 `required=True` 的模式比较。
2. 缺失任一必需模式时返回 `INSUFFICIENT`，原因形如
   `REQUIRED_PATTERN_MISSING:T1,T2`。
3. 存在任何冲突时返回 `CONFLICTED`，原因是 `UNRESOLVED_CONFLICT`。
4. 满足模式后执行声明的确定性算子；算子失败则返回 `UNSUPPORTED`。
5. 若答案契约目标变量未绑定则仍返回 `INSUFFICIENT`；否则返回 `ANSWERED` 和 `EvidencePack`。

可见的运行状态还有 `RUNNING`、`RESOURCE_LIMIT`、`RUNTIME_ERROR`。Runner 在超过
`max_windows` 或 `max_model_calls` 时设置 `RESOURCE_LIMIT` 并写入 `RUN_STATUS`；此时不会把
不完整前缀误标为 `ANSWERED`。

### 7.2 算子注册表

`delaybind_core/operators.py` 已真正执行的原子算子如下：

| 算子 | 行为 |
| --- | --- |
| `EARLIER` / `LATER` | 解析 `YYYY`、`YYYY-MM`、`YYYY-MM-DD` 或斜杠日期后比较先后。 |
| `YOUNGER` / `OLDER` | 以出生时间较晚/较早表示更年轻/更年长。 |
| `COMPARE` | 支持相等、大小、日期先后等显式比较符。 |
| `COUNT` | 对规范化后不重复的集合计数。 |
| `INTERSECTION` / `UNION` | 保持左侧/首次出现顺序的集合交并。 |
| `PROJECT` | 把已绑定的源变量直接投影至目标变量。 |

在 EOS 执行阶段，`DIRECT`、`PATH_JOIN`、`REGISTERED_RULE`、`RULE`、`FILTER` 仅记录为
`DECLARED`，不凭空生成结论；`ARGMAX`、`ARGMIN` 已用于 comparison/bridge-comparison 的
候选选择。计划词表与执行词表仍非完全等价，调用方必须以运行时能力为准。

算子输出通过 `params.output`、`output_var` 或 `result` 写回变量绑定，并记录
`OPERATOR_EXECUTED` 与相应的 `ENTITY_BOUND` 事件。所有变量输入必须在执行前有值，否则报
`OperatorError`，不会静默把未绑定变量当作文本。

### 7.3 两种答案模式

- `answer_mode="runtime"`（默认）：使用 `EvidencePack.answer_value`，即答案契约目标变量的
  已绑定值，最小化最终模型自由度；Plan 必须声明 target。
- `answer_mode="evidence"`：EOS 成功后调用 ANSWER，提示词只包含问题和 EvidencePack；
  Runtime 从问题画像补齐答案类型，不要求 Plan target。ANSWER 的每个 `source_ref` 必须属于
  verified Claim，否则回答被拒绝为 `INSUFFICIENT`；这使模型做最终变量选择但不能引入图外事实。

## 8. 事件、SQLite 与可重放性

### 8.1 存储结构

`SQLiteEventStore` 默认使用 `:memory:`，也可由配置的 `db`/`db_path` 指定文件。它建立四张表：

| 表 | 主键/唯一约束 | 保存内容 |
| --- | --- | --- |
| `runtime_events` | `(run_id, event_seq)`，`(run_id, event_id)` 唯一 | 所有状态转换、来源位置、因果事件序号、事务 ID。 |
| `model_calls` | `(run_id, call_id)`，`(run_id, request_hash, attempt)` 唯一 | 请求、原响应、已解析内容、耗时、token 使用量、异常与缓存标识。 |
| `raw_archive` | `(run_id, source_ref)`，`(run_id, stream_position)` 唯一 | 已读取的 ManifestEntry。 |
| `snapshots` | `(run_id, snapshot_id)` | 每个配置间隔窗口的状态、游标、manifest 和调用计数快照。 |

事件序号在同一 `run_id` 内单调递增；事件 ID 由运行 ID、类型、载荷、来源和流位置的稳定哈希
生成。重复写入同一 ID 会返回既有事件，支持调用重试。

### 8.2 关键事件类型

| 事件 | 触发点 |
| --- | --- |
| `PLAN_CREATED` | 运行时初始化，载荷含完整计划。 |
| `TRIPLE_EVENT_PROPOSED` | 接收一个此前未处理的模型候选。 |
| `CLAIM_DEFERRED` / `CLAIM_COMMITTED` / `CLAIM_SKIPPED` | 运行时完成候选处置。 |
| `ENTITY_BOUND` | 新变量或算子结果被写入绑定表。 |
| `DEFERRED_LOOKUP` | 新绑定按实体与关系族找到待验证事实。 |
| `CLAIM_PROMOTED` / `VERIFY_REJECTED` / `VERIFY_CONFLICT` | 验证终态。 |
| `OPERATOR_EXECUTED` | EOS 确定性算子轨迹。 |
| `SUFFICIENCY_CHECKED` / `RUN_STATUS` | 终止时完整性判定和最终状态。 |

`replay_events()` 是一个纯投影函数：从 `PLAN_CREATED` 重建状态，按 `event_seq` 依次恢复绑定、
deferred、可用、拒绝、冲突与运行状态。它不访问模型和原文，适合离线调试。不过它只投影已有
事件；不会重新执行算子、重新验证、或恢复运行中私有标志，因此不是断点续跑机制。

## 9. 模型接口与 Runner

### 9.1 四个无状态接口

提示词构建位于 `delaybind_core/prompts.py`，每个提示词以 `version=v1` 标记并要求只返回 JSON：

| 接口 | 输入 | 允许输出 | 运行时防线 |
| --- | --- | --- | --- |
| `PLAN` | 问题、QueryPlan JSON Schema、可选纠错 | 查询计划 | Schema + 问题锚定 + 连通性校验。 |
| `UPDATE` | 问题、计划、当前可用 Claim 投影、当前窗口 | 有来源的 `TripleEvent` 列表 | Archive、排序、Pattern 匹配和绑定门。 |
| `VERIFY` | 问题、候选 Claim、已读原文邻域 | 验证状态和可选规范化 Claim | 仅允许对已存在 Claim 应用结果。 |
| `ANSWER` | 问题、EvidencePack | 答案 | 仅在 `answer_mode=evidence` 成功终止后调用。 |

`OpenAICompatibleClient` 使用标准库 `urllib` POST 到以下之一：主机地址加
`/v1/chat/completions`、以 `/v1` 结束的地址加 `/chat/completions`，或完整
`/chat/completions` 地址。请求使用温度、seed、JSON Schema `response_format`，并在
PLAN/UPDATE/VERIFY 默认关闭 `enable_thinking`。

请求以稳定 JSON 哈希作为 cache key。同一 `run_id` 下命中已有成功解析结果时不发网络请求，
而记一次 `attempt=0` 的缓存记录；网络错误最多重试 `max_retries + 1` 次，退避为 1、2、4 秒
（最大 4 秒），失败尝试也会持久化。

### 9.2 `V5Runner.run()` 的时序

```text
读取/复用 Manifest
  -> PLAN（或读取外部计划）并校验
  -> 初始化 Runtime、Cursor、Archive
  -> 对每个窗口：归档原文 -> UPDATE -> 按真值顺序 apply_events
  -> 对本窗口触发的 deferred matches：取已读邻域 -> VERIFY -> apply_verification
  -> EOS finalize
  -> 可选 ANSWER
  -> 导出 run_id、question、完整 state、evidence_pack
```

Runner 的 `RunnerConfig` 目前有效字段为：

| 字段 | 默认值 | 效果 |
| --- | --- | --- |
| `chunk_size` | 5000 | 每个窗口的词数近似预算。 |
| `answer_mode` | `runtime` | 选择运行时答案或最终 ANSWER 模型。 |
| `max_windows` | 100000 | 已读取窗口的上限。 |
| `max_verify_candidates` | 1000 | 每个窗口回查候选的截断上限。 |
| `max_plan_retries` | 1 | 首次 PLAN 失败后的最大纠错次数。 |
| `max_model_calls` | 1000 | 单次运行的逻辑 PLAN/UPDATE/VERIFY/ANSWER 调用上限；超限为 `RESOURCE_LIMIT`。 |
| `snapshot_every_windows` | 1 | 每隔多少个窗口写入一个 SQLite Snapshot；0 表示不自动写。 |
| `verify_committed` | `true` | grounded Claim 是否也先进入 pending 并经过 VERIFY；关闭仅用于低成本消融。 |
| `max_graph_claims` | 128 | UPDATE 提示词最多携带的紧凑 verified Claim 数，不删除 Runtime 事实。 |
| `defer_unbound` | `true` | 是否保存未绑定候选；`false` 是正式的 w/o DEFER 消融。 |
| `max_verify_expansions` | 1 | VERIFY 返回 `NEED_MORE_CONTEXT` 后最多扩展已读上下文的次数。 |
| `verify_expansion_limit` | 32 | 扩展上下文最多包含的同文档句子和实体 mention 数。 |

`max_input_tokens` 和 `max_cost` 仍是实验配置预留字段，当前不做精确 enforcement；窗口预算使用
`text.split()` 估算，正式实验必须另行记录 tokenizer 的真实 token 数与服务账单。

## 10. CLI、配置与运行产物

入口为 `python -m delaybind_core`，见 `delaybind_core/cli.py`。

```bash
# 仅做数据画像，不调用模型
PYTHONPATH=. python -m delaybind_core profile \
  --input data/2wiki/dev.jsonl --output runs/profile.json

# 可重复构建单样本句子清单
PYTHONPATH=. python -m delaybind_core build-manifest \
  --input data/2wiki/dev.jsonl --sample-index 0 \
  --output manifests/dev_0.json --order reverse --seed 4

# 基于已经导出的事件日志离线恢复状态
PYTHONPATH=. python -m delaybind_core replay \
  --events runs/events.json --output runs/replayed_state.json

# API 驱动的完整 V5 运行
PYTHONPATH=. python -m delaybind_core run --config configs/base.json

# Direct / Oracle / Predicted / w/o DEFER 批量矩阵
PYTHONPATH=. python -m delaybind_core experiment \
  --config configs/experiment_smoke.json

# 从 2Wiki gold evidences 编译可人工审查的 Oracle Plan
PYTHONPATH=. python -m delaybind_core compile-oracle-plans \
  --input data/2wiki/dev.json --output runs/oracle_plans.json --sample-count 16
```

`run --config` 接受 JSON；其他后缀会尝试 YAML（需要安装 PyYAML）。它先读取工作目录下的
`.env.local`，但不会覆盖已经导出的环境变量。缺失配置时，依次从配置或环境中获取：

| 配置键 | 环境变量 | 是否必需 |
| --- | --- | --- |
| `input` | `DATASET_INPUT` | 是 |
| `base_url` | `MODEL_BASE_URL` | 是 |
| `api_key` | `MODEL_API_KEY` | 是 |
| `model` | `MODEL_NAME` | 是 |
| `db` / `db_path` | 无 | 否，默认内存数据库 |
| `output` / `result_output` | 无 | 否，写最终 JSON |
| `manifest_output` | 无 | 否，写新建的 Manifest |
| `plan` / `plan_path` | 无 | 否，绕过 PLAN 调用并读取已写计划 |

`profile` 会把问题类型映射为建议算子和答案契约：`compositional/bridge` 对应
`PATH_JOIN`，`inference` 对应 `REGISTERED_RULE`，`comparison` 对应 `COMPARE` 并按问句词面
补充 `EARLIER/LATER`。这只是数据统计和计划建议，不能替代实际计划校验或算子执行。

## 11. 测试证据与当前保证

项目测试位于 `tests/`，可运行：

```bash
PYTHONPATH=. pytest -q tests
```

现有单元测试覆盖以下具体保证：

- FlashRAG/2Wiki 记录转换、稳定 Manifest ID、倒序读取下来源身份不变。
- 非单调流位置被拒绝，未来来源访问被拒绝，模型伪造的来源顺序被更正。
- 锚点缺失、虚构占位符、未知答案变量、自环等计划会被拒绝；结构无效计划会重试一次。
- 倒序事实经历 `DEFER -> BIND -> LOOKUP -> VERIFY -> PROMOTE` 后可重放出一致状态。
- 无关实体或关系族的待定 Claim 不会被错误回查；重复模型事件幂等。
- 日期、数值、集合算子可复现，EOS 可把 `COUNT` 结果绑定为答案。
- 客户端失败重试与成功缓存都会写入 `model_calls`。
- 模拟 API 下的 PLAN、UPDATE、VERIFY 端到端流程可达 `ANSWERED`。

这套测试证明确定性核心的局部不变量；它不等价于真实模型在开放域关系抽取、关系规范化、
答案规范化上的准确率评估。上线前仍需用固定数据切分、固定 Manifest、固定模型版本报告
正确率、证据充分率、冲突率、延迟事实召回率、VERIFY 接受率与调用成本。

## 12. 已知限制与后续实现优先级

以下项目来自当前代码路径与 `V5_IMPLEMENTATION.md` 的明确边界，使用时必须如实处理：

1. **验证开关仍影响成本。** 默认 Runner 对 grounded 与 deferred Claim 都 VERIFY；
   `verify_committed=false` 会恢复 direct commit 直接入图的低成本消融路径，正式结果必须记录该配置。
2. **关系规范化依赖模型。** 运行时只比较字符串的大小写/空白规范化；`birth_year` 到
   `TEMPORAL_ORDER_KEY` 的映射必须由 UPDATE 正确填写 `matched_family`，没有本体或词典兜底。
3. **延迟回查是单向局部 join。** 它只关注“新绑定主语变量 + 关系族”，不处理对象侧绑定、
   多变量合取、集合基数或跨 Claim 的通用查询计划。
4. **结构型算子不等于事实推导。** `ARGMAX/ARGMIN`、集合与比较已实现，但 `RULE`、
   `REGISTERED_RULE` 和 `FILTER` 仍只声明需求，不自动物化 DERIVED Claim。
5. **资源预算分辨率有限。** `max_model_calls`、`max_windows` 和
   `max_verify_candidates` 已由 Runner 强制执行；输入 token 和货币成本仍需服务端 tokenizer/
   价格适配，不能只用当前词数近似推断。
6. **API 方言差异。** 客户端使用 OpenAI-compatible Chat Completions 与 `enable_thinking`，
   某些兼容服务可能不支持 JSON Schema 或该字段；部署前需做服务端契约测试。
7. **Snapshot 目前用于审计，不是自动断点续跑。** Runner 会保存状态和游标位置，但恢复命令仍
   需要实现 Cursor/Archive/模型缓存的协调；`replay_events()` 只负责离线状态投影。
8. **基线尚未集成。** V5 当前为 CPU/API 小规模实验核心；ReMemR1/verl adapter、训练奖励、
   GPU rollout 和产物导出仍需独立设计与实现，不能宣称已替代上游训练系统。

## 13. 维护约定

- 不直接修改上游 ReMemR1 基线；任何有意上游变化都记录到 `PATCH_NOTES.md`。
- 新增模型字段必须同时更新 Pydantic schema、相应提示词、事件载荷和 replay 投影，避免
  “能写不能读”的审计断裂。
- 新增算子至少补齐：计划校验、确定性执行、参数/类型报错、`OPERATOR_EXECUTED` 轨迹和测试。
- 更改延迟匹配键或验证时机时，必须补充“倒序事实、无关同实体事实、冲突事实、重放一致性”
  四类测试。
- 任何新增数据集适配都应保证 `source_ref` 稳定、Manifest 可复现、原始 gold 标注不因展示
  顺序改变。

## 14. 与 ReMemR1 的关系

原始 ReMemR1 的 callback 是模型通过 `<recall>` 触发 TF-IDF 从历史 memory 中检索过去记忆；
V5 的 callback 则由确定性绑定事件触发，并且精确回查原始已读句子。前者面向可训练 Agent
的压缩记忆管理，后者面向带来源、可验证、可重放的证据链管理。

两者共享“长上下文不应只按线性顺序遗忘过去”的问题意识，但 V5 不能被理解为 ReMemR1
训练管线的替代品。合理的后续集成方向是：让 ReMemR1 的模型输出 V5 结构化候选，保留
V5 Runtime 作为权威状态机和审计层，再根据可验证证据质量设计奖励信号。
