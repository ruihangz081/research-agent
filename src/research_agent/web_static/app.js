Lumitrace.mountShell("workspace");

const state = { projects: [], projectId: Lumitrace.selectedProject(), project: null, artifactKey: null, renderedArtifactKey: null, renderedArtifactMarkup: null, events: null, busy: false };
const $ = (id) => document.getElementById(id);
const pipelineStages = [
  ["初始化", ["init"]],
  ["战略规划", ["planning", "await_clarification", "await_outline_approval"]],
  ["信息源分层", ["sourcing", "await_source_approval"]],
  ["采集验证", ["collecting_and_validating", "await_final_source_approval"]],
  ["深度分析", ["analyzing"]],
  ["排版交付", ["formatting"]],
  ["完成", ["done"]],
];

function currentStep(stage) {
  const index = pipelineStages.findIndex(([, values]) => values.includes(stage));
  return index < 0 ? 0 : index;
}

function renderPipeline(project) {
  const active = project ? currentStep(project.stage) : -1;
  $("pipeline").innerHTML = pipelineStages.map(([label], index) => {
    const status = index < active ? "complete" : index === active ? "current" : "";
    const caption = index < active ? "已完成" : index === active ? (project.running ? "进行中" : project.checkpoint ? "待审批" : "当前阶段") : "待开始";
    return `<div class="pipeline-step ${status}"><span class="step-node">${index < active ? Lumitrace.icon("check", 15) : index + 1}</span><strong>${label}</strong><small>${caption}</small></div>`;
  }).join("");
}

function renderTimeline(project) {
  const logs = (project.logs || []).slice(-5).reverse();
  if (!logs.length) {
    $("timeline").innerHTML = '<div class="empty compact"><span class="empty-symbol">◇</span><strong>暂无执行记录</strong></div>';
    $("logList").innerHTML = '<div class="muted">等待 Agent 开始运行……</div>';
    return;
  }
  $("timeline").innerHTML = logs.map((log, index) => `<div class="timeline-row"><time>${Lumitrace.escapeHtml(log.time)}</time><p>${Lumitrace.escapeHtml(log.message)}</p><span class="status-pill ${index === 0 && project.running ? "" : "success"}"><i class="status-dot ${index === 0 && project.running ? "running" : "success"}"></i>${index === 0 && project.running ? "运行中" : "已记录"}</span></div>`).join("");
  $("logList").innerHTML = (project.logs || []).slice().reverse().map((log) => `<div class="log-row"><time>${Lumitrace.escapeHtml(log.time)}</time><span>${Lumitrace.escapeHtml(log.message)}</span></div>`).join("");
}

function renderClarification(project) {
  const panel = $("clarifyPanel");
  const questions = project.clarification_questions || [];
  const pending = project.stage === "await_clarification" && questions.length > 0;
  panel.classList.toggle("hidden", !pending);
  if (!pending) return;
  $("clarifyQuestions").innerHTML = questions.map((question, index) => `
    <label class="clarify-item">
      <span>${index + 1}. ${Lumitrace.escapeHtml(question)}</span>
      <textarea data-clarify="${index}" rows="2" maxlength="500" placeholder="留空则采用 Agent1 的建议默认值"></textarea>
    </label>`).join("");
  const history = project.clarification || [];
  $("clarifyHistory").innerHTML = history.length
    ? `<details style="margin-top:14px"><summary class="muted" style="cursor:pointer;font-weight:700">历史澄清问答（${history.length}）</summary><div class="clarify-history">${history.map((item) => `<div><strong>${Lumitrace.escapeHtml(item.question)}</strong><p>${Lumitrace.escapeHtml(item.answer)}</p></div>`).join("")}</div></details>`
    : "";
}

function renderCheckpoint(project) {
  const panel = $("checkpointPanel");
  panel.classList.toggle("hidden", !project.checkpoint);
  if (!project.checkpoint) return;
  $("checkpointTitle").textContent = `${project.checkpoint.title}审批`;
  $("checkpointName").textContent = project.checkpoint.title;
  if (!state.artifactKey) state.artifactKey = project.checkpoint.key;
}

