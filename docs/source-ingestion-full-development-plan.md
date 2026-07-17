# Research Agent 原文读取与证据基础设施完整开发方案

## 1. 方案定位

本方案直接建设完整生产版本。最终交付覆盖从用户材料上传、原文解析、OCR、版面与表格恢复、检索、原文回读、证据验证、精确引用，到安全、可观测性、评测和部署的完整链路。

实施过程按里程碑逐步合并和验收，但下列能力全部属于正式交付范围：

- 常见办公文档、PDF、网页、图片和压缩材料包输入；
- 文本型和扫描型文档处理；
- 页码、章节、表格、单元格、幻灯片等稳定定位；
- 关键词与语义混合检索；
- 精确原文回读和证据引用；
- 异步解析任务、失败重试、断点恢复和幂等；
- 项目隔离、文件安全、Prompt Injection 防护和审计；
- Web、CLI 和 Agent 共用同一套领域服务；
- 自动化测试、真实材料评测和上线质量门槛。

## 2. 建设目标

系统最终应支持以下完整业务闭环：

```text
用户创建调研项目
  → 上传单个文件或材料包
  → 文件安全检查与内容去重
  → 格式识别、解密或转换
  → 文本/版面/表格/OCR 提取
  → 标准化文档与稳定定位生成
  → 元数据识别与用户校正
  → 结构化切分和混合索引
  → 用户预览、启用和来源分级
  → Strategist 计算已有材料覆盖率
  → Collector 定向检索本地材料并补充外部来源
  → Validator 回查原文、检测冲突和证据不足
  → Analyst 只消费通过验证的证据
  → Formatter 生成可追溯脚注与来源附录
  → 质量审计通过后交付报告
```

### 2.1 核心能力目标

1. 用户上传的材料成为正式的项目来源，而不是临时 Prompt 附件。
2. 每条证据可以定位到具体文件及页码、段落、表格、单元格或幻灯片。
3. 长文通过检索和精确回读进入 Agent 上下文，不整篇灌入模型。
4. 所有解析结果可重新生成、可版本化、可审计。
5. 本地材料和 Web 来源使用统一的来源、证据和引用模型。
6. 模型不能仅凭文字声明“已验证”；关键质量门槛由程序计算。
7. 上传、解析、索引、检索和调研运行相互解耦，任何阶段失败可恢复。

### 2.2 完整版质量指标

以下指标作为初始上线门槛，后续由真实评测集校准：

| 指标 | 目标 |
|---|---:|
| 文本型文档正文提取完整率 | ≥ 98% |
| 页码/章节/单元格定位准确率 | ≥ 99% |
| 规则型表格单元格恢复准确率 | ≥ 95% |
| 清晰扫描件 OCR 字符准确率 | ≥ 95% |
| 固定问题 Retrieval Recall@10 | ≥ 90% |
| 最终报告关键引用支持率 | ≥ 95% |
| 关键数字可追溯率 | 100% |
| 跨项目文件隔离测试通过率 | 100% |
| 解析任务崩溃恢复成功率 | 100% |
| 重复任务幂等通过率 | 100% |

## 3. 完整目标架构

采用“模块化单体 API + 独立后台 Worker”的结构。领域逻辑保持在 Python 包内，Web、CLI、Worker 和 Agent Tool 通过相同 Service 层调用，避免多套实现漂移。

```text
┌─────────────────────────────────────────────────────────────┐
│                         Entry Points                         │
│ Web UI / REST API / CLI / Research Pipeline / Admin Tools   │
└──────────────────────────────┬──────────────────────────────┘
                               │
┌──────────────────────────────▼──────────────────────────────┐
│                    Source Domain Services                    │
│ Upload · Catalog · Version · Parse · Review · Retrieve      │
│ Evidence · Citation · Quality Gate · Audit                  │
└───────────────┬───────────────────┬─────────────────────────┘
                │                   │
       ┌────────▼────────┐  ┌──────▼────────────────────────┐
       │ Background Jobs │  │ Extractor / OCR / Normalizer │
       │ Queue + Worker   │  │ PDF · Office · HTML · Image │
       └────────┬────────┘  └──────┬────────────────────────┘
                │                  │
       ┌────────▼──────────────────▼─────────────────────────┐
       │                 Storage and Indexes                  │
       │ Metadata DB · Raw Object Store · Search · Vectors   │
       └────────┬──────────────────┬─────────────────────────┘
                │                  │
       ┌────────▼────────┐  ┌──────▼────────────────────────┐
       │ Agent Tool APIs │  │ Observability and Operations │
       │ List/Search/Read│  │ Logs · Metrics · Traces      │
       └─────────────────┘  └───────────────────────────────┘
```

