# Research Agent 券商研报排版轻量改造方案

## 1. 方案定位

本方案只解决当前最直接的问题：最终报告图表不够专业、表格和 PDF 格式不稳定、不同报告的视觉风格不统一。

不重建整个报告生产架构，不改造材料中心、检索、证据库和 Agent2↔3 采集验证流程。改造集中在 Agent5 及其后的排版交付阶段，目标开发量为 **6–10 人日**。

核心方法：

```text
现有分析产物
  → 项目专用券商排版 Skill
  → 最终 Markdown + 图表清单
  → 常规图表确定性渲染
  → 特殊图表 LLM 声明式兜底
  → 固定中文 LaTeX 模板
  → Web 预览 + 正式 PDF
  → 渲染检查
```

本方案追求的是在有限改造下达到稳定、专业、接近券商研报的交付效果，不建设金融机构级完整报告发布平台。

## 2. 当前问题

### 2.1 已确认问题

- Agent5 的排版规则主要存在于普通 Prompt 中，缺少可复用 Skill、模板和检查清单；
- 时间序列只能用 ASCII 图或“建议插入图表”，没有真正生成图片；
- `report_layout.py` 让 LLM 每次重新编写整份 LaTeX，容易出现环境不匹配、表格溢出和图形重叠；
- Web Markdown 预览不支持真正的表格、图片和图表占位符；
- 普通 PDF 把 Markdown 表格当成代码文本；
- 网页、普通 PDF 和高级 PDF 使用不同排版逻辑；
- 没有固定中文券商研报色板、字体、图注和来源格式；
- PDF 生成后没有逐页渲染检查。

### 2.2 这次必须解决

1. Agent5 真正加载项目内券商排版 Skill；
2. 报告中出现真实图表，不再输出 ASCII 图或插图建议；
3. 常见图表稳定生成，特殊图表有 LLM 兜底；
4. Markdown 表格在 Web 和 PDF 中正确排版；
5. PDF 使用固定模板，不再由 LLM 重写完整 LaTeX；
6. 图表和表格具备标题、单位、来源和截止日期；
7. 生成后的 PDF 经过自动检查和抽样视觉检查；
8. 现有项目和命令保持兼容。

## 3. 明确不做的内容

以下内容从本次开发范围中删除：

- 不新增完整 `ReportDocument` 领域模型；
- 不建立图表逐点证据数据库；
- 不重构 EvidenceRecord、Dataset 和来源仓储；
- 不建设独立图表/报告 Worker；
- 不引入 PostgreSQL、Redis、S3 等报告专用生产适配；
- 不建设报告版本发布平台；
- 不开发完整历史报告迁移系统；
- 不建设大规模视觉回归平台；
- 不重构整个状态机；
- 不提供任意品牌模板编辑器；
- 不承诺完全复制某家券商的商标或专有版式。

现有文字证据审计继续生效。图表只要求保存清晰来源说明，不在本阶段增加逐点证据关联。

## 4. Skill 选型与使用方式

### 4.1 外部 Skill 调研结论

本方案参考但不直接照搬以下 Skill：

| Skill | 复用内容 | 不直接采用的原因 |
|---|---|---|
| Anthropic `initiating-coverage` | 卖方研报结构、首页信息密度、图表分布、质量清单 | 默认美股、DOCX、完整五阶段工作流 |
| K-Dense `market-research-reports` | LaTeX 模板、样式包、图表/表格排版 | 更偏咨询报告，不符合全部中国券商习惯 |
| `china-initiating-coverage` | A 股评级、中文章节、风险和披露习惯 | 缺少可直接使用的模板和渲染资产 |
| OpenAI `pdf` | PDF 渲染、页面截图和视觉检查 | 只负责 PDF QA，不负责券商结构 |

项目最终创建自己的 Skill，避免外部 Skill 的流程约束污染现有 Research Agent。

### 4.2 项目 Skill 目录

