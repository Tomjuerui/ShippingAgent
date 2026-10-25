# 航运智能账单助手智能体（Shipping Billing Agent）

把散落在 **EML / PDF / 图片 / Excel / Word** 里的航运费用，自动解析为标准 JSON，匹配已有账单并只做增量更新；每一步都留下可审计的来源、快照与推理依据。

技术栈：**Python 3.11+、LangChain / LangGraph、FastAPI、Pydantic v2**。前端为原生 HTML/CSS/JS，无构建工具链，克隆即运行。

---

## 1. 能力一览

| 能力 | 实现 |
| --- | --- |
| 多格式解析 | EML（正文 + 递归附件 ≤3 层）、文本型 PDF（pdfplumber）、扫描 PDF/图片（OCR API）、Excel（多工作表/表头偏移/合并单元格）、Word（DOCX 段落+表格，DOC 文本流降级） |
| 语义提取 | LangChain 结构化输出，LLM 只输出客观事实；温度 0；三级降级链 + 错误回喂重试 |
| 标准化 | 费用名别名/模糊归类、币种 ISO 4217、付款方式 PP/CC、OCR `0/O`、`1/I`、`5/S`、`8/B` 纠错、箱号 ISO 6346 校验位 |
| 匹配更新 | L1 单据号+费用名 → L2 提单号+费用名+币种 → L3 订舱号+… → L4 箱号+双方+日期±3天+金额；null 不覆盖、字段级 diff、冲突送复核 |
| AR/AP | 符号-类型正交矩阵；`CN × 接收方 × 负数 → AR` 双重反转陷阱有专门回归测试 |
| 幂等与防重 | 文件 SHA-256 + 历史账本；重复提交输出 NOCHANGE；同一批次 ID + `--deterministic` 可做到 byte-level 一致 |
| 输出 | 每费用一行、双字段费用名（原始 + 标准）、完整审计链、JSON Schema 校验后落盘 |
| 质量工程 | 146 个离线测试、11 个 golden 场景、9 项指标门禁、CI、失败注入、OCR/LLM 录制回放 |

---

## 2. 架构

```text
Web 上传 / CLI
   │
   ▼
ingest（SHA-256 去重 · 魔数探测）
   ▼
unpack（EML 递归解包 → 内容树）
   ▼
parse（Excel / Word / 文本 PDF / OCR 外发策略链）
   ▼
segment（按页/文档头/长度分块）
   ▼
extract ★ LLM 唯一节点（原始事实 + 行快照 + 置信度）
   ▼
normalize（费用名 / 币种 / 付款方式 / 单证号 / 日期 / 行快照溯源）
   ▼
validate（V1~V9 分币种勾稽、去重、汇总行过滤）
   ▼
arap（确定性决策矩阵 + 主体别名解析）
   ▼
match（L1→L4 短路 + 派生键 + 字段级 diff + 冲突检测）
   ▼
review（置信度门槛 / 复核原因路由）
   ▼
assemble（一对多展开 · 双字段费用名 · 审计模板句）
   ▼
output（batch.json / records.json / summary.json + status.json）
```

**职责边界**：LLM 只做“非结构化 → 结构化”；标准化、校验、AR/AP、匹配、diff、operation 判定全部是确定性代码，可单测、可回归、可解释。

关键代码：

- 状态机：`app/agent/graph.py`
- 契约：`app/extraction/schemas.py`
- LLM 接入：`app/extraction/llm_client.py`
- 字典：`app/governance/dicts/*.yaml`
- 匹配：`app/agent/nodes/match.py`
- 账单仓储（双 Schema + 派生键）：`app/storage/bills_repo.py`
- 外发策略链：`app/storage/file_exposer.py`
- Web：`app/server.py`、`app/web/static/`

---

## 3. 快速开始

### 3.1 环境

```bash
cd shipping-billing-agent
python -m venv .venv
# Windows
.venv\Scripts\activate
# Linux / macOS
# source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env     # Windows: copy .env.example .env
cp config.sample.yaml config.yaml   # config.yaml 属本地运行配置，不入库；样例可直接用
```

### 3.2 配置密钥（不要提交 .env）

编辑 `.env`：

