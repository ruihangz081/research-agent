# Research Agent 券商研报级图表与排版系统完整开发方案

## 1. 方案定位

本方案直接建设完整生产版本，不以“先让图表能显示”为目标，也不把调整 Prompt、增加颜色或补一个图表库包装成 MVP。

最终交付要完整解决以下问题：

- 图表由模型临时决定和绘制，结果不可重复；
- 图表数据缺少逐点来源、计算过程和审计；
- Markdown、Web、普通 PDF 和 LaTeX PDF 的表格与版式不一致；
- 长表格、中文换行、跨页、图注、脚注和来源经常出现格式错误；
- 每份报告都重新选择颜色、字号和布局，无法形成稳定品牌风格；
- 报告生成成功不等于图表正确、版面合格或达到交付标准；
- 历史项目无法在模板升级后稳定重排和回溯。

完整版本必须覆盖从已验证证据、结构化报告、图表规划、确定性绘图、统一模板、Web/PDF 渲染，到质量门禁、视觉回归、运维和历史迁移的全部链路。

实施过程可以按里程碑逐步合并，但下列能力全部属于正式完成范围，不得在交付时降级为“后续优化”：

1. 结构化报告与图表数据契约；
2. 图表数据逐点可追溯和计算审计；
3. 确定性图表渲染与券商研报设计系统；
4. Web、Markdown、HTML、SVG 和 PDF 的统一内容模型；
5. 固定、版本化、可测试的 LaTeX 报告模板；
6. 表格跨页、列宽、中文换行和复杂矩阵排版；
7. 报告生成、重排、预览、下载和版本管理；
8. 自动质量门禁、视觉回归和真实报告评测；
9. 本地与服务器部署、缓存、任务恢复和可观测性；
10. 现有 Markdown 报告和项目状态的兼容迁移。

## 2. 当前实现审计

### 2.1 当前报告链路

```text
Agent4 生成 04_analysis.md
  → Agent5 读取 Markdown 并生成 05_final_report.md
  → Web 使用简化 JavaScript 逐行解析 Markdown
  → 普通 PDF 使用 ReportLab 逐行转换
  → 高级 PDF 再调用 LLM，把整份 Markdown 改写成 LaTeX
  → XeLaTeX/LuaLaTeX 编译
```

### 2.2 已确认的根因

| 根因 | 当前表现 | 生产影响 |
|---|---|---|
| 没有结构化图表数据 | Formatter 只建议表格、ASCII 图或“建议插图” | 图表数量、类型、数据和位置不可控 |
| LLM 直接生成完整 LaTeX | 排版、绘图、转义和内容重组一次完成 | 编译结果不稳定，难以测试和重现 |
| 图表没有证据契约 | 文字引用可审计，图表中的每个点不可审计 | 数字可能误抄、漏单位或混用口径 |
| Web Markdown 解析器不完整 | 表格、脚注、引用、图片和复杂列表不受支持 | 预览与最终 PDF 内容不一致 |
| ReportLab 普通 PDF 逐行渲染 | Markdown 表格被当成代码文本 | 列宽、跨页和中文换行无法控制 |
| 多套输出引擎独立实现 | Web、普通 PDF、高级 PDF 三套逻辑 | 修复一处不会同步修复其他输出 |
| 没有固定设计令牌 | 字体、色板、图形规则依赖单次生成 | 无法形成券商研报式稳定视觉语言 |
| 没有渲染质量门禁 | 文件存在即视为生成成功 | 溢出、重叠、缺图、空白页无法阻断交付 |
| 没有模板与资产版本 | 历史报告无法确定使用了哪套样式 | 无法复现、回滚或批量重排 |

### 2.3 必须废止的做法

- 不再让 LLM 输出完整 LaTeX 文档。
- 不再让 LLM 直接编写 TikZ/PGFPlots 坐标或任意绘图代码。
- 不再从自然语言段落中临时抓数字并自动拼图。
- 不再保留两个内容不同的“普通 PDF”和“高级 PDF”渲染器。
- 不再用正则和逐行判断维护自制 Markdown 子集。
- 不允许没有来源、单位、口径和截止日期的图表进入正式报告。
- 不允许图表因数据不足而由模型补齐、插值或猜测。
- 不允许通过 `eval`、动态 Python、任意 JavaScript 或任意 LaTeX 执行图表配置。

## 3. 完整建设目标

### 3.1 业务目标

最终系统应支持以下完整闭环：

```text
已验证 EvidenceRecord
  → 定量数据集与派生指标
  → 图表候选与表达意图
  → ChartSpec/TableSpec 结构化契约
  → 数据、公式、单位、来源质量门禁
  → 统一主题的 SVG/PDF 图表资产
  → 结构化 ReportDocument
  → Web HTML 与 LaTeX PDF 确定性渲染
  → 内容一致性、编译、溢出和视觉质量门禁
  → 版本化发布、下载、重排与审计
```

### 3.2 核心能力目标

1. 同一份报告在 Web 和 PDF 中具有一致的章节、表格、图表、数字、来源和免责声明。
2. 每个图表数据点都能追溯到 EvidenceRecord，派生指标能追溯到公式和输入数据。
3. 相同输入、相同模板版本和相同渲染器版本必须生成语义一致、视觉稳定的结果。
4. 图表选择由研究意图驱动，但图表代码、样式、坐标和排版由确定性程序负责。
5. 所有报告使用同一套版本化券商研报设计系统，允许受控主题变体，不允许自由生成样式。
6. 表格、图表、脚注、来源、长 URL、中文文本和跨页都由正式排版组件处理。
7. 模板、图表和报告可以单独重渲染，不需要重新运行研究 Agent。
8. 任一质量门禁失败时，系统生成诊断结果但不得把报告标记为正式交付完成。

### 3.3 完成版质量指标

以下指标是生产上线的初始硬门槛，后续只能通过版本化评审调整：

| 指标 | 完成标准 |
|---|---:|
| 图表数据点证据覆盖率 | 100% |
| 派生指标公式与输入可追溯率 | 100% |
| 正文、表格、图表相同指标一致率 | 100% |
| 历史值/预测值/估算值类型标注率 | 100% |
| Web/PDF 章节和资产一致率 | 100% |
| 正式模板 LaTeX 编译成功率 | 100% |
| 丢图、空图、破损 SVG/PDF 发生率 | 0 |
| 阻断级 Overfull/版面溢出 | 0 |
| 表格表头跨页重复正确率 | 100% |
| 图表标题、单位、来源、截止日期完整率 | 100% |
| 黄金样本视觉回归通过率 | 100% |
| 同版本重复渲染内容哈希一致率 | 100% |
| 历史报告迁移后正文与引用保留率 | 100% |

## 4. 核心设计原则

### 4.1 模型负责语义，程序负责事实和呈现

LLM 可以：

