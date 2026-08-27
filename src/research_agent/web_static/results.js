(() => {
const IS_SPA = window.location.pathname.startsWith("/app");

const $ = (id) => document.getElementById(id);
const state = { projectId: Lumitrace.selectedProject(), projects: [], project: null, artifacts: [], selectedKey: null, events: null, _rafId: 0, _fallbackTimer: 0, _listBound: false };

function decorateIcons() {
  [$("pdfIcon"), $("texIcon")].forEach((item) => { item.innerHTML = Lumitrace.icon("download", 17); });
  $("typesetIcon").innerHTML = Lumitrace.icon("arrow", 17);
}

function artifactGroup(key) {
  if (key === "final_report_tex" || key === "chart_manifest") return "delivery";
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
  // 事件委托：容器绑一次
  if (!state._listBound) {
    state._listBound = true;
    $("resultList").addEventListener("click", (event) => {
      const button = event.target.closest("[data-artifact]");
      if (!button) return;
      state.selectedKey = button.dataset.artifact;
      renderList();
      loadPreview();
    });
  }
}

function updateDelivery() {
  const exists = (key) => state.artifacts.some((item) => item.key === key && item.exists);
  $("downloadPdf").disabled = !exists("final_report");
  $("typeset").disabled = !exists("final_report") || state.project?.running;
  $("downloadTex").disabled = !exists("final_report_tex");
  const project = state.project;
  // 状态卡片要反映真实运行状态：失败/等待确认/已暂停/降级完成都不是"生成中"
  const status = project?.stage === "done"
    ? (project?.delivery_status === "done_degraded"
      ? { pill: "warning", dot: "warning", label: "已降级完成" }
      : { pill: "success", dot: "success", label: "已完成" })
    : project?.paused
      ? { pill: "warning", dot: "warning", label: "已暂停（额度/限流）" }
      : project?.failed && !project.running
        ? { pill: "danger", dot: "danger", label: "生成失败" }
        : project?.running
          ? { pill: "", dot: "running", label: "生成中" }
          : project?.checkpoint || String(project?.stage || "").startsWith("await_")
            ? { pill: "warning", dot: "warning", label: "等待确认" }
            : { pill: "", dot: "neutral", label: "已暂停" };
  $("generationStatus").innerHTML = `<span class="status-pill ${status.pill}"><i class="status-dot ${status.dot}"></i>${status.label}</span>`;
  const progress = project ? project.collect_round / Math.max(1, project.max_collect_rounds) : 0;
  $("generationMeter").style.width = project?.stage === "done" ? "100%" : `${Math.max(8, progress * 70)}%`;
  renderDegradation();
  const warning = $("deliveryWarning");
  const showWarning = exists("final_report") && !exists("final_report_tex");
  warning.classList.toggle("hidden", !showWarning);
  if (showWarning) warning.textContent = "正式排版尚未生成，可点击“生成 / 刷新正式版”。";
}

// 降级详情：done_degraded / claims 降级 / 暂停原因都在这里展开展示
function renderDegradation() {
  const project = state.project;
  const reasons = [];
  if (project?.delivery_status === "done_degraded") reasons.push("报告已生成，部分结论台账、图表或 PDF 已降级，不影响正文阅读。");
  if (project?.claims_disabled === true) reasons.push("结论台账不可用，已关闭台账能力。");
  if (Array.isArray(project?.delivery_degradation)) reasons.push(...project.delivery_degradation);
  if (project?.analysis_dropped_claim_ids?.length) reasons.push(`${project.analysis_dropped_claim_ids.length} 条 claim 已移除：${project.analysis_dropped_claim_ids.join("、")}`);
  if (Array.isArray(project?.analysis_claim_warnings) && project.analysis_claim_warnings.length) reasons.push(`${project.analysis_claim_warnings.length} 条台账警告（详见成果）。`);
  if (project?.paused && project?.pause_reason) reasons.push(`暂停原因：${project.pause_reason}`);
  const box = $("degradationBox");
  if (!box) return;
  const hasReasons = reasons.length > 0;
  box.classList.toggle("hidden", !hasReasons);
  if (hasReasons) box.innerHTML = reasons.map((r) => `<div class="degradation-row">${Lumitrace.escapeHtml(r)}</div>`).join("");
}

async function loadPreview() {
  const artifact = state.artifacts.find((item) => item.key === state.selectedKey);
  if (!artifact) return;
  $("previewTitle").textContent = artifact.label;
  $("previewMeta").textContent = artifact.exists ? "已生成" : "等待生成";
  $("previewScroller").scrollTop = 0;
  if (!artifact.exists) {
    $("documentPreview").innerHTML = '<div class="empty"><span class="empty-symbol">◇</span><strong>成果尚未生成</strong><p>完成对应研究阶段后即可在此预览。</p></div>';
    return;
  }
  $("documentPreview").innerHTML = '<div class="empty compact"><span class="spinner"></span><strong>正在读取成果</strong></div>';
  try {
    const data = await Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}/artifacts/${encodeURIComponent(artifact.key)}`);
    const isJsonArtifact = ["research_requirements", "research_tasks", "chart_manifest"].includes(artifact.key)
      || artifact.key.startsWith("feedback_round_")
      || artifact.key.startsWith("task_results_round_");
    $("documentPreview").innerHTML = data.html || (isJsonArtifact
      ? Lumitrace.renderArtifact(artifact.key, data.content)
      : await Lumitrace.renderMarkdownAsync(data.content));
    Lumitrace.hydrateSourceCitations($("documentPreview"), state.projectId);
  } catch (error) {
    $("documentPreview").innerHTML = `<div class="empty"><span class="empty-symbol">!</span><strong>预览失败</strong><p>${Lumitrace.escapeHtml(error.message)}</p></div>`;
  }
}

async function loadProject() {
  if (!state.projectId) return showEmpty();
  try {
    const previous = state.artifacts.find((item) => item.key === state.selectedKey);
    state.project = await Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}`);
    state.artifacts = state.project.artifacts;
    // 保留用户当前选择；仅在选择失效（或首次进入）时回退到优先成果
    if (!state.artifacts.some((item) => item.key === state.selectedKey)) {
      const firstExisting = state.artifacts.find((item) => item.key === "final_report" && item.exists) || state.artifacts.find((item) => item.exists) || state.artifacts[0];
      state.selectedKey = firstExisting?.key || null;
    }
    $("resultsEmpty").classList.add("hidden");
    $("resultsContent").classList.remove("hidden");
    renderList(); updateDelivery();
    // SSE 高频刷新时，所选产物未变化就不重复拉取大报告
    const current = state.artifacts.find((item) => item.key === state.selectedKey);
    if (!previous || previous.key !== current?.key || previous.exists !== current?.exists || previous.version !== current?.version) await loadPreview();
  } catch (error) {
    disconnectEvents();
    $("resultsEmpty").innerHTML = `<span class="empty-symbol">!</span><strong>成果加载失败</strong><p>${Lumitrace.escapeHtml(error.message)}</p>`;
    showEmpty();
  }
}

