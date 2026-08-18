# Research Agent · 通用行业调研 Multi-Agent

**模型无关**的行业调研自动化流水线。支持任何 OpenAI 兼容 API（OpenAI / DeepSeek / Qwen / Ollama / vLLM / 本地模型）。

<p align="center">
  <img src="docs/images/lumitrace-home.png" alt="溯光 Lumitrace 首页" width="100%" />
</p>

## 特点

**架构**

- **模型无关**：通过 `LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL` 三个环境变量切换任意模型
- **零框架依赖**：不依赖 LangChain / claude-agent-sdk / openai SDK，纯 httpx 自建 LLM 客户端与 Agent 循环
- **5 个专业 Agent 分工协作**：战略规划 → 数据搜集 → 信息验证 → 深度分析 → 排版交付
- **CLI 与 Web 共用一份状态机**：行为差异全部收敛到注入的 `PipelineHost`，两端不会流程漂移

**质量保障**

- **工具白名单即能力边界**：每个 Agent 只挂载它该有的工具。Agent4 拿不到任何联网或写证据的工具，"不许上网补数据"是程序上做不到，而非 prompt 约束
- **模型声明不算收敛**：Agent3 写 `converged: true` 只是建议，程序另跑确定性门禁复核；未解决冲突、critical 补研任务未完成、必答问题证据不足，任一命中即继续补采
- **确定性证据链**：Agent 必须保存精确 EvidenceRecord，`excerpt` 需真实存在于原文 chunk；未解决矛盾、无效定位或无证据会阻断交付
- **结构化补研任务台账**：缺口以带稳定 `task_id` 的任务持久化（`03_tasks.json`），Agent2 逐条执行、Agent3 逐条验收，取代自由文本 gap 的反复理解
- **结论必须被引用**：Agent4 输出机读结论台账（`04_claims.json`），程序反向校验重要结论都有 SUPPORTED 证据，且 critical 结论不得在排版阶段丢失
- **信息源 S/A/B/D 四级分层**：默认规则内置，杜绝低质信息

**人机协作**

- **3 个人机确认检查点**：调研提纲、信息源分层、最终数据源清单——确保方向不跑偏
- **需求澄清对话**：Agent1 判断信息不足时主动提问；CLI 直接在终端问答，Web 在工作台以表单形式回答（可一键全用默认值）
- **Agent2↔3 迭代循环**：采集→验证→反馈→补采，默认最多 3 轮，数据质量有保障

**可靠性**

- **断点续跑**：任何阶段中断（Ctrl+C / 网络错误 / API 异常），状态自动保存，一行命令恢复
- **异常自动重试**：每个阶段失败后自动重试 2 次，友好报错；确定性门禁阻断不重试（重跑只会重复同样的结果）
- **防卡死**：同一工具用同样参数连续 3 次返回相同错误即中止本次执行，不再一路烧到轮次上限
- **失败可显式重试**：自动重试耗尽或审查未通过后，项目标记为可重试，保留既有产物，在工作台一键重试；不再需要删库重跑
- **中断自动识别**：服务重启时扫描停在 Agent 执行中的项目，标记为可重试而非静默挂起

**可观测与素材**

- **执行日志持久化**：每条阶段进展写入项目内 `run_log.jsonl`，服务重启后仍可回溯上次失败前的过程
- **Token 用量可见**：按阶段累计消耗，首页提供统计卡、52 周热力图、阶段分布与项目排行
- **多搜索源**：AnySearch / DuckDuckGo（免费）/ SerpAPI / Tavily，可在设置页切换并测试连通性
- **统一材料中心**：PDF、Office、HTML、图片和压缩包统一解析、OCR、版本化和项目隔离检索
- **真实混合检索**：关键词、同义词和数值归一化默认可用；配置 Embedding API 后启用真实语义向量融合

## 架构

```
用户输入主题
  │
  ▼
Orchestrator（单一状态机 · 11 个阶段 · 3 个检查点 + 需求澄清）
  │
  ├── Agent1 · 战略规划    → 需求澄清问答 → outline.md + 需求清单    ⏸ 用户确认
  ├── Agent2 · 数据搜集    → 源分层清单                            ⏸ 用户确认
  ├── Agent2↔3 · 循环      → 采集-验证（≤3轮）→ 源终稿            ⏸ 用户确认
  ├── ── 确定性交付门禁 ── → 需求清单 / 任务台账 / 证据质量三重校验
  ├── Agent4 · 深度分析    → 全方位分析（波特五力/SWOT/...）+ 结论台账
  └── Agent5 · 排版交付    → Markdown + 图表清单 + 安全 HTML + 正式 PDF
```

