# V08 新题集完整 128 题测试

本文记录清理前完成的评测；代码哈希匹配结论对应当时的完整工程。精简后的代码及结果离线核验见 [清理报告](../../v08_cleanup/README.md)，历史代码 manifest 未重写。

EM：**112/128 = 87.5000%**；F1：**0.896425**。

模型 Qwen/Qwen3.5-9B，SiliconFlow，并发 8；V08 代码与旧运行完全一致。

运行状态：{'OK': 125, 'ERROR': 3}。运行错误计入总分母，另行列出，不从 EM 中剔除。

| 类型 | 总数 | 正确 | 错误（含运行错误） | 运行错误 | EM |
| --- | ---: | ---: | ---: | ---: | ---: |
| compositional | 26 | 22 | 4 | 0 | 84.62% |
| comparison | 60 | 59 | 1 | 1 | 98.33% |
| inference | 21 | 13 | 8 | 1 | 61.90% |
| bridge_comparison | 21 | 18 | 3 | 1 | 85.71% |

## 新旧题分组

| 分组 | 总数 | 正确 | EM | F1 |
| --- | ---: | ---: | ---: | ---: |
| kept_98 | 98 | 86 | 87.76% | 0.886098 |
| added_30 | 30 | 26 | 86.67% | 0.930159 |

新增 comparison：24/25；新增 inference：2/5（严格 EM）。

新增 inference 的三道失分都出现姓名或头衔形式差异；保留原始 EM，不更改 gold 或判分口径：

| ID | gold | 预测 |
| --- | --- | --- |
| dev_9710 | Edward Fortunatus | Margrave Edward Fortunatus of Baden |
| dev_9700 | Archibald James Hamilton | Archibald James Hamilton, 12th of Orbiston |
| dev_375 | Jerome, 4th Count de Salis-Soglio | Jerome de Salis |


保留的同一批 98 题：旧运行 93/98，本次 86/98。

新答对：dev_5416, dev_5853, dev_6785。

新失分：dev_10616, dev_7920, dev_4288, dev_11285, dev_448, dev_4237, dev_4021, dev_9193, dev_738, dev_3448。

旧全题集结果仅作历史参考：98/128（76.5625%），F1 0.780506。新题集替换了 30 道问题，不能把两批总分差异解释为代码提升。

## 运行错误

- dev_11285：boxed answer missing
- dev_266：invalid output variable '?321_release_date'
- dev_4288：boxed answer missing

dev_266 两次 PLAN 返回的变量名均以数字开头，校验不通过。dev_4288 与 dev_11285 的 ANSWER 及修复输出均达到 4096 token，API finish_reason=length，缺少最终 boxed 答案。三道均未发生网络请求错误。

## 错题

| ID | 新增/保留 | 类型 | 标准答案 | 预测 | 状态 |
| --- | --- | --- | --- | --- | --- |
| dev_266 | 新增 | comparison | ['3.2.1.'] | None | ERROR |
| dev_10616 | 保留 | inference | ['Hermann Georg of Limburg'] | Hermann Georg of Limburg, count of Limburg and Bronckhorst | OK |
| dev_7920 | 保留 | compositional | ['26 May 1968'] | 5 September 1941 | OK |
| dev_4288 | 保留 | inference | ['Sirikit Kitiyakara', 'Sirikit'] | None | ERROR |
| dev_11285 | 保留 | bridge_comparison | ['Night of the Party', 'The Night Of The Party', 'The Night of the Party'] | None | ERROR |
| dev_7046 | 保留 | inference | ['Chantal de Chevron-Villette'] | Princess Maria Antonia of the Two Sicilies | OK |
| dev_448 | 保留 | compositional | ['Rimini'] | Italy | OK |
| dev_4237 | 保留 | compositional | ['Fulbright Program', 'Fulbright Scholarship', 'Fulbright Fellowship'] | None | OK |
| dev_4021 | 保留 | inference | ['Shahriyar', 'Shahriyar (son of Khosrow II)'] | Muhammad | OK |
| dev_9193 | 保留 | bridge_comparison | ['The Secret Bride', 'Secret Bride'] | Too Many Wives | OK |
| dev_5002 | 保留 | bridge_comparison | ['yes'] | no | OK |
| dev_738 | 保留 | inference | ['Therese von Nassau-Weilburg', 'Princess Therese of Nassau-Weilburg'] | Agrippina Japaridze, Countess von Zarnekau | OK |
| dev_9710 | 新增 | inference | ['Edward Fortunatus', 'Eduard Fortunat von Baden-Baden'] | Margrave Edward Fortunatus of Baden | OK |
| dev_3448 | 保留 | compositional | ['Republic of Indonesia', 'Indonesian', 'id', '🇮🇩', 'IDN', 'ID', 'Si Gomar', 'Indonesia', 'INA'] | Dutch East Indies | OK |
| dev_9700 | 新增 | inference | ['Archibald James Hamilton'] | Archibald James Hamilton, 12th of Orbiston | OK |
| dev_375 | 新增 | inference | ['Jerome, 4th Count de Salis-Soglio'] | Jerome de Salis | OK |

## 验证与记录

128 个唯一样本、128 份轨迹；逐题 EM/F1 重算；生产代码与题集哈希匹配；125 份状态成功重放；运行路由与最终证据包核验无违规。

[完整核验](audit.json) · [错题原始结果](wrong_questions.json) · [保留 98 题逐题对照](retained_question_comparison.json)

[原始结果](../../../results/ultimate/v08_refreshed_benchmark_full128_r1/results.jsonl) · [新题集](../../../data/test/filtered_seed4_128_refresh_v08/README.md)
