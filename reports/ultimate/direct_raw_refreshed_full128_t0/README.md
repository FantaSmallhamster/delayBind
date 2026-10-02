# 完整上下文直接回答：128 题，temperature=0

本文记录清理前完成的评测；生产代码未修改的结论对应当次运行。精简后的代码及结果离线核验见 [清理报告](../../v08_cleanup/README.md)，历史代码 manifest 未重写。

EM：**74/128 = 57.8125%**；F1：**0.653487**。

同一批新题集、同一 Qwen/Qwen3.5-9B、并发 8；每题一次性给完整 50 篇文档和问题，不切 chunk、不规划、不做记忆或事实抽取。

直接提示要求只输出简短 boxed 最终答案、不附解释。enable_thinking=false，top_p=0.95、seed=4、输出上限 4096，均与 V08 一致；不使用 JSON schema 或格式修复。

运行状态：{'OK': 128}。实际请求逐条核对：上下文原文完整、没有截断；128 个独立响应 ID，结束原因 {'stop': 128}。

| 类型 | 总数 | 直接回答正确 | 直接回答错误 | 直接回答 EM | V08 正确 |
| --- | ---: | ---: | ---: | ---: | ---: |
| compositional | 26 | 17 | 9 | 65.38% | 22 |
| comparison | 60 | 45 | 15 | 75.00% | 59 |
| inference | 21 | 0 | 21 | 0.00% | 13 |
| bridge_comparison | 21 | 12 | 9 | 57.14% | 18 |

## 与 V08 的逐题对照

V08：112/128，EM 87.50%，F1 0.896425。

直接回答少答对 38 题，EM 差 -29.6875 个百分点。

直接回答新答对 6 题，新失分 44 题。

inference 为 0/21；抽查新增祖父题发现直接回答停在父亲：dev_9710 回答 William，dev_9700 回答 John Hamilton，dev_375 回答 Leopold，都没有完成第二跳。这是本次提示和模型设置下的现象，不代表其他直接回答提示必然同样表现。

## 实际提示模板

```text
Answer the question using the context below. Give only the short final answer in \boxed{}, without explanation.

Context:
{context}

Question:
{question}
```

[完整核验](audit.json) · [128 题逐题对照](paired_comparison.json) · [原始结果](../../../results/ultimate/direct_raw_refreshed_full128_t0_r1/results.jsonl)

每题原始请求和响应保存在结果目录的 trajectories/；生产代码未修改。