```text
skills/brokerage-report-formatting/
├── SKILL.md
├── agents/
│   └── openai.yaml
├── references/
│   ├── report-structure.md
│   ├── chart-rules.md
│   ├── table-rules.md
│   ├── china-style.md
│   └── quality-checklist.md
└── assets/
    ├── brokerage-report.tex
    ├── brokerage-report.sty
    └── theme.json
```

不添加 README、安装指南或重复说明。`SKILL.md` 只保留核心流程，详细规则放在 `references/`，模板放在 `assets/`。

### 4.3 Skill 触发和职责

Skill 只在以下场景加载：

- Agent5 生成最终调研报告；
- 用户要求生成券商研报风格 PDF；
- 用户要求重新排版已有最终报告；
- 用户要求修复最终报告的图表、表格或 PDF 格式。

Skill 负责：

- 确定报告章节层级和首页结构；
- 控制每章图表和表格密度；
- 选择常规图表类型；
- 编写图表清单；
- 输出统一的图表标题、单位、来源和备注；
- 生成适合固定模板的 Markdown；
- 执行交付前检查清单。

Skill 不负责：

- 重新搜索资料；
- 修改 Agent4 的核心结论；
- 编造缺失数据；
- 生成任意 Python、JavaScript 或 LaTeX 代码；
- 直接决定最终 PDF 的字体、颜色和物理坐标。

### 4.4 Agent5 如何加载 Skill

不建设通用 Skill 平台，只增加一个小型项目内加载器：

```python
skill = load_project_skill("brokerage-report-formatting")
system_prompt = formatter_base_prompt + skill.instructions + skill.required_references
```

加载器只允许读取仓库 `skills/` 下的白名单 Skill，禁止用户通过路径加载任意文件。

Formatter 默认加载：

- `SKILL.md`；
- `report-structure.md`；
- `chart-rules.md`；
- `table-rules.md`；
- `china-style.md`；
- `quality-checklist.md`。

模板资产不进入 LLM 上下文，由排版程序直接读取。

## 5. Agent5 输出契约

### 5.1 保留现有 Markdown

Agent5 继续生成：

```text
05_final_report.md
```

这样不影响现有 CLI、Web 和项目产物。

### 5.2 新增轻量图表清单

Agent5 同时生成：

```text
05_chart_manifest.json
```

示例：

```json
{
  "version": 1,
  "charts": [
    {
      "id": "market_growth",
      "type": "line",
      "title": "中国工业软件市场保持双位数增长",
      "unit": "亿元",
      "as_of_date": "2026-07-19",
      "source": "工信部、公司公告，Research Agent 整理",
      "labels": ["2022", "2023", "2024", "2025E"],
      "series": [
        {
          "name": "市场规模",
          "values": [2100, 2380, 2710, 3050],
          "value_kind": ["actual", "actual", "actual", "forecast"]
        }
      ]
    }
  ]
}
```

轻量校验要求：

- `id` 唯一且只能使用安全文件名字符；
- 类型必须属于允许列表或进入声明式兜底；
- labels 与每个 series 的 values 数量一致；
- 数值只能是数字或 `null`；
- 实际值与预测值必须标记；
- 标题、单位、来源和截止日期不能为空；
- 禁止 URL 数据源、代码、表达式和任意文件路径；
- 图表数量设置合理上限，默认 20 张。

### 5.3 Markdown 图表占位符

最终 Markdown 使用：

```markdown
{{chart:market_growth}}
```

排版程序将占位符替换为对应 SVG/PDF 资产。图表不存在或生成失败时，不静默删除，输出明确错误并阻断正式 PDF。

## 6. 常规图表生成

### 6.1 图表引擎

使用 Matplotlib 生成常规图表：

- Web：SVG；
- PDF：矢量 PDF；
- 预览：PNG。

新增模块：

```text
src/research_agent/report_charts.py
```

本次只支持高频类型：

1. 折线图 `line`；
2. 柱状图 `bar`；
3. 堆叠柱状图 `stacked_bar`；
4. 柱线组合图 `combo`；
5. 散点图 `scatter`；
6. 热力图 `heatmap`；
7. 瀑布图 `waterfall`。

这些类型足以覆盖市场规模、增速、结构、公司比较、估值、敏感性和贡献拆分等常见研报场景。