```bash
LLM_PROFILE=local_proxy
LLM_API_KEY=你的密钥

# 可选：覆盖地址/模型
# LLM_BASE_URL=http://127.0.0.1:8788/v1
# LLM_MODEL=deepseek-v4.1-flash

# OCR 外发（无公网 IP 时至少配置一种，详见第 4.2 节）
OCR_EXPOSER=auto
# OSS_ENDPOINT=...
# OSS_BUCKET=...
# OSS_AK=...
# OSS_SK=...
```

### 3.3 启动 Web

```bash
python -m app.server
# 打开 http://127.0.0.1:8000
```

使用样例演示配置（记账主体设为样例单据签发方，便于看到 AR/AP 判定）：

```bash
# Windows PowerShell
$env:SBA_CONFIG="config.sample.yaml"; python -m app.server
# Git Bash / Linux
SBA_CONFIG=config.sample.yaml python -m app.server
```

### 3.4 CLI 批处理

```bash
python -m app.cli samples/original
python -m app.cli samples/generated --bills samples/bills
python -m app.cli samples/original --config config.sample.yaml
python -m app.cli samples/generated --bills samples/bills --json
```

常用参数：

| 参数 | 说明 |
| --- | --- |
| `--bills PATH...` | 已有账单 JSON 文件或目录 |
| `--batch-id ID` | 指定批次 ID（幂等测试/复现） |
| `--deterministic` | 固定时间戳（配合相同 batch-id 输出 byte-level 一致） |
| `--ignore-ledger` | 忽略历史哈希账本，强制重跑 |
| `--no-cache` | 禁用 LLM 响应缓存 |
| `--llm-profile` | `local_proxy` / `deepseek_official` / `openai` |
| `--config PATH` | 指定配置文件 |

### 3.5 自检

```bash
# 只评估配置，不发网络请求
python -m app.tools.ocr_selfcheck --dry-run
python -m app.tools.llm_selfcheck --dry-run

# 真调 OCR 健康检查 + 外发策略探测
python -m app.tools.ocr_selfcheck

# 真调 LLM /models 与一次 chat completion
python -m app.tools.llm_selfcheck
```

---

## 4. 配置说明

主配置：`config.yaml`；样例演示配置：`config.sample.yaml`；环境变量可覆盖关键项。
配置结构遵循《实现计划.md》§3.1。

### 4.1 LLM 多端点

```yaml
llm:
  profile: "local_proxy"
  profiles:
    local_proxy:       { base_url: "http://127.0.0.1:8788/v1", model: "deepseek-v4.1-flash" }
    deepseek_official: { base_url: "https://api.deepseek.com/v1", model: "deepseek-chat" }
    openai:            { base_url: "https://api.openai.com/v1",   model: "gpt-4o-mini" }
```

- `LLM_PROFILE` 切换 profile；`LLM_BASE_URL` / `LLM_MODEL` 覆盖地址与模型；
- `LLM_API_KEY` 全局优先，profile 也可配置自己的 `api_key_env`（如 `DEEPSEEK_API_KEY`）；
- 结构化输出降级链：`function_calling → json_mode → 提示词内嵌 Schema + Pydantic 校验 + 错误回喂（≤2 次）`；
- 响应缓存位于 `.cache/llm/`，key = `prompt_version + model + base_url + 字典版本 + sha256(输入)`；
- 温度固定 0。

### 4.2 OCR 文件外发策略链（重点）

OCR 服务在公网，**本机 127.0.0.1 / 192.168.x.x 它访问不到**。系统按策略链外发：

| 级别 | 策略 | 启用条件 | 说明 |
| --- | --- | --- | --- |
| A（首选） | 云对象存储预签名直链 | 配置 `OSS_ENDPOINT` / `OSS_BUCKET` / `OSS_AK` / `OSS_SK`（S3 兼容：阿里云 OSS、腾讯云 COS、MinIO） | 私有桶、TTL ≤30 分钟、OCR 完成即删对象 |
| B | 自托管文件代理 | 设置 `FILE_PROXY_BASE_URL`（部署机有公网 IP 或与 OCR 同内网） | FastAPI `GET /files/{token}/{filename}`，token 短 TTL |
| C | URL 直传 | 输入本身是 URL | 原样透传 |
| D（默认关） | 公共临时图床 | `OCR_TEMP_HOST_ENABLED=1` | 仅演示兜底，有数据外泄风险，开启会打印警告 |

