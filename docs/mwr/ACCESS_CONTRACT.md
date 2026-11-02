# MWR v1 访问合同

新方法必须通过 `RunnerConfig.engine` 显式选择：`legacy_v08`、`flat_memory_mwr`、`delaybind_mwr`。
旧实现和原测试位于 `baselines/legacy_v08/`，逐文件哈希位于该目录的 `sha256.json`。
开发副本不修改用户提供的源码目录。128 题只能作为开发诊断，不报告为独立测试集。

新 engine 禁止 raw search、global reread、answer gap search；配置打开任意一项即拒绝。
新路径只注入前向游标与 evidence ID 授权 lookup，不实例化 RawArchive/EvidenceRuntime。
模型不能提交搜索词、任意 source_ref 或任意文档指针来读取档案。
所有指针以文档身份、半开字符区间和原文哈希标识，不能跨越已读范围。
被淘汰条目不再授权来源；同源存活条目或固定邻域仍可能合法覆盖该来源，需分开计量。
审计日志及历史快照只用于离线评价。恢复只加载当前快照，不从日志筛选语义。

运行合同包含代码、提示词、输入、tokenizer、计划、配置及两层模型参数的哈希。
恢复时必须完全匹配合同。最终答案模型采用统一 solver；oracle 标注只在诊断路径使用。
远程包装 token 开销不可精确推知，必须标明本地估算和 provider usage 的区别。