- 判断一个结论是否适合用图表表达；
- 从允许的图表类型中选择语义类型；
- 生成图表标题、核心结论和解释文字；
- 指定需要引用的已存在数据集和指标 ID；
- 建议图表在报告中的章节位置。

LLM 不可以：

- 自由编写绘图代码；
- 自由创建 LaTeX 环境；
- 在没有 DataPoint 的情况下从正文抓取数字；
- 修改已验证数据、单位、公式或来源；
- 自由决定颜色、字体、尺寸和图例位置；
- 创建契约未支持的图表类型。

### 4.2 单一内容模型，多种确定性输出

`ReportDocument` 是正式内容源。Markdown 继续作为人类可读产物，但不再承担全部布局语义。

```text
ReportDocument
  ├── Markdown：可读、可审阅、可归档
  ├── HTML：Web 预览和可访问阅读
  ├── SVG：Web 图表和独立下载
  ├── PDF 图表：LaTeX 矢量嵌入
  └── PDF 报告：正式交付
```

### 4.3 一套图表渲染器

第一正式版本使用 Python Matplotlib 作为唯一规范图表引擎：

- 同一个 `ChartSpec` 同时输出 SVG 和 PDF；
- SVG 用于 Web，PDF 用于 LaTeX；
- PNG 只作为外部系统兼容和缩略图，不作为正式 PDF 的首选资产；
- 不同时维护 Matplotlib 和 ECharts 两套视觉规则；
- 如未来增加交互图表，ECharts 只能作为 Web 增强层，静态 SVG 仍是正式内容基准。

### 4.4 设计系统优先于单份报告美化

所有尺寸、间距、颜色、字体、线条、表格和图表规则都来自版本化主题 `brokerage_research_v1`。报告只能选择已审核主题，不能在 Prompt 中动态定义风格。

### 4.5 证据链延伸到像素前的数据

现有 EvidenceRecord 不能只支撑正文引用。图表数据必须建立：

```text
EvidenceRecord
  → DataPoint
  → DerivedMetric（可选）
  → Dataset
  → ChartSpec
  → RenderedAsset
  → ReportBlock
```

任一环节保存稳定 ID、版本、输入哈希和生成时间。

## 5. 完整目标架构

采用“模块化单体 API + 后台渲染 Worker”的结构。领域逻辑保留在 Python 包内，CLI、Web、Agent 和 Worker 使用相同服务层。

```text
┌──────────────────────────────────────────────────────────────────┐
│ Research Agents                                                  │
│ Strategist · Collector · Validator · Analyst · Formatter         │
└─────────────────────────────┬────────────────────────────────────┘
                              │ 仅引用稳定 ID
┌─────────────────────────────▼────────────────────────────────────┐
│ Report Domain                                                    │
│ Dataset · Metric · ChartSpec · TableSpec · ReportDocument        │
│ Template · Theme · RenderManifest · QualityReport                │
└──────────────┬───────────────────────────────┬───────────────────┘
               │                               │
┌──────────────▼──────────────┐   ┌────────────▼──────────────────┐
│ Evidence and Data Audit     │   │ Rendering Services            │
│ Provenance · Formula · Unit │   │ Chart · HTML · LaTeX · PDF    │
│ Reconciliation · Quality    │   │ Cache · Preview · Thumbnail   │
└──────────────┬──────────────┘   └────────────┬──────────────────┘
               │                               │
┌──────────────▼───────────────────────────────▼───────────────────┐
│ Versioned Artifacts and Storage                                 │
│ JSON · Markdown · HTML · SVG · PDF · Logs · Render Manifests    │
└──────────────┬───────────────────────────────┬───────────────────┘
               │                               │
┌──────────────▼──────────────┐   ┌────────────▼──────────────────┐
│ Web / REST / CLI            │   │ Background Worker             │
│ Preview · Download · Rerun  │   │ Render · Retry · Recover      │
└─────────────────────────────┘   └───────────────────────────────┘
```

### 5.1 本地部署形态

- 元数据与任务：现有 SQLite 仓储扩展；
- 产物：项目目录下版本化文件；
- 图表：Matplotlib 无界面后端；
- HTML：服务端模板和安全 Markdown AST；
- PDF：XeLaTeX；
- 字体：仓库许可允许的固定字体包或安装检查；
- Worker：可在 Web 进程后台运行，也可独立启动；
- 缓存：内容哈希目录。

### 5.2 服务器部署形态

- 元数据：PostgreSQL；
- 产物与图表资产：S3 兼容对象存储；
- 队列：Redis 支持的持久任务队列；
- Worker：按图表/报告渲染任务水平扩容；
- PDF：固定容器镜像内安装 TeX Live、字体和模板依赖；
- 静态资产：对象存储或 CDN，但私有项目必须使用授权 URL；
- Web/API/Worker 共用版本化主题包和 Report Service。

## 6. 代码模块规划

目标目录结构：

```text
src/research_agent/
├── reports/
│   ├── enums.py
│   ├── models.py
│   ├── service.py
│   ├── repository.py
│   ├── artifacts.py
│   ├── versioning.py
│   ├── markdown_adapter.py
│   ├── quality.py
│   ├── reconciliation.py
│   ├── accessibility.py
│   └── migration.py
├── visualization/
│   ├── models.py
│   ├── planner.py
│   ├── validator.py
│   ├── renderer.py
│   ├── registry.py
│   ├── formatting.py
│   ├── annotations.py
│   ├── accessibility.py
│   ├── cache.py
│   ├── themes/
│   │   ├── brokerage_research_v1.py
│   │   └── tokens.py
│   └── charts/
│       ├── line.py
│       ├── bar.py
│       ├── stacked_bar.py
│       ├── scatter.py
│       ├── waterfall.py
│       └── matrix.py
├── rendering/
│   ├── service.py
│   ├── html.py
│   ├── latex.py
│   ├── pdf.py
│   ├── compiler.py
│   ├── diagnostics.py
│   ├── manifest.py
│   └── templates/
│       └── brokerage_research_v1/
│           ├── report.tex.j2
│           ├── cover.tex.j2
│           ├── section.tex.j2
│           ├── chart.tex.j2
│           ├── table.tex.j2
│           ├── references.tex.j2
│           ├── disclaimer.tex.j2
│           └── report.html.j2
├── jobs/handlers/
│   ├── render_chart.py
│   ├── render_report.py
│   ├── rerender_report.py
│   └── migrate_report.py
├── api/routes/
│   ├── reports.py
│   ├── report_assets.py
│   ├── report_jobs.py
│   └── report_quality.py
└── tools/builtins/
    ├── list_report_datasets.py
    ├── create_chart_plan.py
    └── inspect_report_quality.py
```

现有 `report_layout.py` 在迁移完成后只保留兼容入口，内部调用新的 `RenderingService`，不得继续包含 LLM 调用和完整排版实现。`web_app.py` 只负责装配和路由挂载。

## 7. 标准化领域数据契约

### 7.1 EvidenceRef