### 3.1 部署形态

同一套领域接口支持两种运行配置：

#### 本地单机配置

- 原始文件：项目目录文件存储；
- 元数据：SQLite；
- 关键词索引：SQLite FTS5；
- 向量索引：本地持久化向量库；
- Worker：独立本地进程；
- 适合个人使用和本地测试。

#### 服务器配置

- 原始文件：S3 兼容对象存储；
- 元数据：PostgreSQL；
- 关键词与向量：PostgreSQL FTS + pgvector，或独立检索后端；
- 任务队列：Redis 支持的持久队列；
- Worker：可水平扩容；
- 适合云端 Web 前端和多项目并发。

存储、队列和检索必须通过 Port/Adapter 隔离，领域层不得直接依赖具体数据库路径。

## 4. 代码模块规划

集成到现有 Python Research Agent 后，建议形成以下目录：

```text
src/research_agent/
├── sources/
│   ├── models.py
│   ├── enums.py
│   ├── service.py
│   ├── catalog.py
│   ├── versioning.py
│   ├── security.py
│   ├── normalization.py
│   ├── chunking.py
│   ├── metadata_enrichment.py
│   ├── quality.py
│   ├── ports/
│   │   ├── object_store.py
│   │   ├── repository.py
│   │   ├── task_queue.py
│   │   ├── search_index.py
│   │   └── vector_index.py
│   ├── adapters/
│   │   ├── local_object_store.py
│   │   ├── s3_object_store.py
│   │   ├── sqlite_repository.py
│   │   ├── postgres_repository.py
│   │   ├── local_task_queue.py
│   │   └── redis_task_queue.py
│   ├── extractors/
│   │   ├── base.py
│   │   ├── detector.py
│   │   ├── pdf.py
│   │   ├── office.py
│   │   ├── spreadsheet.py
│   │   ├── presentation.py
│   │   ├── html.py
│   │   ├── text.py
│   │   ├── image.py
│   │   └── archive.py
│   ├── ocr/
│   │   ├── service.py
│   │   ├── preprocess.py
│   │   └── engines.py
│   ├── retrieval/
│   │   ├── lexical.py
│   │   ├── semantic.py
│   │   ├── hybrid.py
│   │   ├── reranker.py
│   │   └── filters.py
│   └── evidence/
│       ├── models.py
│       ├── repository.py
│       ├── verifier.py
│       └── citation.py
├── jobs/
│   ├── models.py
│   ├── worker.py
│   ├── retry.py
│   └── handlers/
│       ├── ingest_source.py
│       ├── reprocess_source.py
│       ├── rebuild_index.py
│       └── delete_source.py
├── tools/builtins/
│   ├── list_project_sources.py
│   ├── search_project_sources.py
│   ├── read_project_source.py
│   └── inspect_source_evidence.py
└── api/routes/
    ├── sources.py
    ├── source_jobs.py
    └── source_search.py
```

现有 `web_app.py` 只保留应用装配与路由挂载，不能继续承载上传、解析、索引等业务细节。

## 5. 来源生命周期

### 5.1 状态机

```text
created
  → uploading
  → quarantined
  → validating
  → needs_password / rejected / queued
  → extracting
  → ocr_processing
  → normalizing
  → indexing
  → needs_review / ready
  → active
  → archived / superseded / failed
```

### 5.2 状态规则

