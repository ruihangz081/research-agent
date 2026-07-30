Lumitrace.mountShell("home");

const state = { projects: [], filter: "all", query: "", defaultRounds: 3, usage: null, usageRange: "daily" };
const $ = (id) => document.getElementById(id);

$("topSearchIcon").innerHTML = Lumitrace.icon("search", 17);
$("newIcon").innerHTML = Lumitrace.icon("plus", 17);
$("refreshProjects").innerHTML = Lumitrace.icon("refresh", 17);
$("closeCreate").innerHTML = Lumitrace.icon("close", 17);

function renderRoundPicker() {
  $("roundInput").value = state.defaultRounds;
  $("roundPicker").innerHTML = [1, 2, 3, 4, 5].map((value) => `<button class="round-option${value === state.defaultRounds ? " active" : ""}" type="button" data-round="${value}">${value}</button>`).join("");
}

async function loadDefaults() {
  try {
    const config = await Lumitrace.api("/api/config");
    state.defaultRounds = Number(config.default_rounds) || 3;
  } catch (_) {
    state.defaultRounds = 3;
  }
  renderRoundPicker();
}

// ─── Token 用量版块 ───────────────────────────────────────────

const DAY_MS = 86400000;

function formatTokens(value) {
  const count = Number(value) || 0;
  if (count >= 100000000) return `${(count / 100000000).toFixed(count >= 1000000000 ? 0 : 1)}亿`;
  if (count >= 10000) return `${(count / 10000).toFixed(count >= 1000000 ? 0 : 1)}万`;
  return count.toLocaleString("zh-CN");
}

function bucketDaily(daily) {
  const map = new Map();
  daily.forEach((row) => map.set(row.date, row));
  return map;
}

/** 把每日数据按周（周一起算）合并，用于"每周"视图。 */
function bucketWeekly(daily) {
  const map = new Map();
  daily.forEach((row) => {
    const date = new Date(`${row.date}T00:00:00`);
    const offset = (date.getDay() + 6) % 7;
    const monday = new Date(date.getTime() - offset * DAY_MS);
    const key = monday.toISOString().slice(0, 10);
    const existing = map.get(key) || { date: key, total_tokens: 0, calls: 0 };
    existing.total_tokens += row.total_tokens;
    existing.calls += row.calls;
    map.set(key, existing);
  });
  return map;
}

/** 累计视图：逐日累加，展示消耗的增长曲线。 */
function bucketCumulative(daily) {
  const map = new Map();
  let running = 0;
  daily.forEach((row) => {
    running += row.total_tokens;
    map.set(row.date, { date: row.date, total_tokens: running, calls: row.calls });
  });
  return map;
}

function heatLevel(value, max) {
  if (!value) return 0;
  if (max <= 0) return 0;
  const ratio = value / max;
  if (ratio > 0.6) return 4;
  if (ratio > 0.3) return 3;
  if (ratio > 0.1) return 2;
  return 1;
}

function renderUsageStats(usage) {
  const hasData = usage.total_tokens > 0;
  const cards = [
    ["累计 Token 数", hasData ? formatTokens(usage.total_tokens) : "—"],
    ["峰值单日 Token", hasData ? formatTokens(usage.peak_daily_tokens) : "—"],
    ["模型调用次数", hasData ? usage.calls.toLocaleString("zh-CN") : "—"],
    ["当前连续天数", hasData ? `${usage.current_streak} 天` : "—"],
    ["最长连续天数", hasData ? `${usage.longest_streak} 天` : "—"],
  ];
  $("usageStats").innerHTML = cards.map(([label, value]) => `<div class="usage-stat${hasData ? "" : " muted-value"}"><strong>${Lumitrace.escapeHtml(value)}</strong><small>${label}</small></div>`).join("");
}