```python
class EvidenceRef(BaseModel):
    evidence_id: str
    source_id: str
    source_version: int
    locator: str
    excerpt_hash: str
```

要求：

- 必须引用当前项目中 `SUPPORTED` 的 EvidenceRecord；
- `source_version` 与 locator 必须能回读原文；
- `excerpt_hash` 防止原文或解析版本变化后静默复用；
- 已归档或被替代来源仍可复现历史报告，但新报告默认不得引用失效版本。

### 7.2 DataPoint

```python
class DataPoint(BaseModel):
    point_id: str
    label: str
    value: Decimal | None
    display_value: str | None
    period: str | None
    dimensions: dict[str, str]
    unit: str
    currency: str | None
    value_kind: Literal["actual", "forecast", "estimate", "scenario"]
    evidence_refs: list[EvidenceRef]
    confidence: Literal["high", "medium", "low"]
    missing_reason: str | None
```

规则：

- `value` 为计算和绘图使用的规范数值，不能混入百分号、千分位或货币符号；
- `display_value` 只用于来源原文展示；
- 缺失值使用 `None`，禁止用 0 代替；
- 预测、估算和情景值必须显式区分；
- 一个数据点可以由多条证据交叉支持；
- 单位换算不覆盖原始点，必须通过 DerivedMetric 表达。

### 7.3 DerivedMetric

```python
class DerivedMetric(BaseModel):
    metric_id: str
    name: str
    formula_type: Literal[
        "sum", "difference", "ratio", "share", "growth", "cagr",
        "weighted_average", "index", "custom_registered"
    ]
    input_point_ids: list[str]
    parameters: dict[str, Decimal | str]
    result: DataPoint
    formula_version: str
    calculation_trace: list[str]
```

要求：

- 只允许注册公式，不能执行模型提供的任意表达式；
- 使用 Decimal，并保存舍入前结果、舍入规则和展示精度；
- CAGR、占比、同比和百分点必须使用不同公式类型；
- 输入点单位不兼容时直接拒绝计算；
- 结果必须可由输入点重新计算并通过误差阈值校验。

### 7.4 Dataset

```python
class Dataset(BaseModel):
    dataset_id: str
    project_id: str
    name: str
    description: str
    dimensions: list[str]
    measures: list[str]
    points: list[DataPoint]
    derived_metrics: list[DerivedMetric]
    frequency: Literal["daily", "monthly", "quarterly", "annual", "event", "none"]
    coverage_start: str | None
    coverage_end: str | None
    as_of_date: str
    source_note: str
    dataset_version: int
    content_hash: str
```

### 7.5 ChartSpec

```python
class ChartSpec(BaseModel):
    chart_id: str
    title: str
    takeaway: str
    chart_type: Literal[
        "line", "bar", "stacked_bar", "scatter", "waterfall", "matrix"
    ]
    dataset_id: str
    x_dimension: str
    y_measures: list[str]
    series_dimension: str | None
    unit: str
    scale: Literal["linear", "log"] = "linear"
    sort: Literal["source", "ascending", "descending", "chronological"]
    show_values: bool
    reference_lines: list[ReferenceLine]
    annotations: list[ChartAnnotation]
    source_note: str
    as_of_date: str
    alt_text: str
    theme_id: str
    theme_version: str
```

`ChartSpec` 只描述语义和受控选项，不接受颜色代码、任意字体、像素坐标、Python 代码、JavaScript 或 LaTeX。

### 7.6 TableSpec

```python
class TableSpec(BaseModel):
    table_id: str
    title: str
    takeaway: str | None
    columns: list[TableColumn]
    rows: list[TableRow]
    header_groups: list[HeaderGroup]
    footnotes: list[str]
    source_note: str
    as_of_date: str
    repeat_header: bool = True
    allow_landscape: bool = False
```

每列必须声明：

- 文本、整数、小数、百分比、货币、日期或评级等语义类型；
- 左对齐、右对齐或居中规则；
- 最小/最大宽度和宽度权重；
- 小数精度、空值表示和单位；
- 是否允许换行和是否可以在窄屏隐藏。

### 7.7 ReportDocument

```python
class ReportDocument(BaseModel):
    report_id: str
    project_id: str
    title: str
    subtitle: str | None
    report_type: Literal["industry", "company", "thematic", "strategy"]
    audience: str
    created_at: datetime
    as_of_date: str
    version: int
    template_id: str
    template_version: str
    theme_id: str
    theme_version: str
    executive_summary: list[KeyFinding]
    sections: list[ReportSection]
    datasets: list[str]
    chart_specs: list[str]
    table_specs: list[str]
    references: list[EvidenceRef]
    disclaimer: DisclaimerBlock
```

`ReportSection.blocks` 使用受控联合类型：

- `ParagraphBlock`
- `HeadingBlock`
- `BulletListBlock`
- `KeyFindingBlock`
- `QuoteBlock`
- `ChartBlock`
- `TableBlock`
- `RiskBlock`
- `PageBreakBlock`
- `MethodologyBlock`

禁止在正式结构中保存任意 HTML 或任意 LaTeX。

### 7.8 RenderManifest

```python
class RenderManifest(BaseModel):
    render_id: str
    report_id: str
    report_version: int
    template_version: str
    theme_version: str
    renderer_version: str
    input_hash: str
    assets: list[RenderedAsset]
    outputs: list[RenderedOutput]
    started_at: datetime
    completed_at: datetime | None
    status: Literal["queued", "rendering", "validating", "passed", "failed"]
    diagnostics: list[RenderDiagnostic]
```

## 8. 数据与图表生产流程

### 8.1 定量数据集生成

Analyst 不再只生成 Markdown。分析阶段同时生成：

- `04_analysis.md`：人类可读分析；
- `04_report_data.json`：经过证据绑定的 Dataset 和 DerivedMetric；
- `04_chart_candidates.json`：候选表达意图，只引用 dataset/metric ID；
- `04_data_quality.json`：缺口、冲突、单位和公式诊断。

数据提取流程：

1. 从 `SUPPORTED` EvidenceRecord 中选择精确数据；
2. 保存原始显示值和规范数值；
3. 标准化期间、单位、币种和维度；
4. 用注册公式计算派生指标；
5. 对同名指标进行口径冲突检测；
6. 通过后形成不可变 Dataset 版本；
7. 图表规划只能引用通过验证的数据集版本。

### 8.2 图表规划

图表规划器接收：

- 章节目标；
- 关键结论；
- 可用 Dataset 元数据；
- 允许的图表类型；
- 报告类型和版面预算。

输出仅为 `ChartSpec`。程序必须拒绝：

- 未知 dataset/measure/dimension；
- 不受支持的图表类型；
- 单位不一致的同轴序列；
- 数据点过少或过多；
- 时间轴乱序；
- 负值不适用的图表；
- 占比合计明显异常；
- 未标记的预测数据；
- 缺少来源说明或截止日期。

### 8.3 图表选择规则

