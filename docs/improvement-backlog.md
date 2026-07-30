# 改进待办清单

> 2026-07-28 代码审查产出。按性价比排序，序号即优先级建议。
> 本文档只记录问题与建议方案，不代表已实施。完成的条目请标记 `[x]` 并补上落地说明。

## 现状基线

- 代码量：约 8400 行 Python，零 Agent 框架依赖
- 分层：LLM 层（`llm/`）→ Agent Loop 层（`agent_loop/`）→ 工具层（`tools/`）→ 编排层（`orchestrator.py` + `state.py`），旁挂证据层（`sources/`，约 2000 行）与交付层（`report_*.py` + `skills/`）
- 核心设计优势：`sources/quality.py` 的确定性质量门 + `orchestrator._deterministic_convergence()`，模型的"已收敛"声明仅作建议，最终由持久化证据裁定。这是防幻觉的正确架构，后续改动不应削弱它。

## 进度

| 项 | 主题 | 状态 |
|---|---|---|
| 1 | 状态机两份实现 | ✅ 2026-07-28 |
| 2 | 后台任务无引用保管 + 重启无恢复 | ✅ 2026-07-28 |
| 3 | 执行日志仅存内存 | ✅ 2026-07-28 |
| 4 | 搜索 provider 死配置 | ✅ 2026-07-28 |
| 5 | Token 消耗不可见 | ✅ 2026-07-28 |
| 6 | `AgentSession` 缺重复错误保护 | ✅ 2026-07-28 |
| 7 | 流式 tool_call 排序键错误 | ✅ 2026-07-28 |
| 8 | Web 服务无认证 | ✅ 2026-07-28 |
| 9 | `build_runtime()` 连接生命周期混乱 | ✅ 2026-07-28 |
| 10 | 前端全量轮询 | 待处理（建议与 token 展示一并做） |
| 11 | Web 侧 Agent1 多轮对话缺失 | ✅ 2026-07-28 |

### 研究质量与 Agent 能力

该轨道与上面的工程问题并行，`R` 序号表示研究质量方向的优先级，不改变原有 1—11 项的编号。

| 项 | 主题 | 状态 |
|---|---|---|
| R1 | 研究要求由已有证据反推，存在完整性盲区 | 待处理 |
| R2 | Agent4 可绕过证据链引入新事实 | 待处理 |
| R3 | Agent2↔3 缺少结构化补研任务协议 | 待处理 |
| R4 | 缺少 Claim—Evidence 关系与报告引用覆盖门禁 | 待处理 |
| R5 | Agent 文件工具缺少项目目录强制隔离 | 待处理 |
| R6 | 缺少研究质量、稳定性与成本评测体系 | 待处理 |

---

## 1. 状态机有两份实现（结构性风险，最高优先级）

- [x] 已完成（2026-07-28）—— 见文末「已完成」区

**问题**

同一套 10 阶段流水线存在两份并行实现：

- `orchestrator.py:338` `_run_pipeline_inner()` —— CLI 用
- `web_app.py:449` `_run_state_machine()` —— Web 用

任何流程改动都要改两遍，漏一处就是行为不一致的 bug，且这类 bug 测试难以覆盖。2026-07-28 新增失败重试时已实际踩到：`_quality_gate_error()` 与 `state.clear_failure()` 都必须在两处分别植入。

**根因**

两者对检查点的处理方式不同 —— CLI 阻塞式 `input()` 等用户，Web 是 return 出去等下一个 HTTP 请求。

**建议方案**

把检查点抽象成注入的 `CheckpointHandler` 协议：CLI 传阻塞实现，Web 传"保存状态并退出"实现，流水线本体只保留一份。这是其余改动的地基 —— 在它统一之前，每加一个功能都要付两倍成本。

---

## 2. 后台任务无引用保管 + 服务重启后无恢复

- [x] 已完成（2026-07-28）—— 见文末「已完成」区

**问题 A**：`web_app.py:564` `asyncio.create_task(_run_until_pause(project_id))` 的返回值未被任何变量持有。CPython 事件循环只持弱引用，任务理论上可能在完成前被 GC 回收。

**问题 B**：服务在某个 Agent 执行中途被杀掉时，`state.json` 的 stage 停在运行态（如 `collecting_and_validating`）但没有失败标记，前端显示"已暂停"，用户需自己想起来点"继续"。

**建议方案**

- A：任务存入模块级 `set`，`add_done_callback` 中 `discard`
- B：启动时扫描项目目录，将 stage 处于运行态（非 checkpoint、非 `done`）的项目标记为中断并在界面提示

---

## 3. 执行日志仅存内存，重启即失

- [x] 已完成（2026-07-28）—— 见文末「已完成」区

