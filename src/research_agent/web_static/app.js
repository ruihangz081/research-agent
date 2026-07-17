const state = {
  projects: [],
  currentProjectId: null,
  currentArtifactKey: null,
  pollHandle: null,
};

const stageLabels = {
  init: "初始化",
  planning: "战略规划",
  await_outline_approval: "等待提纲审批",
  sourcing: "信息源分层",
  await_source_approval: "等待源草案审批",
  collecting_and_validating: "采集验证",
  await_final_source_approval: "等待最终源审批",
  analyzing: "深度分析",
  formatting: "排版交付",
  done: "已完成",
};

const $ = (id) => document.getElementById(id);

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function renderMarkdown(text) {
  if (!text) return '<div class="empty-state">暂无内容</div>';
  const blocks = [];
  let inCode = false;
  let code = [];
  let list = [];

  const flushList = () => {
    if (list.length) {
      blocks.push(`<ul>${list.map((item) => `<li>${item}</li>`).join("")}</ul>`);
      list = [];
    }
  };
  const inline = (value) =>
    escapeHtml(value)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/`([^`]+)`/g, "<code>$1</code>");

  for (const rawLine of text.split("\n")) {
    const line = rawLine.trimEnd();
    if (line.startsWith("```")) {
      if (inCode) {
        blocks.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
        code = [];
        inCode = false;
      } else {
        flushList();
        inCode = true;
      }
      continue;
    }
    if (inCode) {
      code.push(rawLine);
      continue;
    }
    if (!line.trim()) {
      flushList();
      continue;
    }
    if (line.startsWith("# ")) {
      flushList();
      blocks.push(`<h1>${inline(line.slice(2))}</h1>`);
    } else if (line.startsWith("## ")) {
      flushList();
      blocks.push(`<h2>${inline(line.slice(3))}</h2>`);
    } else if (line.startsWith("### ")) {
      flushList();
      blocks.push(`<h3>${inline(line.slice(4))}</h3>`);
    } else if (/^[-*]\s+/.test(line)) {
      list.push(inline(line.replace(/^[-*]\s+/, "")));
    } else {
      flushList();
      blocks.push(`<p>${inline(line)}</p>`);
    }
  }
  flushList();
  if (inCode) blocks.push(`<pre><code>${escapeHtml(code.join("\n"))}</code></pre>`);
  return `<div class="markdown">${blocks.join("")}</div>`;
}

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `${res.status} ${res.statusText}`);
  }
  return res.json();
}

function setBusy(isBusy) {
  $("continueBtn").disabled = isBusy || !state.currentProjectId;
  $("approveBtn").disabled = isBusy;
  $("rejectBtn").disabled = isBusy;
}

async function loadConfig() {
  const cfg = await api("/api/config");
  $("configLine").textContent = `${cfg.model} · ${cfg.has_api_key ? "API Key 已配置" : "缺少 API Key"}`;
}

async function loadProjects() {
  const data = await api("/api/projects");
  state.projects = data.projects;
  renderProjectList();
  if (!state.currentProjectId && state.projects.length) {
    state.currentProjectId = state.projects[0].id;
  }
  if (state.currentProjectId) await loadProject(state.currentProjectId);
}

function renderProjectList() {
  const list = $("projectList");
  if (!state.projects.length) {
    list.innerHTML = '<div class="empty-state">暂无项目</div>';
    return;
  }
  list.innerHTML = state.projects
    .map((project) => {
      const active = project.id === state.currentProjectId ? " active" : "";
      const stage = stageLabels[project.stage] || project.stage;
      const running = project.running ? "运行中" : stage;
      return `
        <button class="project-item${active}" type="button" data-project="${project.id}">
          <strong title="${escapeHtml(project.topic)}">${escapeHtml(project.topic)}</strong>
          <span class="project-meta"><span>${escapeHtml(running)}</span><span>${project.collect_round}/${project.max_collect_rounds}</span></span>
        </button>`;
    })
    .join("");
  list.querySelectorAll("[data-project]").forEach((button) => {
    button.addEventListener("click", async () => {
      state.currentProjectId = button.dataset.project;
      state.currentArtifactKey = null;
      await loadProject(state.currentProjectId);
      renderProjectList();
    });
  });
}