| 研究意图 | 首选图表 | 使用条件 | 禁止或降级条件 |
|---|---|---|---|
| 时间趋势 | 折线图 | 连续时期至少 3 个点 | 离散类别不得伪装为时间轴 |
| 类别比较 | 横向/纵向柱状图 | 类别一般不超过 12 个 | 标签过长时使用横向柱状图 |
| 结构变化 | 堆叠柱状图 | 各期口径一致 | 合计不完整时不得展示 100% 堆叠 |
| 两变量关系 | 散点图 | 至少 8 个有效观测 | 不得暗示未经验证的因果关系 |
| 增减贡献 | 瀑布图 | 起点、变化项和终点可核对 | 无法闭合时阻断生成 |
| 风险/机会分布 | 矩阵图 | 轴定义和评级规则明确 | 不得用纯主观文本冒充定量评分 |
| 精确数值比较 | 表格 | 数据密集或需要查数 | 不强制把所有表格转换为图 |

不纳入默认类型：

- 3D 图；
- 雷达图；
- 仪表盘；
- 装饰性面积图；
- 多环饼图；
- 桑基图和网络图，除非后续建立独立、可验证的关系数据契约。

### 8.4 图表数量与信息密度

- 每张图只表达一个主要结论；
- 单页一般不超过 2 张主要图；
- 折线图默认不超过 5 条序列；
- 分组柱状图默认不超过 4 个系列；
- 类别超过 12 个时采用 Top N + 其他、分页表格或小多图；
- 图例不能替代直接标注时，优先在线尾或柱端标注；
- 数据标签发生碰撞时自动降级为关键点标注；
- 如果结论需要读精确数字，图表下方必须有表格或附表入口。

## 9. 确定性图表渲染系统

### 9.1 渲染器职责

`ChartRenderer` 必须完成：

1. 加载并校验 ChartSpec、Dataset 和主题版本；
2. 规范化数值、排序、缺失值和预测区间；
3. 根据图表类型选择已注册渲染器；
4. 计算安全尺寸、边距和标签策略；
5. 绘制标题之外的图形主体；
6. 生成 SVG、PDF 和缩略 PNG；
7. 生成可访问 alt text 和数据表；
8. 保存输入哈希、资产哈希和诊断；
9. 执行空图、边界、文本碰撞和文件有效性检查。

### 9.2 输出资产

每张图生成：

```text
05_assets/charts/{chart_id}/
├── chart.svg
├── chart.pdf
├── thumbnail.png
├── data.csv
├── spec.json
├── provenance.json
└── diagnostics.json
```

正式报告图注、来源和备注由模板统一排版，不烘焙进图像主体；独立下载版本可以额外生成带完整标题和来源的 standalone SVG/PNG。

### 9.3 渲染缓存

缓存键至少包含：

```text
SHA256(
  ChartSpec canonical JSON
  + Dataset content_hash
  + theme_id/theme_version
  + renderer_version
  + font_manifest_hash
)
```

命中缓存时仍要验证资产存在和哈希一致。主题、字体、渲染器或数据版本变化必须自动失效。

### 9.4 字体策略

- 生产环境固定安装并嵌入中文字体，不依赖宿主机随机字体；
- 默认无衬线用于图表与表格，正文可配置衬线或无衬线；
- 主题记录字体家族、版本和文件哈希；
- 启动健康检查验证所有字体；
- 字体缺失时正式渲染失败，不静默替换后继续交付；
- SVG 可选择嵌入字体轮廓或使用经过部署保证的字体栈；
- PDF 必须通过字体嵌入检查。

## 10. 券商研报设计系统

### 10.1 主题令牌

`brokerage_research_v1` 至少定义：

- 页面尺寸、页边距、栏宽和基线网格；
- 标题、正文、图表、表格、脚注和来源字体；
- 主色、强调色、灰阶、风险色和预测色；
- 线宽、点大小、网格线、圆角和间距；
- 图表宽高预设；
- 表格单元格内边距、表头和分隔线；
- 封面、摘要、章节标题和页眉页脚布局；
- 正值、负值、风险、预测和估算等语义颜色。

主题建议采用克制的深蓝/蓝灰体系：

- 一个品牌主色；
- 一个强调色；
- 4–6 级中性灰；
- 中国金融语境的上涨/正向与下跌/负向色，但必须同时使用符号或文字，不能只依赖颜色；
- 预测值使用虚线、浅色或纹理，确保黑白打印也能区分。

禁止：

- 渐变背景；
- 3D 效果；
- 大面积阴影；
- 高饱和多色轮盘；
- 无语义装饰图形；
- 仅靠红绿区分含义。

### 10.2 页面系统

正式报告固定结构：

1. 封面；
2. 报告信息与数据截止日期；
3. 核心观点/投资要点；
4. 目录；
5. 正文章节；
6. 方法与数据口径；
7. 风险提示；
8. 数据来源与可追溯证据索引；
9. 免责声明。

正文默认 A4 单栏。只有以下内容允许受控双栏：

- 核心观点卡片；
- 机会与风险对照；
- 两张尺寸一致、语义相关的图表；
- 简短的指标摘要。

长正文、复杂表格和来源附录不得使用自由双栏。

### 10.3 图表组成

每张正式图表必须包含：

- 图表编号；
- 结论式标题；
- 可选副标题；
- 坐标轴名称和单位；
- 清晰图例或直接标注；
- 实际值/预测值区分；
- 数据来源；
- 数据截止日期；
- 必要的口径、计算和四舍五入备注；
- 可访问替代文本。

标题应表达结论，例如：

- 推荐：`中国工业软件市场预计 2024—2027E 保持双位数增长`
- 不推荐：`市场规模变化`

### 10.4 表格系统

- 默认三线表，不绘制完整网格；
- 文本左对齐，数字右对齐，短标签可居中；
- 统一小数位、百分号、千分位、币种和负号；
- 使用真正的空值符号，不用 0 代替缺失；
- 长表格使用 `longtable` 并重复表头；
- 宽表优先缩短表头、调整列权重、横向页面或拆表，禁止整体缩放到不可读；
- 单元格文字允许换行，禁止任意截断；
- 来源和表注随表格一起跨页管理；
- 文字密集的 SWOT、风险清单和对照矩阵必须使用表格，不用固定坐标 TikZ。

## 11. 统一报告渲染链路

### 11.1 Markdown 输入适配

保留 Markdown 作为审阅格式，但使用正式 AST 解析器：

- 支持标题、段落、有序/无序列表；
- 支持 GFM 表格；
- 支持脚注、引用、代码块和水平线；
- 支持受控的图表与表格引用指令；
- 禁止任意 HTML；
- 所有链接进行协议白名单和转义；
- Markdown 解析为 ReportBlock，不直接拼接 HTML 或 LaTeX。

建议指令：

```markdown
{{chart:market_size_01}}
{{table:company_comparison_01}}
```

指令只能引用已存在并通过质量门禁的资产。

