# Golden 评测集标注规范

本目录是 `eval/run_eval.py` 的离线黄金集：输入文件 + 录制的 LLM / OCR 响应 + 期望输出。
评测过程**不访问任何外部服务**，可在 CI 与无网络环境复现。

## 目录

```text
eval/
├─ make_fixtures.py      # 生成 files/、bills/、cases.json（可重复执行）
├─ run_eval.py           # 指标计算、门禁、报告
├─ golden/
│  ├─ cases.json         # 用例清单（本文件是唯一真源）
│  ├─ files/             # EML / PDF / 图片 / Excel / Word 输入
│  └─ bills/             # 已有账单 JSON（扩展格式 + 最小格式）
└─ reports/              # 评测报告（JSON + Markdown）
```

重新生成夹具：

```bash
python eval/make_fixtures.py
python eval/run_eval.py --fixtures
```

## cases.json 字段

| 字段 | 说明 |
| --- | --- |
| `id` | 用例 ID，同时作为批次 ID |
| `file` | 输入文件（相对 `eval/golden/`） |
| `bills` | 已有账单文件列表（可选） |
| `match_keywords` | 录制 LLM 响应的触发词；只在**文档正文**匹配，不看来源文件名 |
| `ocr` | 图片用例的录制 OCR 结果：`lines[]` + `confidence` |
| `extraction` | 录制 LLM 的 `DocumentExtraction` 输出（原始事实，不含 operation/posting_type） |
| `expected_records[]` | 期望输出记录，字段为**归一化后**的值 |

## 标注规则

1. `expected_records` 的字段必须与 `BillRecord` 输出对齐：
   - `document_type` 使用归一值：`Debit Note` / `Credit Note` / `Invoice` / `Statement` / `Other`；
   - `reference_no` / `document_date` / 运输号使用归一后的值（日期 `YYYY-MM-DD`）；
   - `charge_item` 使用字典中的标准费用名；
   - `currency` 使用 ISO 4217 三位代码；
   - `payment_term` 使用 `PP` / `CC` / `null`；
   - `operation` 使用 `CREATE` / `UPDATE` / `NOCHANGE` / `PENDING_REVIEW`；
   - `match_level` 使用 `L1`–`L4` / `NONE`；
   - `review_reason` 使用 FR-16 枚举或 `null`。
2. 金额比较容差 0.01；字符串精确比较；`null` 必须显式书写。
3. `changed_fields_contains` 只标注必须出现的字段路径（如 `charge_details.amount`），不要求穷举。
4. `extraction.charges[].snippet` 必须是输入文本中可溯源的逐字片段（数字格式可不同，评测会归一比较）。
   这是幻觉指标的依据：快照无法溯源的记录会被 `V8` 标记并计入 `hallucination_rate`。
5. 每个用例应尽量只覆盖一个主要行为，便于失败定位。

## 指标与门禁

| 指标 | 门限 | 说明 |
| --- | --- | --- |
| `field_f1` | ≥ 0.95 | 14 个字段的微平均 F1 |
| `arap_accuracy` | ≥ 0.98 | `posting_type` 准确率（含 `null`） |
| `decision_accuracy` | ≥ 0.95 | `operation` 准确率 |
| `match_accuracy` | ≥ 0.90 | `match_level` 准确率 |
| `record_count_accuracy` | ≥ 0.95 | 每用例记录数完全一致 |
| `json_validity` | = 1.0 | 记录与批包装均通过 Pydantic Schema |
| `hallucination_rate` | = 0 | 快照不可溯源记录占比 |
| `unexpected_review_rate` | ≤ 0.05 | 期望非复核却被送复核的占比 |
| `changed_field_accuracy` | = 1.0 | 必须出现的变更字段全部命中 |

`review_rate` 只作信息展示，不作为门禁：黄金集包含 4 个**故意**触发复核的对抗用例，
整体复核率不代表生产数据的复核率，因此门禁使用 `unexpected_review_rate`。

## 新增用例

1. 在 `make_fixtures.py` 的 `build_files()` / `build_bills()` 中加入输入与账单；
2. 在 `build_cases()` 追加 `cases.append({...})`；
3. 运行 `python eval/make_fixtures.py && python eval/run_eval.py --fixtures`；
4. 确认门禁全绿后提交生成物（`files/`、`bills/`、`cases.json`）。
