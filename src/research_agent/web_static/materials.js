(() => {
const IS_SPA = window.location.pathname.startsWith("/app");

const requestedSourceId = new URLSearchParams(window.location.search).get("source");
const state = { projectId: Lumitrace.selectedProject() || "default-project", projects: [], sources: [], selectedId: requestedSourceId, _sourcesBound: false };
const $ = (id) => document.getElementById(id);
const statuses = ["created", "uploading", "quarantined", "validating", "needs_password", "parsing", "ocr", "indexing", "needs_review", "ready", "active", "superseded", "failed", "archived"];

function decorateIcons() {
  $("globalSearchIcon").innerHTML = Lumitrace.icon("search", 17);
  $("uploadIcon").innerHTML = Lumitrace.icon("upload", 17);
  $("refresh").innerHTML = Lumitrace.icon("refresh", 17);
  $("closeInspector").innerHTML = Lumitrace.icon("close", 16);
  $("closeEdit").innerHTML = Lumitrace.icon("close", 16);
  $("statusFilter").innerHTML = '<option value="">全部状态</option>' + statuses.map((status) => `<option value="${status}">${Lumitrace.sourceStatus(status)}</option>`).join("");
}

function setMessage(text, tone = "") {
  $("message").innerHTML = text ? `<span class="status-pill ${tone}">${tone === "running" ? '<span class="spinner"></span>' : `<i class="status-dot ${tone || "neutral"}"></i>`}${Lumitrace.escapeHtml(text)}</span>` : "";
}

function visibleSources() {
  const status = $("statusFilter").value;
  const tier = $("tierFilter").value;
  return state.sources.filter((item) => (!status || item.status === status) && (!tier || item.source_tier === tier));
}

function renderSummary() {
  const counts = state.sources.reduce((result, source) => { result[source.status] = (result[source.status] || 0) + 1; return result; }, {});
  $("summary").innerHTML = `<span class="summary-chip">材料 ${state.sources.length} 份</span>${Object.entries(counts).map(([status, count]) => `<span class="summary-chip">${Lumitrace.sourceStatus(status)} ${count}</span>`).join("")}`;
}

function statusTone(status) {
  if (["active", "ready"].includes(status)) return "success";
  if (["failed", "needs_password"].includes(status)) return "danger";
  if (["needs_review", "quarantined"].includes(status)) return "warning";
  return ["uploading", "validating", "parsing", "ocr", "indexing"].includes(status) ? "running" : "neutral";
}

function renderSources() {
  const sources = visibleSources();
  $("empty").classList.toggle("hidden", sources.length > 0);
  $("sources").innerHTML = sources.map((source) => {
    const tone = statusTone(source.status);
    const versions = state.sources.filter((item) => item.logical_source_id === source.logical_source_id).length;
    return `<tr data-source="${source.source_id}"><td><button class="source-name row-link" type="button" data-action="preview"><span class="file-icon">${Lumitrace.icon("file", 17)}</span><span class="table-title"><strong>${Lumitrace.escapeHtml(source.title || source.original_filename)}</strong><small>${Lumitrace.escapeHtml(source.original_filename)}</small></span></button></td><td><span class="status-pill ${tone}"><i class="status-dot ${tone}"></i>${Lumitrace.sourceStatus(source.status)}</span></td><td>v${source.version}.0</td><td><span class="tier">${Lumitrace.escapeHtml(source.source_tier)}</span></td><td>${Lumitrace.formatDate(source.updated_at || source.created_at)}</td><td><div class="row-actions"><button class="icon-button" type="button" data-action="preview" title="预览">${Lumitrace.icon("search", 15)}</button><button class="icon-button" type="button" data-action="edit" title="编辑">✎</button>${["ready", "needs_review"].includes(source.status) ? `<button class="icon-button" type="button" data-action="activate" title="激活">${Lumitrace.icon("check", 15)}</button>` : ""}<button class="icon-button" type="button" data-action="more" title="更多操作">${Lumitrace.icon("more", 16)}</button></div><div class="hidden" data-version-count="${versions}"></div></td></tr>`;
  }).join("");
  // 事件委托：容器上绑一次
  if (!state._sourcesBound) {
    state._sourcesBound = true;
    $("sources").addEventListener("click", (event) => {
      const row = event.target.closest("tr[data-source]");
      if (!row) return;
      const action = event.target.closest("[data-action]")?.dataset.action;
      handleRowAction(row.dataset.source, action);
    });
  }
  $("empty").querySelector("[data-choose-files]")?.addEventListener("click", () => $("files").click());
}

async function loadProjects() {
  try {
    const data = await Lumitrace.api("/api/projects");
    state.projects = data.projects;
    if (!state.projectId && data.projects.length) state.projectId = data.projects[0].id;
    const options = [...data.projects.map((project) => [project.id, project.topic])];
    if (!options.some(([id]) => id === state.projectId)) options.unshift([state.projectId || "default-project", state.projectId || "默认项目"]);
    $("projectSelect").innerHTML = options.map(([id, label]) => `<option value="${Lumitrace.escapeHtml(id)}"${id === state.projectId ? " selected" : ""}>当前项目：${Lumitrace.escapeHtml(label)}</option>`).join("");
  } catch (error) {
    $("projectSelect").innerHTML = `<option value="${Lumitrace.escapeHtml(state.projectId)}">当前项目：${Lumitrace.escapeHtml(state.projectId)}</option>`;
  }
}

async function refresh() {
  setMessage("正在刷新材料", "running");
  try {
    const data = await Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}/sources?include_superseded=true`);
    state.sources = data.items;
    renderSummary();
    renderSources();
    setMessage("");
    if (state.selectedId && state.sources.some((item) => item.source_id === state.selectedId)) await preview(state.selectedId);
  } catch (error) {
    state.sources = [];
    renderSummary(); renderSources();
    setMessage(`加载失败：${error.message}`, "danger");
  }
}

async function upload() {
  const files = [...$("files").files];
  if (!files.length) return;
  const body = new FormData();
  files.forEach((file) => body.append("files", file));
  setMessage(`正在上传 ${files.length} 份材料`, "running");
  try {
    const data = await Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}/source-batches`, { method: "POST", body });
    await Promise.all(data.items.filter((item) => item.job).map((item) => pollJob(item.job.job_id)));
    setMessage("材料处理完成", "success");
    $("files").value = "";
    await refresh();
  } catch (error) { setMessage(`上传失败：${error.message}`, "danger"); }
}