### 6.2 固定视觉主题

主题来自 Skill 的 `assets/theme.json`，不由 LLM 自由决定：

- 深蓝主色；
- 一个强调色；
- 中性灰阶；
- 实际值使用实线/实色；
- 预测值使用虚线/浅色；
- 中文无衬线字体；
- 轻量水平网格；
- 禁止 3D、渐变和大面积阴影；
- 默认图表宽度适配 A4 正文；
- 数字自动应用千分位、百分比和单位；
- 来源和备注由报告模板排版，不烘焙进图形主体。

### 6.3 图表选择规则

| 研究意图 | 默认类型 |
|---|---|
| 时间趋势 | 折线图 |
| 类别比较 | 柱状图 |
| 结构变化 | 堆叠柱状图 |
| 规模与增速 | 柱线组合图 |
| 两个指标关系 | 散点图 |
| 敏感性分析 | 热力图 |
| 增减贡献 | 瀑布图 |
| 精确数字密集 | 保留表格 |

图表不是越多越好。每个主要定量章节建议 1–3 张，每张图只表达一个核心结论。

## 7. 特殊图表 LLM 兜底

### 7.1 触发条件

只有同时满足以下条件才进入兜底：

- 图表确有必要；
- 数据完整且来源明确；
- 7 类常规图表无法合理表达；
- 不是因为数据不足或单位冲突；
- Skill 明确允许该图进入兜底。

### 7.2 兜底形式

LLM 输出受限 Vega-Lite JSON，不输出 Python、JavaScript 或 LaTeX。

允许：

- layer；
- facet；
- repeat；
- concat；
- 受控 mark 和 encoding；
- 受控聚合、排序、分箱和堆叠。

禁止：

- 外部 URL；
- 内联伪造数据；
- calculate 和任意表达式；
- 外部图片、字体和脚本；
- 任意主题配置；
- 任意文件系统访问。

程序负责绑定图表清单中的真实数据、注入统一主题并通过 `vl-convert` 输出 SVG/PDF。

### 7.3 失败处理

- 最多向 LLM 返回一次结构化错误进行修复；
- 第二次失败后降级成表格或文字矩阵；
- 必需图表失败时阻断正式 PDF；
- 兜底原始 JSON 和最终清洗 JSON保存在项目目录，方便诊断。

## 8. 固定中文 LaTeX 模板

### 8.1 模板策略

不再让 LLM 编写整份 LaTeX。统一使用：

```text
skills/brokerage-report-formatting/assets/brokerage-report.tex
skills/brokerage-report-formatting/assets/brokerage-report.sty
```

模板固定处理：

- A4 页面和页边距；
- 中文字体；
- 封面；
- 执行摘要；
- 目录；
- 章节标题；
- 页眉页脚和页码；
- 三线表；
- 长表格；
- 图表编号、标题、来源和备注；
- 风险提示；
- 来源索引；
- 免责声明。

### 8.2 Markdown 转换

使用 Pandoc 将最终 Markdown 转换为 LaTeX/HTML：

```text
Markdown
  → 图表占位符预处理
  → Pandoc AST
  → 固定 LaTeX 模板
  → XeLaTeX
```

使用 Pandoc 的原因：

- 正确处理标题、列表、图片、脚注和 GFM 表格；
- 避免维护自制 Markdown 正则解析器；
- 同一 Markdown 可以生成 HTML 和 LaTeX；
- 模板稳定，便于调试。

运行时必须检查 `pandoc` 和 `xelatex`。缺失时保留 Markdown 和图表资产，并返回明确安装提示，不调用 LLM 临时生成 LaTeX 兜底。

### 8.3 表格策略

- 普通表格使用三线表；
- 长表格使用 `longtable` 并重复表头；
- 宽表格允许横向页面；
- 文本左对齐、数字右对齐；
- 禁止把整个表格缩小到不可读；
- 无法容纳时优先拆表；
- 来源和表注紧随表格；
- SWOT、机会风险矩阵等文字密集内容保留表格，不生成 TikZ。

## 9. Web 与下载改造

### 9.1 Web 预览

