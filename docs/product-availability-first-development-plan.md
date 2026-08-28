# 产品可用性优先开发计划

## 1. 背景与目标

当前研究流程将正文、结论台账、图表和 PDF 等产物放在同一条硬门禁链路中。只要任一辅助产物校验失败，整个项目就会停止，导致已经具备阅读价值且引用有效的分析报告无法交付。

本次开发目标是调整门禁边界：

> 核心正文的事实与引用继续严格校验；结论台账、图表、排版等辅助能力失败时自动降级，尽可能交付可阅读、可追溯的报告。

项目状态调整为：

- `done`：完整交付。
- `done_degraded`：报告已交付，但部分结论台账、图表或文件格式发生降级。
- `failed`：核心正文无法安全交付。
- `paused`：限流、额度耗尽等情况需要等待外部条件恢复。

## 2. 实施原则

### 2.1 继续硬拦截的问题

以下问题可能让用户看到错误事实，必须阻断：

- 正文引用不存在。
- 引用证据状态不是 `SUPPORTED`。
- 引用中的来源版本、Evidence ID 或 chunk 定位与证据库不一致。
- Agent5 修改、遗漏或重排 Agent4 正文。
- 核心分析报告缺失或无法解析。
- API 密钥、模型或 Base URL 配置错误，导致模型无法调用。

### 2.2 自动降级的问题

以下问题只影响辅助能力，不应阻断整份报告：

- claim 文本无法与正文匹配。
- claim 的 `question_id` 分类不一致。
- 部分研究问题没有 claim 覆盖。
- `04_claims.json` 格式、枚举或重复 ID 错误。
- 图表数值无法完成溯源。
- 图表类型不支持或图表渲染失败。
- PDF 生成失败。
- 个别来源采集失败。
- 个别研究问题证据不足。

## 3. 第一阶段：建立回归基线

**预计工作量：0.5 个开发日。**

先补充失败场景测试，不修改业务行为：

1. 多条 claim 无法匹配正文。
2. claim 与 evidence 的 `question_id` 不一致。
3. claim 引用了不存在的 evidence。
4. `04_claims.json` JSON 损坏。
5. 图表数值无法在正文或证据中找到。
6. PDF 渲染异常。
7. 正文引用了不存在的证据。
8. Agent5 最终报告丢失 Agent4 正文。

验收标准：

- 每个场景都能稳定复现。
- 明确记录当前失败阶段和错误类型。
- 保存当前测试结果，作为修改后的对照基线。

主要测试文件：

- `tests/test_claims_gate.py`
- `tests/test_agent4_evidence_boundary.py`
- `tests/test_chart_v2.py`

## 4. 第二阶段：Agent4 结论台账降级

**预计工作量：1 个开发日。**

### 4.1 拆分台账校验结果

将 claim 校验结果拆分为：

```python
hard_errors: list[str]
warnings: list[str]
dropped_claim_ids: list[str]
```

处理规则：

| 问题 | 处理方式 |
| --- | --- |
| claim 文本不在正文 | 删除该 claim，记录 warning |
| `question_id` 不一致 | 保留 claim，记录 warning |
| `question_id` 不存在 | 删除该 claim，记录 warning |
| 必答问题没有 claim 覆盖 | 记录 warning |
| evidence ID 不存在 | 从 claim 删除；critical claim 无有效证据时删除该 claim |
| evidence 状态不是 `SUPPORTED` | 从 claim 删除；critical claim 无有效证据时删除该 claim |
| JSON 枚举错误 | 确定性归一化 |
| JSON 无法解析 | 尝试一次修复；仍失败则关闭 claims 能力 |
| 正文引用错误 | hard error，阻断流程 |

### 4.2 台账不再阻断正文交付

Agent4 阶段调整为：

1. 完成正文引用审计。
2. 尝试加载并清洗 claims。
3. 清洗成功时保存有效 claims。
4. 清洗失败时记录 `claims_disabled=true`。
5. 无论 claims 是否完整，都继续进入 Agent5。
6. 只有正文引用审计等真实性错误才停止流程。

涉及文件：

- `src/research_agent/sources/claims.py`
- `src/research_agent/orchestrator.py`
- `src/research_agent/agents/analyst.py`