**问题**

`web_app.py:77` `JOBS` 是模块级 dict，`web_app.py:223` 仅保留最后 300 条日志。服务重启后历史执行记录清零 —— 而这恰恰是排查上一次失败最需要的信息。

**建议方案**

按项目落盘 `run_log.jsonl`，读取时与内存日志合并。

---

## 4. `SEARCH_API_PROVIDER` / `SEARCH_API_KEY` 是死配置（会误导用户）

- [x] 已完成（2026-07-28）—— 见文末「已完成」区

**问题**

两个变量在 `config.py:23-24` 定义、在 `/api/config` 返回、在 README 承诺支持 SerpAPI 与 Tavily，但 `tools/builtins/web_search.py` 从头到尾只走 DuckDuckGo，两个变量在搜索代码里一次都没被读取。用户在设置页填了 Key 却完全不生效。

**建议方案**

倾向于补实现而非删配置：DuckDuckGo 无 Key 免费但限流严重、结果质量对行业研究偏弱，而搜索质量直接决定证据质量。若暂不实现，则必须从配置项、`/api/config` 响应和 README 中一并移除。

---

## 5. Token 消耗完全不可见

- [x] 已完成（2026-07-28）—— 见文末「已完成」区

**问题**

`llm/client.py:257` 已解析 `usage` 字段，但无任何地方聚合。单次调研中 Agent2 单轮 `max_turns=40`、Agent4 `max_turns=50`，3 轮采集验证的实际消耗可能达几十万 token，用户全程无感知。

2026-07-28 新增的失败重试会追加轮次预算，成倍放大开销，使这个盲区更值得优先补上。

**建议方案**

在 `state.json` 累计 `prompt_tokens` / `completion_tokens`，按阶段分组，工作台展示。

---

## 6. `AgentSession` 缺少 `run_agent` 已有的重复错误保护

- [x] 已完成（2026-07-28）—— 见文末「已完成」区

**问题**

`agent_loop/loop.py` 中 `run_agent()` 有 `repeated_errors` 计数，同一工具连续返回相同错误达 `max_repeated_tool_errors`（默认 3）次即抛 `AgentLoopStuckError` 止损。但 `loop.py:150` `AgentSession.get_response()` 完全没有这段逻辑 —— 工具反复报同样的错会一路烧到 `max_turns`。

**建议方案**

抽出共用的工具执行循环，两个入口共享同一份保护逻辑。

---

## 7. 流式 tool_call 拼接的排序键用错

- [x] 已完成（2026-07-28）—— 见文末「已完成」区

**问题**

`agent_loop/loop.py:286` 按 `id` 字符串排序组装 tool_calls，但 OpenAI 流式协议中的顺序标识是 `index`，`id` 只是随机串。多个并行 tool_call 时顺序可能错乱。

**建议方案**

累加器本身已用 `index` 作 key，排序改为按 key 取值即可：`sorted(tool_calls_acc.items(), key=lambda item: item[0])`。

---

## 8. Web 服务无任何认证

- [x] 已完成（2026-07-28）—— 见文末「已完成」区

**问题**

当前绑定 `127.0.0.1` 本机使用无风险，但 `web_app.py:906` 的 `--host` 是可配的。一旦改成 `0.0.0.0`，任何人都能读取全部调研数据、修改模型配置（含写入 `.env`）、删除项目。

**建议方案**

host 为非回环地址时强制要求 token，或至少在启动时输出明确的安全警告。

---

## 9. `build_runtime()` 连接生命周期管理混乱

- [x] 已完成（2026-07-28）—— 见文末「已完成」区

**问题**

`build_runtime()` 每次调用新开一个 SQLite 连接，在 6 个模块中被调用，生命周期管理方式各不相同：

- `agents/source_context.py`、`orchestrator._deterministic_convergence()` —— 显式 `repository.close()`
- `tools/builtins/project_sources.py` —— `lru_cache` 保持单例
- `web_app.py:47` —— 模块级长连接

WAL 模式下多连接可工作，但这种混合模式迟早出问题。

**建议方案**

统一为显式单例或依赖注入。

---

## 10. 前端 3 秒全量轮询

- [ ] 待处理（建议与第 5 项 token 展示一并实施，避免前端数据流改两次）

**问题**

`web_static/app.js` 每 3 秒拉取完整 project payload，含全部 300 条日志。单用户本地可接受，但浪费且日志无法增量更新。

**建议方案**

改为 SSE 推送，顺带解决日志增量问题。

---

## 11. Web 模式下 Agent1 多轮对话能力被绕过（功能缺口）

- [x] 已完成（2026-07-28）—— 见文末「已完成」区

**问题**