不再使用当前简化正则解析器显示最终报告。后端通过 Pandoc 生成安全 HTML，前端直接展示：

- 标题和段落；
- 有序/无序列表；
- 表格；
- 脚注和链接；
- SVG 图表；
- 图表标题、来源和备注。

HTML 进入页面前必须清理危险标签和属性。

### 9.2 下载入口

保留：

- Markdown 下载；
- 正式 PDF 下载；
- LaTeX 源文件下载，作为调试入口。

原“普通 PDF”和“高级 PDF”按钮合并成一个“正式 PDF”。兼容接口暂时都指向同一个正式 PDF，避免破坏旧链接。

## 10. PDF 检查

PDF 生成后执行：

1. 检查 LaTeX 编译返回码；
2. 检查 undefined reference、missing glyph 和严重 overfull；
3. 使用 PyMuPDF 或 `pdfinfo` 检查页数；
4. 使用 `pdftotext` 确认标题、风险提示和免责声明存在；
5. 使用 `pdftoppm` 将首页、摘要页和含复杂表格/图表的页面渲染为 PNG；
6. 自动检查图片是否为空或尺寸异常；
7. 开发阶段人工检查抽样 PNG 的裁切、重叠、字号和对齐。

本阶段不建设全量像素级视觉回归平台，但保留 2–3 份黄金样例 PDF 作为人工和集成测试基准。

## 11. 代码改造范围

### 11.1 新增文件

```text
skills/brokerage-report-formatting/
src/research_agent/agent_skills.py
src/research_agent/report_charts.py
src/research_agent/report_formatting.py
tests/test_brokerage_report_skill.py
tests/test_report_charts.py
tests/test_report_formatting.py
tests/fixtures/brokerage_report/
```

### 11.2 修改文件

| 文件 | 修改 |
|---|---|
| `agents/formatter.py` | 加载 Skill，要求输出 Markdown 和 chart manifest |
| `agents/prompts/formatter.md` | 缩减为基础约束，详细排版规则移入 Skill |
| `report_layout.py` | 移除 LLM 生成整份 LaTeX，改为调用固定排版服务 |
| `state.py` | 只增加 chart manifest、HTML 和正式 PDF 可选路径 |
| `config.py` | 增加 Skill、Pandoc、主题和图表数量配置 |
| `web_app.py` | 调用统一正式 PDF/HTML 输出 |
| `web_static/results.js` | 展示 HTML 报告和统一下载按钮 |
| `web_static/ui.js` | 不再负责最终报告 Markdown 语法解析 |
| `web_static/styles.css` | 增加报告、表格、图表和打印样式 |
| `pyproject.toml` | 增加 Matplotlib、HTML 清理和 Vega-Lite 兜底依赖 |
| `README.md` | 更新依赖、配置和正式报告输出说明 |

### 11.3 保持不变

- 来源解析与 OCR；
- 搜索与 Embedding；
- EvidenceRecord 和引用审计；
- Collector/Validator 主体逻辑；
- 现有检查点；
- 项目目录命名；
- CLI 的 new/resume/status 主命令。

## 12. 配置项

新增：

```text
REPORT_FORMATTING_SKILL=brokerage-report-formatting
REPORT_THEME=brokerage_research_v1
REPORT_MAX_CHARTS=20
REPORT_ENABLE_LLM_CHART_FALLBACK=true
REPORT_PANDOC_BIN=pandoc
REPORT_LATEX_ENGINE=xelatex
REPORT_RENDER_TIMEOUT=120
```

默认配置在本地可运行。服务器部署时需要安装 Pandoc、XeLaTeX、固定中文字体和 Poppler。

## 13. 测试范围

### 13.1 单元测试

- Skill 白名单加载；
- Skill 文件缺失和路径穿越拒绝；
- chart manifest JSON 校验；
- 7 类常规图表渲染；
- 实际/预测样式；
- 缺失值和非法数据拒绝；
- LLM 兜底 URL、表达式和外部资源拒绝；
- 图表占位符替换；
- Markdown/LaTeX 特殊字符；
- HTML 清理。