function renderFailure(project) {
  const panel = $("failurePanel");
  const failed = Boolean(project.failed) && !project.running;
  panel.classList.toggle("hidden", !failed);
  $("retryBtn").classList.toggle("hidden", !project.can_retry);
  $("retryBtn").disabled = !project.can_retry;
  if (!failed) return;
  $("failureStage").textContent = Lumitrace.stageLabel(project.failed_stage || project.stage);
  $("failureError").textContent = project.last_error || project.job_message || "未知错误";
  const reasons = project.quality_gate_reasons || [];
  // 门禁"通过但有限制"时，这些是留存的已知局限，不是阻断原因；标成"未通过"会误导排查方向
  const blocked = project.quality_gate === "blocked" || project.quality_gate === "needs_more_research";
  const reasonsTitle = blocked ? "证据门槛未通过原因" : "证据门槛已通过，但存在以下局限（非本次失败原因）";
  $("failureReasons").innerHTML = reasons.length
    ? `<p class="eyebrow" style="margin-top:14px">${reasonsTitle}</p><ul class="failure-reasons">${reasons.map((item) => `<li>${Lumitrace.escapeHtml(item)}</li>`).join("")}</ul>`
    : "";
  $("retryMeta").textContent = project.retry_blocked_reason || (project.retry_count ? `已重试 ${project.retry_count} 次` : "重试会保留已生成的产物");
  $("failureRetryBtn").disabled = !project.can_retry;
  $("failureRetryBtn").title = project.retry_blocked_reason || "";
}

function updateRerunHint() {
  const option = $("rerunStage").selectedOptions[0];
  $("rerunHint").textContent = option
    ? `将重新执行「${option.textContent}」及其后的全部阶段。`
    : "当前没有可回退的阶段。";
}

function renderRerun(project) {
  const options = project.rerun_stages || [];
  const panel = $("rerunPanel");
  panel.classList.toggle("hidden", options.length === 0);
  if (!options.length) return;

  const select = $("rerunStage");
  const previous = select.value;
  select.innerHTML = options.map((item) =>
    `<option value="${Lumitrace.escapeHtml(item.value)}">${Lumitrace.escapeHtml(item.label)}</option>`
  ).join("");
  select.value = options.some((item) => item.value === previous)
    ? previous
    : options[options.length - 1].value;
  select.disabled = !project.can_rerun;
  $("rerunBtn").disabled = !project.can_rerun;
  updateRerunHint();
}

function previewableArtifacts(project) {
  return project.artifacts;
}

function renderResearchPlan(project) {
  const plan = project.research_plan || { available: false, requirements: [] };
  const migratePanel = $("planMigratePanel");
  migratePanel.classList.toggle("hidden", plan.available !== false);
  if (plan.available === false) {
    $("planMigrateError").textContent = plan.error || "项目缺少 research_requirements.json。";
  }

  const panel = $("planPanel");
  panel.classList.toggle("hidden", !plan.available);
  if (!plan.available) return;

  const coverage = plan.coverage || {};
  const requirements = plan.requirements || [];
  const met = requirements.filter((item) => (coverage[item.question_id] ?? 0) >= 1).length;
  $("planBadge").textContent = `${met} / ${requirements.length} 达标`;
  const rows = requirements.map((item) => {
    const ratio = coverage[item.question_id];
    const label = ratio === undefined ? "未评估" : `${Math.round(ratio * 100)}%`;
    // 只有"必答且未达标"才是阻断项；可选问题缺证据不单独阻断交付
    const tone = ratio === undefined ? "" : ratio >= 1 ? "success" : item.required ? "danger" : "warning";
    return `<tr>
      <td><code>${Lumitrace.escapeHtml(item.question_id)}</code></td>
      <td>${Lumitrace.escapeHtml(item.text)}</td>
      <td>${item.required ? "必答" : "可选"}</td>
      <td>${item.min_supported}</td>
      <td>${Lumitrace.escapeHtml(item.min_source_tier || "不限")}</td>
      <td>${item.require_numeric ? "是" : "否"}</td>
      <td><span class="status-pill ${tone}">${label}</span></td>
    </tr>`;
  }).join("");
  const warning = plan.warning
    ? `<p class="muted">提示：${Lumitrace.escapeHtml(plan.warning)}</p>`
    : "";
  $("planTable").innerHTML = `${warning}<div class="table-scroll"><table class="data-table"><thead><tr>
      <th>question_id</th><th>研究问题</th><th>必答</th><th>最低证据数</th><th>最低来源等级</th><th>需数值</th><th>覆盖</th>
    </tr></thead><tbody>${rows}</tbody></table></div>`;
}

