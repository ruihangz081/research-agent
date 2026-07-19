Lumitrace.mountShell("workspace");

const state = { projects: [], projectId: Lumitrace.selectedProject(), project: null, artifactKey: null, poll: null };
const $ = (id) => document.getElementById(id);
const pipelineStages = [
  ["初始化", ["init"]],
  ["战略规划", ["planning", "await_outline_approval"]],
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

function renderCheckpoint(project) {
  const panel = $("checkpointPanel");
  panel.classList.toggle("hidden", !project.checkpoint);
  if (!project.checkpoint) return;
  $("checkpointTitle").textContent = `${project.checkpoint.title}审批`;
  $("checkpointName").textContent = project.checkpoint.title;
  if (!state.artifactKey) state.artifactKey = project.checkpoint.key;
}

function previewableArtifacts(project) {
  return project.artifacts;
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
    loadArtifact();
  }));
}

function updateDownloads(project) {
  const exists = (key) => project.artifacts.some((item) => item.key === key && item.exists);
  $("downloadPdfBtn").disabled = !exists("final_report");
  $("typesetBtn").disabled = !exists("final_report") || project.running;
  $("downloadTexBtn").disabled = !exists("final_report_tex");
}

async function loadArtifact() {
  if (!state.projectId || !state.artifactKey) {
    $("artifactView").innerHTML = '<div class="empty"><span class="empty-symbol">◇</span><strong>暂无产物</strong></div>';
    return;
  }
  const selected = state.project?.artifacts.find((item) => item.key === state.artifactKey);
  if (!selected?.exists) {
    $("artifactView").innerHTML = '<div class="empty"><span class="empty-symbol">◇</span><strong>等待生成</strong><p>该产物将在对应研究阶段完成后出现。</p></div>';
    return;
  }
  try {
    const artifact = await Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}/artifacts/${encodeURIComponent(state.artifactKey)}`);
    $("artifactView").innerHTML = artifact.html || Lumitrace.renderMarkdown(artifact.content);
  } catch (error) {
    $("artifactView").innerHTML = `<div class="empty"><span class="empty-symbol">!</span><strong>内容读取失败</strong><p>${Lumitrace.escapeHtml(error.message)}</p></div>`;
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
  const runLabel = project.running ? "运行中" : project.job_status === "error" ? "运行失败" : project.checkpoint ? "等待审批" : project.stage === "done" ? "已完成" : "已暂停";
  $("runText").innerHTML = `<i class="status-dot ${tone}"></i> ${runLabel}`;
  $("continueBtn").disabled = project.running || project.stage === "done" || Boolean(project.checkpoint);
  $("roundBadge").textContent = `第 ${project.collect_round} / ${project.max_collect_rounds} 轮`;
  renderPipeline(project);
  renderTimeline(project);
  renderCheckpoint(project);
  renderArtifacts(project);
  updateDownloads(project);
}

async function loadProject() {
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

$("continueBtn").addEventListener("click", continueProject);
$("approveBtn").addEventListener("click", () => approve(true));
$("rejectBtn").addEventListener("click", () => approve(false));
$("downloadPdfBtn").addEventListener("click", () => openDownload("/download/final-report.pdf"));
$("typesetBtn").addEventListener("click", typeset);
$("downloadTexBtn").addEventListener("click", () => openDownload("/download/final-report.tex"));

loadProject();
state.poll = window.setInterval(loadProject, 3000);
