# PR-00 legacy 测试迁移记录

原始测试副本保存在 baselines/legacy_v08/tests。首次运行：147 passed，39 failed，23 subtests passed。
原始完整输出：reports/mwr/baseline-tests.txt。

- test_runtime_core / test_operators_and_order / test_plan_validation / test_query_graph / test_prompts / test_runner_mock：旧 graph fixtures 显式改为 GraphQueryPlan。未删除或减弱断言。
- test_runner_mock / test_experiment_harness：显式选择 plan_format=graph，避免默认 subqueries 分派到与 fixture 不匹配的新接口。
- test_prompts：显式选择 graph_plan_prompt；答案测试传入真实 AnswerResponse schema，继续要求输出出现 source_refs。原 fixture 的空 schema 不包含该字段。

迁移后应全量运行 tests；不靠跳过失败测试建立工程门。