`web_app.py:377` 的 system prompt 明确写"不要向用户追问；根据主题和补充说明直接生成可审阅提纲"，而 CLI 走 `AgentSession` 最多 5 轮澄清。README 宣传的"多轮对话澄清需求"在 Web 上实际不存在，只能通过驳回提纲 + 填修改意见间接实现。

这是功能缺口而非 bug。

**建议方案**

若要补齐，需为 Web 增加提纲阶段的对话式检查点（新增一个 stage 或在 `notes` 中维护对话历史）。

---

## R1. 研究要求由已有证据反推，存在完整性盲区

- [ ] 待处理

**问题**

`orchestrator._deterministic_convergence()` 和 `_assert_delivery_ready()` 当前从已经存在且状态为 `SUPPORTED` 的 EvidenceRecord 中提取 `research_question_id`，再据此创建 `ResearchRequirement`。

这会形成循环定义：系统根据“已经找到了什么”决定“应该检查什么”。如果提纲要求回答某个关键问题，但采集阶段一条相关证据都没有找到，该问题可能不会进入 QualityGate 的要求集合，最终只能依赖 Agent3 的自由文本 `gap_list` 发现，无法由确定性门禁稳定阻断。

**建议方案**

- Agent1 在生成提纲时同步输出结构化 `research_requirements.json`，为每个研究问题分配稳定 ID。
- 每项要求至少包含 `question_id`、问题文本、是否必答、最低支持证据数、最低来源等级、是否要求数值证据。
- Agent2、Agent3、QualityGate、Agent4 和 Agent5 全程引用同一批稳定问题 ID。
- QualityGate 始终读取预定义要求全集，不再从已有 EvidenceRecord 反向生成要求。

**验收标准**

- 任意必答问题零证据时，质量门稳定返回 `NEEDS_MORE_RESEARCH` 或 `BLOCKED`。
- 删除某个问题的全部 EvidenceRecord 后，测试能够证明交付被阻断。
- 旧项目没有结构化要求时有明确迁移或兼容策略，不能静默按空要求放行。

---

## R2. Agent4 可绕过证据链引入新事实

- [ ] 待处理

**问题**

Agent4 的工具白名单仍包含 `WebSearch` 和 `WebFetch`。它可以在 Agent3 验证完成后搜索并引用新信息，但这些信息没有经过来源入库、版本化、原文定位、EvidenceRecord 和冲突检查，造成“采集验证阶段严格、分析阶段重新开口”的证据旁路。

**建议方案**

- 默认移除 Agent4 的 `WebSearch` / `WebFetch`，只允许读取已确认产物和项目材料库。
- Agent4 发现信息不足时输出结构化补研请求，将流程回退给 Agent2↔3，而不是直接把新网页写进分析。
- 如果确实需要保留分析阶段搜索，必须强制走 `CaptureProjectWebSource -> ReadProjectSource -> RecordProjectEvidence`，并重新执行 QualityGate。

**验收标准**

- Agent4 无法把未入库网页作为报告事实依据。
- 分析阶段产生新事实时，必须能追溯到有效 EvidenceRecord。
- 补研完成前不得进入 Agent5。

---

## R3. Agent2↔3 缺少结构化补研任务协议

- [ ] 待处理

**问题**

Agent3 当前通过 `feedback_round_N.json` 向下一轮 Agent2 反馈缺口，但缺少统一的任务状态、优先级、完成条件和证据回填字段。Agent2 需要从自由文本中重新理解任务，容易遗漏、重复搜索或把“找到网页”误判为“已经补齐证据”。

**建议方案**

新增结构化 `ResearchTask` 契约，至少包含：

- `task_id`、`question_id`、`task_type`、`priority`
- 目标时期、最低来源等级、所需独立来源数量
- 任务产生原因与可验证完成条件
- `pending / completed / blocked / waived` 状态
- 完成后关联的 `source_id`、`evidence_id` 和未完成原因

Agent3 负责创建和验收任务，Agent2 负责执行和回填；Orchestrator 根据未完成的高优先级任务决定是否继续下一轮。

**验收标准**

- 每个未解决缺口都有稳定 task ID，不依赖自然语言位置匹配。
- 同一任务重试不会重复创建来源或 EvidenceRecord。
- 高优先级必答任务未完成时，模型声明 `converged=true` 也不能放行。

---

## R4. 缺少 Claim—Evidence 关系与报告引用覆盖门禁

- [ ] 待处理

**问题**

当前已经有 EvidenceRecord，但分析报告中的结论仍主要由模型自由组织。系统能够证明“项目存在合格证据”，却不能稳定证明“报告里的每个重要事实和数字都由合格证据支持”，也缺少支持证据与反对证据的显式关系。