- `OCR_EXPOSER=auto` 时按 A→B 探测；无可用策略时，**文本型 PDF 仍可本地解析**，扫描 PDF/图片按设计标记 `OCR_UNAVAILABLE` 送复核，不会拖垮整批；
- 启动时可用 `python -m app.tools.ocr_selfcheck` 真调探测；`/api/health` 展示当前策略；
- 生产建议：账单属财务敏感数据，使用自有账号私有桶，或部署到有公网 IP 的自有服务器走 B 级。

### 4.3 记账主体与口径

```yaml
accounting_entity:
  name: "Origin Agent"
  aliases: ["Origin Agent Co., Ltd.", "OA"]

arap:
  policy: "reversed_posting"   # 可选 same_account_offset
```

- 主体解析：别名 + 剥离 `Co., Ltd./Ltd/GmbH/公司` 等后缀 + 双向包含 + 相似度兜底；
- 无法对应 → `ENTITY_UNRESOLVED`；同时对应双方 → `SELF_TRADE`；均送复核；
- `same_account_offset`：Credit Note 不翻转科目，装配层将金额取负；
- PP/CC 与正文收费方向语句仅作佐证，矛盾送复核。

### 4.4 匹配与阈值

```yaml
matching:
  l4_enabled: true
  l4_to_review: true
  l4_allow_missing_container: true
  amount_tolerance: 0.01
  date_window_days: 3
  derived_keys_enabled: true

thresholds:
  review_below: 0.70
  low_confidence_below: 0.85
```

---

## 5. Web 使用

三视图：

1. **上传**：拖拽/选择多文件；可选上传已有账单 JSON；前端 + 服务端双重校验（格式、20 MB、单批 20 个）。
2. **进度**：批次 ID、总进度、逐文件状态（排队 / 解析 / OCR / 提取 / 判定 / 完成 / 失败 / 跳过）、各 operation 计数；2 秒轮询。
3. **结果**：
   - 摘要卡片与 operation 筛选（全部 / 新增 / 更新 / 无变化 / 待复核）；
   - “仅看低置信”开关；
   - 记录表：记录 ID、操作、单据号、费用名称（原始 → 标准）、金额币种、AR/AP、置信度；
   - 点击记录打开详情：结构化字段、字段变更、校验与修复、来源、原文快照、完整处理依据、语法高亮 JSON；
   - 支持复制 JSON、单条下载、整批导出。
4. 右侧历史批次可回看，服务状态卡展示 LLM / OCR / 外发策略。

### REST API

| 端点 | 方法 | 说明 |
| --- | --- | --- |
| `/api/batches` | POST | `files[]` + 可选 `bills[]`，返回 `batch_id` |
| `/api/batches` | GET | 历史批次 |
| `/api/batches/{id}` | GET | 状态 + 摘要 |
| `/api/batches/{id}/records` | GET | `?operation=UPDATE,PENDING_REVIEW`、`?low_confidence=true` |
| `/api/batches/{id}/export` | GET | 下载 `batch.json`（PRD 6.5 包装） |
| `/api/health` | GET | `?probe=1` 真调 OCR/LLM 连通性 |
| `/files/{token}/{filename}` | GET | B 级自托管文件代理（短 TTL） |

---

## 6. 数据契约

### 6.1 已有账单输入（双 Schema 兼容）

- **扩展格式**（PRD 6.2）：含 `shipment_info` + `charges[]`；
- **最小格式**（《要求.md》第五节）：扁平单费用记录，只有 `document_info` / `transaction_party` / `charge_details` / `financial_attributes` / `audit_trail`。

最小格式没有提单/订舱/箱号时，加载期会做**派生键重建**：

- `reference_no` 按提单号 / 箱号（ISO 6346 校验位确认）/ 订舱号模式探测；
- 扫描 `remarks` 与 `audit_trail` 文本挖掘编号；
- 派生键标记 `derived: true`，只用于 L2–L4；批报告输出匹配键覆盖率。

### 6.2 输出

单条记录字段见 PRD 6.3；每费用项目一条，`charge_details` 同时包含：

```json
{
  "original_charge_name": "H/D Fee",
  "charge_item": "Handling Charge",
  "amount": 40.0,
  "currency": "USD",
  "payment_term": "PP"
}
```

映射发生时，`audit_trail.ai_reasoning` 必含确定性模板句：

```text
原始费用名称 'H/D Fee' 已映射为标准费用项 'Handling Charge'
```

批产物：

