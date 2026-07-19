Lumitrace.mountShell("results");

const state = { projectId: Lumitrace.selectedProject(), projects: [], project: null, artifacts: [], selectedKey: null };
const $ = (id) => document.getElementById(id);

[$("pdfIcon"), $("texIcon"), $("typesetPdfIcon")].forEach((item) => { item.innerHTML = Lumitrace.icon("download", 17); });
$("typesetIcon").innerHTML = Lumitrace.icon("arrow", 17);

function artifactGroup(key) {
  if (key === "final_report_tex" || key === "final_report_typeset_pdf") return "delivery";
  if (key.startsWith("round_") || key.startsWith("feedback_round_")) return "round";
  if (key.includes("source")) return "source";
  return "report";
}

function filteredArtifacts() {
  const type = $("typeFilter").value;
  return state.artifacts.filter((item) => !type || artifactGroup(item.key) === type);
}

function renderList() {
  const artifacts = filteredArtifacts();
  if (!artifacts.length) {
    $("resultList").innerHTML = '<div class="empty compact"><span class="empty-symbol">◇</span><strong>暂无该类型成果</strong></div>';
    return;
  }
  if (!artifacts.some((item) => item.key === state.selectedKey)) state.selectedKey = artifacts[0].key;
  $("resultList").innerHTML = artifacts.map((artifact) => `<button class="result-item${artifact.key === state.selectedKey ? " active" : ""}" type="button" data-artifact="${artifact.key}"><span class="file-icon">${Lumitrace.icon("file", 16)}</span><span><strong>${Lumitrace.escapeHtml(artifact.label)}</strong><small>${artifact.exists ? Lumitrace.escapeHtml(artifact.name || "已生成") : "等待生成"}</small></span><span class="status-pill ${artifact.exists ? "success" : ""}">${artifact.exists ? "已生成" : "待生成"}</span></button>`).join("");
  $("resultList").querySelectorAll("[data-artifact]").forEach((button) => button.addEventListener("click", () => { state.selectedKey = button.dataset.artifact; renderList(); loadPreview(); }));
}

function updateDelivery() {
  const exists = (key) => state.artifacts.some((item) => item.key === key && item.exists);
  $("downloadPdf").disabled = !exists("final_report");
  $("typeset").disabled = !exists("final_report") || state.project?.running;
  $("downloadTex").disabled = !exists("final_report_tex");
  $("downloadTypesetPdf").disabled = !exists("final_report_typeset_pdf");
  const complete = state.project?.stage === "done";
  $("generationStatus").innerHTML = `<span class="status-pill ${complete ? "success" : ""}"><i class="status-dot ${complete ? "success" : "running"}"></i>${complete ? "已完成" : "生成中"}</span>`;
  $("generationMeter").style.width = complete ? "100%" : `${Math.max(8, (state.project ? state.project.collect_round / Math.max(1, state.project.max_collect_rounds) * 70 : 0))}%`;
  $("deliveryWarning").classList.toggle("hidden", exists("final_report_typeset_pdf") || !exists("final_report_tex"));
  $("deliveryWarning").innerHTML = "高级 PDF 尚未生成。如果本机未安装 xelatex 或 lualatex，系统只会生成 TeX 源文件。";
}

async function loadPreview() {
  const artifact = state.artifacts.find((item) => item.key === state.selectedKey);
  if (!artifact) return;
  $("previewTitle").textContent = artifact.label;
  $("previewMeta").textContent = artifact.exists ? "已生成" : "等待生成";
  if (!artifact.exists) {
    $("documentPreview").innerHTML = '<div class="empty"><span class="empty-symbol">◇</span><strong>成果尚未生成</strong><p>完成对应研究阶段后即可在此预览。</p></div>';
    return;
  }
  if (artifact.key === "final_report_typeset_pdf") {
    $("documentPreview").innerHTML = `<div class="empty"><span class="empty-symbol">PDF</span><strong>高级排版 PDF 已生成</strong><p>PDF 文件请通过右侧下载区域打开。</p></div>`;
    return;
  }
  $("documentPreview").innerHTML = '<div class="empty compact"><span class="spinner"></span><strong>正在读取成果</strong></div>';
  try {
    const data = await Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}/artifacts/${encodeURIComponent(artifact.key)}`);
    $("documentPreview").innerHTML = Lumitrace.renderMarkdown(data.content);
  } catch (error) {
    $("documentPreview").innerHTML = `<div class="empty"><span class="empty-symbol">!</span><strong>预览失败</strong><p>${Lumitrace.escapeHtml(error.message)}</p></div>`;
  }
}

async function loadProject() {
  if (!state.projectId) return showEmpty();
  try {
    state.project = await Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}`);
    state.artifacts = state.project.artifacts;
    const firstExisting = state.artifacts.find((item) => item.key === "final_report" && item.exists) || state.artifacts.find((item) => item.exists) || state.artifacts[0];
    state.selectedKey = firstExisting?.key || null;
    $("resultsEmpty").classList.add("hidden");
    $("resultsContent").classList.remove("hidden");
    renderList(); updateDelivery(); await loadPreview();
  } catch (error) {
    $("resultsEmpty").innerHTML = `<span class="empty-symbol">!</span><strong>成果加载失败</strong><p>${Lumitrace.escapeHtml(error.message)}</p>`;
    showEmpty();
  }
}

function showEmpty() { $("resultsEmpty").classList.remove("hidden"); $("resultsContent").classList.add("hidden"); }

async function initialize() {
  try {
    const data = await Lumitrace.api("/api/projects");
    state.projects = data.projects;
    if (!state.projectId) state.projectId = data.projects[0]?.id || "";
    $("projectSelect").innerHTML = data.projects.length ? data.projects.map((project) => `<option value="${Lumitrace.escapeHtml(project.id)}"${project.id === state.projectId ? " selected" : ""}>${Lumitrace.escapeHtml(project.topic)}</option>`).join("") : '<option value="">暂无项目</option>';
    await loadProject();
  } catch (_) { showEmpty(); }
}

function openDownload(path) { if (state.projectId) window.open(`/api/projects/${encodeURIComponent(state.projectId)}${path}`, "_blank"); }

async function typeset() {
  Lumitrace.setButtonBusy($("typeset"), true, "排版中");
  try { const result = await Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}/typeset/final-report`, { method: "POST" }); Lumitrace.toast(result.message, result.status === "pdf" ? "success" : "warning"); await loadProject(); }
  catch (error) { Lumitrace.toast(error.message, "danger"); }
  finally { Lumitrace.setButtonBusy($("typeset"), false); }
}

$("projectSelect").addEventListener("change", () => { state.projectId = $("projectSelect").value; Lumitrace.rememberProject(state.projectId); loadProject(); });
$("typeFilter").addEventListener("change", () => { renderList(); loadPreview(); });
$("downloadPdf").addEventListener("click", () => openDownload("/download/final-report.pdf"));
$("typeset").addEventListener("click", typeset);
$("downloadTex").addEventListener("click", () => openDownload("/download/final-report.tex"));
$("downloadTypesetPdf").addEventListener("click", () => openDownload("/download/final-report-typeset.pdf"));

initialize();