状态机只有一份实现（`orchestrator.run_state_machine`）。CLI 与 Web 的行为差异全部收敛到
注入的 `PipelineHost` 协议：CLI 在检查点阻塞等待输入，Web 返回 `PAUSE` 让状态机退出、
由 HTTP 审批接口推进阶段后重新调度。两端不会出现流程漂移。

### Agent Harness

每个 Agent 都跑在同一套五层 harness 上，每层负责一类失败：

```
Orchestrator._safe_run     自动重试 2 次 · 失败落盘 state.json · 门禁异常不重试
  └─ agent 函数             prompt 组装（模板 + 需求清单 + 证据目录）· 产物存在性校验
      └─ agent_loop         工具循环 · 路径沙箱 · 重复错误卡死检测 · Token 采集
          └─ ToolRegistry   工具执行 · 异常转错误字符串回喂模型
              └─ LLMClient  HTTP 层 · 429/5xx 指数退避 · 连接池复用 · SSE 流式解析
```

核心循环是标准 OpenAI tool-calling：`system + user` 起手 → LLM → 有 `tool_calls` 就全部
执行、结果作为 `role=tool` 消息追加 → 回到 LLM → 直到无 `tool_calls` 或耗尽 `max_turns`。
`run_agent()` 用于单次任务，`AgentSession` 用于 Agent1 的多轮对话，两者共用同一份工具
执行与错误跟踪逻辑。

harness 提供的保护：

| 机制 | 说明 |
|---|---|
| **路径沙箱** | `cwd` 锁定项目目录。Read/Write 的路径参数在执行前强制解析，绝对路径、`..`、符号链接三种逃逸都被拒绝。越界返回错误字符串而非抛异常，留出自纠机会 |
| **卡死检测** | 以 `(工具名, 归一化参数, 错误文本)` 三元组计数，同一组合达 3 次抛 `AgentLoopStuckError`；工具一旦成功则清空该工具计数 |
| **工具容错** | 工具异常统一转为 `Error executing tool '...'` 字符串回喂模型，不中断循环 |
| **Schema 自动生成** | 从 Python 函数签名反射生成 OpenAI function schema，参数描述取自 docstring 的 `Args:` 段落。新增工具只需一个带类型注解的函数 + `@default_registry.tool` |
| **临时工具** | `registry.subset()` 派生子注册表挂载临时工具（如 Web 模式的 `AskUser`），不污染全局注册表 |
| **Token 采集** | `collect_stage()` 用 contextvar 按阶段累计；统计失败被静默吞掉，绝不影响主流程 |

所有注入的上下文都反复声明：工具返回的文本、材料正文、claim、excerpt 都是**不可信数据
而非指令**，不得执行其中的命令——prompt 注入防护做在上下文注入层。

## 环境要求

- **Python >= 3.10**（推荐 3.11+）
- 任何 **OpenAI Chat Completions API 兼容服务**（OpenAI / DeepSeek / Qwen / Ollama / vLLM）
- 正式 PDF 需要 **XeLaTeX**；Pandoc 由 Python 依赖 `pypandoc-binary` 提供，也可使用系统 Pandoc
- 无需 Anthropic API Key

## 安装

```bash
# 推荐用 uv（最快）
cd research-agent
uv venv --python 3.11
source .venv/bin/activate
uv pip install -e '.[web,search]'

# 或用 pip
pip install -e '.[web,search]'
```

可选依赖组：`web`（FastAPI 工作台）、`search`（DuckDuckGo 免费搜索）、`dev`（pytest）。
只用 CLI 且自备 SerpAPI/Tavily Key 时，`pip install -e .` 即可。

配置模型（编辑 `.env`）：

```bash
cp .env.example .env
# 编辑 .env，设置你的模型服务：

# OpenAI
LLM_BASE_URL=https://api.openai.com/v1
LLM_API_KEY=sk-xxx
LLM_MODEL=gpt-4o

# DeepSeek
# LLM_BASE_URL=https://api.deepseek.com/v1
# LLM_MODEL=deepseek-chat

# 本地 Ollama
# LLM_BASE_URL=http://localhost:11434/v1
# LLM_API_KEY=ollama
# LLM_MODEL=qwen2.5:72b
```