### 4.3 限制模型修复次数

结论台账最多进行一次定向修复。修复失败后：

- 不重新生成或修改 Agent4 正文。
- 不继续调用模型重复修复台账。
- 进入 `claims_disabled` 降级模式。
- 保留原始台账供排查，不覆盖原文件。
- 将清洗后的结果写入独立文件，例如 `04_claims_sanitized.json`。

## 5. 第三阶段：Agent5 图表与交付降级

**预计工作量：1 个开发日。**

### 5.1 图表降级

图表数值验证顺序：

1. 匹配有效 claim。
2. 匹配 claim 关联的 `SUPPORTED` evidence。
3. 匹配已经通过引用审计的 Agent4 正文。
4. 均无法匹配时删除该图表。

失败后的处理方式：

- 有可靠结构化数据时，回退为 Markdown/HTML 表格。
- 没有可靠数据时，删除图表并保留正文。
- 不再因为“必需图”失败而终止整份报告。

涉及文件：

- `src/research_agent/chart_provenance.py`
- `src/research_agent/report_formatting.py`
- `src/research_agent/agents/formatter.py`

### 5.2 多格式独立交付

产物按以下优先级独立生成：

1. Markdown
2. HTML
3. PDF

降级规则：

- Markdown 成功、HTML 失败：仍然完成交付。
- HTML 成功、PDF 失败：交付 HTML 和 Markdown。
- PDF 失败不能将项目状态设置为 `failed`。
- 三种格式全部失败时才视为交付失败。

### 5.3 Agent5 保留的硬门禁

以下问题不能降级：

- Agent5 最终正文与 Agent4 正文不一致。
- Agent5 引入新的未经验证事实。
- 最终报告引用失效。

## 6. 第四阶段：状态与前端提示

**预计工作量：0.5～1 个开发日。**

在项目状态中增加：

```json
{
  "delivery_status": "done_degraded",
  "degradation_reasons": [],
  "dropped_claim_ids": [],
  "omitted_chart_ids": [],
  "available_artifacts": [],
  "claims_enabled": true
}
```

前端展示三类结果。

### 6.1 完整完成

> 调研完成，报告及全部产物已生成。

### 6.2 降级完成

> 报告已生成。部分结论台账、图表或 PDF 已降级，不影响正文阅读。

支持展开降级详情，例如：

- 12 条 claim 未映射正文，已移除。
- 2 条证据存在跨问题归类。
- 1 张图表无法验证数值，已转为表格。
- PDF 生成失败，请下载 HTML。

### 6.3 核心失败

主错误只显示用户可处理的信息，例如：

- API 认证失败。
- 模型不存在。
- 正文引用校验失败。
- 没有任何可交付报告。

完整的 Pydantic 和调用栈错误放入诊断详情，不直接作为页面主错误展示。

## 7. 第五阶段：统一重试策略

**预计工作量：0.5 个开发日。**

避免 LLM 客户端重试和 Agent 阶段重试叠加。

| 错误类型 | 重试策略 |
| --- | --- |
| 网络超时、连接中断、普通 5xx | 指数退避，最多 3 次 |
| 短期限流 429 | 根据响应信息退避 |
| 固定窗口额度耗尽 | 不重试，进入 `paused` |
| 401/403 | 不重试，提示检查密钥 |
| 404/model not found | 不重试，提示检查模型名 |
| Pydantic/JSON 格式错误 | 一次定向修复，然后降级 |
| 确定性证据错误 | 不重复执行相同操作 |
| 图表/PDF 错误 | 不重试整个 Agent5，直接局部降级 |

## 8. 验收用例

上线前必须通过以下端到端用例：

1. **14 条 claim 全部无法逐字对应正文**  
   预期：项目进入 `done_degraded`，报告正常生成。

2. **claim 跨 question 挂载证据**  
   预期：记录 warning，不阻断。

3. **claim 引用不存在的 evidence**  
   预期：删除相关 claim，正文继续交付。

4. **正文引用不存在的 evidence**  
   预期：阻断，不能交付虚假引用报告。

5. **`04_claims.json` 完全损坏**  
   预期：修复一次；失败后关闭台账能力并继续。