- `quarantined`：文件已保存但尚未通过安全检查。
- `needs_password`：加密文档等待用户提供一次性密码。
- `needs_review`：解析完成但存在高严重度警告，需要用户确认。
- `ready`：解析和索引已完成，但尚未参与调研。
- `active`：已被用户启用，可供 Agent 检索。
- `superseded`：同一逻辑来源上传了新版本，旧版本保留引用但不再默认检索。
- `failed`：任务失败且自动重试耗尽，保存阶段和错误原因。

状态更新必须使用乐观锁或版本号，避免重复任务覆盖较新的状态。

## 6. 完整输入格式范围

| 类别 | 格式 | 完整处理要求 |
|---|---|---|
| PDF | PDF、加密 PDF、扫描 PDF | 文本、页码、坐标、标题、表格、图片、脚注、OCR、密码流程 |
| Word | DOCX、DOC | 标题、段落、列表、页眉页脚、批注、脚注、表格、图片 OCR |
| Spreadsheet | XLSX、XLS、CSV、TSV | 工作表、单元格范围、公式、显示值、合并单元格、隐藏行列、图表元数据 |
| Presentation | PPTX、PPT | 页面、标题、正文、备注、表格、图表、图片 OCR |
| Web | HTML、MHTML | 正文、标题、表格、链接、发布时间、作者、原始 URL |
| Text | MD、TXT、RTF | 标题层级、行号、编码、表格和代码块 |
| Image | PNG、JPEG、TIFF、HEIC | OCR、旋转校正、版面块、坐标定位 |
| Archive | ZIP | 安全解包、递归深度限制、文件清单、批量入库 |

### 6.1 解析技术选择

- PDF 文本与布局：PyMuPDF；
- PDF 表格补充：pdfplumber；
- PDF 结构修复：在保留原始文本基础上使用规则或模型辅助，不覆盖原始层；
- DOCX：python-docx；
- XLSX：openpyxl；
- PPTX：python-pptx；
- DOC/XLS/PPT：受控的 LibreOffice headless 转换；
- HTML：BeautifulSoup + 正文提取规则；
- OCR 主引擎：PaddleOCR，支持中文和英文；
- OCR 预处理：方向检测、去噪、二值化、透视校正；
- 可选 OCR 兜底：Tesseract；
- 文件类型识别：Magic Number + MIME + 解析器探测，不能只信扩展名。

### 6.2 PDF 完整处理策略

PDF 是最重要输入格式，必须单独实现完整策略：

1. 检测是否加密、损坏、线性化或含异常对象。
2. 逐页统计文字覆盖率，自动识别文本页、扫描页和混合页。
3. 文本页提取字符、块、坐标和阅读顺序。
4. 扫描页进入 OCR，并保留 OCR 置信度和文字框坐标。
5. 提取页眉页脚候选并标记，不直接删除原文。
6. 识别标题层级、段落、列表、脚注、表格和图片说明。
7. 表格同时保留 Markdown、二维单元格和页内坐标三种表示。
8. 混合页对原生文字和 OCR 结果去重。
9. 生成逐页预览、解析警告和质量评分。
10. 引用 locator 至少包含页码；表格引用包含表号或坐标范围。

## 7. 标准化数据契约

### 7.1 SourceAsset

```python
class SourceAsset(BaseModel):
    source_id: str
    project_id: str
    collection_id: str | None
    logical_source_id: str
    version: int
    original_filename: str
    detected_media_type: str
    file_size: int
    sha256: str
    storage_uri: str
    status: SourceStatus
    enabled: bool
    title: str | None
    publisher: str | None
    authors: list[str]
    published_at: datetime | None
    language: str | None
    source_tier: Literal["S", "A", "B", "D", "unclassified"]
    confidentiality: Literal["public", "internal", "restricted"]
    tags: list[str]
    user_notes: str
    parser_version: str | None
    created_at: datetime
    updated_at: datetime
```

### 7.2 SourceDocument

```python
class SourceDocument(BaseModel):
    source_id: str
    document_id: str
    metadata: DocumentMetadata
    pages: list[PageInfo]
    blocks: list[ContentBlock]
    tables: list[TableBlock]
    images: list[ImageBlock]
    warnings: list[ExtractionWarning]
    quality: ExtractionQuality
```