async function pollJob(id) {
  for (let i = 0; i < 60; i += 1) {
    const job = await Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}/source-jobs/${encodeURIComponent(id)}`);
    setMessage(`处理进度 ${job.progress}% · ${job.status}`, "running");
    if (["succeeded", "failed", "cancelled"].includes(job.status)) return job;
    await new Promise((resolve) => window.setTimeout(resolve, 500));
  }
  throw new Error("处理任务超时");
}

async function preview(id) {
  state.selectedId = id;
  $("inspectorBody").innerHTML = '<div class="empty compact"><span class="spinner"></span><strong>正在读取材料</strong></div>';
  try {
    const data = await Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}/sources/${encodeURIComponent(id)}`);
    const source = data.source;
    const document = data.document;
    $("inspectorTitle").textContent = source.title || source.original_filename;
    const warnings = document?.warnings?.length ? `<div class="warning-box">${document.warnings.map((item) => `${Lumitrace.escapeHtml(item.code)}：${Lumitrace.escapeHtml(item.message)}`).join("<br>")}</div>` : "";
    // 正文预览：取前若干个 block 的文本。过滤掉过短的碎片块（如单个词/数字），
    // 优先展示成段落的语义内容，避免"只有词汇"的可读性差问题。
    const blocks = document?.blocks || [];
    const meaningful = blocks
      .filter((b) => b.text && b.text.trim().length >= 8)
      .slice(0, 16);
    const text = meaningful.length
      ? meaningful.map((b) => b.text.trim()).join("\n\n")
      : (blocks.length ? blocks.slice(0, 16).map((b) => b.text).join("\n\n") : "");
    // 源文件链接：优先用 origin_url（网页原始出处），否则不给链接
    const originLink = source.origin_url
      ? `<a class="button secondary small" href="${Lumitrace.escapeHtml(source.origin_url)}" target="_blank" rel="noopener noreferrer">${Lumitrace.icon("arrow", 14)} 打开源文件</a>`
      : "";
    $("inspectorBody").innerHTML = `<div class="source-detail-head"><div class="inline" style="min-width:0"><span class="file-icon">${Lumitrace.icon("file", 18)}</span><span class="table-title" style="min-width:0"><strong>${Lumitrace.escapeHtml(source.original_filename)}</strong><small>v${source.version}.0 · ${Lumitrace.sourceStatus(source.status)}</small></span></div>${originLink ? `<div class="source-detail-actions">${originLink}</div>` : ""}</div>${warnings}<h3 style="margin:20px 0 10px">正文预览</h3>${text ? `<pre class="inspector-preview">${Lumitrace.escapeHtml(text)}</pre>` : '<p class="muted">该材料尚未解析出正文，可尝试重新处理。</p>'}<h3 style="margin:20px 0 12px">元数据</h3><div class="detail-list"><div class="detail-row"><span>来源等级</span><strong>${Lumitrace.escapeHtml(source.source_tier)}</strong></div><div class="detail-row"><span>材料状态</span><strong>${Lumitrace.sourceStatus(source.status)}</strong></div><div class="detail-row"><span>版本</span><strong>v${source.version}.0</strong></div>${source.origin_url ? `<div class="detail-row"><span>原始出处</span><strong><a href="${Lumitrace.escapeHtml(source.origin_url)}" target="_blank" rel="noopener noreferrer" style="color:#6274e9;word-break:break-all">${Lumitrace.escapeHtml(source.origin_url)}</a></strong></div>` : ""}</div><div class="inspector-actions"><button class="button secondary" data-detail-action="edit">编辑信息</button>${["ready", "needs_review"].includes(source.status) ? '<button class="button secondary" data-detail-action="activate">激活</button>' : ""}${!["archived", "superseded"].includes(source.status) ? '<button class="button secondary" data-detail-action="reprocess">重新处理</button>' : ""}${source.status !== "archived" ? '<button class="button danger" data-detail-action="archive">归档</button>' : ""}${state.sources.filter((item) => item.logical_source_id === source.logical_source_id).length > 1 ? '<button class="button secondary" data-detail-action="compare">版本比较</button>' : ""}</div>`;
    $("inspectorBody").querySelectorAll("[data-detail-action]").forEach((button) => button.addEventListener("click", () => handleRowAction(id, button.dataset.detailAction)));
  } catch (error) {
    $("inspectorBody").innerHTML = `<div class="empty compact"><span class="empty-symbol">!</span><strong>预览失败</strong><p>${Lumitrace.escapeHtml(error.message)}</p></div>`;
  }
}