```text
output/<batch_id>/
├─ batch.json      # 完整批包装（PRD 6.5）
├─ records.json    # 记录数组
├─ summary.json    # 摘要 + 文件结果 + 元数据
└─ status.json     # 实时进度（Web 轮询）
```

`output/ledger.json` 记录已处理文件哈希，用于重复提交识别与断点重跑。

---

## 7. Harness 质量工程

### 7.1 离线测试

```bash
pytest -q
```

覆盖：

- 解析：魔数错位、嵌套 EML 深度、HTML 表格、Excel 合并单元格/表头偏移、Word、文本 PDF、坏文件隔离；
- OCR：策略选择/清理、S3/代理/临时图床、重试/降级、低质量、响应结构归一；
- LLM：function calling、json mode、Schema 重试、连接失败、缓存命中；
- 标准化与校验：附录 A 全量映射、币种/付款方式、USO→USD、箱号校验位、日期歧义、分币种勾稽、去重、汇总行过滤；
- AR/AP：36 用例穷举，含 `CN × 接收方 × -40 → AR` 陷阱回归；
- 匹配：L1–L4、THC 别名、O→0、最小格式派生键、null 继承、冲突、歧义；
- 集成：多格式批、EML 附件根哈希、UPDATE、重复提交 NOCHANGE、idempotency、LLM/OCR 不可用、坏文件隔离、共享账单行歧义；
- API：上传→状态→筛选→导出→历史→静态页→404/400。

当前结果：**146 passed**。

### 7.2 Golden 评测（fixtures 离线）

```bash
python eval/make_fixtures.py       # 重新生成夹具（可选）
python eval/run_eval.py --fixtures
```

11 个场景覆盖：Excel 标准单、Word Credit Note 负数、PDF 更新、EML 附件、图片 OCR 录制回放、最小格式 L2 派生键、多币种、勾稽不平、主体无法对应、数据冲突、匹配歧义。

当前结果：

| 指标 | 结果 | 门限 |
| --- | --- | --- |
| 字段级 F1 | 1.000 | ≥ 0.95 |
| AR/AP 准确率 | 1.000 | ≥ 0.98 |
| 决策准确率 | 1.000 | ≥ 0.95 |
| 匹配级别准确率 | 1.000 | ≥ 0.90 |
| 记录数一致率 | 1.000 | ≥ 0.95 |
| JSON 合法率 | 1.000 | = 1.0 |
| 幻觉率 | 0.000 | = 0 |
| 非预期复核率 | 0.000 | ≤ 0.05 |
| 变更字段命中率 | 1.000 | = 1.0 |

报告写入 `eval/reports/`（JSON + Markdown）。黄金集故意包含 4 个对抗复核用例，因此整体 `review_rate` 只作信息展示，门禁使用 `unexpected_review_rate`。

### 7.3 CI

`.github/workflows/ci.yml`：安装依赖 → `pytest -q` → `python eval/run_eval.py --fixtures` → 两个 dry-run 自检。全流程无外部服务。

### 7.4 确定性与追踪

- 相同输入 + 相同 batch-id + `--deterministic` → `batch.json` byte-level 一致（有集成测试）；
- 所有 LLM/OCR 响应可按输入哈希缓存/录制，CI 回放；
- LangChain 支持可观测：设置 `LANGCHAIN_TRACING_V2=true` 与 `LANGCHAIN_API_KEY` 即可接入 LangSmith；不上云时用 `output/<batch>/audit_trail` 与 `status.json` 定位。

---

## 8. 目录结构

```text
shipping-billing-agent/
├─ app/
│  ├─ server.py / cli.py
│  ├─ config/settings.py
│  ├─ agent/
│  │  ├─ graph.py / state.py
│  │  └─ nodes/  ingest / unpack_eml / parse_* / ocr_client / segment / extract /
│  │             normalize / validate / arap / match / review / assemble
│  ├─ extraction/ schemas.py / llm_client.py / prompts/v1_extract.txt
│  ├─ governance/ dictionaries.py / ocr_fix.py / dicts/*.yaml
│  ├─ storage/ file_exposer.py / bills_repo.py / outputs.py / status.py
│  ├─ tools/ ocr_selfcheck.py / llm_selfcheck.py
│  └─ web/static/ index.html / style.css / app.js
├─ eval/ golden/ + make_fixtures.py + run_eval.py + reports/
├─ tests/ unit/ + integration/ + fixtures/
├─ samples/ original/ + generated/ + bills/
├─ config.yaml / config.sample.yaml
├─ requirements.txt / pytest.ini
└─ .github/workflows/ci.yml
```