### 7.3 ContentBlock

```python
class ContentBlock(BaseModel):
    block_id: str
    source_id: str
    block_type: Literal[
        "title", "heading", "paragraph", "list", "table",
        "footnote", "header", "footer", "caption", "image_text"
    ]
    text: str
    sequence: int
    heading_path: list[str]
    locator: SourceLocator
    bbox: list[float] | None
    extraction_method: Literal["native", "ocr", "converted", "model_repaired"]
    confidence: float
```

### 7.4 SourceLocator

```python
class SourceLocator(BaseModel):
    page: int | None = None
    slide: int | None = None
    sheet: str | None = None
    cell_range: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    paragraph_start: int | None = None
    paragraph_end: int | None = None
    table_id: str | None = None
    block_ids: list[str] = []
```

### 7.5 SourceChunk

```python
class SourceChunk(BaseModel):
    chunk_id: str
    source_id: str
    source_version: int
    text: str
    heading_path: list[str]
    locators: list[SourceLocator]
    block_ids: list[str]
    token_count: int
    embedding_model: str | None
    embedding_version: str | None
    content_hash: str
```

### 7.6 EvidenceRecord

```python
class EvidenceRecord(BaseModel):
    evidence_id: str
    project_id: str
    research_question_id: str
    claim: str
    normalized_value: str | float | int | None
    unit: str | None
    period: str | None
    source_id: str
    source_version: int
    chunk_id: str
    locator: SourceLocator
    excerpt: str
    source_tier: str
    verification_status: Literal[
        "unverified", "supported", "partially_supported",
        "contradicted", "stale", "rejected"
    ]
    confidence: float
    verified_by: str | None
    verified_at: datetime | None
```

所有最终报告引用必须来自 `EvidenceRecord`，不能让 Formatter 临时根据 Markdown 猜测来源。

## 8. 原始文件、派生文件与版本管理

### 8.1 存储规则

- 原始文件不可变，使用 SHA-256 校验。
- 同项目完全相同文件直接去重并关联已有资产。
- 跨项目是否复用物理文件由存储层决定，逻辑权限始终隔离。
- 解析产物按 `source_id/parser_version/content_hash` 版本化。
- 索引可重建，不作为唯一事实存储。
- 用户删除时先进入归档或软删除，确认无引用后执行物理清理。

### 8.2 来源版本

同一报告上传新版时：

- 保留相同 `logical_source_id`；
- `version` 递增；
- 默认只检索最新版；
- 历史报告引用仍绑定旧版；
- 可生成版本差异摘要，包括新增、删除和数字变化。

### 8.3 近重复识别

除 SHA-256 精确去重外，使用正文指纹识别：

- 同一报告不同下载版本；
- 网页正文与 PDF 转存件；
- 去掉封面或页眉后的近重复内容；
- 材料包中重复附件。

近重复结果提示用户，不自动删除具有不同定位或版本价值的文件。

## 9. 异步任务系统

### 9.1 任务类型

- `source.security_scan`
- `source.extract`
- `source.ocr`
- `source.normalize`
- `source.enrich_metadata`
- `source.chunk`
- `source.embed`
- `source.index`
- `source.reprocess`
- `source.rebuild_index`
- `source.delete`

### 9.2 任务要求

- 每个任务拥有稳定 `job_id` 和幂等键；
- 保存输入版本、处理阶段、尝试次数和错误类型；
- 支持指数退避和最大重试；
- Worker 崩溃后任务可重新领取；
- 同一 source 同一 parser 版本不能并发重复处理；
- 支持取消和管理员重新执行；
- Web 通过轮询或 SSE 获取进度；
- 大文件按页或子文件汇报进度；
- 失败不会破坏上一个已就绪版本。

## 10. 检索系统

### 10.1 索引字段

每个 Chunk 至少索引：

- 正文；
- 标题路径；
- 文件标题；
- 发布者、作者和标签；
- 来源等级；
- 发布时间；
- 格式类型；
- 页码、工作表或幻灯片；
- source_id、version 和 project_id；
- 启用状态和保密等级。