function openEdit(source) {
  $("editSourceId").value = source.source_id;
  $("editTitle").value = source.title || source.original_filename;
  $("editTier").value = source.source_tier;
  $("editFilename").textContent = source.original_filename;
  $("editDrawer").classList.add("open");
}

async function saveEdit(event) {
  event.preventDefault();
  const button = $("saveEdit");
  Lumitrace.setButtonBusy(button, true, "保存中");
  try {
    await Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}/sources/${encodeURIComponent($("editSourceId").value)}`, { method: "PATCH", body: JSON.stringify({ title: $("editTitle").value, source_tier: $("editTier").value }) });
    closeEdit(); Lumitrace.toast("材料信息已更新"); await refresh();
  } catch (error) { Lumitrace.toast(error.message, "danger"); }
  finally { Lumitrace.setButtonBusy(button, false); }
}

function closeEdit() { $("editDrawer").classList.remove("open"); }

async function sourceAction(id, action) {
  try {
    const value = await Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}/sources/${encodeURIComponent(id)}/${action}`, { method: "POST" });
    if (value.job_id) await pollJob(value.job_id);
    Lumitrace.toast(action === "activate" ? "材料已激活" : "材料已重新处理");
    await refresh();
  } catch (error) { Lumitrace.toast(error.message, "danger"); }
}

async function archive(id) {
  if (!window.confirm("确定归档这份材料吗？归档后不会参与检索。")) return;
  try { await Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}/sources/${encodeURIComponent(id)}`, { method: "DELETE" }); Lumitrace.toast("材料已归档", "warning"); state.selectedId = null; await refresh(); clearInspector(); }
  catch (error) { Lumitrace.toast(error.message, "danger"); }
}

async function compareVersions(source) {
  const versions = state.sources.filter((item) => item.logical_source_id === source.logical_source_id).sort((a, b) => a.version - b.version);
  $("inspectorTitle").textContent = "版本比较";
  $("inspectorBody").innerHTML = '<div class="empty compact"><span class="spinner"></span><strong>正在读取版本</strong></div>';
  try {
    const data = await Promise.all(versions.map((item) => Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}/sources/${encodeURIComponent(item.source_id)}`)));
    $("inspectorBody").innerHTML = data.map((item) => `<section class="search-result"><div class="inline"><strong>${Lumitrace.escapeHtml(item.source.original_filename)} · v${item.source.version}.0</strong><span class="status-pill">${Lumitrace.sourceStatus(item.source.status)}</span></div><pre class="inspector-preview">${Lumitrace.escapeHtml(item.document ? item.document.blocks.slice(0, 8).map((block) => block.text).join("\n\n") : "等待解析")}</pre></section>`).join("");
  } catch (error) { Lumitrace.toast(error.message, "danger"); }
}

