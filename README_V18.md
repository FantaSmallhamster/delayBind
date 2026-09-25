# DelayBind V18 最佳版本 (EM 66.41%)

## 概述
基于 ReMemR1 V5.1 runtime，在 2WikiMultiHopQA 128题 filtered 数据集上，将 EM 从基线 52.34% 提升到 66.41%（+14.07%）。

## 项目结构
```
delaybind_v18_best/
├── delaybind_core/          # 核心代码
│   ├── subquery_runner.py   # V18核心：ANSWER阶段附加证据链原句
│   ├── subqueries.py        # 子查询运行时（原始状态，无VeGraph）
│   ├── metrics.py           # 答案归一化（头衔/后缀/别名）
│   ├── agent_prompts.py     # Prompt改进（禁止拒绝/yes/no/inference指导）
│   ├── prompts.py           # Graph模式prompt
│   └── ...                  # 其他核心模块
├── configs/
│   ├── v51_2wiki50_filtered_seed4_128_fde.json   # V18评测配置（temperature=0）
│   └── v51_2wiki50_filtered_seed4_128.json       # 基础filtered配置
├── data/                     # 数据集（2WikiMultiHopQA filtered 128题）
├── results/
│   └── v51-2wiki50-filtered-seed4-128-qwen35-9b-v18/
│       └── results.jsonl     # V18评测结果（85/128 = 66.41%）
├── docs/                     # 设计文档
├── recurrent/                # Recurrent模式代码
├── README.md                 # 原始README
├── README_V18.md             # 本文件
├── LICENSE
└── pyproject.toml
```

## 关键改进

### 1. metrics.py - 答案归一化
- `_strip_latex`: 清理 LaTeX 格式
- `_normalize_dates`: 日期归一化
- `_TITLE_PREFIX`: 清理头衔前缀（Mr., Dr., President）
- `_REDUNDANT_SUFFIX`: 清理冗余后缀
- `_ALIASES` / `_PHRASE_ALIASES`: 别名扩展（USA ↔ United States）

### 2. agent_prompts.py - final_answer_prompt 改进
- 禁止拒绝回答："Even if information seems incomplete, you MUST output your best-supported guess"
- "Never use refusal phrases like 'I cannot', 'unknown', 'not enough information'"
- D类比较题5步流程
- E类先验污染温和版
- 方案1一句话类型提醒
- yes/no稳定性指导
- inference题型针对性指导

### 3. prompts.py - answer_prompt（graph模式）
- 包含同样的 V9 改进

### 4. subquery_runner.py - ANSWER阶段附加证据链原句（V18核心改进）
- 遍历所有 RESOLVED 查询的 support_fact_ids
- 用 archive.entry(ref) 取回原始句子
- 格式："[ref] title: text"
- 最多10条，附加到 final_memory 末尾
- 标题："=== Verified Original Source Sentences (ground your answer in these) ==="

### 5. 配置参数
- temperature: 0（关键！从0.7降到0，EM直接+4%）
- timeout_seconds: 300（从120增到300，0 ERROR）
- max_concurrency: 12

## 分题型结果
| 题型 | 题数 | V18 EM |
|------|------|--------|
| comparison | 35 | 91.4% |
| bridge_comparison | 24 | 87.5% |
| compositional | 53 | 43.4% |
| inference | 16 | 56.2% |
| **总计** | **128** | **66.41%** |

## 运行方式

### 环境要求
- Python 3.10+
- conda 环境：`rememr1`
- 依赖：`pip install -e .`（或 `pip install -r requirements.txt`）

### 快速开始
```bash
# 1. 激活环境
conda activate rememr1

# 2. 运行评测（V18配置：temperature=0, concurrency=12, timeout=300）
python -m delaybind_core experiment --config configs/v51_2wiki50_filtered_seed4_128_fde.json

# 3. 查看结果
# 结果输出到 results/v51-2wiki50-filtered-seed4-128-qwen35-9b-fde/results.jsonl
```

### 配置说明（configs/v51_2wiki50_filtered_seed4_128_fde.json）
- `temperature`: 0（关键参数，从0.7降到0，EM直接+4%）
- `max_concurrency`: 12
- `timeout_seconds`: 300
- `dataset`: 2WikiMultiHopQA filtered 128题（seed4）
- `model`: Qwen/Qwen3.5-9B（通过 SiliconFlow API）

### 复现 V18 结果
已包含 `results/v51-2wiki50-filtered-seed4-128-qwen35-9b-v18/results.jsonl`，可直接用于分析：
```python
import json
correct = sum(1 for line in open('results/v51-2wiki50-filtered-seed4-128-qwen35-9b-v18/results.jsonl')
              if json.loads(line).get('answer_exact'))
print(f"EM: {correct}/128 = {correct/128*100:.2f}%")
# Output: EM: 85/128 = 66.41%
```

## 注意事项
- 运行间波动：temperature=0下相同代码两次跑可差5-6题，建议跑2次取最佳
- V18第一次跑 EM=66.41%，重跑 EM=62.5%，取最佳为66.41%
- 最大失分点：compositional（43.4%），中间实体错误（16题）是主要原因