function renderArtifacts(project) {
  const artifacts = previewableArtifacts(project);
  if (!state.artifactKey || !artifacts.some((item) => item.key === state.artifactKey)) {
    state.artifactKey = project.checkpoint?.key || artifacts.find((item) => item.exists)?.key || artifacts[0]?.key || null;
  }
  $("artifactTabs").innerHTML = artifacts.map((artifact) => `<button class="artifact-tab${artifact.key === state.artifactKey ? " active" : ""}${artifact.exists ? "" : " missing"}" type="button" data-artifact="${artifact.key}">${Lumitrace.icon("file", 15)}<span>${Lumitrace.escapeHtml(artifact.label)}</span></button>`).join("");
  $("artifactTabs").querySelectorAll("[data-artifact]").forEach((button) => button.addEventListener("click", () => {
    state.artifactKey = button.dataset.artifact;
    renderArtifacts(project);
    loadArtifact(true);
  }));
}

function updateDownloads(project) {
  const exists = (key) => project.artifacts.some((item) => item.key === key && item.exists);
  $("downloadPdfBtn").disabled = !exists("final_report");
  $("typesetBtn").disabled = !exists("final_report") || project.running;
  $("downloadTexBtn").disabled = !exists("final_report_tex");
}

async function loadArtifact(forceTop = false) {
  const view = $("artifactView");
  const artifactKey = state.artifactKey;
  const artifactChanged = state.renderedArtifactKey !== artifactKey;
  if (!state.projectId || !state.artifactKey) {
    view.innerHTML = '<div class="empty"><span class="empty-symbol">◇</span><strong>暂无产物</strong></div>';
    state.renderedArtifactKey = null;
    state.renderedArtifactMarkup = null;
    return;
  }
  const selected = state.project?.artifacts.find((item) => item.key === state.artifactKey);
  if (!selected?.exists) {
    view.innerHTML = '<div class="empty"><span class="empty-symbol">◇</span><strong>等待生成</strong><p>该产物将在对应研究阶段完成后出现。</p></div>';
    state.renderedArtifactKey = state.artifactKey;
    state.renderedArtifactMarkup = null;
    if (forceTop || artifactChanged) view.scrollTop = 0;
    return;
  }
  try {
    const artifact = await Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}/artifacts/${encodeURIComponent(artifactKey)}`);
    if (state.artifactKey !== artifactKey) return;
    const markup = artifact.html || Lumitrace.renderMarkdown(artifact.content);
    if (state.renderedArtifactKey !== artifactKey || state.renderedArtifactMarkup !== markup) {
      view.innerHTML = markup;
      Lumitrace.hydrateSourceCitations(view, state.projectId);
      state.renderedArtifactKey = artifactKey;
      state.renderedArtifactMarkup = markup;
    }
    if (forceTop || artifactChanged) view.scrollTop = 0;
  } catch (error) {
    view.innerHTML = `<div class="empty"><span class="empty-symbol">!</span><strong>内容读取失败</strong><p>${Lumitrace.escapeHtml(error.message)}</p></div>`;
  }
}

function renderProject(project) {
  state.project = project;
  Lumitrace.rememberProject(project.id);
  $("workspaceEmpty").classList.add("hidden");
  $("workspaceContent").classList.remove("hidden");
  $("projectTitle").textContent = project.topic;
  $("stageText").textContent = Lumitrace.stageLabel(project.stage);
  const tone = Lumitrace.stageTone(project);
  const runLabel = project.running ? "运行中" : project.failed ? "运行失败" : project.stage === "await_clarification" ? "等待澄清" : project.checkpoint ? "等待审批" : project.stage === "done" ? "已完成" : "已暂停";
  $("runText").innerHTML = `<i class="status-dot ${tone}"></i> ${runLabel}`;
  $("continueBtn").disabled = project.running || project.stage === "done" || Boolean(project.checkpoint) || Boolean(project.can_retry) || project.stage === "await_clarification";
  $("continueBtn").title = project.can_retry ? "项目处于失败状态，请先点击重试" : project.stage === "await_clarification" ? "请先回答澄清问题" : "";
  $("deleteBtn").disabled = project.running;
  $("roundBadge").textContent = `第 ${project.collect_round} / ${project.max_collect_rounds} 轮`;
  renderPipeline(project);
  renderTimeline(project);
  renderClarification(project);
  renderCheckpoint(project);
  renderFailure(project);
  renderResearchPlan(project);
  renderRerun(project);
  renderArtifacts(project);
  updateDownloads(project);
}

async function loadProject() {
  if (state.busy) return;
  if (!state.projectId) {
    try {
      const data = await Lumitrace.api("/api/projects");
      state.projectId = data.projects[0]?.id || "";
    } catch (_) { /* empty state below */ }
  }
  if (!state.projectId) { renderPipeline(null); return; }
  try {
    const project = await Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}`);
    renderProject(project);
    await loadArtifact();
  } catch (error) {
    $("workspaceEmpty").innerHTML = `<span class="empty-symbol">!</span><strong>项目加载失败</strong><p>${Lumitrace.escapeHtml(error.message)}</p><a class="button secondary" href="/research">返回研究首页</a>`;
    $("workspaceEmpty").classList.remove("hidden");
    $("workspaceContent").classList.add("hidden");
  }
}