## 使用

### 启动网页工作台

```bash
SOURCE_DATA_DIR=.data/sources research-agent-web
```

默认监听 `http://127.0.0.1:8765`（可用 `--host` / `--port` 调整）。工作台包含：

| 页面 | 用途 |
|---|---|
| `/` | 首页：Token 统计卡、52 周热力图、阶段分布、项目排行 |
| `/research` | 发起新调研、项目列表、重试与删除 |
| `/workspace` | 进行中项目的工作台：阶段进展、澄清问答、检查点审批 |
| `/results` | 交付物预览与下载（Markdown / HTML / TeX / PDF） |
| `/materials` | 材料中心：上传、处理进度、预览、元数据、激活、重处理、版本比较、搜索、归档 |
| `/settings` | 模型与搜索源配置，含连通性测试 |

本地 Web 会后台处理上传任务；生产环境仍建议单独运行 worker：

```bash
research-agent-source-worker --data-dir .data/sources
```

> **访问控制**：工作台默认绑定 `127.0.0.1`，仅本机可访问，无需认证。若要暴露到网络，
> 必须先在 `.env` 设置 `WEB_AUTH_TOKEN`，否则启动会被拒绝——未认证的工作台允许任何
> 访问者读取全部调研数据、修改模型配置（含写入 `.env`）和删除项目。设置令牌后，
> 首次访问带上 `?token=…`（随后写入 cookie），或在请求中携带 `X-Auth-Token` 头。
> 确认风险后也可用 `--allow-insecure-host` 强制绑定公开地址，不推荐。

CLI 使用同一个领域服务（`--data-dir` 默认 `.data/sources`）：

```bash
research-agent sources upload PROJECT report.pdf financials.xlsx
research-agent sources list PROJECT [--all-versions]
research-agent sources process [--once]
research-agent sources search PROJECT '2025 年营业收入' [--limit 10]
research-agent sources read PROJECT SOURCE_ID [--chunk-id CHUNK]
research-agent sources activate PROJECT SOURCE_ID
research-agent sources archive PROJECT SOURCE_ID
research-agent sources inspect PROJECT SOURCE_ID
research-agent sources verify PROJECT
research-agent sources rebuild-index PROJECT
research-agent sources backup ./backups/2026-07-17
```

### 启动新调研

```bash
python -m research_agent new "新能源汽车行业"
```

系统会：
1. 创建项目目录 `projects/新能源汽车行业_20260424/`
2. Agent1 判断信息是否充分；不足时向你提问澄清（可跳过，用建议默认值）
3. 生成提纲，并同步固化研究需求清单 `research_requirements.json` → 你确认后 → 继续推进后续阶段
4. 全部完成后输出 Markdown、真实图表、安全 HTML、LaTeX 源文件和统一正式 PDF

### 研究需求清单（确定性完整性门禁）

Agent1 生成提纲时，会把提纲《核心研究问题》小节固化成 `research_requirements.json`：

```json
{
  "schema_version": 1,
  "topic": "新能源汽车行业",
  "source_outline": "0a1b2c3d4e5f6789",
  "requirements": [
    {
      "question_id": "q1",
      "text": "2024 年市场规模是多少？",
      "required": true,
      "min_supported": 1,
      "min_source_tier": null,
      "require_numeric": false
    }
  ]
}
```

这份清单是全流程唯一的问题标识来源：

- Agent2/3/4/5 的 prompt 都注入同一张表，`RecordProjectEvidence` 的 `research_question_id` 必须取自其中，表外 ID 会被工具拒绝
- 确定性质量门读取**清单全集**判定覆盖率，而不是从已有证据反推。必答问题一条证据都没有时同样会被检测到并阻断交付
- `source_outline` 绑定生成清单时的提纲摘要；当前 `01_outline.md` 发生变化后，旧清单立即失效，必须重新确认研究范围
- 需要收紧某个问题的通过条件（最低证据数、最低来源等级、是否要求数值），直接编辑该文件即可；改成 `"required": false` 的问题缺证据不会单独阻断交付

提纲缺少可解析的《核心研究问题》有序列表，或者清单缺失、为空、`question_id` 重复、JSON 损坏、与当前提纲摘要不一致时，交付一律阻断，不会用章节标题、通用问题或空要求放行。

