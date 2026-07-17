# research-agent 项目的 claude_agent_sdk 依赖审计报告

**审计日期**: 2026-04-24  
**项目位置**: /Users/ruihang/WorkBuddy/20260423111718/research-agent/  
**SDK 最小版本**: 0.1.65+

---

## 1. 依赖声明总结

### 1.1 pyproject.toml 声明

**文件**: `/Users/ruihang/WorkBuddy/20260423111718/research-agent/pyproject.toml`

```toml
dependencies = [
    "claude-agent-sdk>=0.1.65",
    "anyio>=4.0",
    "python-dotenv>=1.0",
    "rich>=13.0",
    "pydantic>=2.0",
]
```

- **SDK包名**: claude-agent-sdk
- **最小版本**: 0.1.65
- **依赖来源**: 生产依赖（必须）

---

## 2. SDK 导入清单（5个Agent文件 + 1个测试）

### 2.1 Agent1 · 战略规划（strategist.py）

**文件路径**: `/Users/ruihang/WorkBuddy/20260423111718/research-agent/src/research_agent/agents/strategist.py`

**导入语句** (第 11-17 行):
```python
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
)
```

**导入类/函数**:
- `AssistantMessage` - 消息类型（行为：接收助手回复）
- `ClaudeAgentOptions` - 配置对象
- `ClaudeSDKClient` - 多轮对话客户端（关键能力）
- `ResultMessage` - 结果消息类型
- `TextBlock` - 文本块类型

**使用模式**:
- **第 94 行**: 初始化多轮对话客户端
  ```python
  async with ClaudeSDKClient(options=options) as client:
  ```
- **第 95 行**: 首轮查询
  ```python
  await client.query(first_prompt)
  ```
- **第 99 行**: 接收响应迭代
  ```python
  async for msg in client.receive_response():
  ```
- **第 117 行**: 轮次上限强制收敛查询
  ```python
  await client.query("已达到本次对话的轮次上限。请立即基于当前信息生成调研提纲...")
  ```
- **第 133 行**: 用户输入轮次
  ```python
  await client.query(user_input)
  ```

**ClaudeAgentOptions 配置** (第 72-79 行):
```python
options = ClaudeAgentOptions(
    system_prompt=system_prompt,
    model=config.DEFAULT_MODEL,           # 默认值: "claude-sonnet-4-5"
    allowed_tools=["Read", "Write"],      # 工具权限：仅文件操作
    permission_mode="acceptEdits",        # 自动接受编辑建议
    cwd=str(project_dir),                 # 工作目录：项目目录
    max_turns=40,                         # 单次query内部最大轮次
)
```

**SDK能力**: 
- 多轮对话驱动 (`ClaudeSDKClient`)
- 对话轮次无限制（用户与AI交替）
- 流式接收消息
- 自主文件编辑（写 outline.md）

---

### 2.2 Agent2 · 数据搜集（collector.py）

**文件路径**: `/Users/ruihang/WorkBuddy/20260423111718/research-agent/src/research_agent/agents/collector.py`

**导入语句** (第 14-20 行):
```python
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)
```

**导入类/函数**:
- `AssistantMessage` - 消息类型
- `ClaudeAgentOptions` - 配置对象
- `ResultMessage` - 结果消息类型
- `TextBlock` - 文本块类型
- `query` - 单次查询函数（关键能力）

**使用模式 - 阶段 2-A: 信息源分层**

**_stream_print 辅助函数** (第 42-53 行):
```python
async def _stream_print(prompt: str, options: ClaudeAgentOptions) -> None:
    """执行一次 query 并流式打印 assistant 文本。"""
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    console.print(block.text, style="bright_green")
        elif isinstance(msg, ResultMessage):
            if msg.subtype != "success":
                console.print(f"[yellow]Agent2 结果异常：subtype={msg.subtype}[/yellow]")
```

**ClaudeAgentOptions 配置 - 阶段 2-A** (第 103-110 行):
```python
options = ClaudeAgentOptions(
    system_prompt=system_prompt,
    model=config.DEFAULT_MODEL,
    allowed_tools=["Read", "Write", "WebSearch"],     # +WebSearch权限
    permission_mode="acceptEdits",
    cwd=str(state.project_dir),
    max_turns=25,                                      # 降低轮次限制
)
```

**调用点** (第 125 行):
```python
await _stream_print(user_prompt, options)
```

**使用模式 - 阶段 2-B: 按级采集**