**建议方案**

- 新增结构化 Claim：`claim_id`、`question_id`、结论文本、重要性、状态和置信度。
- Claim 显式关联支持 Evidence、反对 Evidence、冲突状态和限制条件。
- Agent4 基于 Claim 生成分析；Agent5 保留 Claim 引用或转换为正式引用标记。
- 交付前增加 citation audit，检查重要事实、数字、预测假设和图表数据的证据覆盖。

**验收标准**

- 报告中的重要数字引用覆盖率为 100%。
- 任意重要 Claim 没有有效支持证据时阻断交付。
- Evidence 版本失效后，关联 Claim 自动变为待复核，不能继续作为有效结论。

---

## R5. Agent 文件工具缺少项目目录强制隔离

- [ ] 待处理

**问题**

Agent Loop 只会把相对路径解析到 `options.cwd`；如果模型传入绝对路径，Read/Write 工具会直接访问该路径，没有程序级 `project_dir` 边界。当前主要依赖 Prompt 约束，在处理外部网页和上传材料时无法构成可靠的提示注入防线。

这与第 8 项 Web 认证是两个不同边界：Web 认证控制“谁能调用系统”，工具隔离控制“Agent 被调用后能访问什么”。

**建议方案**

- ToolRegistry 执行文件工具时注入允许的根目录，而不是只做相对路径拼接。
- 使用 `resolve()` 后校验目标必须位于项目目录或显式只读目录内。
- Read 与 Write 分别配置权限；模板、Skill 等共享资源只读，项目目录按 Agent 职责授予写权限。
- 对越界、符号链接逃逸和路径穿越返回结构化拒绝结果并写入审计日志。

**验收标准**

- 绝对路径、`..`、符号链接均不能逃逸项目根目录。
- Agent2/4 读取恶意网页内容后，即使被诱导调用 Write，也不能修改项目外文件。
- 合法的项目产物读写和只读共享资源访问不受影响。

---

## R6. 缺少研究质量、稳定性与成本评测体系

- [ ] 待处理

**问题**

现有测试能验证状态机、材料处理、质量门和交付代码是否按预期运行，但还不能持续回答以下问题：换模型或改 Prompt 后，研究结论是否更准确、问题覆盖是否下降、引用是否仍有效、重复运行是否稳定、成本是否值得。

**建议方案**

建立三层评测：

1. 确定性工程测试：状态转换、断点恢复、证据失效、版本变化、幂等性、权限边界和报告渲染。
2. 研究质量基准：必答问题覆盖率、数字准确率、原文摘录一致率、引用有效率、矛盾发现率、来源等级分布和重要 Claim 证据覆盖率。
3. 稳定性与成本：同题多次运行的关键结论一致率、来源重合率、总 Token、阶段耗时、搜索次数、无效工具调用次数和每条有效证据成本。

基准结果应按模型、Prompt 版本和代码提交保存，进入 CI 或发布验收，避免只凭主观阅读判断升级效果。

**验收标准**

- 至少建立一组带标准答案和原文证据的固定题库。
- 每次模型、Prompt 或检索策略变更都能生成可比较的评测报告。
- 关键质量指标下降超过阈值时阻断发布，而不是只要求测试进程退出码为 0。

---

## 已完成

### Token 用量统计与首页展示（2026-07-28，对应第 5 项）

原问题：`LLMResponse.usage` 早已解析但无人聚合，一次调研可能消耗几十万 token，用户全程无感知。

采集链路——不改任何 Agent 签名。用 `ContextVar` 做隐式采集：`agent_loop` 在每次 LLM 响应后调 `token_usage.report()`，`orchestrator._safe_run()` 用 `collect_stage()` 上下文包住阶段执行，退出时把该阶段用量写盘。这样 5 个 Agent 一行代码都不用改，且新增 Agent 自动纳入统计。没有活动采集器时 `report()` 是空操作，单测直接调 Agent 也不会报错。

落地内容：

- 新增 `token_usage.py`：`TokenUsage`（累加，兼容 `prompt/completion` 与 `input/output` 两套字段命名，只给 `total` 时也不丢）、`collect_stage()` 上下文、`record_stage_usage()`、`aggregate()`
- 流式调用此前拿不到 usage：`_build_body` 加 `stream_options.include_usage`，`StreamChunk` 新增 `usage` 字段，`_parse_sse_chunk` 兼容"最后一个 chunk 只带 usage、choices 为空"的形态。不支持该扩展的服务会忽略它，此时只是统计缺失
- 阶段名归并：`Agent2·采集第3轮` → `Agent2·采集`，否则轮次一多明细会被同一 Agent 的多轮记录淹没
- 双层存储：总量与阶段分布进 `state.json`；日粒度单独存 `token_usage.jsonl`（热力图需按天回看，而 `state.json` 每次保存整体重写）
- 阶段失败也记账——失败的调用同样花了钱，有测试守住
- 新增 `GET /api/usage`：跨项目汇总，含总量、峰值单日、调用次数、连续天数、阶段分布、项目排行、日粒度序列
- 首页新增「Token 用量」版块：5 个统计卡 + 52×7 热力图（每日/每周/累计三视图切换）+ 按阶段分布 + 消耗最多的项目
- 连续天数从今天**或昨天**起算：调研常跨夜运行，若今天还没开始就归零会显得跳变