// 用 SSE 增量推送替代 3 秒全量轮询：项目状态或日志变化时服务端推事件，
// 客户端收到事件后再拉取最新快照，空闲时不再空转请求。
function connectEvents() {
  if (!state.projectId) return;
  if (state.events) state.events.close();
  const source = new EventSource(`/api/projects/${encodeURIComponent(state.projectId)}/events`);
  source.addEventListener("update", () => {
    if (state.projectId) loadProject();
  });
  // EventSource 会自动重连；连接建立后立即刷新一次以对齐首屏状态
  source.addEventListener("open", () => {
    if (state.projectId) loadProject();
  });
  state.events = source;
}

async function continueProject() {
  const button = $("continueBtn");
  Lumitrace.setButtonBusy(button, true, "启动中");
  try {
    await Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}/continue`, { method: "POST" });
    Lumitrace.toast("研究已继续运行");
    await loadProject();
  } catch (error) { Lumitrace.toast(error.message, "danger"); }
  finally { Lumitrace.setButtonBusy(button, false); }
}

async function approve(approved) {
  const button = approved ? $("approveBtn") : $("rejectBtn");
  Lumitrace.setButtonBusy(button, true, approved ? "提交中" : "驳回中");
  try {
    await Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}/approval`, { method: "POST", body: JSON.stringify({ approved, feedback: $("feedbackInput").value }) });
    $("feedbackInput").value = "";
    Lumitrace.toast(approved ? "已通过，研究继续运行" : "已驳回，准备重新执行", approved ? "success" : "warning");
    await loadProject();
  } catch (error) { Lumitrace.toast(error.message, "danger"); }
  finally { Lumitrace.setButtonBusy(button, false); }
}

function openDownload(path) { if (state.projectId) window.open(`/api/projects/${encodeURIComponent(state.projectId)}${path}`, "_blank"); }

async function typeset() {
  const button = $("typesetBtn");
  Lumitrace.setButtonBusy(button, true, "排版中");
  try {
    const result = await Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}/typeset/final-report`, { method: "POST" });
    Lumitrace.toast(result.message, result.status === "pdf" ? "success" : "warning");
    await loadProject();
  } catch (error) { Lumitrace.toast(error.message, "danger"); }
  finally { Lumitrace.setButtonBusy(button, false); }
}

async function retryProject(button) {
  state.busy = true;
  Lumitrace.setButtonBusy(button, true, "重试中");
  try {
    const result = await Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}/retry`, { method: "POST", body: JSON.stringify({ extra_rounds: 1 }) });
    Lumitrace.toast(result.message || "已重新开始运行");
  } catch (error) { Lumitrace.toast(error.message, "danger"); }
  finally {
    Lumitrace.setButtonBusy(button, false);
    state.busy = false;
    await loadProject();
  }
}