**ClaudeAgentOptions 配置 - 阶段 2-B** (第 204-211 行):
```python
options = ClaudeAgentOptions(
    system_prompt=system_prompt,
    model=config.DEFAULT_MODEL,
    allowed_tools=["Read", "Write", "WebSearch", "WebFetch"],  # +WebFetch权限
    permission_mode="acceptEdits",
    cwd=str(state.project_dir),
    max_turns=40,                                     # 提升轮次限制
)
```

**调用点** (第 224 行):
```python
await _stream_print(user_prompt, options)
```

**SDK能力**:
- 单次查询 (`query()` 函数)
- 流式消息接收
- 两个工具权限变体：
  - 阶段 2-A: Read, Write, WebSearch (信息源识别)
  - 阶段 2-B: Read, Write, WebSearch, WebFetch (实际采集)

---

### 2.3 Agent3 · 信息验证（validator.py）

**文件路径**: `/Users/ruihang/WorkBuddy/20260423111718/research-agent/src/research_agent/agents/validator.py`

**导入语句** (第 21-27 行):
```python
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)
```

**导入类/函数**: 同 Agent2

**使用模式**:

**_stream_print 辅助函数** (第 81-91 行):
```python
async def _stream_print(prompt: str, options: ClaudeAgentOptions) -> None:
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    console.print(block.text, style="bright_yellow")
        elif isinstance(msg, ResultMessage):
            if msg.subtype != "success":
                console.print(f"[yellow]Agent3 结果异常：subtype={msg.subtype}[/yellow]")
```

**ClaudeAgentOptions 配置** (第 170-177 行):
```python
options = ClaudeAgentOptions(
    system_prompt=system_prompt,
    model=config.DEFAULT_MODEL,
    allowed_tools=["Read", "Write"],                  # 仅文件操作
    permission_mode="acceptEdits",
    cwd=str(state.project_dir),
    max_turns=30,
)
```

**调用点** (第 188 行):
```python
await _stream_print(user_prompt, options)
```

**设计要点** (文件注释第 1-13 行):
```
- 用 `query()` 单次执行（不需多轮对话）
- JSON 输出做严格校验（pydantic 模型），不合法就抛错由 Orchestrator 兜底
```

**SDK能力**:
- 单次查询 (`query()`)
- 流式输出处理
- 结构化 JSON 验证

---

### 2.4 Agent4 · 深度分析（analyst.py）

**文件路径**: `/Users/ruihang/WorkBuddy/20260423111718/research-agent/src/research_agent/agents/analyst.py`

**导入语句** (第 11-17 行):
```python
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)
```

**使用模式**:

**_stream_print 辅助函数** (第 34-44 行):
```python
async def _stream_print(prompt: str, options: ClaudeAgentOptions) -> None:
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    console.print(block.text, style="bright_blue")
        elif isinstance(msg, ResultMessage):
            if msg.subtype != "success":
                console.print(f"[yellow]Agent4 结果异常：subtype={msg.subtype}[/yellow]")
```

**ClaudeAgentOptions 配置** (第 102-109 行):
```python
options = ClaudeAgentOptions(
    system_prompt=system_prompt,
    model=config.DEFAULT_MODEL,
    allowed_tools=["Read", "Write", "WebSearch", "WebFetch"],
    permission_mode="acceptEdits",
    cwd=str(state.project_dir),
    max_turns=50,  # 分析可能需要较多步骤
)
```

**调用点** (第 122 行):
```python
await _stream_print(user_prompt, options)
```

**SDK能力**:
- 单次查询 (`query()`)
- 高轮次上限 (50轮) 用于复杂分析
- 完整工具套件 (Read, Write, WebSearch, WebFetch)

---

### 2.5 Agent5 · 排版交付（formatter.py）

**文件路径**: `/Users/ruihang/WorkBuddy/20260423111718/research-agent/src/research_agent/agents/formatter.py`

**导入语句** (第 11-17 行):
```python
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    query,
)
```

**使用模式**:

**_stream_print 辅助函数** (第 34-44 行):
```python
async def _stream_print(prompt: str, options: ClaudeAgentOptions) -> None:
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    console.print(block.text, style="bright_white")
        elif isinstance(msg, ResultMessage):
            if msg.subtype != "success":
                console.print(f"[yellow]Agent5 结果异常：subtype={msg.subtype}[/yellow]")
```