---

## 9. 故障排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 图片/扫描 PDF 全部 `OCR_UNAVAILABLE` | 本机无公网可达地址，A/B 策略未配置 | 配置 S3 兼容桶或公网代理；纯文本 PDF 不受影响 |
| `/api/health` 显示 OCR 在线但外发未配置 | 健康检查只测服务可达，不代表服务能回访本机 | 这是两件事：配置策略链并用 `ocr_selfcheck` 真调探测 |
| LLM 401 / 超时 | key 缺失或 profile 错误 | 检查 `LLM_API_KEY`、`LLM_PROFILE`、`LLM_BASE_URL`；用 `llm_selfcheck` |
| 所有记录 `PENDING_REVIEW` + `ENTITY_UNRESOLVED` | `accounting_entity` 与单据双方不匹配 | 修改 `config.yaml` 主体与别名；样例可用 `config.sample.yaml` |
| 重复上传输出 NOCHANGE | 文件哈希已存在于 `output/ledger.json` | 预期行为；强制重跑加 `--ignore-ledger` |
| `.doc` 表格结构丢失 | 旧 DOC 无纯 Python 结构化解析器，当前为文本流降级 | 提供 DOCX；解析质量会标记 `low` 并在审计中提示 |
| 中文路径/控制台乱码 | Windows 控制台编码 | 设置 `PYTHONIOENCODING=utf-8`；输出文件本身为 UTF-8 |
| 端口占用 | 8000 已被使用 | 修改 `config.yaml` 的 `web.port`，或关闭占用进程 |

---

## 10. 已知边界（如实说明）

1. **一次运行只支持一个记账主体**（PRD Q1）；多主体需分批次或改造配置。
2. **扫描件依赖公网文件外发**；无云桶/公网代理时，扫描 PDF 与图片只能送复核。文本型 PDF、Excel、Word、EML 不受影响。
3. **不做汇率折算**：只保留原币种，交由财务系统按记账时点汇率处理。
4. **`.doc` 旧格式**只做文本流抽取，表格结构可能丢失，建议 DOCX。
5. **方向语句佐证**基于关键词表，非自然语言理解；矛盾时送复核，不做静默改写。
6. **L4 弱键**默认命中即复核；同一已有账单费用行被多条输入命中时全部送复核。
7. **OCR 响应适配**为容错归一（文本/行/表格/坐标/置信度常见结构），若服务商返回全新结构，需在 `app/agent/nodes/ocr_client.py` 增加适配。
8. **无鉴权/多租户**：定位为本地或内网财务工具；对公网部署需自行加反向代理鉴权与 HTTPS。
9. **上传原件与产物默认保留**：`data/uploads/`、`output/`；生产环境需配置清理策略与访问控制。
10. **LLM 成本/延迟**：默认快速档模型；复杂多页表格可经 `LLM_MODEL` 零代码切换更强模型，并用 golden 集回归。

---

## 11. 安全与合规

- 云桶必须私有 + 预签名短 TTL + OCR 完成即删；公共临时图床默认关闭；
- AK/SK、LLM key 只走环境变量，仓库中无硬编码密钥；
- 上传文件名净化，防止路径穿越；`.json` 账单与账单文件分开存储；
- EML 只做 MIME 解析，不加载外部资源、不执行宏/脚本；HTML 解析忽略 `script/style`；
- 输出审计链包含来源文件、SHA-256、邮件元数据、原文快照、匹配/更新/AR-AP 依据，满足财务复核与追溯；
- 账单数据默认只发送到所配置的 LLM 与 OCR 端点；切换官方公网 profile 意味着文本出网，需使用方确认合规。

---

## 12. 验收与文档

- 验收自查表（AC-1~AC-11 逐项证据）：`../docx/验收自查表.md`
- 基于 Web 的演示脚本：`../docx/演示脚本.md`
- 交付说明与执行记录：`../docx/交付说明.md`
- 原始需求与设计：《要求.md》《PRD.md》《实现计划.md》（上级目录）

```bash
# 最终复验
pytest -q
python eval/run_eval.py --fixtures
python -m app.tools.ocr_selfcheck --dry-run
```