### 11.2 HTML/Web 渲染

- 服务端将 ReportDocument 渲染为受控 HTML；
- 表格、图表、来源和脚注使用语义化标签；
- 图表显示规范 SVG，并提供“查看数据”与“下载图表”；
- 移动端宽表使用滚动和列优先级，不改动数据；
- 图表 alt text、表格 caption 和标题层级满足可访问要求；
- 页面不依赖第三方 CDN；
- 前端不再维护 Markdown 语法解析正则；
- 浏览器预览展示当前模板、主题、数据和渲染版本。

### 11.3 LaTeX/PDF 渲染

使用版本化 Jinja2 模板生成 LaTeX：

- 模板负责所有环境、转义和宏；
- 内容块只调用受控宏；
- 图表使用已生成的矢量 PDF；
- 表格由 TableSpec 决定 `tabularx`、`longtable` 或横向页面；
- 目录、页眉页脚、书签、链接、图表编号和表格编号自动生成；
- 编译运行两次或使用受控构建工具解决目录与引用；
- 使用 `-halt-on-error`，并解析日志中的错误与 Overfull；
- 禁止 shell escape、外部网络资源和任意用户宏；
- 编译输出进入临时目录，通过质量门禁后原子替换正式产物。

### 11.4 统一下载行为

完成迁移后只保留一个正式 PDF：

- `05_final_report.pdf`：唯一正式交付 PDF；
- `05_final_report.md`：审阅和归档；
- `05_final_report.html`：Web 快照；
- `05_final_report.tex`：可选调试产物，不作为普通用户主入口；
- 原“普通 PDF”和“高级 PDF”接口在兼容期映射到同一正式 PDF，并返回弃用提示；
- 兼容期结束后删除重复按钮和重复渲染代码。

## 12. Agent 职责调整

### 12.1 Strategist

在提纲中新增：

- 报告类型；
- 目标读者；
- 需要回答的核心定量问题；
- 预期数据频率和时间范围；
- 需要精确查数的表格；
- 图表预算和正式交付格式。

Strategist 不决定具体色彩或图形细节。

### 12.2 Collector

采集阶段补充：

- 数据口径；
- 时间频率；
- 单位和币种；
- 实际/预测属性；
- 原始表格 locator；
- 可用于计算的精确数值，而不只保存叙述摘要。

### 12.3 Validator

新增验证：

- 同一指标跨来源口径冲突；
- 单位与币种冲突；
- 财年/自然年和累计/单季混用；
- 实际值与预测值混用；
- 表格标题、行列和数据点 locator 完整性；
- 派生指标输入证据有效性。

### 12.4 Analyst

Analyst 同时输出分析和结构化数据，不能只在 Markdown 表格中保存数字。所有定量结论必须引用 Dataset/Metric ID。

Analyst 可以提出图表候选，但不能生成最终图片或任意绘图代码。

### 12.5 Formatter

Formatter 的职责收敛为：

- 组织 ReportDocument；
- 从已通过门禁的图表和表格中选择内容；
- 生成执行摘要、章节要点和过渡文字；
- 指定 ChartBlock/TableBlock 的位置；
- 保持事实、指标、来源和免责声明不变。

Formatter 不再：

- 生成 LaTeX；
- 将引用改成无法回溯的临时脚注；
- 自行从正文提取图表数据；
- 输出 ASCII 图；
- 标注“建议插入图表”后结束。

## 13. 报告状态机与产物

### 13.1 ProjectState 扩展

新增可选字段：

```python
report_data_path: str | None
chart_candidates_path: str | None
report_document_path: str | None
render_manifest_path: str | None
report_quality_path: str | None
final_report_html_path: str | None
final_report_pdf_path: str | None
report_version: int
active_render_id: str | None
```

保留旧字段读取能力，并提供一次性迁移映射。

### 13.2 渲染任务状态

```text
created
  → validating_data
  → planning_assets
  → rendering_charts
  → rendering_document
  → compiling_pdf
  → validating_outputs
  → passed
  → published

任一阶段 → retrying → failed
```

状态必须持久化，进程重启后可从最近成功阶段继续。

### 13.3 项目产物结构

```text
projects/{project}/
├── 04_analysis.md
├── 04_report_data.json
├── 04_chart_candidates.json
├── 04_data_quality.json
├── 05_report_document.json
├── 05_final_report.md
├── 05_final_report.html
├── 05_final_report.tex
├── 05_final_report.pdf
├── 05_render_manifest.json
├── 05_report_quality.json
├── 05_assets/
│   ├── charts/
│   └── tables/
└── render_history/
    └── {render_id}/
```

正式文件采用原子替换。失败渲染保留在 `render_history`，不得覆盖上一版已通过报告。

## 14. API、CLI 与 Web 功能面

### 14.1 REST API

```text
GET    /api/projects/{id}/report
GET    /api/projects/{id}/report/document
GET    /api/projects/{id}/report/datasets
GET    /api/projects/{id}/report/charts
GET    /api/projects/{id}/report/charts/{chart_id}
GET    /api/projects/{id}/report/quality
GET    /api/projects/{id}/report/renders
POST   /api/projects/{id}/report/render
POST   /api/projects/{id}/report/rerender
POST   /api/projects/{id}/report/validate
GET    /api/projects/{id}/report/download/{format}
GET    /api/projects/{id}/report/assets/{asset_id}
```

要求：

- 写操作使用幂等键；
- 同项目同报告版本只允许一个活动渲染任务；
- 下载接口验证项目权限；
- 资产接口使用白名单 ID，不接收任意路径；
- API 返回模板、主题、报告和渲染器版本；
- 正式下载只指向最近 `passed/published` 版本。

### 14.2 CLI

```bash
research-agent report validate PROJECT
research-agent report render PROJECT
research-agent report rerender PROJECT --template brokerage_research_v1
research-agent report inspect-chart PROJECT CHART_ID
research-agent report quality PROJECT
research-agent report migrate PROJECT
research-agent report export PROJECT --format pdf
```

CLI、Web 和 Agent 必须调用同一个 Report Service，不复制业务逻辑。

### 14.3 Web 成果中心

完整页面应支持：

- 报告 HTML 预览；
- 章节目录导航；
- 图表放大、数据表切换和 SVG/CSV 下载；
- 表格横向滚动和来源查看；
- 图表数据点证据回查；
- 当前质量状态和阻断问题；
- 报告、模板、主题和数据版本；
- 重渲染和失败诊断；
- Markdown、HTML 和正式 PDF 下载；
- 历史渲染版本比较。

## 15. 完整质量门禁

### 15.1 数据门禁

阻断条件：

- 图表引用不存在的数据集或指标；
- 任一有效数据点没有 EvidenceRef；
- EvidenceRef 不能回读或版本不匹配；
- 单位、币种或时间口径不一致；
- 派生指标无法按注册公式复算；
- 预测值没有 `value_kind`；
- 缺失值被 0 替代；
- 图表标题结论与数据方向明显矛盾；
- 瀑布图无法闭合；
- 100% 堆叠数据无法在容差内合计为 100%。

