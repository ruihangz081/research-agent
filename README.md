# Research Agent · 通用行业调研 Multi-Agent

**模型无关**的行业调研自动化流水线。支持任何 OpenAI 兼容 API（OpenAI / DeepSeek / Qwen / Ollama / vLLM / 本地模型）。

## 特点

- **模型无关**：通过 `LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL` 三个环境变量切换任意模型
- **零框架依赖**：不依赖 LangChain / claude-agent-sdk / openai SDK，纯 httpx 自建 LLM 客户端

- **5 个专业 Agent 分工协作**：战略规划 → 数据搜集 → 信息验证 → 深度分析 → 排版交付
- **3 个人机确认检查点**：调研提纲、信息源分层、最终数据源清单——确保方向不跑偏
- **Agent2↔3 迭代循环**：采集→验证→反馈→补采，最多 3 轮，数据质量有保障
- **断点续跑**：任何阶段中断（Ctrl+C / 网络错误 / API 异常），状态自动保存，一行命令恢复
- **异常自动重试**：每个阶段失败后自动重试 2 次，友好报错
- **信息源 S/A/B/D 四级分层**：默认规则内置，杜绝低质信息
- **统一材料中心**：PDF、Office、HTML、图片和压缩包统一解析、OCR、版本化和项目隔离检索
- **确定性证据链**：Agent 必须保存精确 EvidenceRecord；未解决矛盾、无效定位或无证据会阻断交付
- **真实混合检索**：关键词、同义词和数值归一化默认可用；配置 Embedding API 后启用真实语义向量融合

## 架构

```
用户输入主题
  │
  ▼
Orchestrator（状态机 · 10 个阶段 · 3 个检查点）
  │
  ├── Agent1 · 战略规划    → 多轮对话（≤5轮）→ outline.md     ⏸ 用户确认
  ├── Agent2 · 数据搜集    → 源分层清单                        ⏸ 用户确认
  ├── Agent2↔3 · 循环      → 采集-验证（≤3轮）→ 源终稿        ⏸ 用户确认
  ├── Agent4 · 深度分析    → 全方位分析（波特五力/SWOT/...）
  └── Agent5 · 排版交付    → 最终报告 (Markdown)
```

## 环境要求

- **Python >= 3.10**（推荐 3.11+）
- 任何 **OpenAI Chat Completions API 兼容服务**（OpenAI / DeepSeek / Qwen / Ollama / vLLM）
- 无需 Anthropic API Key

## 安装

```bash
# 推荐用 uv（最快）
cd research-agent
uv venv --python 3.11
source .venv/bin/activate
uv pip install -e .

# 或用 pip
pip install -e .
```

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

### 启动材料中心

```bash
pip install -e '.[web,search]'
SOURCE_DATA_DIR=.data/sources research-agent-web
```

打开 `http://127.0.0.1:8000/materials`，即可上传、查看处理进度、预览、编辑元数据、激活、重处理、比较版本、搜索和归档材料。本地 Web 会后台处理上传任务；生产环境仍建议单独运行：

```bash
research-agent-source-worker --data-dir .data/sources
```

CLI 使用同一个领域服务：

```bash
research-agent sources --data-dir .data/sources upload PROJECT report.pdf financials.xlsx
research-agent sources --data-dir .data/sources process
research-agent sources --data-dir .data/sources search PROJECT '2025 年营业收入'
research-agent sources --data-dir .data/sources verify PROJECT
research-agent sources --data-dir .data/sources rebuild-index PROJECT
research-agent sources --data-dir .data/sources backup ./backups/2026-07-17
```

### 启动新调研

```bash
python -m research_agent new "新能源汽车行业"
```

系统会：
1. 创建项目目录 `projects/新能源汽车行业_20260424/`
2. 启动 Agent1 与你多轮对话，澄清调研需求
3. 生成提纲 → 你确认后 → 继续推进后续阶段
4. 全部完成后输出最终报告

### 断点续跑

```bash
python -m research_agent resume projects/新能源汽车行业_20260424
```

从上次中断的阶段继续。

### 查看项目状态

```bash
python -m research_agent status projects/新能源汽车行业_20260424
```

## 项目结构