function showEmpty() { $("resultsEmpty").classList.remove("hidden"); $("resultsContent").classList.add("hidden"); }

// 与工作区一致：SSE 增量推送 + rAF 合并刷新 + 断线降级轮询，
// 保证生成状态、进度条和成果列表在研究运行期间实时更新。
function scheduleRefresh() {
  if (state._rafId) return;
  state._rafId = window.requestAnimationFrame(() => {
    state._rafId = 0;
    if (state.projectId) loadProject();
  });
}

function clearFallback() {
  if (state._fallbackTimer) { window.clearInterval(state._fallbackTimer); state._fallbackTimer = 0; }
}

function startFallback() {
  if (state._fallbackTimer) return;
  state._fallbackTimer = window.setInterval(() => { if (state.projectId) loadProject(); }, 3000);
}

function connectEvents() {
  disconnectEvents();
  if (!state.projectId) return;
  const source = new EventSource(`/api/projects/${encodeURIComponent(state.projectId)}/events`);
  source.addEventListener("update", scheduleRefresh);
  source.addEventListener("open", clearFallback);
  source.addEventListener("error", startFallback);
  state.events = source;
}

function disconnectEvents() {
  if (state.events) { try { state.events.close(); } catch (_) {} state.events = null; }
  if (state._rafId) { window.cancelAnimationFrame(state._rafId); state._rafId = 0; }
  clearFallback();
}

async function initialize() {
  try {
    const data = await Lumitrace.api("/api/projects");
    state.projects = data.projects;
    if (!state.projectId) state.projectId = data.projects[0]?.id || "";
    $("projectSelect").innerHTML = data.projects.length ? data.projects.map((project) => `<option value="${Lumitrace.escapeHtml(project.id)}"${project.id === state.projectId ? " selected" : ""}>${Lumitrace.escapeHtml(project.topic)}</option>`).join("") : '<option value="">暂无项目</option>';
    await loadProject();
  } catch (_) { showEmpty(); }
}

function openDownload(path) {
  if (!state.projectId) return;
  const link = document.createElement("a");
  link.href = `/api/projects/${encodeURIComponent(state.projectId)}${path}`;
  link.download = "";
  document.body.appendChild(link);
  link.click();
  link.remove();
}

async function typeset() {
  Lumitrace.setButtonBusy($("typeset"), true, "排版中");
  try { const result = await Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}/typeset/final-report`, { method: "POST" }); Lumitrace.toast(result.message, result.status === "pdf" ? "success" : "warning"); await loadProject(); }
  catch (error) { Lumitrace.toast(error.message, "danger"); }
  finally { Lumitrace.setButtonBusy($("typeset"), false); }
}

function bindEvents() {
  $("projectSelect").addEventListener("change", () => { state.projectId = $("projectSelect").value; Lumitrace.rememberProject(state.projectId); state.selectedKey = null; loadProject(); connectEvents(); });
  $("typeFilter").addEventListener("change", () => { renderList(); loadPreview(); });
  $("downloadPdf").addEventListener("click", () => openDownload("/download/final-report.pdf"));
  $("typeset").addEventListener("click", typeset);
  $("downloadTex").addEventListener("click", () => openDownload("/download/final-report.tex"));
}

async function init() {
  decorateIcons();
  bindEvents();
  await initialize();
  connectEvents();
}

function destroy() {
  disconnectEvents();
  state.project = null;
  state.artifacts = [];
  state.selectedKey = null;
  state._listBound = false;
}

// SPA：注册视图供 router 调用；旧 /results 页面直接初始化。
if (window.Lumitrace?.views?.register) {
  Lumitrace.views.register("results", { init, destroy });
}
if (!IS_SPA) {
  Lumitrace.mountShell("results");
  init();
}
})();