**旧项目迁移**：本机制上线前创建的项目没有这个文件。用以下命令从既有提纲重建，或在工作台的「需要重新确认研究计划」面板点击「生成研究需求清单」：

```bash
python -m research_agent migrate-plan projects/新能源汽车行业_20260424
```

迁移只固定问题清单，不补造证据——必答问题仍需要合格证据才能通过交付门禁。
已有且与当前提纲匹配的有效清单不会被迁移命令覆盖，以免丢失人工收紧的证据门槛或重新分配问题 ID。

### 断点续跑

```bash
python -m research_agent resume projects/新能源汽车行业_20260424
```

从上次中断的阶段继续。

### 重试失败的调研

审查未通过（证据质量门槛不达标）或某个 Agent 阶段报错后，项目会被标记为**失败但可重试**，既有产物（提纲、源清单、历史采集轮次、已入库证据）全部保留：

```bash
# 重试失败阶段；审查未通过时自动追加 1 轮采集验证预算
python -m research_agent retry projects/新能源汽车行业_20260424

# 追加更多轮次
python -m research_agent retry projects/新能源汽车行业_20260424 --extra-rounds 2
```

网页工作台在失败时会展示失败阶段、原始错误和证据门槛未通过原因，并提供「重试并继续」按钮；研究首页的项目列表也可直接重试。

重试的复位规则：

| 失败情形 | 重试行为 |
|---|---|
| 采集验证轮次用尽但证据未收敛 | 追加轮次预算（默认 +1 轮），从下一轮继续补采验证 |
| 交付前证据门槛阻断（分析/排版阶段） | 回退到采集验证阶段重新补证据 |
| Agent4 判定需要补研（`needs_more_research`） | gap 转为 critical 补研任务，回退到采集验证阶段 |
| critical 补研任务未完成 | 回退到采集验证阶段继续执行任务 |
| 缺少研究需求清单 | 重试被拒绝，需先执行 `migrate-plan` 或在工作台生成清单 |
| 工具重复报错导致提前中止 | 不自动重试；需修正工具参数后续跑 |
| 其他 Agent 阶段报错 | 原地重跑该阶段 |

轮次预算没有硬上限，避免项目彻底卡死；超过 10 轮后重试提示会附带成本提醒，建议此时改为在材料中心补充权威材料。

### 删除项目

```bash
python -m research_agent delete projects/新能源汽车行业_20260424 -y
```

网页工作台与研究首页的项目列表也提供删除按钮（二次确认；运行中的项目不允许删除）。删除会移除项目目录及其全部产物，不可恢复。

### 查看项目状态

```bash
python -m research_agent status projects/新能源汽车行业_20260424
```

失败项目会额外显示失败阶段、失败原因和重试命令。

## 项目结构