### 10.2 混合检索流程

```text
用户问题或 Agent 子问题
  → 查询意图规范化
  → 关键词查询 + 语义查询
  → 项目/来源/时间/等级过滤
  → 分数归一化与融合
  → 近重复结果折叠
  → Cross-Encoder 或 LLM Rerank
  → 邻接块扩展
  → 返回片段、定位和命中解释
```

### 10.3 检索约束

- project_id 必须是不可绕过的服务端过滤条件；
- 默认只检索 `active` 且最新的来源版本；
- 支持限定指定 source、来源等级、时间范围和格式；
- 返回结果必须包含 locator 和匹配原因；
- Reranker 不得改变原文，只能排序；
- Embedding 模型和版本必须记录，升级后支持后台重建索引；
- 表格需同时索引表题、表头、行列上下文和单元格文本。

### 10.4 Agent 工具

#### ListProjectSources

列出当前项目材料、版本、状态、来源等级、发布时间、标签和解析质量。

#### SearchProjectSources

支持查询、来源过滤、时间过滤、top_k、是否包含表格等参数，返回 `chunk_id + excerpt + locator + score`。

#### ReadProjectSource

根据 `source_id + chunk_id/locator` 返回精确原文及前后文，不能接受任意文件路径。

#### InspectSourceEvidence

返回某条 EvidenceRecord 的原文、定位、验证状态、冲突和相关证据。

## 11. 元数据识别与用户校正

系统自动提取：

- 标题；
- 发布者和作者；
- 发布/修订时间；
- 报告类型；
- 语言；
- 文档编号、股票代码或监管编号；
- 推荐来源等级；
- 涉及的研究主题和实体。

元数据来源按优先级合并：

1. 文件内明确字段；
2. PDF/Office 元数据；
3. 文件名和目录；
4. 规则识别；
5. 模型辅助识别。

模型生成的元数据必须标记置信度，用户可以修改。用户修改值不得在重新解析时被自动覆盖。

## 12. Web 与 CLI 完整功能

### 12.1 Web API

```text
POST   /api/projects/{project_id}/sources
POST   /api/projects/{project_id}/source-batches
GET    /api/projects/{project_id}/sources
GET    /api/projects/{project_id}/sources/{source_id}
PATCH  /api/projects/{project_id}/sources/{source_id}
DELETE /api/projects/{project_id}/sources/{source_id}
POST   /api/projects/{project_id}/sources/{source_id}/password
POST   /api/projects/{project_id}/sources/{source_id}/reprocess
POST   /api/projects/{project_id}/sources/{source_id}/activate
POST   /api/projects/{project_id}/sources/{source_id}/archive
GET    /api/projects/{project_id}/sources/{source_id}/preview
GET    /api/projects/{project_id}/sources/{source_id}/blocks
GET    /api/projects/{project_id}/sources/{source_id}/versions
GET    /api/projects/{project_id}/source-jobs/{job_id}
POST   /api/projects/{project_id}/source-search
GET    /api/projects/{project_id}/source-evidence/{evidence_id}
```

上传采用分片或流式写入，不能把大文件一次性读入内存。

### 12.2 Web UI

项目页增加完整“材料中心”：

- 拖拽上传和批量材料包；
- 上传、解析、OCR、索引实时进度；
- 格式、页数、工作表、大小和版本；
- 标题、发布者、日期、来源等级、保密等级和标签编辑；
- 逐页或逐章节预览；
- OCR 结果与原图对照；
- 表格预览；
- 解析警告和失败原因；
- 搜索调试页面；
- 启用、禁用、重处理、版本比较、归档和删除；
- 当前材料对研究问题的覆盖矩阵。

### 12.3 CLI

```bash
research-agent sources add <project> <files...>
research-agent sources add-batch <project> <directory-or-zip>
research-agent sources list <project>
research-agent sources inspect <project> <source_id>
research-agent sources preview <project> <source_id>
research-agent sources search <project> "query"
research-agent sources activate <project> <source_id>
research-agent sources reprocess <project> <source_id>
research-agent sources rebuild-index <project>
research-agent sources compare <project> <source_id> --versions 1 2
```