function renderUsageHeatmap(usage) {
  const days = Math.max(7, Number(usage.days) || 364);
  const buckets = state.usageRange === "weekly"
    ? bucketWeekly(usage.daily || [])
    : state.usageRange === "total"
      ? bucketCumulative(usage.daily || [])
      : bucketDaily(usage.daily || []);
  const max = Math.max(0, ...Array.from(buckets.values(), (row) => row.total_tokens));

  // 末列对齐本周，首列回退到 days 天前的周一，保证 7 行对应周一至周日
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const endOffset = (today.getDay() + 6) % 7;
  const lastMonday = new Date(today.getTime() - endOffset * DAY_MS);
  const weeks = Math.ceil(days / 7);
  const start = new Date(lastMonday.getTime() - (weeks - 1) * 7 * DAY_MS);

  const cells = [];
  const monthLabels = [];
  let lastMonth = -1;
  for (let week = 0; week < weeks; week += 1) {
    const columnStart = new Date(start.getTime() + week * 7 * DAY_MS);
    const month = columnStart.getMonth();
    monthLabels.push(month === lastMonth ? "" : `${month + 1}月`);
    lastMonth = month;
    for (let day = 0; day < 7; day += 1) {
      const current = new Date(columnStart.getTime() + day * DAY_MS);
      if (current > today) { cells.push('<i class="usage-cell is-empty"></i>'); continue; }
      const key = current.toISOString().slice(0, 10);
      const lookup = state.usageRange === "weekly" ? columnStart.toISOString().slice(0, 10) : key;
      const row = buckets.get(lookup);
      const value = row ? row.total_tokens : 0;
      const title = value
        ? `${key}：${formatTokens(value)} tokens（${row.calls} 次调用）`
        : `${key}：无消耗`;
      cells.push(`<i class="usage-cell level-${heatLevel(value, max)}" title="${title}"></i>`);
    }
  }
  $("usageHeatmap").innerHTML = cells.join("");
  $("usageMonths").innerHTML = monthLabels.map((label) => `<span>${label}</span>`).join("");
}

function renderUsageRows(target, rows, labelKey) {
  if (!rows.length) {
    $(target).innerHTML = '<p class="usage-empty">暂无数据，运行一次调研后即可看到分布。</p>';
    return;
  }
  const max = Math.max(...rows.map((row) => row.total_tokens));
  $(target).innerHTML = rows.slice(0, 6).map((row) => {
    const percent = max > 0 ? Math.max(3, (row.total_tokens / max) * 100) : 3;
    return `<div class="usage-row"><span title="${Lumitrace.escapeHtml(row[labelKey])}">${Lumitrace.escapeHtml(row[labelKey])}</span><span>${formatTokens(row.total_tokens)}</span><div class="usage-row-bar"><i style="width:${percent}%"></i></div></div>`;
  }).join("");
}

function renderUsage() {
  const usage = state.usage;
  if (!usage) return;
  renderUsageStats(usage);
  renderUsageHeatmap(usage);
  renderUsageRows("usageStages", usage.stages || [], "stage");
  renderUsageRows("usageProjects", usage.projects || [], "topic");
}

async function loadUsage() {
  try {
    state.usage = await Lumitrace.api("/api/usage");
    renderUsage();
  } catch (error) {
    $("usageStats").innerHTML = `<p class="usage-empty">用量数据读取失败：${Lumitrace.escapeHtml(error.message)}</p>`;
  }
}

function needsUserInput(project) {  return Boolean(project.checkpoint) || project.stage === "await_clarification";
}

function matchesFilter(project) {
  if (state.filter === "running") return project.running;
  if (state.filter === "approval") return needsUserInput(project);
  if (state.filter === "done") return project.stage === "done";
  if (state.filter === "failed") return Boolean(project.failed) || project.job_status === "error";
  return true;
}

function visibleProjects() {
  const query = state.query.trim().toLowerCase();
  return state.projects.filter(matchesFilter).filter((project) => !query || project.topic.toLowerCase().includes(query));
}

function renderSummary() {
  const total = state.projects.length;
  const running = state.projects.filter((item) => item.running).length;
  const approvals = state.projects.filter(needsUserInput).length;
  const done = state.projects.filter((item) => item.stage === "done").length;
  const failed = state.projects.filter((item) => item.failed || item.job_status === "error").length;
  const values = [["全部研究", total, 100], ["正在运行", running, total ? running / total * 100 : 0], ["等待审批", approvals, total ? approvals / total * 100 : 0], ["已完成", done, total ? done / total * 100 : 0], ["失败待处理", failed, total ? failed / total * 100 : 0]];
  $("summaryStrip").innerHTML = values.map(([label, value, percent]) => `<article class="panel summary-card"><span><small>${label}</small><strong>${value}</strong></span><i class="summary-ring" style="--value:${Math.max(4, percent)}%"></i></article>`).join("");
}