```
research-agent/
├── pyproject.toml
├── .env.example
├── src/research_agent/
│   ├── __main__.py           # CLI 入口（new / resume / retry / delete / status / migrate-plan / sources）
│   ├── config.py             # 全局配置与常量
│   ├── state.py              # 11 阶段状态机 + JSON 持久化 + 失败/重试标记 + 澄清历史
│   ├── research_plan.py      # 研究需求清单：提纲派生、schema 校验、全流程 question_id 契约
│   ├── checkpoints.py        # 3 个人机确认检查点（Rich CLI 渲染与询问）
│   ├── run_log.py            # 按项目持久化执行日志（run_log.jsonl）
│   ├── token_usage.py        # Token 用量采集、按阶段聚合、跨项目汇总
│   ├── orchestrator.py       # 唯一状态机 + PipelineHost 协议 + 异常兜底 + 确定性门禁 + 失败复位
│   ├── web_app.py            # FastAPI 工作台（含 Web 宿主与 SSE 事件推送）
│   ├── agent_skills.py       # 白名单项目 Skill 加载器
│   ├── report_formatting.py  # 报告渲染流水线（Markdown → HTML/TeX/PDF）
│   ├── report_charts.py      # 图表清单校验与真实图表渲染
│   ├── agent_loop/           # Agent Harness
│   │   ├── loop.py           #   run_agent / AgentSession / 路径沙箱 / 卡死检测 / SSE 解析
│   │   └── types.py          #   AgentOptions / AgentLoopStuckError
│   ├── llm/                  # 自建 LLM 客户端
│   │   ├── client.py         #   httpx 异步客户端 + 指数退避 + 流式
│   │   ├── types.py          #   ChatMessage / ToolDefinition / LLMResponse
│   │   └── errors.py         #   分类异常（认证/限流/上下文超长/模型不存在）
│   ├── tools/                # 工具层
│   │   ├── registry.py       #   注册表 + subset 派生 + 执行容错
│   │   ├── schemas.py        #   从函数签名反射生成 OpenAI schema
│   │   └── builtins/         #   Read / Write / WebSearch / WebFetch / 项目源与证据工具
│   ├── sources/              # 统一材料中心（解析、OCR、检索、证据、质量门、任务台账）
│   │   ├── quality.py        #   确定性质量门（excerpt 必须真实存在于 chunk）
│   │   ├── tasks.py          #   结构化补研任务台账与门禁
│   │   ├── claims.py         #   Agent4 结论台账校验
│   │   └── citations.py      #   引用格式与方向性校验
│   └── agents/
│       ├── strategist.py     # Agent1 战略规划（需求澄清 + 提纲）
│       ├── collector.py      # Agent2 数据搜集（源分层 + 按级采集）
│       ├── validator.py      # Agent3 信息验证（交叉验证 + JSON 反馈）
│       ├── analyst.py        # Agent4 深度分析（+ AnalysisOutcome 契约）
│       ├── formatter.py      # Agent5 排版交付（+ 三道交付审计）
│       ├── source_context.py # 各 Agent 共用的证据边界上下文注入
│       └── prompts/          # 各 Agent 的 system prompt（.md）
│           ├── strategist.md
│           ├── collector.md
│           ├── collector_round.md
│           ├── validator.md
│           ├── analyst.md
│           └── formatter.md
├── skills/                   # 项目内白名单 Skill
│   └── brokerage-report-formatting/
│       ├── SKILL.md
│       ├── references/       # 报告结构、图表、表格、中文排版、质检清单
│       └── assets/
├── projects/                 # 调研项目数据（每次一个子目录）
│   └── {topic}_{date}/
│       ├── state.json
│       ├── run_log.jsonl
│       ├── token_usage.jsonl
│       ├── 01_outline.md
│       ├── research_requirements.json
│       ├── 02_sources_draft.md
│       ├── 02_sources_final.md
│       ├── 03_tasks.json             # 结构化补研任务台账
│       ├── 03_raw_data/
│       │   ├── round_1.md
│       │   ├── feedback_round_1.json
│       │   ├── task_results_round_1.json
│       │   └── ...
│       ├── 03_validation_report.md
│       ├── 04_analysis.md
│       ├── 04_claims.json            # 机读结论台账
│       ├── 04_analysis_outcome.json  # Agent4→5 转换契约
│       ├── 05_final_report.md
│       ├── 05_chart_manifest.json
│       ├── 05_charts/                # SVG / PDF / PNG 图表
│       ├── 05_final_report.html
│       ├── 05_final_report.tex
│       └── 05_final_report.pdf
└── tests/                    # 28 个测试文件（状态机、harness、门禁、工具边界、Web 鉴权等）
```

## 各 Agent 职责

| Agent | 职责 | 工具权限 | 轮次上限 | 输出 |
|---|---|---|---|---|
| **Agent1 战略规划** | 澄清目标/范围/交付物（必要时向用户提问）→ 生成提纲 → 固化研究需求清单 | CLI：Read, Write, ListProjectSources, SearchProjectSources<br>Web：Read, Write, AskUser | 40 / 20 | `01_outline.md` + `research_requirements.json` |
| **Agent2 数据搜集** | 2-A 信息源识别与 S/A/B/D 分层；2-B 按级采集 + 执行补研任务 | Read, Write, WebSearch, WebFetch, CaptureProjectWebSource, 项目源读取工具 | 25 / 40 | `02_sources_draft.md` + `round_N.md` + `task_results_round_N.json` |
| **Agent3 信息验证** | 原文回查、写入 EvidenceRecord、冲突检测、淘汰低质源、验收补研任务 | Read, Write, SearchProjectSources, ListProjectSourceChunks, ReadProjectSource, **RecordProjectEvidence**, InspectSourceEvidence | 30 | `feedback_round_N.json` + `03_validation_report.md` + `03_tasks.json` |
| **Agent4 深度分析** | 波特五力/SWOT/PEST + 定量分析，严格限定在已验证证据内 | Read, Write, ListProjectSources, InspectSourceEvidence | 50 | `04_analysis.md` + `04_claims.json` + `04_analysis_outcome.json` |
| **Agent5 排版交付** | 逐字复制 Agent4 正文，仅生成并插入图表后输出交付物 | Read, Write | 12 | `05_final_report.md` + `05_chart_manifest.json` + HTML/TeX/PDF |