async function loadProject(projectId) {
  const project = await api(`/api/projects/${encodeURIComponent(projectId)}`);
  const stage = stageLabels[project.stage] || project.stage;
  $("projectTitle").textContent = project.topic;
  $("stageBadge").textContent = project.running ? `运行中 · ${stage}` : stage;
  $("continueBtn").disabled = project.running || project.stage === "done";
  $("roundText").textContent = `${project.collect_round} / ${project.max_collect_rounds}`;
  const percent =
    project.max_collect_rounds > 0
      ? Math.min(100, Math.round((project.collect_round / project.max_collect_rounds) * 100))
      : 0;
  $("roundMeter").style.width = `${percent}%`;

  renderCheckpoint(project);
  renderLogs(project.logs || []);
  renderArtifacts(project);
  updateDownloadButton(project);

  if (!state.currentArtifactKey) {
    const firstExisting = project.artifacts.find((item) => item.exists);
    state.currentArtifactKey = firstExisting ? firstExisting.key : null;
  }
  await loadArtifact(project.id, state.currentArtifactKey);
  setBusy(project.running);
}

function renderCheckpoint(project) {
  const panel = $("checkpointPanel");
  if (!project.checkpoint) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  $("checkpointTitle").textContent = project.checkpoint.title;
  if (!state.currentArtifactKey) state.currentArtifactKey = project.checkpoint.key;
}

function renderLogs(logs) {
  const list = $("logList");
  if (!logs.length) {
    list.innerHTML = '<div class="empty-state">暂无日志</div>';
    return;
  }
  list.innerHTML = logs
    .slice()
    .reverse()
    .map(
      (row) =>
        `<div class="log-row"><span>${escapeHtml(row.time)}</span><span>${escapeHtml(row.message)}</span></div>`,
    )
    .join("");
}

function renderArtifacts(project) {
  const tabs = $("artifactTabs");
  if (!project.artifacts.length) {
    tabs.innerHTML = "";
    return;
  }
  tabs.innerHTML = project.artifacts
    .map((artifact) => {
      const active = artifact.key === state.currentArtifactKey ? " active" : "";
      const missing = artifact.exists ? "" : " missing";
      return `<button class="tab${active}${missing}" type="button" data-artifact="${artifact.key}">${escapeHtml(artifact.label)}</button>`;
    })
    .join("");
  tabs.querySelectorAll("[data-artifact]").forEach((button) => {
    button.addEventListener("click", async () => {
      state.currentArtifactKey = button.dataset.artifact;
      renderArtifacts(project);
      await loadArtifact(project.id, state.currentArtifactKey);
    });
  });
}

function updateDownloadButton(project) {
  const hasFinalReport = project.artifacts.some(
    (artifact) => artifact.key === "final_report" && artifact.exists,
  );
  const hasTex = project.artifacts.some(
    (artifact) => artifact.key === "final_report_tex" && artifact.exists,
  );
  const hasTypesetPdf = project.artifacts.some(
    (artifact) => artifact.key === "final_report_typeset_pdf" && artifact.exists,
  );
  $("downloadPdfBtn").disabled = !hasFinalReport;
  $("typesetBtn").disabled = !hasFinalReport || project.running;
  $("downloadTexBtn").disabled = !hasTex;
  $("downloadTypesetPdfBtn").disabled = !hasTypesetPdf;
  $("downloadPdfBtn").title = hasFinalReport ? "下载普通 PDF" : "最终报告生成后可下载";
  $("typesetBtn").title = hasFinalReport ? "生成 LaTeX 源文件并尝试编译高级 PDF" : "最终报告生成后可排版";
  $("downloadTexBtn").title = hasTex ? "下载 LaTeX 源文件" : "先执行 LaTeX 排版";
  $("downloadTypesetPdfBtn").title = hasTypesetPdf ? "下载高级排版 PDF" : "需要本机 LaTeX 编译器";
}