function renderProjects() {
  const projects = visibleProjects();
  if (!projects.length) {
    $("projectTable").innerHTML = `<div class="empty"><span class="empty-symbol">✦</span><strong>${state.projects.length ? "没有匹配的研究" : "还没有研究项目"}</strong><p>${state.projects.length ? "尝试切换状态或搜索其他关键词。" : "创建第一个研究，溯光会从规划、搜集到报告生成全程推进。"}</p><button class="button primary" type="button" data-open-create>${Lumitrace.icon("plus", 16)}新建调研</button></div>`;
    $("projectTable").querySelector("[data-open-create]")?.addEventListener("click", openDrawer);
    return;
  }
  $("projectTable").innerHTML = `<table class="data-table"><thead><tr><th>项目名称</th><th>当前阶段</th><th>采集轮次</th><th>最近更新</th><th>运行状态</th><th>待审批提醒</th><th class="align-right">操作</th></tr></thead><tbody>${projects.map((project) => {
    const tone = Lumitrace.stageTone(project);
    const failed = Boolean(project.failed) || project.job_status === "error";
    const status = project.running ? "运行中" : failed ? "失败" : project.stage === "done" ? "已完成" : project.stage === "await_clarification" ? "待澄清" : project.checkpoint ? "待审批" : "已暂停";
    const id = Lumitrace.escapeHtml(project.id);
    const retryButton = project.can_retry
      ? `<button class="icon-button" type="button" data-retry="${id}" title="重试失败阶段">${Lumitrace.icon("refresh", 16)}</button>`
      : "";
    const failureHint = failed && project.last_error
      ? `<small class="row-error" title="${Lumitrace.escapeHtml(project.last_error)}">${Lumitrace.escapeHtml(project.last_error.slice(0, 60))}${project.last_error.length > 60 ? "…" : ""}</small>`
      : "";
    return `<tr><td><a class="table-title row-link" href="/workspace?project=${encodeURIComponent(project.id)}"><strong>${Lumitrace.escapeHtml(project.topic)}</strong><small>${id}</small></a></td><td>${Lumitrace.stageLabel(project.stage)}</td><td>${project.collect_round} / ${project.max_collect_rounds}</td><td>${Lumitrace.formatDate(project.updated_at)}</td><td><span class="status-pill ${tone}"><i class="status-dot ${tone}"></i>${status}</span>${failureHint}</td><td>${project.stage === "await_clarification" ? '<span class="status-pill warning">需求澄清待回答</span>' : project.checkpoint ? `<span class="status-pill warning">1 项待审批</span>` : '<span class="muted">—</span>'}</td><td><div class="row-actions">${retryButton}<button class="icon-button danger" type="button" data-delete="${id}" title="删除项目"${project.running ? " disabled" : ""}>${Lumitrace.icon("trash", 16)}</button><a class="icon-button" href="/workspace?project=${encodeURIComponent(project.id)}" title="打开项目">${Lumitrace.icon("arrow", 16)}</a></div></td></tr>`;
  }).join("")}</tbody></table>`;
  $("projectTable").querySelectorAll("[data-retry]").forEach((button) => button.addEventListener("click", () => retryProject(button.dataset.retry, button)));
  $("projectTable").querySelectorAll("[data-delete]").forEach((button) => button.addEventListener("click", () => deleteProject(button.dataset.delete, button)));
}

async function retryProject(projectId, button) {
  Lumitrace.setButtonBusy(button, true, "");
  try {
    const result = await Lumitrace.api(`/api/projects/${encodeURIComponent(projectId)}/retry`, { method: "POST", body: JSON.stringify({ extra_rounds: 1 }) });
    Lumitrace.toast(result.message || "已重新开始运行");
  } catch (error) { Lumitrace.toast(error.message, "danger"); }
  finally { Lumitrace.setButtonBusy(button, false); await loadProjects(); }
}