CLI 只能调用 Source Service，不复制解析流程。

## 13. Agent 流程改造

### 13.1 Strategist

- 调研规划前读取来源目录和材料摘要；
- 建立“研究问题 × 已有材料”覆盖矩阵；
- 标记只依赖用户材料、需要外部验证和完全缺失的问题；
- 将材料的时间范围和保密等级纳入调研计划。

### 13.2 Collector

- 先检索项目材料，再执行 Web 搜索；
- 每个事实生成 EvidenceRecord；
- 对表格数字保留单元格范围；
- 对 PDF 引用保留页码和原文片段；
- 不允许用一个来源编号代表多份文件；
- 无法定位原文的内容只能进入候选证据，不能直接进入分析。

### 13.3 Validator

- 对关键事实强制回读原文；
- 检查 excerpt 是否支持 claim；
- 检查来源等级、独立性、时效和口径；
- 检查同一数字在不同材料中的冲突；
- 检查用户材料是否为二手转引；
- 将验证状态写入 EvidenceRecord；
- 不再由模型单独决定 `converged`。

### 13.4 Analyst

- 默认只消费 `supported` 证据；
- 对 `partially_supported` 证据必须显式披露限制；
- 区分事实、计算、推断和预测；
- 所有计算记录输入证据和公式；
- 禁止引用已经被归档或 superseded 的最新版替代来源，除非报告明确讨论历史版本。

### 13.5 Formatter

- 引用从 EvidenceRecord 确定性生成；
- 本地文件引用展示文件名、发布者、版本和 locator；
- Web 来源展示标题、URL、发布日期和访问日期；
- 附录生成来源清单、证据局限和版本说明；
- 最终报告生成后执行引用审计，不能由 Formatter 自己补造来源。

## 14. 确定性质量门槛

原文读取层接入后，研究流程的通过条件由程序计算：

```text
关键研究问题覆盖率达到阈值
关键事实均存在 EvidenceRecord
关键数字 100% 可定位和可复算
关键事实满足：
  1 个直接 S 级来源
  或 2 个相互独立来源交叉支持
关键冲突为 0，或有明确裁决和披露
高严重度解析警告已经人工确认
不存在引用已删除或失效来源
引用支持率达到阈值
```

质量状态统一为：

- `passed`
- `passed_with_limitations`
- `needs_more_research`
- `needs_human_review`
- `blocked`

模型只提交建议和解释，最终状态由 Quality Gate 计算。

## 15. 安全方案

### 15.1 文件安全

- 文件名规范化，禁止绝对路径、`../`、控制字符和符号链接逃逸；
- Magic Number、MIME、扩展名和解析器结果交叉验证；
- 上传文件先进入隔离区；
- 服务器部署接入恶意文件扫描；
- 限制文件大小、压缩后大小、页数、像素、工作表和递归深度；
- ZIP 防路径穿越、压缩炸弹和嵌套炸弹；
- 不执行宏、PDF JavaScript、嵌入程序和外部链接；
- LibreOffice 转换运行在受限子进程或容器；
- 加密文件密码只用于当前任务，不写日志，默认不持久化。

### 15.2 数据权限

- 所有查询必须绑定 project_id；
- 服务器模式增加用户/租户/项目 ACL；
- 原始文件下载使用鉴权或短期签名地址；
- `restricted` 材料不得用于跨项目缓存或模型训练；
- 删除、下载、重新处理、改变来源等级均写入审计日志；
- 日志默认只记录 source_id，不记录完整正文。

### 15.3 Prompt Injection 防护

- 文档内容统一标记为 untrusted evidence；
- 系统提示明确禁止执行文档中的指令；
- 检测“忽略先前指令”“执行命令”“泄露系统提示”等高风险片段；
- 高风险片段保留原文但添加安全标签；
- 来源工具只返回文本和定位，不提供执行能力；
- Agent 工具仍受固定白名单约束。

## 16. 可观测性与运维

### 16.1 结构化日志

所有事件携带：

- request_id
- project_id
- source_id
- source_version
- job_id
- parser_version
- stage
- duration_ms
- result/error_code