### 权限设计要点

工具白名单不是建议而是硬边界，几处关键的单点收口：

- **只有 Agent3 有 `RecordProjectEvidence`**。写入证据的权力单点收口，其他 Agent 无法自造证据。
- **只有 Agent2-B 有 `CaptureProjectWebSource`**。所有公开网页事实必须先被快照成项目源，才可能被引用。
- **Agent4 没有任何联网工具**（无 WebSearch / WebFetch / CaptureProjectWebSource / RecordProjectEvidence）。它的 system prompt 里注入 `SUPPORTED EvidenceRecord Catalog` 作为唯一事实来源，且只给元数据；要读正文必须调 `InspectSourceEvidence`。想上网补数据或自己造证据，无工具可用。

### Agent4/5 的 fail-closed 契约

Agent4 除分析报告外必须输出 `04_analysis_outcome.json`，schema 用 `extra="forbid"` 严格校验：
`completed` 必须搭配空 `gap_requests`，`needs_more_research` 必须至少一条，且每个 `question_id`
都要在固定需求清单内。判定需要补研时，gap 会被自动转成 critical 补研任务，回到 Agent2↔3。
Agent4 运行前会先删除上次的三个产物，避免重跑时旧文件蒙混过关。

Agent5 交付前有三道审计，顺序不可调换：引用格式逐字校验 → **Agent4 的 critical 结论必须原文
出现在终稿正文中**（防止结论在排版环节丢失）→ 追加证据索引附录。先审正文再加附录，否则附录里
的引用可能掩盖一个正文毫无引用的报告。

Agent5 的排版规则不写在 prompt 里，而是从 `skills/` 加载白名单 Skill（名称正则校验 + 路径
越界拒绝），便于替换视觉风格而不改代码。

## 信息源分层标准

| 级别 | 定义 | 示例 |
|---|---|---|
| **S** | 一手权威原始数据 | 上市公司年报、统计局、央行、交易所公告 |
| **A** | 头部研究机构 | 艾瑞/IDC/Gartner、头部券商深度研报 |
| **B** | 专业财经媒体 | 36氪、虎嗅、财新、彭博、路透 |
| **D** | UGC/低可信（默认剔除） | 知乎回答、自媒体公众号 |

## 确定性质量门禁

这套流水线的核心假设是：**模型的自我评价不可信，程序判定才算数**。三处门禁互相独立：

### 1. 收敛判定（每轮采集验证后）

Agent3 在反馈里写 `converged: true` 只是建议。程序另外复核，以下任一命中即不收敛、继续补采：

- 质量门未通过（见下）
- `gap_list` 仍有实质缺口
- `conflicts` 中存在没有 `resolution` 的冲突
- 存在 `critical` 且 `pending` 的补研任务
- 需求清单缺失或任务台账损坏（按阻断处理，绝不静默放行）

### 2. 证据质量门（`QualityGate`）

逐条校验每个 EvidenceRecord：source 存在、`version` 匹配、chunk 存在，且 **`excerpt` 必须真实
出现在 chunk 原文中**。任一不满足即计入 invalid，直接 `BLOCKED`。

覆盖率按**需求清单全集**计算，而不是从已有证据反推——所以"某个必答问题一条证据都没有"能被
发现。空需求集会被显式判为 `BLOCKED`，避免"没什么要检查的"导致项目轻松通过。指向清单外
`question_id` 的证据也不会被静默丢弃，它意味着清单过期或模型在编 ID，两者都必须可见。

状态分五档：`passed` / `passed_with_limitations` / `needs_more_research` / `needs_human_review` / `blocked`，
前两档放行。

### 3. 交付前门禁（Agent5 启动之前）

按此顺序检查，任一失败即阻断且**不自动重试**：需求清单是否存在 → 任务台账是否可解析 →
critical 任务是否全部完成 → 证据质量门是否通过。先查清单是有意的：不知道要回答什么问题时，
"证据够不够"没有意义。

### 结构化补研任务

缺口不再以自由文本 gap 传递，而是持久化为带稳定 `task_id` 的任务（`03_tasks.json`）：