### 15.2 内容一致性门禁

建立指标索引，比较：

- 正文中的关键数字；
- KeyFinding 中的关键数字；
- TableSpec 中的数字；
- ChartSpec/Dataset 中的数字；
- 执行摘要中的数字。

同一 metric ID 必须使用同一个规范值。展示精度可以不同，但必须满足配置的舍入容差。

### 15.3 图表门禁

- 标题、takeaway、单位、来源、截止日期和 alt text 完整；
- SVG 可解析且 viewBox 有效；
- PDF 图表页边界有效；
- 有效数据点至少达到图表类型最低要求；
- 标签不得超出画布；
- 图例不得覆盖数据区域；
- 颜色对比度达到主题要求；
- 黑白渲染后实际/预测和不同系列仍可区分；
- 图表数据 CSV 与渲染输入哈希一致；
- 空图和全缺失图直接阻断。

### 15.4 表格门禁

- 列数、列类型和每行单元格数一致；
- 数值格式符合列定义；
- 关键列没有无解释缺失；
- 跨页表头重复；
- 来源和表注存在；
- 不允许内容超出页面裁切区；
- 字号不能低于主题规定的最小值；
- 宽表不能通过无限缩小解决。

### 15.5 PDF 门禁

- LaTeX 返回码为 0；
- 日志无 undefined reference、missing glyph 和阻断级 overfull；
- PDF 可由 PyMuPDF 打开；
- 页数大于 0；
- 字体嵌入检查通过；
- 目录链接和外部 URL 合法；
- 图表和表格数量与 RenderManifest 一致；
- 首页、风险提示、来源和免责声明存在；
- 文本抽取结果包含所有必需章节；
- 不存在意外空白页和大面积裁切。

### 15.6 正式发布条件

只有以下全部通过才能把状态设为 `published`：

```text
Evidence Gate
AND Dataset Gate
AND Formula Gate
AND Content Reconciliation Gate
AND Chart Gate
AND Table Gate
AND HTML Gate
AND PDF Gate
AND Visual Regression Gate
```

“文件已生成”“模型已完成”或“PDF 可以打开”都不能单独代表正式完成。

## 16. 测试与评测体系

### 16.1 单元测试

- Pydantic 数据契约与非法输入；
- 单位、币种和期间标准化；
- Decimal 公式与舍入；
- 图表类型选择规则；
- 标签、数轴和排序策略；
- Markdown AST 到 ReportBlock；
- HTML/LaTeX 转义；
- 表格列宽分配；
- 缓存键和版本失效；
- 质量门禁原因码。

### 16.2 集成测试

- EvidenceRecord → Dataset → ChartSpec → SVG/PDF；
- ReportDocument → HTML/LaTeX/PDF；
- 长中文标题、长 URL 和复杂表格；
- 预测值和实际值混合；
- 任务失败、重试和恢复；
- 主题升级后的缓存失效；
- 历史 Markdown 报告迁移；
- Web/API/CLI 调用同一服务结果一致。

### 16.3 黄金样本

建立 `tests/report_golden/`，至少覆盖：

1. 行业研究报告；
2. 公司对比报告；
3. 政策专题报告；
4. 财务指标密集报告；
5. 长表格和复杂中文报告；
6. 含预测/情景数据的报告；
7. 只有少量可用数据的降级报告；
8. 历史 Markdown 迁移报告。

每个样本保存：

- 输入 Dataset/ReportDocument；
- 预期 ChartSpec/TableSpec；
- SVG 结构快照；
- PDF 页图基线；
- 文本抽取基线；
- RenderManifest；
- 质量报告。

### 16.4 视觉回归

- 将 PDF 按固定 DPI 渲染为图片；
- 对页面尺寸、主要区域和像素差异设置阈值；
- 对图表单独做结构和图像快照；
- 字体或模板升级必须显式更新基线并经过人工审阅；
- CI 保存失败差异图，不只输出布尔结果；
- 数据变化测试与模板变化测试分开，避免合法数据更新造成误报。

### 16.5 真实报告评测

从用户认可的 3–5 份券商研报中提炼版式基准，不复制商标、版权内容或专有模板。评测维度：

- 信息层级；
- 图表标题和结论表达；
- 数据来源完整性；
- 字体、间距和对齐；
- 图表密度与可读性；
- 表格跨页与查数效率；
- 黑白打印效果；
- 长报告连续阅读体验。

人工评审表必须版本化并进入发布记录。

## 17. 安全与权限

- 图表和报告资产继承项目 ACL；
- 下载接口不得通过文件路径直接访问；
- 所有 Markdown、标题、来源和 URL 必须转义；
- 禁止任意 HTML、JavaScript、LaTeX 宏和 shell escape；
- 图表渲染器只接收 Pydantic 校验后的受控字段；
- LaTeX 编译在受限用户和临时目录执行；
- 限制报告长度、图表数量、单图数据点数、编译时间和内存；
- 外部图片默认禁止，允许时必须先下载、扫描、固化和记录哈希；
- 临时目录和失败产物按保留策略清理；
- RenderManifest 和发布操作写入审计日志；
- 私有报告不得通过公共 CDN 暴露静态资产。

## 18. 性能、并发与恢复

### 18.1 性能目标

初始生产目标：

| 操作 | 目标 |
|---|---:|
| 单张常规图表 SVG/PDF 渲染 | P95 ≤ 2 秒 |
| 20 张图表并行渲染 | P95 ≤ 20 秒 |
| 50 页报告 LaTeX 编译 | P95 ≤ 60 秒 |
| 已命中缓存的报告重排 | P95 ≤ 15 秒 |
| Web 报告首屏 HTML | P95 ≤ 1 秒（不含网络） |

真实阈值必须由黄金样本和部署环境校准。

### 18.2 并发规则

- 图表可以并行渲染；
- 同一 report/version/template 只允许一个正式 PDF 编译任务；
- 重复请求通过幂等键合并；
- 发布使用乐观锁，旧任务不能覆盖新版本；
- Worker 崩溃后按 RenderManifest 恢复未完成阶段；
- 已成功资产按内容哈希复用；
- 失败重试区分瞬时错误和确定性数据/模板错误。

### 18.3 原子发布

```text
临时渲染目录
  → 生成全部资产
  → 执行全部门禁
  → 写 RenderManifest
  → 原子更新 active_render_id
  → 正式下载指向新版本
```

任何失败都保留上一版已发布报告。

## 19. 可观测性与运维

### 19.1 结构化日志字段

- project_id；
- report_id/report_version；
- render_id；
- chart_id/table_id；
- template/theme/renderer version；
- job_id/attempt；
- stage；
- duration_ms；
- cache_hit；
- input/output hash；
- diagnostic_code；
- exception_type。