验证：新增 33 项测试（20 项 `token_usage`、3 项 agent_loop 上报、4 项 `/api/usage`、其余为字段兼容与边界）。用 node 单独验证了热力图网格数学（52 列 × 7 行 = 364 格，首列必为周一，末列包含今天）与数字格式化（19 亿 / 1.8 亿 / 7.3 万）。另造了一份 120 天的仿真数据端到端验证聚合结果，确认后已删除。

已知限制：老项目（本次上线前创建）没有统计数据，会安静跳过，首页只统计新运行的调研。热力图颜色分档用相对阈值（占峰值的 10%/30%/60%），单日数据量差异极大时对比度可能偏低。

---

### Web 侧 Agent1 需求澄清（2026-07-28，对应第 11 项）

原问题：Web 的 system prompt 明确写"不要向用户追问"，README 宣传的多轮澄清在 Web 上不存在，只能靠驳回提纲间接实现。

关键设计——用工具调用代替阻塞提问。CLI 能用 `Prompt.ask` 阻塞等输入，Web 不能。所以给 Agent1 挂一个 `AskUser` 工具：调用它即表示需要用户回答，本次执行随即结束，状态机挂起到新增的 `AWAIT_CLARIFICATION` 阶段；用户提交答案后重新执行 Agent1，历史问答通过 prompt 回灌。这样 Agent1 自己决定是否需要提问，而不是由代码写死。

落地内容：

- `Stage` 新增 `AWAIT_CLARIFICATION`；它既不是 `is_checkpoint`（不是产物审批）也不是 `is_agent_running`（是等用户，重启不该标记为中断），两条语义都有测试守住
- `ProjectState` 新增 `clarification: list[dict]` 持久化问答历史
- `orchestrator` 新增 `StrategistOutcome`（提纲或问题的二选一返回）、`_as_strategist_outcome()`（兼容直接返回 `Path` 的宿主）、`_append_clarification()`
- `PipelineHost` 协议新增 `resolve_clarification()`：返回 `None` 表示挂起（Web），返回列表表示已就地拿到答案（CLI 不会走到这里）
- `ToolRegistry` 新增 `subset()`：派生只含指定工具的注册表，用来挂载临时的 `AskUser` 而不污染全局注册表
- 澄清预算上限 9 条问答，用尽后不再提供 `AskUser`，强制 Agent1 用默认值收敛；驳回重做时也不再提问（用户已通过修改意见表达了意图）
- 提纲优先：若 Agent1 既提问又写了提纲，以提纲为准，说明它已能收敛
- 新增 `POST /api/projects/{id}/clarification`，支持提交答案或 `skip` 全部用默认值
- 工作台新增澄清面板（问题列表 + 历史问答折叠区 + 「提交回答」/「全部用默认值」）；流水线把该阶段归入「战略规划」步；研究首页状态显示「待澄清」并计入"等待审批"概览
- 留空的回答显式写成"（用户未回答，请用合理默认值）"，让 Agent1 能区分"未回答"和"空字符串"
- 答案数量少于问题数量时自动补齐，不抛 `IndexError`

验证：新增 17 项测试（6 项状态机、7 项 Web、2 项 registry subset、2 项阶段语义）。实测端到端流程：挂起在 `await_clarification` → 提交部分答案 → 回到 `planning`，留空项正确标注默认值。

---

### 材料运行时统一为共享连接（2026-07-28，对应第 9 项）

原问题：`build_runtime()` 每次调用新开 SQLite 连接，6 个模块各自决定是否 `close()`——有的显式关、有的用 `lru_cache` 保持单例、Web 层是模块级长连接。混合模式让"谁负责关闭"无法推理。

落地内容：

- 新增 `sources/runtime.py`：按数据目录缓存 `(service, queue)`，全进程共享同一条连接
  - `get_runtime()` / `get_service()` —— 取（必要时创建）
  - `create_runtime()` —— 绕过缓存新建独立连接（备份等场景）
  - `reset_runtime(data_dir=None)` —— 关闭并清空缓存，测试切换 tmp 目录时调用