async function deleteProject(projectId, button) {
  const project = state.projects.find((item) => item.id === projectId);
  const confirmed = await Lumitrace.confirmAction({
    title: "删除项目",
    message: `将永久删除「${project?.topic || projectId}」及其全部产物，操作不可恢复。`,
    confirmLabel: "永久删除",
  });
  if (!confirmed) return;
  Lumitrace.setButtonBusy(button, true, "");
  try {
    await Lumitrace.api(`/api/projects/${encodeURIComponent(projectId)}`, { method: "DELETE" });
    if (localStorage.getItem("lumitrace.project") === projectId) localStorage.removeItem("lumitrace.project");
    Lumitrace.toast("项目已删除", "warning");
  } catch (error) { Lumitrace.toast(error.message, "danger"); }
  finally { Lumitrace.setButtonBusy(button, false); await loadProjects(); }
}

async function loadProjects() {
  try {
    const data = await Lumitrace.api("/api/projects");
    state.projects = data.projects;
    renderSummary();
    renderProjects();
  } catch (error) {
    $("projectTable").innerHTML = `<div class="empty"><span class="empty-symbol">!</span><strong>项目加载失败</strong><p>${Lumitrace.escapeHtml(error.message)}</p><button class="button secondary" data-retry>重试</button></div>`;
    $("projectTable").querySelector("[data-retry]")?.addEventListener("click", loadProjects);
  }
}

function openDrawer() {
  $("createDrawer").classList.add("open");
  $("createDrawer").setAttribute("aria-hidden", "false");
  window.setTimeout(() => $("topicInput").focus(), 50);
}

function closeDrawer() {
  $("createDrawer").classList.remove("open");
  $("createDrawer").setAttribute("aria-hidden", "true");
}

async function createProject(event) {
  event.preventDefault();
  const button = $("submitCreate");
  Lumitrace.setButtonBusy(button, true, "创建中");
  try {
    const project = await Lumitrace.api("/api/projects", { method: "POST", body: JSON.stringify({ topic: $("topicInput").value, brief: $("briefInput").value, max_collect_rounds: Number($("roundInput").value) }) });
    Lumitrace.rememberProject(project.id);
    window.location.href = `/workspace?project=${encodeURIComponent(project.id)}`;
  } catch (error) {
    Lumitrace.toast(error.message, "danger");
    Lumitrace.setButtonBusy(button, false);
  }
}

$("openCreate").addEventListener("click", openDrawer);
$("closeCreate").addEventListener("click", closeDrawer);
$("cancelCreate").addEventListener("click", closeDrawer);
$("createDrawer").addEventListener("click", (event) => { if (event.target === $("createDrawer")) closeDrawer(); });
$("createForm").addEventListener("submit", createProject);
$("refreshProjects").addEventListener("click", () => { loadProjects(); loadUsage(); });
$("projectSearch").addEventListener("input", (event) => { state.query = event.target.value; renderProjects(); });
$("filterTabs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-filter]");
  if (!button) return;
  state.filter = button.dataset.filter;
  $("filterTabs").querySelectorAll(".filter-tab").forEach((item) => item.classList.toggle("active", item === button));
  renderProjects();
});
$("roundPicker").addEventListener("click", (event) => {
  const button = event.target.closest("[data-round]");
  if (!button) return;
  $("roundInput").value = button.dataset.round;
  $("roundPicker").querySelectorAll(".round-option").forEach((item) => item.classList.toggle("active", item === button));
});

$("usageRangeTabs").addEventListener("click", (event) => {
  const button = event.target.closest("[data-range]");
  if (!button) return;
  state.usageRange = button.dataset.range;
  $("usageRangeTabs").querySelectorAll(".filter-tab").forEach((item) => item.classList.toggle("active", item === button));
  renderUsage();
});

if (new URLSearchParams(window.location.search).get("new") === "1") openDrawer();
loadDefaults();
loadProjects();
loadUsage();