6. **所有图表溯源失败**  
   预期：生成无图版报告。

7. **PDF 引擎失败**  
   预期：HTML、Markdown 正常交付。

8. **Agent5 改写或丢失 Agent4 正文**  
   预期：阻断。

9. **API 固定窗口额度耗尽**  
   预期：进入 `paused`，不连续消耗重试次数。

## 9. 发布方案

建议分两次发布。

### 9.1 第一版：可用性修复

包含：

- Agent4 claims 降级。
- Agent5 图表降级。
- PDF 降级。
- `done_degraded` 状态。
- 当前快手项目回归验证。

### 9.2 第二版：稳定性完善

包含：

- 全链路错误分类。
- 重试策略收敛。
- 前端诊断详情。
- 降级率监控。

## 10. 完成标准

满足以下条件才算开发完成：

- 台账格式问题不再阻断最终报告。
- 图表和 PDF 问题不再阻断正文交付。
- 真实性错误仍然无法绕过门禁。
- 所有降级行为在状态和页面上可见。
- 当前快手项目可以从现有断点继续完成。
- 新增测试和现有测试全部通过。
- 不修改或重新生成已经通过审计的 Agent4 正文。

## 11. 工作量预估

整体预计工作量为 **3～4 个开发日**。

第一版“可用性修复”优先实现 Agent4 台账降级、Agent5 图表/PDF 降级和 `done_degraded` 状态，预计可以压缩到 **2 个开发日左右**完成。

## 12. 附录：全链路中断点审计

本节对流水线中所有可能让项目停在 `failed` 的 `raise` / `mark_failure` / 门禁阻断点做
了全量盘点（基于当前工作区代码），按「是否真的需要人工介入」归并成 24 类中断点，
分五个层级。

降级可行性分四档：

- 🟢 可安全降级
- 🟡 可部分降级（需先改审计 / 状态模型）
- 🔴 必须硬阻断（真实性安全边界，不可降级）
- ⚫ 环境 / 配置错误（只能 `paused` 或修环境，不是“降级”）

### 12.1 LLM 客户端层（所有 Agent 共用，最外层）

| # | 中断点 | 原因 | 降级 |
|---|---|---|---|
| 1 | `LLMClient._request_with_retry` / `chat_stream` 重试 3 次后仍失败（`llm/client.py:186-202`） | 429 / 5xx / 网络中断，指数退避 3 次耗尽 | 🟡 **可部分**：需把 429 细分为「短期限流」vs「固定窗口额度耗尽」→ 后者进 `paused` 而非 `failed`（§7 的核心） |
| 2 | `AuthenticationError`（401/403）`llm/client.py:212` | API key 无效 | ⚫ 不可降级，只能改 key |
| 3 | `ModelNotFoundError`（404）`llm/client.py:216` | 模型名错 | ⚫ 不可降级，只能改模型名 |
| 4 | `ContextLengthExceededError` `llm/client.py:225` | 上下文超限 | ⚫ 不可降级，需缩输入 / 换模型 |

**关键缺口**：当前 `_raise_for_status` 把所有 429 都当 `RateLimitError` 退避重试，
**没有**「固定窗口额度耗尽」的语义。§7 要区分，必须先给 `llm/errors.py` 加子类，
否则「额度耗尽」会白白烧掉 3 次退避 + 阶段级 2 次重试。

### 12.2 采集-验证层（Agent2↔3，最依赖外部网络）