async function search() {
  const query = $("query").value.trim();
  if (!query) return Lumitrace.toast("请输入搜索关键词", "warning");
  $("inspectorTitle").textContent = "搜索结果";
  $("inspectorBody").innerHTML = '<div class="empty compact"><span class="spinner"></span><strong>正在检索材料</strong></div>';
  try {
    const data = await Lumitrace.api(`/api/projects/${encodeURIComponent(state.projectId)}/source-search`, { method: "POST", body: JSON.stringify({ query, limit: 10 }) });
    $("inspectorBody").innerHTML = data.items.length ? data.items.map((item) => `<article class="search-result"><strong>${Lumitrace.escapeHtml(item.source.original_filename)}</strong><p>${Lumitrace.escapeHtml(item.chunk.text)}</p><small>${Lumitrace.escapeHtml(item.chunk.chunk_id)} · 综合 ${item.score.toFixed(3)} · 关键词 ${item.keyword_score.toFixed(3)} · 语义 ${item.semantic_score.toFixed(3)}</small></article>`).join("") : '<div class="empty compact"><span class="empty-symbol">◇</span><strong>没有匹配结果</strong></div>';
  } catch (error) { $("inspectorBody").innerHTML = `<div class="empty compact"><span class="empty-symbol">!</span><strong>搜索失败</strong><p>${Lumitrace.escapeHtml(error.message)}</p></div>`; }
}

function handleRowAction(id, action) {
  if (!action) return;
  const source = state.sources.find((item) => item.source_id === id);
  if (!source) return;
  if (action === "preview") preview(id);
  else if (action === "edit") openEdit(source);
  else if (action === "activate" || action === "reprocess") sourceAction(id, action);
  else if (action === "archive") archive(id);
  else if (action === "compare") compareVersions(source);
  else if (action === "more") {
    const next = source.status !== "archived" ? "归档" : "版本比较";
    if (next === "归档") archive(id); else compareVersions(source);
  }
}

function clearInspector() {
  state.selectedId = null;
  $("inspectorTitle").textContent = "材料详情";
  $("inspectorBody").innerHTML = '<div class="empty compact"><span class="empty-symbol">◇</span><strong>选择一份材料</strong><p>查看正文、解析警告和元数据。</p></div>';
}

function bindEvents() {
  $("chooseFiles").addEventListener("click", () => $("files").click());
  $("files").addEventListener("change", upload);
  $("refresh").addEventListener("click", refresh);
  $("search").addEventListener("click", search);
  $("query").addEventListener("keydown", (event) => { if (event.key === "Enter") search(); });
  $("statusFilter").addEventListener("change", renderSources);
  $("tierFilter").addEventListener("change", renderSources);
  $("projectSelect").addEventListener("change", () => { state.projectId = $("projectSelect").value; Lumitrace.rememberProject(state.projectId); state.selectedId = null; clearInspector(); refresh(); });
  $("closeInspector").addEventListener("click", clearInspector);
  $("editForm").addEventListener("submit", saveEdit);
  $("closeEdit").addEventListener("click", closeEdit);
  $("cancelEdit").addEventListener("click", closeEdit);
}

function init() {
  // SPA 下 URL 的 ?project= / ?source= 会在导航时变化，进入视图时重新读取
  const fromUrl = new URLSearchParams(window.location.search).get("project");
  if (fromUrl) state.projectId = fromUrl;
  const fromSource = new URLSearchParams(window.location.search).get("source");
  if (fromSource) state.selectedId = fromSource;
  decorateIcons();
  bindEvents();
  loadProjects().then(refresh);
}

function destroy() {
  state.sources = [];
  state.projects = [];
  state.selectedId = null;
  state._sourcesBound = false;
}

// SPA：注册视图供 router 调用；旧 /materials 页面直接初始化。
if (window.Lumitrace?.views?.register) {
  Lumitrace.views.register("materials", { init, destroy });
}
if (!IS_SPA) {
  Lumitrace.mountShell("materials");
  init();
}
})();