不得记录 API Key、私有原文全文或完整受限报告内容。

### 19.2 指标

- 报告生成成功率；
- 各质量门禁失败率；
- 图表和 PDF 渲染耗时；
- 缓存命中率；
- LaTeX 编译错误类型；
- overfull/missing glyph 数量；
- 图表数据追溯失败数；
- 队列深度和任务重试数；
- 历史迁移成功率；
- 模板版本使用分布。

### 19.3 健康检查

启动检查：

- Matplotlib 可导入；
- 无界面后端可用；
- 固定字体存在且哈希匹配；
- XeLaTeX 和所需包可用；
- 模板可编译最小文档；
- 临时目录和资产目录可写；
- 数据库和对象存储可访问。

### 19.4 运维工具

- 重放失败渲染；
- 校验并修复孤立资产；
- 重建缩略图；
- 按模板版本批量重排；
- 比较两个 RenderManifest；
- 导出质量报告；
- 清理超过保留期的失败产物；
- 验证备份中的报告、数据和资产完整性。

## 20. 配置与依赖

建议新增依赖：

- `matplotlib`：规范图表渲染；
- `jinja2`：受控 HTML/LaTeX 模板；
- `markdown-it-py`：Markdown AST；
- `mdit-py-plugins`：脚注等受控扩展；
- `bleach`：必要的 HTML 安全清理；
- `pandas` 不是领域契约的必需依赖，可仅在受控数据适配层使用；
- 继续使用 PyMuPDF 做 PDF 检查和页面渲染。

新增配置：

```text
REPORT_TEMPLATE_ID=brokerage_research_v1
REPORT_THEME_ID=brokerage_research_v1
REPORT_ASSET_DIR=
REPORT_RENDER_WORKERS=
REPORT_RENDER_TIMEOUT=
REPORT_MAX_CHARTS=
REPORT_MAX_POINTS_PER_CHART=
REPORT_MAX_PAGES=
REPORT_CACHE_DIR=
REPORT_KEEP_RENDER_HISTORY=
REPORT_VISUAL_REGRESSION_THRESHOLD=
REPORT_LATEX_ENGINE=xelatex
REPORT_FONT_MANIFEST=
```

生产镜像必须锁定 Python 包、TeX Live 包、模板和字体版本。只锁 Python 依赖不足以保证 PDF 可复现。

## 21. 历史迁移与兼容策略

### 21.1 迁移原则

- 不删除旧 Markdown、TeX 或 PDF；
- 新字段全部先做可选；
- 每次迁移生成备份和迁移报告；
- 迁移失败不改变原项目状态；
- 不从缺少证据的旧图表反向虚构 DataPoint；
- 无法可靠结构化的旧内容作为 LegacyBlock 保留并标记待审阅；
- 正式重新发布必须通过新质量门禁。

### 21.2 迁移步骤

1. 扫描历史项目和现有产物；
2. 备份 `state.json` 和报告文件；
3. 将 Markdown 解析为初始 ReportDocument；
4. 将标准 Markdown 表格转换为 TableSpec；
5. 识别来源索引并绑定现有 EvidenceRecord；
6. 只对可以可靠复原的数据生成 Dataset；
7. 无法验证的图表占位标记为 `needs_review`；
8. 用新模板生成 HTML/PDF 候选版本；
9. 执行内容差异和质量检查；
10. 用户确认后更新 `active_render_id`，保留旧版本下载入口。

### 21.3 API 兼容期

- 旧 `final-report.pdf` 和 `final-report-typeset.pdf` 暂时都返回正式新 PDF；
- 响应头和 API 文档标注弃用；
- 前端只显示一个“正式 PDF”按钮；
- 兼容至少一个正式版本周期；
- 确认无调用方后删除 ReportLab 旧渲染器和重复接口。

## 22. 实施里程碑

里程碑用于控制依赖和验收，不代表缩减最终范围。

### M1：领域契约与版本基础

交付：

- ReportDocument、Dataset、DerivedMetric、ChartSpec、TableSpec；
- RenderManifest 和 QualityReport；
- Pydantic 校验、Canonical JSON、内容哈希；
- ProjectState 兼容字段；
- 仓储接口和本地产物布局。

验收：

- 合法/非法契约单元测试通过；
- 相同输入生成稳定哈希；
- 旧状态文件可无损读取。

### M2：证据数据化与计算审计

交付：

- EvidenceRecord → DataPoint 转换；
- 单位、期间、币种和 value_kind 标准化；
- 注册公式与 CalculationTrace；
- Dataset 质量门禁；
- Analyst 结构化数据输出。

验收：

- 所有图表候选数据点 100% 可追溯；
- 派生指标可重复计算；
- 口径冲突能阻断生成。

### M3：完整图表渲染器

交付：

- 6 类正式图表；
- `brokerage_research_v1` 主题；
- SVG/PDF/PNG/CSV/alt text；
- 字体、标签、图例、预测样式和黑白规则；
- 内容哈希缓存和资产诊断。

验收：

- 所有图表黄金样本通过；
- 相同输入重复渲染稳定；
- 空图、碰撞和非法配置可被阻断。

### M4：结构化报告与 Markdown 适配

交付：

- Markdown AST；
- ReportBlock 转换；
- Chart/Table 引用指令；
- Formatter 改造；
- 内容一致性索引。

验收：

- 表格、脚注、引用和复杂列表正确转换；
- 不允许任意 HTML/LaTeX；
- 正文、表格和图表指标一致。

### M5：券商研报 LaTeX 模板

交付：

- 封面、摘要、目录、正文、图表、表格、来源、风险和免责声明模板；
- 中文字体和转义；
- longtable/tabularx/横向页算法；
- 编译器和日志诊断；
- 正式 PDF 原子发布。

验收：

- 黄金报告 100% 编译；
- 无阻断级溢出、缺字、丢图和空白页；
- 长表格和宽表格样本通过。

### M6：统一 Web 成果中心

交付：

- 服务端 HTML 渲染；
- 语义表格、SVG 图表、证据回查和数据下载；
- 报告目录、版本、质量状态和历史渲染；
- 单一正式 PDF 下载；
- 旧接口兼容映射。

验收：

- Web/PDF 内容资产一致；
- 移动端宽表和图表可用；
- 权限和路径安全测试通过。

### M7：任务、缓存与可观测性

交付：

- 持久渲染任务；
- 幂等、重试、恢复和并发锁；
- 指标、日志、健康检查和运维 CLI；
- 本地/服务器适配器。

验收：

- Worker 崩溃可恢复；
- 重复请求不产生重复发布；
- 性能目标在基准环境通过。

### M8：完整质量门禁与视觉回归

交付：

- 数据、公式、内容、图表、表格、HTML、PDF 门禁；
- 黄金样本和页面视觉回归；
- 差异图片和诊断报告；
- CI 发布阻断。

验收：

- 任一已知格式缺陷都能被测试复现并阻断；
- 黄金样本全部通过；
- 失败报告不能进入 published。