| # | 中断点 | 原因 | 降级 |
|---|---|---|---|
| 5 | `AgentLoopStuckError`（`agent_loop/loop.py:68`） | 同一工具同参数反复返回相同错误 ≥ 阈值 | 🟡 **可部分**：现在 `_safe_run` 直接 `raise PipelineError` 阻断。可改为「记录该工具失败，跳过该源继续」，只对 `WebSearch`/`WebFetch` 这类采集工具降级，不动确定性门禁 |
| 6 | 采集轮未生成 raw data（`agents/collector.py:221`） | LLM 一轮内没写文件 | 🔴 阶段内无法降级（没有 raw 就没有后续），但**可整体降级**：该轮作废、复用历史轮次继续（现 `_safe_run` 的 2 次重试已覆盖一部分） |
| 7 | `TasksError`：任务台账损坏 / ID 抄错 / 回填 source 不存在（`sources/tasks.py:58,246,268,322,446`） | 结构性内容错误 | 🟡 **可部分**：`blocking_pending_tasks` 才该阻断；台账 JSON 损坏这类可降级为「丢弃台账、跳过 R3 门禁」——但要评估是否削弱补研安全 |
| 8 | `_quality_gate_error`：轮次用尽仍未收敛（`orchestrator.py:195`） | 必答问题证据不足 | 🔴 **必答问题**不能降级（R1 防幻觉地基）；🟢 **可选问题**（`required=False`）证据不足可以降级——这是 §2.2 最需要澄清的边界 |
| 9 | `apply_feedback_tasks`：`waived` 需人工审批（`sources/tasks.py:331`） | 豁免需要人 | 🔴 人为设计，不可降级（豁免本身就需要人拍板） |

### 12.3 Agent4 分析门禁层（真实性安全核心）

| # | 中断点 | 原因 | 降级 |
|---|---|---|---|
| 10 | `AnalysisOutcomeError`（缺失/空/损坏/未知 question_id）`orchestrator.py:345` | Agent4 的 fail-closed 契约文件 | 🔴 不可降级（没有 outcome 无法判断是否需要补研） |
| 11 | `audit_analysis_citations` 失败：未知 source / stale version / 无 SUPPORTED locator / bare URL（`sources/citations.py:136-195`） | 正文引用造假/失效 | 🔴 **绝对不可降级**（§2.1 明确列出的硬门禁） |
| 12 | `needs_more_research` 补研请求（`orchestrator.py:355`） | Agent4 主动判定证据不足 | 🔴 不可降级（这是真实缺口，不是辅助功能故障） |
| 13 | `load_claims_file` 抛 `ClaimsError`：台账缺失 / JSON 损坏 / 枚举错（`sources/claims.py:141-161`） | 台账结构问题 | 🟢 **可降级**（§4 的核心）：修复一次 → 失败 `claims_disabled=true` 继续 |
| 14 | `validate_claims` 错误：claim 引用不存在证据 / 非 SUPPORTED（`sources/claims.py:241-250`） | 台账引用失效 | 🟢 **可降级**：删该 claim；critical 无有效证据则删 claim（§4.1） |
| 15 | `validate_claims`：claim 文本不在正文（`sources/claims.py:232`） | 台账与正文脱节 | 🟢 **可降级**：删该 claim + warning |
| 16 | `validate_claims`：必答问题无 claim 覆盖（`sources/claims.py:261`） | 覆盖不全 | 🟡 **可部分**：`required=True` 缺覆盖可降级为 warning（正文还在，只是台账缺一行）；但若正文本身也没覆盖该问题，仍应走质量门硬阻断 |

### 12.4 Agent5 排版交付层（降级空间最大）

| # | 中断点 | 原因 | 降级 |
|---|---|---|---|
| 17 | `run_formatting` 前置：outline/analysis/sources_final 缺失（`agents/formatter.py:271-275`） | 上游产物缺失 | 🔴 上游真实缺失，不可在 Agent5 降级（应回退上游） |
| 18 | `_require_delivery_evidence`：质量门未过 / 引用审计失败（`agents/formatter.py:55-69`） | 交付证据门槛 | 🔴 必答证据不足/引用失效，不可降级 |
| 19 | 图表清单未生成（`agents/formatter.py:352`） | LLM 没写 manifest | 🟢 **可降级**：无 manifest → 无图版报告继续（§5.1） |
| 20 | `apply_provenance_gate`：必需图数值溯源失败（`chart_provenance.py:475,514`） | 必需图数值无法匹配 | 🟢 **可降级**：需先改门禁——现在必需图失败是 `DeterministicContentError` 硬阻断，应改为「降级为表格 + 记 `omitted_chart_ids`」 |
| 21 | `render_chart_manifest`：必需图无渲染器 / LLM 兜底失败（`report_charts.py:816,851,863`） | 图表类型不支持或渲染失败 | 🟢 **可降级**：同 #20，`required` 图降级为表格而非阻断 |
| 22 | `build_report_html` / `build_report_latex` 缺 Pandoc、模板资产不完整（`report_formatting.py:736,768,774`） | 渲染环境缺依赖 | 🟢 **可降级**：Markdown 恒成功；HTML 失败不影响 Markdown；PDF 失败不影响 HTML |
| 23 | `compile_report_pdf` / `generate_print_pdf`：LaTeX 编译失败 / Chrome 打印失败 / 覆盖率不足 / 页数不一致（`report_formatting.py:888-911`、`report_print.py:232,258,465,474`） | PDF 引擎失败 | 🟢 **可降级**（§5.2 的核心）：PDF 失败 ≠ `failed`，交付 HTML+Markdown |