async function migrateResearchPlan(button) {
  state.busy = true;
  Lumitrace.setButtonBusy(button, true, "生成中");
  try {
    const result = await Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}/research-plan/migrate`, { method: "POST" });
    Lumitrace.toast(result.message || "研究需求清单已生成");
  } catch (error) { Lumitrace.toast(error.message, "danger"); }
  finally {
    Lumitrace.setButtonBusy(button, false);
    state.busy = false;
    await loadProject();
  }
}

async function rerunProject() {
  const select = $("rerunStage");
  const stage = select.value;
  const label = select.selectedOptions[0]?.textContent || stage;
  if (!stage) return;
  state.busy = true;
  const confirmed = await Lumitrace.confirmAction({
    title: `从「${label}」重新运行？`,
    message: "该阶段及之后的现有产物会移入项目备份，然后重新生成。更早阶段的产物和项目材料不会受影响。",
    confirmLabel: "回退并运行",
    tone: "primary",
  });
  if (!confirmed) { state.busy = false; return; }

  const button = $("rerunBtn");
  Lumitrace.setButtonBusy(button, true, "回退中");
  try {
    const result = await Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}/rerun`, {
      method: "POST",
      body: JSON.stringify({ stage }),
    });
    Lumitrace.toast(result.message || "已回退并重新开始运行", "success");
  } catch (error) { Lumitrace.toast(error.message, "danger"); }
  finally {
    Lumitrace.setButtonBusy(button, false);
    state.busy = false;
    await loadProject();
  }
}

async function deleteProject(button) {
  state.busy = true;
  const confirmed = await Lumitrace.confirmAction({
    title: "删除项目",
    message: `将永久删除「${state.project?.topic || state.projectId}」及其全部产物，操作不可恢复。`,
    confirmLabel: "永久删除",
  });
  if (!confirmed) { state.busy = false; return; }
  Lumitrace.setButtonBusy(button, true, "删除中");
  try {
    await Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}`, { method: "DELETE" });
    if (state.events) state.events.close();
    localStorage.removeItem("lumitrace.project");
    Lumitrace.toast("项目已删除", "warning");
    window.location.href = "/research";
  } catch (error) {
    Lumitrace.toast(error.message, "danger");
    Lumitrace.setButtonBusy(button, false);
    state.busy = false;
    await loadProject();
  }
}

async function submitClarification(skip) {
  const button = skip ? $("clarifySkipBtn") : $("clarifySubmitBtn");
  const answers = Array.from(
    $("clarifyQuestions").querySelectorAll("[data-clarify]")
  ).map((item) => item.value);
  state.busy = true;
  Lumitrace.setButtonBusy(button, true, "提交中");
  try {
    await Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}/clarification`, {
      method: "POST",
      body: JSON.stringify({ answers, skip }),
    });
    Lumitrace.toast(skip ? "已跳过，Agent1 将使用默认值" : "回答已提交，Agent1 继续起草提纲");
  } catch (error) { Lumitrace.toast(error.message, "danger"); }
  finally {
    Lumitrace.setButtonBusy(button, false);
    state.busy = false;
    await loadProject();
  }
}

$("continueBtn").addEventListener("click", continueProject);
$("clarifySubmitBtn").addEventListener("click", () => submitClarification(false));
$("clarifySkipBtn").addEventListener("click", () => submitClarification(true));
$("retryBtn").addEventListener("click", () => retryProject($("retryBtn")));
$("failureRetryBtn").addEventListener("click", () => retryProject($("failureRetryBtn")));
$("rerunStage").addEventListener("change", updateRerunHint);
$("rerunBtn").addEventListener("click", rerunProject);
$("deleteBtn").addEventListener("click", () => deleteProject($("deleteBtn")));
$("failureDeleteBtn").addEventListener("click", () => deleteProject($("failureDeleteBtn")));
$("planMigrateBtn").addEventListener("click", () => migrateResearchPlan($("planMigrateBtn")));
$("approveBtn").addEventListener("click", () => approve(true));
$("rejectBtn").addEventListener("click", () => approve(false));
$("downloadPdfBtn").addEventListener("click", () => openDownload("/download/final-report.pdf"));
$("typesetBtn").addEventListener("click", typeset);
$("downloadTexBtn").addEventListener("click", () => openDownload("/download/final-report.tex"));

async function bootstrap() {
  await loadProject();
  connectEvents();
}

bootstrap();