async function loadArtifact(projectId, key) {
  if (!projectId || !key) {
    $("artifactView").innerHTML = '<div class="empty-state">暂无产物</div>';
    return;
  }
  const artifact = await api(
    `/api/projects/${encodeURIComponent(projectId)}/artifacts/${encodeURIComponent(key)}`,
  );
  $("artifactView").innerHTML = artifact.exists
    ? renderMarkdown(artifact.content)
    : '<div class="empty-state">暂无内容</div>';
}

function downloadFinalReportPdf() {
  if (!state.currentProjectId) return;
  const projectId = encodeURIComponent(state.currentProjectId);
  window.open(`/api/projects/${projectId}/download/final-report.pdf`, "_blank");
}

async function typesetFinalReport() {
  if (!state.currentProjectId) return;
  setBusy(true);
  $("typesetBtn").textContent = "排版中";
  try {
    const projectId = encodeURIComponent(state.currentProjectId);
    const result = await api(`/api/projects/${projectId}/typeset/final-report`, {
      method: "POST",
    });
    await loadProjects();
    if (result.status === "pdf") {
      window.open(`/api/projects/${projectId}/download/final-report-typeset.pdf`, "_blank");
    } else {
      alert(result.message);
      window.open(`/api/projects/${projectId}/download/final-report.tex`, "_blank");
    }
  } catch (error) {
    alert(error.message);
  } finally {
    $("typesetBtn").textContent = "LaTeX 排版";
    setBusy(false);
  }
}

function downloadFinalReportTex() {
  if (!state.currentProjectId) return;
  const projectId = encodeURIComponent(state.currentProjectId);
  window.open(`/api/projects/${projectId}/download/final-report.tex`, "_blank");
}

function downloadTypesetPdf() {
  if (!state.currentProjectId) return;
  const projectId = encodeURIComponent(state.currentProjectId);
  window.open(`/api/projects/${projectId}/download/final-report-typeset.pdf`, "_blank");
}

async function createProject(event) {
  event.preventDefault();
  setBusy(true);
  try {
    const project = await api("/api/projects", {
      method: "POST",
      body: JSON.stringify({
        topic: $("topicInput").value,
        brief: $("briefInput").value,
        max_collect_rounds: Number($("roundInput").value || 3),
      }),
    });
    state.currentProjectId = project.id;
    state.currentArtifactKey = null;
    $("createForm").reset();
    $("roundInput").value = 3;
    await loadProjects();
  } catch (error) {
    alert(error.message);
  } finally {
    setBusy(false);
  }
}

async function approveProject(approved) {
  if (!state.currentProjectId) return;
  setBusy(true);
  try {
    await api(`/api/projects/${encodeURIComponent(state.currentProjectId)}/approval`, {
      method: "POST",
      body: JSON.stringify({
        approved,
        feedback: $("feedbackInput").value,
      }),
    });
    $("feedbackInput").value = "";
    await loadProjects();
  } catch (error) {
    alert(error.message);
  } finally {
    setBusy(false);
  }
}

async function continueProject() {
  if (!state.currentProjectId) return;
  setBusy(true);
  try {
    await api(`/api/projects/${encodeURIComponent(state.currentProjectId)}/continue`, {
      method: "POST",
    });
    await loadProject(state.currentProjectId);
  } catch (error) {
    alert(error.message);
  } finally {
    setBusy(false);
  }
}

function startPolling() {
  if (state.pollHandle) window.clearInterval(state.pollHandle);
  state.pollHandle = window.setInterval(async () => {
    try {
      await loadProjects();
    } catch (error) {
      console.warn(error);
    }
  }, 3000);
}

$("createForm").addEventListener("submit", createProject);
$("refreshBtn").addEventListener("click", loadProjects);
$("continueBtn").addEventListener("click", continueProject);
$("approveBtn").addEventListener("click", () => approveProject(true));
$("rejectBtn").addEventListener("click", () => approveProject(false));
$("downloadPdfBtn").addEventListener("click", downloadFinalReportPdf);
$("typesetBtn").addEventListener("click", typesetFinalReport);
$("downloadTexBtn").addEventListener("click", downloadFinalReportTex);
$("downloadTypesetPdfBtn").addEventListener("click", downloadTypesetPdf);

loadConfig().catch(console.warn);
loadProjects().catch(console.warn);
startPolling();