- 路径先 `resolve()` 再做 key，避免 `a/sources` 与 `a/sources/.` 建两条连接
- `build_runtime()` 保留为兼容入口，内部转调 `get_runtime()`
- 移除全部 5 处 `repository.close()` 调用点与 `project_sources` 的 `lru_cache`——调用方不再负责关闭，连接随进程生命周期存在
- `reset_runtime()` 容忍连接已被外部关闭的情况，不因此阻止缓存清理

过程中发现的真实问题：改成共享连接后有 7 个测试报 `Cannot operate on a closed database`。原因是这些测试通过 `_deterministic_convergence()` 等函数触发 `close()`，之后同一条共享连接被其他代码继续使用。这恰好证明了原设计的隐患——在旧代码里各调用方碰巧用的是各自的独立连接才没暴露；一旦共享，"谁都可以关"就变成了真实故障。

验证：新增 10 项测试，包括同目录共享同一实例、等价路径不重复建连、`create_runtime` 绕过缓存且关闭它不影响共享连接、工具层与编排层落到同一条连接（否则证据写入与门禁读取会错位）。

---

### 工作台访问控制（2026-07-28，对应第 8 项）

原问题：`--host` 可配为 `0.0.0.0`，一旦暴露，任何人都能读取全部调研数据、修改模型配置（含写入 `.env`）、删除项目。

落地内容：

- 新增 `WEB_AUTH_TOKEN` 配置项。**留空时不做任何校验**——本机使用不应被打扰
- 新增 HTTP 中间件：配置了令牌后校验每个请求，令牌可来自 `X-Auth-Token` 头、`?token=` 查询参数或 cookie。用 `secrets.compare_digest` 比较，避免计时侧信道
- 带 `?token=` 首访成功后写入 `HttpOnly` + `SameSite=strict` cookie，之后浏览器无需再带参数
- `main()` 新增启动前检查：未设令牌却绑定非回环地址时**拒绝启动**并给出三个明确选项（设令牌 / 改回环 / `--allow-insecure-host` 显式放弃保护），而不是静默暴露
- 新增 `_is_loopback()`：识别 `127.0.0.1` / `localhost` / `::1` 等回环地址
- README 增加访问控制说明与配置项

设计取舍：选择「默认宽松 + 暴露时强制」而非「一律要求令牌」。本地单用户是主要场景，强制令牌会显著增加日常摩擦；而真正的风险只在绑定公开地址时出现，此时用启动失败来阻止误操作最有效。

验证：新增 10 项测试——无令牌时开放、缺令牌 401、错令牌 401、头部令牌通过、查询参数令牌写 cookie 且后续请求靠 cookie 通过、写操作（含 `PUT /api/config/model` 与 `DELETE /api/projects/…`）同受保护、回环地址识别、无令牌公开绑定拒绝启动、有令牌可公开绑定、`--allow-insecure-host` 可覆盖。实测 `--host 0.0.0.0` 确实被拒绝并打印指引。

---

### Agent Loop 两处修复（2026-07-28，对应第 6、7 项）

**第 6 项**——`AgentSession` 缺少 `run_agent` 已有的重复错误保护，工具反复报同样的错会一路烧到 `max_turns`。

- 抽出 `_ToolErrorTracker` 类与 `_run_tool_calls()` 辅助函数，`run_agent` 与 `AgentSession` 共用同一份保护逻辑，从结构上排除两个入口保护强度不一致
- `AgentSession` 的 tracker 是实例级的，错误计数**跨对话轮次累计**——否则模型可以通过换轮次绕过阈值
- 保留原有语义：某工具成功一次即清空它此前的错误计数（避免偶发错误累积成误杀），且清空只作用于该工具

**第 7 项**——流式 tool_call 按 `id` 字符串排序，但 OpenAI 协议的顺序标识是 `index`，`id` 只是随机串，并行 tool_call 时顺序会错乱。

- 改为 `sorted(tool_calls_acc.items(), key=lambda item: item[0])`，即按 `index` 排序，并加注释说明原因

验证：新增 7 项测试。其中排序那条特意让 `id` 的字典序与 `index` 顺序相反（`index=0` 对应 `id="zzz"`，`index=1` 对应 `id="aaa"`），旧实现下必然失败。另有一条断言 `AgentSession` 在第 2 次相同错误处就停下（`client.calls == 2`），而不是烧到 `max_turns=10`。

---

### 多搜索源落地（2026-07-28，对应第 4 项）

原问题：`SEARCH_API_PROVIDER` / `SEARCH_API_KEY` 在配置和 README 里存在，但搜索代码只走 DuckDuckGo，两个变量从未被读取。