### 16.2 指标

- 上传文件数和字节量；
- 各格式解析成功率；
- OCR 页数、耗时和平均置信度；
- 每页/每文件解析耗时；
- 任务排队、重试和失败数量；
- 索引大小和重建耗时；
- 检索 P50/P95 延迟；
- Retrieval Recall、引用支持率；
- Agent 本地材料命中率；
- 每份报告使用的本地/外部来源比例。

### 16.3 运维工具

- 查看和重新执行失败任务；
- 重建单个来源或整个项目索引；
- 切换 parser/embedding 版本后批量迁移；
- 检查孤儿文件和失效引用；
- 导出来源清单、证据库和审计日志；
- 校验数据库、对象存储和索引的一致性。

## 17. 测试与评测体系

### 17.1 单元测试

- 文件检测、安全校验、路径隔离；
- 各格式解析器；
- OCR 路由和去重；
- 标题、段落、表格和 locator；
- Chunk 边界和内容哈希；
- 来源版本、精确去重和近重复；
- 任务幂等、重试和恢复；
- 关键词、语义和混合排序；
- EvidenceRecord 验证；
- 引用生成和失效检测。

### 17.2 Golden Fixtures

```text
tests/fixtures/sources/
├── pdf/
│   ├── text_report.pdf
│   ├── scanned_zh.pdf
│   ├── mixed_text_scan.pdf
│   ├── financial_tables.pdf
│   ├── encrypted.pdf
│   └── malformed.pdf
├── word/
├── spreadsheets/
├── presentations/
├── html/
├── images/
├── archives/
└── malicious/
```

每个 fixture 配套人工标注的正文、表格、locator、元数据和预期警告。

### 17.3 集成测试

- Web 上传到后台解析完成；
- CLI 与 Web 读取同一来源状态；
- Worker 中断后恢复；
- 重复提交不重复生成资产；
- 搜索结果能回读精确原文；
- 项目 A 无法读取项目 B；
- 来源新版上传后历史引用仍有效；
- 两份互相矛盾的材料能够触发冲突；
- 最终报告脚注能够回到原文位置。

### 17.4 端到端调研评测

至少维护 20 个固定问题，覆盖：

- 财报数字；
- 行业规模；
- 政策法规；
- 公司产品和技术；
- 多版本报告差异；
- 表格跨行列读取；
- 扫描件；
- 中英文材料；
- 冲突来源；
- 过期来源；
- 恶意 Prompt 文档。

每次合并影响解析、检索、证据或 Prompt 的代码，都运行对应回归评测。

### 17.5 性能与稳定性测试

- 单个超长 PDF；
- 大量小文件材料包；
- 多项目并发上传；
- OCR Worker 并发；
- 检索延迟和索引重建；
- Worker 强制退出；
- 数据库或对象存储短暂不可用；
- 磁盘空间不足；
- 解析器版本升级和批量迁移。

## 18. 全量实施里程碑

下面是交付顺序，不是功能裁剪。全部里程碑完成才视为完整版本交付。

### 里程碑 1：领域模型与基础设施

交付：

- 完整 Source/Evidence/Job Schema；
- Repository、ObjectStore、TaskQueue、SearchIndex Port；
- 本地和服务器配置骨架；
- 数据库迁移；
- 原始文件不可变存储、哈希、版本和审计；
- pytest 测试基础与 Golden Fixtures 规范。

验收：数据模型、迁移、项目隔离、精确去重和版本测试通过。

### 里程碑 2：全格式解析器

交付：

- PDF、Word、Spreadsheet、Presentation、HTML、Text、Image、Archive；
- Legacy Office 转换；
- 标准 ContentBlock、TableBlock 和 SourceLocator；
- 解析质量报告和警告系统。

验收：所有格式 Golden Fixtures 达到定位和正文提取门槛。

### 里程碑 3：OCR、版面与表格恢复

交付：

- 扫描/混合 PDF 自动路由；
- OCR 预处理、方向检测和坐标保留；
- OCR 与原生文本去重；
- 表格结构恢复；
- OCR/原图对照预览。