### 13.2 集成测试

- Agent5 生成 Markdown + chart manifest；
- chart manifest 生成 SVG/PDF；
- Markdown 表格正确生成 HTML；
- Markdown + 图表正确生成正式 PDF；
- 旧 PDF 下载接口仍可用；
- 缺少 Pandoc/XeLaTeX 时返回明确错误；
- PDF 包含标题、图表、来源、风险和免责声明。

### 13.3 示例报告

建立一份固定中文行业报告样例，至少包含：

- 折线图；
- 柱线组合图；
- 热力图；
- 一张长表格；
- 实际值和预测值；
- 来源和免责声明。

测试时生成 PDF，并渲染首页、图表页和长表格页进行检查。

## 14. 实施顺序与工作量

### 阶段 1：项目排版 Skill（1–2 人日）

- 初始化 Skill；
- 编写券商结构、图表、表格、中文风格和检查清单；
- 增加 Agent5 Skill 加载器；
- 测试触发和白名单加载。

验收：Agent5 运行日志能够确认加载 Skill，最终 Markdown 符合 Skill 结构。

### 阶段 2：常规图表（1–2 人日）

- chart manifest；
- Matplotlib 主题；
- 7 类图表；
- SVG/PDF/PNG 输出；
- 占位符处理。

验收：样例报告常规图表全部生成，实际/预测视觉区分正确。

### 阶段 3：固定模板与 Pandoc（2–3 人日）

- 中文 LaTeX 模板和样式；
- Markdown → HTML/LaTeX；
- 图表和表格排版；
- 统一正式 PDF。

验收：样例 PDF 可编译，表格、图表、页眉页脚和中文无明显问题。

### 阶段 4：LLM 特殊图表兜底（1 人日）

- 受限 Vega-Lite JSON；
- 安全校验；
- 主题注入；
- 一次修复和失败降级。

验收：支持一个常规注册表之外的分面/组合图，并拒绝 URL 和表达式。

### 阶段 5：Web、PDF 检查与收尾（1–2 人日）

- Web 安全 HTML；
- 统一下载按钮；
- PDF 日志和页面检查；
- README 与部署依赖；
- 完整测试。

验收：Web/PDF 内容一致，测试通过，抽样页面无裁切、重叠和破损表格。

总计：**6–10 人日**。

## 15. 完成验收标准

- [ ] Agent5 确实加载 `brokerage-report-formatting` Skill；
- [ ] Skill 目录通过 `skill-creator` 校验；
- [ ] 最终报告不再包含 ASCII 图或“建议插入图表”；
- [ ] 至少 7 类常规图表可以生成 SVG/PDF；
- [ ] 注册类型无法表达时可以进入受控 LLM 兜底；
- [ ] 图表标题、单位、来源和截止日期完整；
- [ ] 实际值和预测值视觉区分；
- [ ] Markdown 表格在 Web 和 PDF 中正确显示；
- [ ] LLM 不再生成整份 LaTeX；
- [ ] Web 和 PDF 使用同一份 Markdown 和图表资产；
- [ ] 只有一个正式 PDF 下载入口；
- [ ] PDF 无缺字、丢图、严重溢出和破损表格；
- [ ] 现有研究流程和证据审计测试继续通过；
- [ ] 新增单元测试和集成测试全部通过；
- [ ] 中文样例报告完成抽样页面检查。

## 16. 关键取舍

这次改造不追求完整报告基础设施，而是把有限开发量集中在最影响用户感受的部分：

1. 用 Skill 固定券商研报结构和排版规则；
2. 用真实图表替换 ASCII 和占位描述；
3. 用固定模板替换不稳定的 LLM 全量 LaTeX；
4. 用 Pandoc 解决 Markdown、表格、HTML 和 LaTeX 转换；
5. 用 PDF 渲染检查发现实际格式问题；
6. 保留特殊图表的 LLM 能力，但不执行任意代码。

方案完成后的主要效果应是：报告更像专业研究交付物、图表数量和质量稳定、表格不再显示成代码、PDF 样式统一，同时不为一个排版问题重构整个 Research Agent。