**ClaudeAgentOptions 配置** (第 96-103 行):
```python
options = ClaudeAgentOptions(
    system_prompt=system_prompt,
    model=config.DEFAULT_MODEL,
    allowed_tools=["Read", "Write", "WebSearch"],
    permission_mode="acceptEdits",
    cwd=str(state.project_dir),
    max_turns=40,
)
```

**调用点** (第 115 行):
```python
await _stream_print(user_prompt, options)
```

**SDK能力**:
- 单次查询 (`query()`)
- 排版和文档生成

---

### 2.6 测试文件（test_state_machine.py）

**文件路径**: `/Users/ruihang/WorkBuddy/20260423111718/research-agent/tests/test_state_machine.py`

**使用方式** (第 10-17 行):
```python
# 注入 claude_agent_sdk stub (避免 import 报错)
sdk_stub = types.ModuleType("claude_agent_sdk")
for name in [
    "AssistantMessage", "ClaudeAgentOptions", "ClaudeSDKClient",
    "ResultMessage", "TextBlock", "query",
]:
    setattr(sdk_stub, name, type(name, (), {}))
sys.modules["claude_agent_sdk"] = sdk_stub
```

**说明**: 测试中通过 stub 模拟 SDK 以避免 API 调用，仅验证状态机逻辑

---

## 3. 模型配置分析

### 3.1 config.py 硬编码检查

**文件**: `/Users/ruihang/WorkBuddy/20260423111718/research-agent/src/research_agent/config.py`

**第 12 行**:
```python
DEFAULT_MODEL: str = os.getenv("RESEARCH_AGENT_MODEL", "claude-sonnet-4-5")
```

**配置特性**:
- **默认模型**: `claude-sonnet-4-5`
- **可配置**: 通过环境变量 `RESEARCH_AGENT_MODEL` 覆盖
- **使用范围**: 所有 5 个 Agent 都引用 `config.DEFAULT_MODEL`

**所有模型使用点**:
1. strategist.py 第 74 行: `model=config.DEFAULT_MODEL`
2. collector.py 第 105 行 (阶段 2-A): `model=config.DEFAULT_MODEL`
3. collector.py 第 206 行 (阶段 2-B): `model=config.DEFAULT_MODEL`
4. validator.py 第 172 行: `model=config.DEFAULT_MODEL`
5. analyst.py 第 104 行: `model=config.DEFAULT_MODEL`
6. formatter.py 第 98 行: `model=config.DEFAULT_MODEL`

**模型统一性**: 
- 所有 Agent 使用同一模型
- 无 Agent 级别的模型差异化
- 便于统一升级或实验对比

---

## 4. SDK 能力使用统计

### 4.1 API 调用模式

| 能力 | 使用方式 | 用途 | Agent |
|------|---------|------|-------|
| `ClaudeSDKClient` | 多轮对话客户端 (context manager) | 持续交互 + 状态管理 | strategist (Agent1) |
| `query()` 函数 | 单次查询 (异步迭代) | 独立任务执行 | collector, validator, analyst, formatter (Agent2-5) |

### 4.2 消息处理类型

| 类型 | 用途 | 使用 |
|------|------|------|
| `AssistantMessage` | 接收AI回复 | 所有 Agent - 流式打印 |
| `ResultMessage` | 任务完成标记 | 所有 Agent - 异常检查 |
| `TextBlock` | 文本内容块 | 所有 Agent - 消息体提取 |

### 4.3 工具权限分布

| Agent | allowed_tools | max_turns |
|-------|---------------|-----------|
| Agent1 (strategist) | ["Read", "Write"] | 40 |
| Agent2a (collector - 阶段 2-A) | ["Read", "Write", "WebSearch"] | 25 |
| Agent2b (collector - 阶段 2-B) | ["Read", "Write", "WebSearch", "WebFetch"] | 40 |
| Agent3 (validator) | ["Read", "Write"] | 30 |
| Agent4 (analyst) | ["Read", "Write", "WebSearch", "WebFetch"] | 50 |
| Agent5 (formatter) | ["Read", "Write", "WebSearch"] | 40 |

**权限模式**:
- 所有 Agent: 基础 Read + Write (文件操作)
- 采集类: +WebSearch 权限 (Agent2a, Agent2b, Agent4, Agent5)
- 采集执行: +WebFetch 权限 (Agent2b, Agent4)
- 验证类: 不需网络权限 (Agent3)

---

## 5. 直接 HTTP 调用检查

**搜索范围**: 所有 Python 文件