**最大的结构性 bug**：#22、#23 虽然单点失败理论上可降级，但 `run_formatting` 里
`generate_typeset_artifacts` 是**一把梭**（`agents/formatter.py:373-410`）——HTML、PDF、
QA 检查在同一段 `try` 里，任何一个抛异常都会 `raise RuntimeError` 打挂整个 Agent5，
再由 `_safe_run` 重试 2 次。**这就是 §5.2 要拆的「多格式独立交付」，也是当前最该先修
的降级缺口。**

### 12.5 状态机 / 配置 / 环境层

| # | 中断点 | 原因 | 降级 |
|---|---|---|---|
| 24 | `ResearchPlanBlockedError`：需求清单缺失/空/损坏（`orchestrator.py:216`、`research_plan.py` 多处） | R1 地基缺失 | 🔴 不可降级（需求清单是全部门禁的输入，缺失时不能放行） |
| 25 | `_assert_delivery_ready`：critical 补研任务未完成（`orchestrator.py:295`） | 交付前任务门槛 | 🔴 不可降级（补研未完成交付 = 交付残缺结论） |
| 26 | 文件工具路径越界 `ValueError`（`agent_loop/loop.py:302`） | 安全边界 | 🟢 已被 `_execute_tool_call` 捕获为错误回传给模型，不中断；无需改 |
| 27 | `config.py:84`：非法 `REPORT_PDF_ENGINE` 导入期报错 | 配置错误 | ⚫ 不可降级（fail fast，让用户立刻知道配错） |
| 28 | `OCR`：tesseract 缺失（`sources/ocr.py:42`） | 环境缺依赖 | 🟡 **可部分**：单份扫描件 OCR 失败可降级为「该源跳过」，不阻断整个采集 |
| 29 | `web_app.py:865`：Agent1 既没提纲也没澄清问题 | LLM 空跑 | 🔴 阶段内不可降级（没有提纲全流程无从开始） |

### 12.6 结论与优先改造顺序

三个最值得先做的降级点：

1. **Agent5 多格式独立交付（#22/#23）**——当前 HTML/PDF 失败会连带打挂整个 Agent5 并
   重试 2 次，纯属浪费。这是收益最直接、风险最低的改动。
2. **必需图溯源失败降级（#20/#21）**——`apply_provenance_gate` 里 `chart.required` 失败抛
   `DeterministicContentError`，与 §5.1「必需图失败不再终止整份报告」矛盾，需要把
   「必需图 → 阻断」改成「必需图 → 降级为表格 + 记录」。
3. **429 限流细分（#1）**——否则 §7 的「固定窗口额度耗尽进 `paused`」无法落地，额度耗尽
   会被当成瞬态故障白烧重试次数。

**必须死守、不能降级的红线**：#11（正文引用审计）、#10（AnalysisOutcome）、#18（交付证据
门槛）、#24（需求清单）、#25（补研任务）、#12（needs_more_research）。这些是「用户看到错误
事实」的唯一防线，与 §2.1 完全一致。

## 13. 实施进度（第一版：可用性修复）

> 记录截至当前工作区的已落地改动。测试结果：`pytest tests/` 全量 **446 passed**。

### 13.1 已完成的改动