### M9：历史迁移与生产发布

交付：

- 历史项目扫描、备份、迁移和回滚；
- API 兼容期；
- 容器、字体、TeX 和依赖锁定；
- 真实券商研报式人工评审；
- 运维手册和上线清单。

验收：

- 历史正文与引用无损；
- 正式评测集达到全部质量指标；
- 本地和服务器部署均完成端到端验收。

## 23. 现有文件改造清单

### 必须修改

- `src/research_agent/state.py`：增加报告数据、结构化文档、渲染和质量状态；
- `src/research_agent/config.py`：增加主题、模板、资产、缓存和渲染配置；
- `src/research_agent/agents/prompts/analyst.md`：要求 Dataset、Metric 和图表候选；
- `src/research_agent/agents/prompts/formatter.md`：删除 ASCII 图、临时图表和 LaTeX 职责；
- `src/research_agent/agents/analyst.py`：保存结构化数据并执行数据门禁；
- `src/research_agent/agents/formatter.py`：生成 ReportDocument 并调用 RenderingService；
- `src/research_agent/orchestrator.py`：增加数据化、渲染和发布质量阶段；
- `src/research_agent/report_layout.py`：改为兼容入口，移除 LLM 生成 LaTeX；
- `src/research_agent/web_app.py`：移除逐行 ReportLab PDF，挂载 Report API；
- `src/research_agent/web_static/ui.js`：不再自行解析 Markdown；
- `src/research_agent/web_static/results.js`：展示统一 HTML、资产、质量和正式 PDF；
- `src/research_agent/web_static/styles.css`：接入主题化报告预览样式；
- `pyproject.toml`：增加正式渲染依赖和可选部署依赖；
- `README.md`：更新报告产物、依赖、启动、配置和迁移说明。

### 必须新增

- `src/research_agent/reports/`；
- `src/research_agent/visualization/`；
- `src/research_agent/rendering/`；
- 报告 API、CLI、Worker handlers；
- 版本化 LaTeX/HTML 模板；
- 报告黄金样本、视觉回归和迁移测试；
- `docs/report-rendering-operations.md`；
- `docs/report-migration.md`；
- 字体清单和许可证说明。

### 迁移完成后删除

- LLM 生成完整 LaTeX 的代码路径；
- ReportLab 逐行 Markdown PDF 渲染器；
- 前端正则 Markdown 解析器；
- 重复的普通/高级 PDF 按钮和接口实现；
- Prompt 中的 ASCII 图与“建议插入图表”规则。

## 24. 风险与控制措施

| 风险 | 控制措施 |
|---|---|
| 中文字体在不同机器表现不同 | 固定字体包、哈希、嵌入检查和容器化 |
| Matplotlib 自动布局仍可能碰撞 | 图表类型限制、尺寸预设、bbox 检查、降级规则和视觉回归 |
| 宽表格不可读 | TableSpec 列语义、宽度算法、拆表、横向页和最小字号门禁 |
| LLM 选择错误图表 | 受控类型、确定性规则校验和无损降级为表格 |
| 模型生成错误数字 | 只能引用 Dataset/Metric ID，逐点证据和内容核对 |
| LaTeX 编译资源高 | 后台任务、缓存、超时、并发限制和固定镜像 |
| 模板升级破坏历史报告 | 版本锁定、RenderManifest、黄金样本和历史重排需显式触发 |
| SVG/Markdown 注入 | 受控渲染、转义、协议白名单和禁止任意 HTML |
| 多输出内容漂移 | 单一 ReportDocument、统一资产和一致性门禁 |
| 迁移旧报告无法恢复图表数据 | 保留旧产物，不反向编造，标记 needs_review |

## 25. 完整版最终验收清单

### 数据与证据

- [ ] 每个图表数据点都有有效 EvidenceRef；
- [ ] 每个派生指标都有注册公式、输入和计算轨迹；
- [ ] 单位、币种、频率、口径和截止日期完整；
- [ ] 实际、预测、估算和情景值明确区分；
- [ ] 正文、摘要、表格和图表相同指标完全一致；
- [ ] 缺失值、冲突和低置信度不会被静默隐藏。

### 图表

- [ ] 6 类图表均有生产实现和黄金样本；
- [ ] SVG/PDF/PNG/CSV/alt text 全部生成；
- [ ] 标题、takeaway、单位、来源、日期和备注完整；
- [ ] 标签、图例和注释无重叠或裁切；
- [ ] 实际值和预测值在彩色与黑白模式下均可区分；
- [ ] 相同输入和版本可重复生成稳定结果。

### 表格与版式

- [ ] Markdown 表格在 Web/PDF 都是真正表格；
- [ ] 长表格正确跨页并重复表头；
- [ ] 宽表格不会通过不可读缩放解决；
- [ ] 中文换行、长标题、长 URL 和脚注正确；
- [ ] 封面、摘要、目录、页眉页脚、风险、来源和免责声明完整；
- [ ] PDF 无缺字、丢图、空白页和阻断级溢出。

### 系统一致性

- [ ] Web、Markdown、HTML 和 PDF 来自同一 ReportDocument；
- [ ] Web/PDF 图表来自同一 ChartSpec/Dataset；
- [ ] 只有一个正式 PDF 输出；
- [ ] 模板、主题、渲染器、字体和数据版本可追溯；
- [ ] 失败渲染不会覆盖上一版已发布报告；
- [ ] CLI、API、Web 和 Agent 使用同一 Report Service。

### 质量、运维与迁移

- [ ] 数据、公式、内容、图表、表格、HTML、PDF 和视觉门禁全部启用；
- [ ] CI 对已知格式问题能够稳定阻断；
- [ ] 任务支持幂等、重试、恢复和并发控制；
- [ ] 日志、指标、健康检查和运维 CLI 完整；
- [ ] 本地和服务器部署均通过端到端验证；
- [ ] 历史项目可备份、迁移、重排和回滚；
- [ ] 真实研报式人工评审通过。

## 26. 完成定义

本项目只有在以下条件全部满足时，才能声明“券商研报级图表与排版问题已解决”：

1. 图表不是由模型临时画出，而是由受控、可验证、可复现的结构化管线生成；
2. 图表中的每个数字和派生计算都可以回到原始证据；
3. Web 和正式 PDF 使用同一个报告模型、同一份数据和同一批图表资产；
4. 报告模板、字体、色板、图表和表格规则形成稳定设计系统；
5. 长表格、跨页、中文、脚注、来源和复杂版式通过自动化测试；
6. 所有质量门禁通过后才能发布，文件存在不再等同于完成；
7. 历史报告、失败任务、模板升级和服务器部署都有可恢复路径；
8. 黄金样本和用户认可的真实研报式评审均达到正式交付标准。

本方案的核心不是“生成更多图”，而是把研究证据、定量数据、视觉表达和正式交付连成一条可审计、可复现、可持续演进的生产链路。