```
research-agent/
├── pyproject.toml
├── .env.example
├── src/research_agent/
│   ├── __main__.py           # CLI 入口（new / resume / status）
│   ├── config.py             # 全局配置与常量
│   ├── state.py              # 10 阶段状态机 + JSON 持久化
│   ├── checkpoints.py        # 3 个人机确认检查点（Rich CLI）
│   ├── orchestrator.py       # 主控调度 + 异常兜底 + 重试
│   └── agents/
│       ├── strategist.py     # Agent1 战略规划（多轮对话）
│       ├── collector.py      # Agent2 数据搜集（源分层 + 按级采集）
│       ├── validator.py      # Agent3 信息验证（交叉验证 + JSON 反馈）
│       ├── analyst.py        # Agent4 深度分析
│       ├── formatter.py      # Agent5 排版交付
│       └── prompts/          # 各 Agent 的 system prompt（.md）
│           ├── strategist.md
│           ├── collector.md
│           ├── collector_round.md
│           ├── validator.md
│           ├── analyst.md
│           └── formatter.md
├── projects/                 # 调研项目数据（每次一个子目录）
│   └── {topic}_{date}/
│       ├── state.json
│       ├── 01_outline.md
│       ├── 02_sources_draft.md
│       ├── 02_sources_final.md
│       ├── 03_raw_data/
│       │   ├── round_1.md
│       │   ├── feedback_round_1.json
│       │   └── ...
│       ├── 03_validation_report.md
│       ├── 04_analysis.md
│       └── 05_final_report.md
└── tests/
    └── test_state_machine.py
```

## 各 Agent 职责

| Agent | 职责 | 工具权限 | 输出 |
|---|---|---|---|
| **Agent1 战略规划** | 多轮对话澄清目标/范围/交付物 → 生成提纲 | Read, Write | `01_outline.md` |
| **Agent2 数据搜集** | 信息源识别+S/A/B/D分层 + 按级采集 | Read, Write, WebSearch, WebFetch | `02_sources_draft.md` + `round_N.md` |
| **Agent3 信息验证** | 原文回查、EvidenceRecord、冲突检测、淘汰低质源 | ReadProjectSource, RecordProjectEvidence, Write | `feedback_round_N.json` + `03_validation_report.md` |
| **Agent4 深度分析** | 波特五力/SWOT/PEST + 定量分析 | Read, Write, WebSearch, WebFetch | `04_analysis.md` |
| **Agent5 排版交付** | 结构化排版 + 脚注 + 执行摘要 | Read, Write, WebSearch | `05_final_report.md` |

## 信息源分层标准

| 级别 | 定义 | 示例 |
|---|---|---|
| **S** | 一手权威原始数据 | 上市公司年报、统计局、央行、交易所公告 |
| **A** | 头部研究机构 | 艾瑞/IDC/Gartner、头部券商深度研报 |
| **B** | 专业财经媒体 | 36氪、虎嗅、财新、彭博、路透 |
| **D** | UGC/低可信（默认剔除） | 知乎回答、自媒体公众号 |

## 配置项

通过环境变量或 `.env` 文件配置：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LLM_BASE_URL` | `https://api.openai.com/v1` | 模型服务地址 |
| `LLM_API_KEY` | — | 必填（本地 Ollama 可填任意值） |
| `LLM_MODEL` | `gpt-4o` | 模型名称 |
| `LLM_TIMEOUT` | `120` | 请求超时（秒） |
| `LLM_MAX_RETRIES` | `3` | 失败重试次数 |
| `LLM_TEMPERATURE` | `0.7` | 生成温度 |
| `SEARCH_API_PROVIDER` | `duckduckgo` | 搜索引擎（duckduckgo / serpapi / tavily） |
| `SEARCH_API_KEY` | — | 搜索 API Key（DuckDuckGo 不需要） |
| `STRATEGIST_MAX_ROUNDS` | `5` | Agent1 多轮对话上限 |
| `MAX_COLLECT_ROUNDS` | `3` | Agent2↔3 采集-验证循环上限 |
| `SOURCE_DATA_DIR` | `.data/sources` | 材料目录、SQLite catalog 和不可变原文件存储 |
| `SOURCE_EMBEDDING_BASE_URL` | — | OpenAI 兼容 Embeddings API 地址；不配置则只使用离线检索 |
| `SOURCE_EMBEDDING_API_KEY` | — | Embeddings API Key |
| `SOURCE_EMBEDDING_MODEL` | — | 真实语义向量模型，例如多语言 embedding 模型 |
| `SOURCE_API_KEYS_JSON` | — | 可选项目 ACL，例如 `{"key-a":["project-a"],"admin":"*"}` |

## License

MIT