| 范围 | 改动 | 涉及文件 |
|---|---|---|
| Agent4 台账降级 | `_validate_analysis_transition` 恢复严格校验（失败 raise），`run_state_machine` 走「修复一次 → 修复仍失败则 `_sanitize_claims_and_continue` 确定性清洗/关闭」的完整链路，台账不再阻断正文交付 | `src/research_agent/orchestrator.py` |
| 台账确定性清洗 | 新增 `sanitize_claims()`：删除正文不存在的 claim / 无效证据引用 / 重复 ID，critical 无有效证据删整条，覆盖缺口只记 warning；新增 `ClaimsSanitizeResult` | `src/research_agent/sources/claims.py` |
| 清洗产物隔离 | 新增 `FILE_CLAIMS_SANITIZED = "04_claims_sanitized.json"`，清洗结果写独立文件，原始台账不覆盖 | `src/research_agent/config.py` |
| 图表降级 | `apply_provenance_gate` 已改为「数值溯源失败 → 降级为数据表，不阻断」（`chart_provenance.py`） | （此前已完成，本次确认） |
| Agent5 多格式独立交付 | `generate_report_artifacts` 拆成 HTML/PDF 独立 try，任一失败只进 `degradation` 列表不抛异常；`run_formatting` 对 `generate_typeset_artifacts` 加最终兜底，失败降级为仅 Markdown | `src/research_agent/report_formatting.py`、`src/research_agent/agents/formatter.py` |
| 图表清单缺失降级 | 无 manifest 时降级为空清单 → 无图版报告，不再 `raise RuntimeError` | `src/research_agent/agents/formatter.py` |
| 降级状态 | `delivery_status`（`done` / `done_degraded`）+ `delivery_degradation` 落到 `state.notes`，供前端展示 | `src/research_agent/agents/formatter.py` |

### 13.2 新增/更新测试

- `tests/test_claims_gate.py`：新增 `sanitize_claims` 三条测试（正文缺失删 claim、无效证据引用清洗、缺需求清单关闭 claims）。
- `tests/test_agent4_evidence_boundary.py`：新增端到端 `test_claim_gate_degrades_and_reaches_done_when_repair_fails`（修复失败 → 降级 → 跑到 DONE）。
- `tests/test_agent_evidence_integration.py`：`test_formatter_reuses_report_and_surfaces_typeset_failure` 从「断言 raise」改为「断言降级后仍完成交付」。

### 13.3 尚未实施（第二版：稳定性完善）

- **429 限流细分**（§7，中断点 #1）：仍需给 `llm/errors.py` 加「固定窗口额度耗尽」子类，接入 `paused` 状态。
- **`paused` 状态机**（§1/§7）：`Stage` 尚无 `PAUSED`，限流/额度耗尽仍会落 `failed`。
- **前端降级详情展示**（§6）：`state.notes` 已落 `delivery_status`/`delivery_degradation`/`claims_disabled`/`analysis_dropped_claim_ids`，但 `web_static/results.js` 尚未消费展示。
- **可选问题证据不足降级**（§2.2，中断点 #8）：`QualityGate` 仍对 `required=False` 与 `required=True` 一视同仁，需在 `_assert_delivery_ready` / `quality.py` 区分。
- **采集工具失败跳过源**（中断点 #5/#28）：`AgentLoopStuckError` 仍直接阻断。

## 14. 实施进度（第二版：稳定性完善）

> 测试结果：`pytest tests/` 全量 **455 passed**。

### 14.1 已完成的改动