验收：中文、英文、混合扫描件及财务表格评测达到目标。

### 里程碑 4：异步任务与恢复

交付：

- 上传到索引的完整 Job Pipeline；
- 任务进度、取消、重试、幂等和崩溃恢复；
- Worker 独立运行；
- 失败诊断和管理员重跑。

验收：故障注入测试全部通过，失败不破坏已就绪版本。

### 里程碑 5：混合检索

交付：

- 结构化 Chunking；
- 关键词和语义索引；
- 过滤、融合、去重、邻接扩展和 Rerank；
- 表格检索；
- 搜索调试与离线评测。

验收：固定评测集 Recall@10 和延迟达到门槛。

### 里程碑 6：Web 材料中心与 CLI

交付：

- 全部来源管理 API；
- 批量/分片上传；
- 解析进度、预览、警告、版本比较和搜索调试 UI；
- 完整 CLI；
- Web、CLI 共用 Service 层。

验收：用户无需修改文件系统即可完成整个材料生命周期管理。

### 里程碑 7：Agent 与证据链集成

交付：

- 四个项目级来源工具；
- Strategist 覆盖矩阵；
- Collector 结构化证据生成；
- Validator 原文回查；
- Analyst 证据状态约束；
- Formatter 确定性引用；
- 本地材料和 Web 来源统一证据模型。

验收：真实材料端到端报告的关键引用全部可回到正确原文位置。

### 里程碑 8：质量门槛、安全和运维

交付：

- 确定性 Research Quality Gate；
- 文件、权限、Prompt Injection 和审计防护；
- 日志、指标、Tracing 和运维工具；
- 性能、并发、故障恢复和安全测试；
- 数据备份、迁移和索引重建流程。

验收：质量、安全和稳定性门槛全部通过，形成上线检查清单。

### 里程碑 9：迁移与正式切换

交付：

- 将现有项目材料和历史报告迁移到新 Source/Evidence 模型；
- 识别旧 `[src: ...]` 引用并标记为待验证；
- 禁止新流程继续直接使用任意路径 `Read` 读取材料；
- CLI 和 Web 状态机调用同一领域服务；
- 灰度运行、结果对比和回滚方案；
- 完整使用文档和维护手册。

验收：新旧流程对照验证完成，正式调研仅走新原文读取与证据链。

## 19. 建议的 Git 交付节奏

每个里程碑拆成可验证提交，提交顺序建议：

1. 数据契约和数据库迁移；
2. 存储、安全与版本；
3. 各格式 extractor；
4. OCR 和表格恢复；
5. 后台任务；
6. 索引与检索；
7. Web/CLI；
8. Agent 工具；
9. Evidence 与 Quality Gate；
10. 安全、可观测性和迁移。

每个提交必须附对应测试；每个里程碑结束建立 Git tag 或明确里程碑提交，避免长周期改造失去可回滚节点。

## 20. 完整版本完成定义

满足以下全部条件才算完成：

- 所有约定格式能够上传、解析、预览、版本化和检索；
- 文本 PDF、扫描 PDF、Office 表格和图片 OCR 均通过 Golden 测试；
- Agent 不再依赖任意路径 Read 读取上传材料；
- 所有检索结果和证据都有稳定 locator；
- 所有关键数字都能回到原文并记录计算过程；
- 最终报告关键引用支持率达到门槛；
- 解析、OCR、索引任务支持重试、幂等和崩溃恢复；
- Web、CLI、Worker 和 Agent 共用同一领域服务；
- 本地和服务器部署配置均完成验证；
- 文件安全、项目隔离和 Prompt Injection 测试通过；
- 日志、指标、任务诊断、索引重建和备份恢复可用；
- 20 个端到端调研问题回归评测通过；
- 历史材料迁移完成，旧引用得到明确状态标记；
- 使用真实用户材料完成至少三类调研报告的全流程验收。

完成以上建设后，原文读取层不再只是“上传附件”功能，而会成为 Research Agent 的统一来源库、检索层和证据基础设施，直接决定调研结果的覆盖度、准确性和可审计性。