- Agent3 提出任务并指定 `priority`（`critical` / `normal`）、`min_source_tier`、`required_independent_sources`、`completion_criteria`
- Agent2 逐条执行并把结果回填到 `task_results_round_N.json`
- Agent3 下一轮逐条验收：补齐则标 `completed` 并回填 `completed_evidence_ids`，否则保持 `pending` 或标 `blocked` 并写明原因
- **Agent 不得自行将任务标为 `waived`**，豁免只能由显式人工操作写入台账

## 配置项

通过环境变量或 `.env` 文件配置：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_BASE_URL` | `https://api.openai.com/v1` | 模型服务地址 |
| `LLM_API_KEY` | — | 必填（本地 Ollama 可填任意值） |
| `LLM_MODEL` | `gpt-4o` | 模型名称 |
| `LLM_TIMEOUT` | `120` | 请求超时（秒） |
| `LLM_MAX_RETRIES` | `3` | 失败重试次数 |
| `LLM_TEMPERATURE` | `0.7` | 生成温度（见下方说明，当前不作用于 Agent 循环） |
| `SEARCH_API_PROVIDER` | `anysearch` | 搜索引擎（`anysearch` / `duckduckgo` / `serpapi` / `tavily`）；AnySearch 失败或无结果时自动降级到 DuckDuckGo |
| `SEARCH_API_KEY` | — | AnySearch Key 可选；`serpapi` / `tavily` 必填；DuckDuckGo 不需要 |
| `WEB_AUTH_TOKEN` | — | 网页工作台访问令牌。留空时只允许绑定回环地址；绑定 `0.0.0.0` 等公开地址必须设置 |
| `STRATEGIST_MAX_ROUNDS` | `5` | Agent1 CLI 澄清对话轮次上限（Web 侧澄清问答上限固定 9 条） |
| `MAX_COLLECT_ROUNDS` | `3` | Agent2↔3 采集-验证循环上限 |
| `OUTPUT_PREFERENCE` | `balanced` | 报告详略偏好（`fast` / `balanced` / `deep`），影响 Agent5 写作要求 |
| `PROJECTS_DIR` | `./projects` | 调研项目数据根目录 |
| `REPORT_FORMATTING_SKILL` | `brokerage-report-formatting` | Agent5 加载的项目内白名单排版 Skill |
| `REPORT_THEME` | `brokerage_research_v1` | 固定券商研报视觉主题 |
| `REPORT_MAX_CHARTS` | `20` | 单份报告图表数量上限 |
| `REPORT_ENABLE_LLM_CHART_FALLBACK` | `true` | 是否允许特殊图表使用受限 Vega-Lite 兜底 |
| `REPORT_PANDOC_BIN` | `pandoc` | Pandoc 命令；未找到时自动使用 Python bundle |
| `REPORT_LATEX_ENGINE` | `xelatex` | 正式 PDF 的 LaTeX 引擎 |
| `REPORT_RENDER_TIMEOUT` | `120` | 单个报告渲染步骤超时（秒） |
| `SOURCE_DATA_DIR` | `.data/sources` | 材料目录、SQLite catalog 和不可变原文件存储 |
| `SOURCE_EMBEDDING_BASE_URL` | — | OpenAI 兼容 Embeddings API 地址；不配置则只使用离线检索 |
| `SOURCE_EMBEDDING_API_KEY` | — | Embeddings API Key |
| `SOURCE_EMBEDDING_MODEL` | — | 真实语义向量模型，例如多语言 embedding 模型 |
| `SOURCE_API_KEYS_JSON` | — | 可选项目 ACL，例如 `{"key-a":["project-a"],"admin":"*"}` |

> **关于 `LLM_TEMPERATURE`**：该变量目前只作用于直连 LLM 的少数调用（模型连通性测试、
> 图表兜底生成），五个 Agent 的执行循环使用 `AgentOptions` 的默认温度 `0.7`，不读取这个
> 环境变量。在 `.env` 或设置页调整温度不会改变 Agent 的生成行为。

## 测试

```bash
pip install -e '.[dev,web,search]'
pytest
```

覆盖状态机推进与失败复位、Agent harness（工具循环、路径越界、卡死检测）、三处确定性门禁、
证据与引用校验、材料解析与 OCR、检索、Web 鉴权与配置写入、报告渲染等。

## License

MIT