| 范围 | 改动 | 涉及文件 |
|---|---|---|
| 429 限流细分 | 新增 `QuotaExhaustedError`（固定窗口额度耗尽，继承 `RateLimitError`），`_raise_for_status` 按响应体关键词（quota/balance/billing/额度/余额/欠费/配额/计费/耗尽 等）区分额度耗尽 vs 短期限流 | `src/research_agent/llm/errors.py`、`src/research_agent/llm/client.py` |
| 额度耗尽不重试 | `chat_stream` 与 `_request_with_retry` 对 `QuotaExhaustedError` 直接上抛，不进入指数退避重试 | `src/research_agent/llm/client.py` |
| 错误导出 | `QuotaExhaustedError` 加入 `llm/__init__.py` 导出 | `src/research_agent/llm/__init__.py` |
| `paused` 语义 | 新增 `PipelinePausedError`（可恢复等待，非失败）；`_safe_run` 捕获 `QuotaExhaustedError` → 写 `notes["paused"]` + `pause_reason`，不落 failed，不重试 | `src/research_agent/pipeline_errors.py`、`src/research_agent/orchestrator.py` |
| CLI paused 处理 | `run_pipeline` 单独捕获 `PipelinePausedError`，打印「已暂停 + resume 指引」，不落 failed | `src/research_agent/orchestrator.py` |
| Web paused 处理 | `_run_until_pause` 捕获 `PipelinePausedError` → `job.status = "paused"` + notes 标记；`_serialize_state` 暴露 `paused`/`pause_reason`/`delivery_status`/`delivery_degradation`/`claims_disabled`/`analysis_dropped_claim_ids`/`analysis_claim_warnings` | `src/research_agent/web_app.py` |
| 前端降级展示 | `results.js` 状态卡片区分「已完成 / 已降级完成 / 已暂停（额度）/ 生成失败」；新增 `degradationBox` 展开展示降级原因与暂停原因 | `src/research_agent/web_static/results.js`、`results.html`、`styles.css` |
| 静态版本号 | `STATIC_VERSION` 与全部 HTML `?v=` 统一 bump 到 `20260825-availability-v2` | `src/research_agent/web_app.py`、`web_static/*.html` |

### 14.2 新增/更新测试

- `tests/test_llm_errors.py`（新增）：8 条测试锁定 429 细分（额度关键词 → `QuotaExhaustedError`、裸 429 → `RateLimitError`、401/404/context_length/未知 400 分类）。
- `tests/test_state_machine.py`：新增 `test_paused_notes_persist_and_serialize`，验证 paused 序列化且不落 failed。

### 14.3 确认无需改动的项

- **可选问题证据不足降级**（§2.2，中断点 #8）：`quality.py` 第 91 行**已实现** `if requirement.required` 才触发 `missing_required`——非必答问题证据不足不会阻断，无需改代码。

## 15. 实施进度（第三版：采集工具失败跳过源）

> 测试结果：`pytest tests/` 全量 **459 passed**。

### 15.1 已完成的改动

| 范围 | 改动 | 涉及文件 |
|---|---|---|
| `SKIP_SOURCE` 标记 | 采集工具源级失败返回 `SKIP_SOURCE:` 前缀（可区分「换下一个源」vs「参数/配置错误停下」） | `web_fetch.py`、`web_search.py`、`project_sources.py` |
| 源级 vs 配置错误区分 | `WebFetch`/`CaptureProjectWebSource` 的 HTTP 状态错误（403/404 等）是源级失败 → `SKIP_SOURCE`；`WebSearch` 的 provider 401/403 是配置错误 → 保留 `Error:` | `web_search.py`（`httpx.HTTPStatusError` vs `httpx.RequestError` 区分） |
| 跳过 stuck 计数 | `_ToolErrorTracker` 对采集类工具（`WebSearch`/`WebFetch`/`CaptureProjectWebSource`）的 `SKIP_SOURCE` 不触发普通 stuck；同一目标（URL）重复跳过超过阈值仍触发 stuck 防空转 | `agent_loop/loop.py` |
| 提示词引导 | `collector_round.md` 明确「跳过 ≠ 完成」：`SKIP_SOURCE` 后换下一个候选源，同一 URL 不再重试 | `agents/prompts/collector_round.md` |

### 15.2 新增测试

- `tests/test_agent_loop.py`：4 条测试锁定第三阶段行为（跳过不 stuck、同 URL 重复跳过仍 stuck、普通工具错误仍 stuck、`_skip_target` 提取）。

### 15.3 安全边界（本次刻意未放宽）

- `Read`/`Write`/`RecordProjectEvidence` 等确定性工具**保持严格 stuck**，失败仍是 `Error executing tool` 并计数。
- 「跳过源」不写入本轮事实，且 `CaptureProjectWebSource` 只有返回真实 `source_id` 才算采集进展——不放行伪造源。
- 同一 URL 重复跳过超过 `max(threshold, 3)` 次仍抛 `AgentLoopStuckError`，防止模型在一个坏源上无限空转。