采用「补实现」而非「删配置」，理由是搜索质量直接决定证据质量。

落地内容：

- `tools/builtins/web_search.py` 重写为 provider 路由：`duckduckgo`（默认，双通道：ddgs 库 → HTML 接口兜底）、`serpapi`、`tavily`
- 需要 Key 的 provider 未配置 Key 时**明确报错而非静默降级**——静默换源会让用户误以为配置生效
- 未知 provider 返回明确错误并列出受支持值
- 统一 `_format_results()` 输出格式，三个 provider 的结果结构一致
- 新增 `PUT /api/config/search`（保存 provider + Key，写入 `.env`）与 `POST /api/config/search/test`（发一次真实搜索验证连通性，失败透传为 400）
- `/api/config` 新增 `search_providers`、`search_key_required` 字段
- 设置页第 2 区从只读展示改为可编辑：provider 下拉 + Key 输入 + 「测试搜索」/「保存搜索配置」按钮；切换 provider 时动态提示 Key 是否必需
- README 配置表标注哪些 provider 必填 Key

验证：新增 13 项测试（7 项 provider 路由 + 6 项配置 API）。用 `httpx.MockTransport` 断言 SerpAPI 的 Key 进了 query、Tavily 的 Key 进了 `Authorization` 头、401 被转成 `Error:` 前缀、缺 Key 时 DuckDuckGo 通道确实**没有**被调用。服务重启后实测 `POST /api/config/search/test` 走通 DuckDuckGo 并返回真实结果预览。

---

### 执行日志持久化（2026-07-28，对应第 3 项）

原问题：`JOBS` 是模块级 dict，服务重启后历史执行记录清零，而这正是排查上次失败最需要的信息。

落地内容：

- 新增 `run_log.py`：`append()` / `read()` / `log_path()`，按项目写 `run_log.jsonl`
- 选 JSONL 而非 JSON 数组，使追加写入无需读取重写整个文件
- `read()` 跳过无法解析的行——进程在写入中途被杀会留下截断的尾行，不应因此丢掉前面所有日志
- 超过 `_COMPACT_THRESHOLD`（1200 行）时压实到最近 300 条，避免长期运行无限增长
- 写盘失败（`OSError`）静默忽略：日志是辅助信息，不应中断调研
- `web_app._log()` 同时写内存与磁盘；`web_app._job()` 首次访问某项目时从磁盘回填日志与最后一条消息

验证：新增 6 项 `run_log` 单元测试（往返、缺失文件、自动建目录、截断行容错、limit、压实）+ 1 项 Web 集成测试（写两条日志 → `JOBS.clear()` 模拟重启 → `/api/projects/{id}` 仍返回完整日志与 `job_message`）。实测重启后日志正确回读。

---

### 后台任务引用保管与中断恢复（2026-07-28，对应第 2 项）

原问题 A：`asyncio.create_task()` 返回值未被持有，事件循环只持弱引用，任务可能在完成前被 GC 回收。
原问题 B：服务在 Agent 执行中途被杀时，项目停在运行态但无失败标记，界面显示"已暂停"，用户需自己想起来点继续。

落地内容：

- 新增模块级 `BACKGROUND_TASKS: set`，`_schedule()` 存入强引用并用 `add_done_callback(BACKGROUND_TASKS.discard)` 在完成后摘除
- `Stage` 新增 `is_agent_running` 属性，标识五个「Agent 正在执行」的阶段（`PLANNING` / `SOURCING` / `COLLECTING_AND_VALIDATING` / `ANALYZING` / `FORMATTING`）
- 新增 `_recover_interrupted_projects()`：扫描项目目录，把停在运行态且无失败标记的项目标记为可重试，附带说明"上次运行被中断"
- 已有失败原因的项目不被覆盖（避免用泛化文案盖掉具体错误）；检查点与 `DONE` 阶段不受影响
- 通过 FastAPI `lifespan` 上下文管理器在启动时执行（未用已废弃的 `@app.on_event("startup")`）

验证：新增 4 项测试——中断项目被标记且检查点/已完成项目不受影响、已有失败原因不被覆盖、`_schedule` 持有强引用且完成后自动摘除。实测新建停在 `collecting_and_validating` 的项目，启动扫描后正确变为 `can_retry`。

---

### 状态机统一为单一实现（2026-07-28，对应第 1 项）

原问题：CLI 与 Web 各持一份 10 阶段流水线实现，任何流程改动需改两遍。

落地内容：