**检测内容**: 
- `import requests`
- `import urllib`
- `import httpx`
- `import aiohttp`
- `import http.client`

**检查结果**: **无任何检出**

**结论**: 
- 项目完全通过 SDK 进行 API 交互
- 无绕过 SDK 的直接 HTTP 调用
- SDK 中的 WebSearch/WebFetch 工具由 SDK 内部实现

---

## 6. 配置参数完整表

### 6.1 ClaudeAgentOptions 参数一览

所有 Agent 共用的参数模式:

```python
ClaudeAgentOptions(
    system_prompt=<agent_specific_prompt>,         # 从外部 .md 文件加载
    model=config.DEFAULT_MODEL,                    # "claude-sonnet-4-5" (可覆盖)
    allowed_tools=[...],                           # 根据 Agent 需求
    permission_mode="acceptEdits",                 # 统一设置
    cwd=str(state.project_dir or project_dir),     # 工作目录固定
    max_turns=<agent_specific>,                    # 25-50 之间
)
```

**权限模式**: `acceptEdits` (自动接受 AI 的文件编辑建议)

---

## 7. 依赖关系图

```
orchestrator.py
  ├── strategist.py (Agent1)
  │   └── from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions, ...
  ├── collector.py (Agent2)
  │   └── from claude_agent_sdk import query, ClaudeAgentOptions, ...
  ├── validator.py (Agent3)
  │   └── from claude_agent_sdk import query, ClaudeAgentOptions, ...
  ├── analyst.py (Agent4)
  │   └── from claude_agent_sdk import query, ClaudeAgentOptions, ...
  └── formatter.py (Agent5)
      └── from claude_agent_sdk import query, ClaudeAgentOptions, ...

config.py
  └── DEFAULT_MODEL = os.getenv("RESEARCH_AGENT_MODEL", "claude-sonnet-4-5")
      └── 被所有 5 个 Agent 引用
```

---

## 8. 关键发现总结

### 8.1 架构模式

1. **混合模式设计**: 
   - 1 个多轮对话 Agent (strategist)
   - 4 个单次查询 Agent (其他)

2. **工具权限分层**:
   - 文件操作 Agent: Read + Write
   - 采集类 Agent: +WebSearch +WebFetch
   - 验证类 Agent: 仅文件操作

3. **轮次限制合理**:
   - 分析最复杂: max_turns=50
   - 信息源分层最简: max_turns=25
   - 其他: max_turns=30-40

### 8.2 模型配置

- 单一模型集中管理 (`config.DEFAULT_MODEL`)
- 环境变量可覆盖 (`RESEARCH_AGENT_MODEL`)
- 默认值: claude-sonnet-4-5

### 8.3 SDK 依赖

- **最小版本**: 0.1.65
- **核心类/函数**: 
  - ClaudeSDKClient (多轮)
  - query (单次)
  - ClaudeAgentOptions
  - AssistantMessage, ResultMessage, TextBlock
- **无其他网络库**依赖

### 8.4 消息处理一致性

- 所有 Agent 使用相同的 `_stream_print()` 模式
- 统一的流式消息接收 + 错误检查
- 一致的输出样式 (rich Console)

---

## 9. 检查清单

- [x] pyproject.toml 依赖版本验证 (>=0.1.65)
- [x] 所有 SDK 导入文件确认 (5 个 Agent + 1 个测试)
- [x] 导入类/函数完整列举
- [x] 使用模式分类 (多轮 vs 单次)
- [x] ClaudeAgentOptions 参数文档化
- [x] 模型配置硬编码检查
- [x] 直接 HTTP 调用检查 (无)
- [x] 工具权限统计
- [x] 轮次限制一览

---

## 10. 建议和注意事项

### 10.1 版本管理
- 当前指定 >=0.1.65，建议定期检查 SDK 更新
- 如需锁定特定版本，改为 `claude-agent-sdk==0.1.65`

### 10.2 环境变量
- 使用 `RESEARCH_AGENT_MODEL` 可灵活切换模型
- 建议在 .env 文件中配置或 CI/CD pipeline 中指定

### 10.3 异常处理
- 所有 Agent 通过 `ResultMessage.subtype != "success"` 检查错误
- Orchestrator 负责统一的重试和异常恢复

### 10.4 工具权限审计
- Agent3 (validator) 故意不给 WebSearch 权限 - 设计合理
- Agent2/4/5 的 WebFetch 权限用于实际数据采集