- `orchestrator.py` 新增 `PipelineHost` 协议（`Protocol`），把 CLI / Web 的全部行为差异收敛为四个方法：`run_strategist()`、`resolve_checkpoint()`、`log()`、`announce_done()`
- 新增 `CheckpointSpec` + `CHECKPOINT_SPECS` + `checkpoint_for()`：三个检查点的 stage / 产物 key / 前端标题 / CLI 面板标题 / 文件名集中定义，成为唯一事实来源
- 新增 `CheckpointDecision`（`APPROVED` / `REJECTED` / `PAUSE`）与 `CheckpointResult`。`PAUSE` 是消除两份实现的关键——Web 宿主返回 `PAUSE`，状态机退出，由 HTTP 审批接口推进阶段后重新调度；CLI 宿主永不返回 `PAUSE`，阻塞询问用户
- 状态机唯一实现 `run_state_machine(state, host)`，采集验证循环抽出为 `_run_collect_validate_loop()`
- `CliPipelineHost` 位于 `orchestrator.py`（Agent1 多轮对话 + `checkpoints.ask_approval` 阻塞询问 + 完成横幅）；`WebPipelineHost` 位于 `web_app.py`（Agent1 单次生成 + 检查点 `PAUSE` + 日志写入 `JOBS`）
- 删除 `web_app._run_state_machine()`（约 110 行重复逻辑），`_run_until_pause()` 改调 `orchestrator.run_state_machine`
- `web_app._checkpoint_file()` 由 8 行硬编码分支改为从 `checkpoint_for()` 派生，前端 payload 不变
- 原 `_run_pipeline_inner()` 的递归自调用（驳回时 `return await _run_pipeline_inner(state)`）改为 `while True` + `continue`，消除递归栈增长
- `_run_web_strategist()` 签名改为接收显式 `feedback` 参数，不再自行 `notes.pop`，与 CLI 侧 `strategist.run_strategist` 对齐

验证：

- 测试 97 → 108（新增 11 项）：检查点规格与 `Stage.is_checkpoint` 一致性断言（防止两处定义漂移）、`PAUSE` 宿主在首个检查点挂起、驳回后反馈透传给 Agent1 并重跑、通过后进入 Agent2、CLI 风格宿主用同一状态机推到 `DONE`、驳回终稿清空采集进度、`run_pipeline` 默认使用 `CliPipelineHost`、`CliPipelineHost` 决策映射、Web 侧断言 `web_app` 不再持有 `_run_state_machine`、三个检查点的 API payload 均由统一规格派生
- 行数：`orchestrator.py` 514 → 661，`web_app.py` 915 → 825（净减 55 行，且重复逻辑归零）
- 重启服务实测：落地页 / `/research` / `/workspace` 均 200，`/api/projects` 序列化正常

副作用发现：重启后 `SK海力士股价预测` 项目从 `failed=False` 变为 `failed=True`，`failed_stage=Agent5·排版交付`，原因是 `05_final_report.layout-warnings.log` 记录了 LaTeX Overfull hbox 警告导致排版交付判定失败。这是 7 月 21 日那次真实运行就已存在的失败，此前因失败状态只存内存而不可见；现在失败已持久化，所以正确地显示出来了。该项目可直接点「重试」。

---

### 失败重试与项目删除（2026-07-28）

原问题：审查失败后调研直接失败退出，不支持重试；工作台不支持删除失败项目。

落地内容：

- `state.py` 新增持久化字段 `failed_stage` / `last_error` / `retry_count`，配 `mark_failure()` / `clear_failure()`
- `orchestrator.py` 新增 `QualityGateError`、`can_retry()` / `retry_blocked_reason()` / `prepare_retry()`；重试保留全部既有产物，仅复位失败阶段
- 复位规则：采集验证轮次用尽 → 追加轮次预算从下一轮继续；交付证据门槛阻断 → 回退采集验证；其他阶段报错 → 原地重跑
- 轮次预算不设硬上限（避免死锁后移），超过 `RETRY_ROUND_SOFT_LIMIT`（10）轮时提示模型开销
- API：`POST /api/projects/{id}/retry`、`DELETE /api/projects/{id}`
- CLI：`retry`（带 `--extra-rounds`）、`delete -y`
- UI：工作区失败面板（失败阶段 + 原始错误 + 证据门槛逐条原因）、首页表格行内重试/删除按钮、概览新增"失败待处理"
- 测试：新增 22 项，覆盖追加轮次后真实跑到第 2 轮、交付门槛回退、Agent 报错后重试成功、删除的运行中拦截与路径穿越拦截、驳回清除失败标记

遗留：`_run_until_pause` 中 `DeliveryBlockedError` 分支仍设 `status = "idle"`，但该路径内部已通过 `_assert_delivery_ready` 调用 `mark_failure`，因此界面仍显示为可重试的失败态。若需将"交付暂停"与"运行失败"在界面上区分，需单独增加状态位。
