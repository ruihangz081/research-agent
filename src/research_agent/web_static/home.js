Lumitrace.mountShell("home");

const state = { projects: [], filter: "all", query: "", defaultRounds: 3 };
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

function matchesFilter(project) {
  if (state.filter === "running") return project.running;
  if (state.filter === "approval") return Boolean(project.checkpoint);
  if (state.filter === "done") return project.stage === "done";
  if (state.filter === "failed") return project.job_status === "error";
  return true;
}

function visibleProjects() {
  const query = state.query.trim().toLowerCase();
  return state.projects.filter(matchesFilter).filter((project) => !query || project.topic.toLowerCase().includes(query));
}

function renderSummary() {
  const total = state.projects.length;
  const running = state.projects.filter((item) => item.running).length;
  const approvals = state.projects.filter((item) => item.checkpoint).length;
  const done = state.projects.filter((item) => item.stage === "done").length;
  const values = [["全部研究", total, 100], ["正在运行", running, total ? running / total * 100 : 0], ["等待审批", approvals, total ? approvals / total * 100 : 0], ["已完成", done, total ? done / total * 100 : 0]];
  $("summaryStrip").innerHTML = values.map(([label, value, percent]) => `<article class="panel summary-card"><span><small>${label}</small><strong>${value}</strong></span><i class="summary-ring" style="--value:${Math.max(4, percent)}%"></i></article>`).join("");
}

function renderProjects() {
  const projects = visibleProjects();
  if (!projects.length) {
    $("projectTable").innerHTML = `<div class="empty"><span class="empty-symbol">✦</span><strong>${state.projects.length ? "没有匹配的研究" : "还没有研究项目"}</strong><p>${state.projects.length ? "尝试切换状态或搜索其他关键词。" : "创建第一个研究，溯光会从规划、搜集到报告生成全程推进。"}</p><button class="button primary" type="button" data-open-create>${Lumitrace.icon("plus", 16)}新建调研</button></div>`;
    $("projectTable").querySelector("[data-open-create]")?.addEventListener("click", openDrawer);
    return;
  }
  $("projectTable").innerHTML = `<table class="data-table"><thead><tr><th>项目名称</th><th>当前阶段</th><th>采集轮次</th><th>最近更新</th><th>运行状态</th><th>待审批提醒</th><th></th></tr></thead><tbody>${projects.map((project) => {
    const tone = Lumitrace.stageTone(project);
    const status = project.running ? "运行中" : project.stage === "done" ? "已完成" : project.job_status === "error" ? "失败" : project.checkpoint ? "待审批" : "已暂停";
    return `<tr><td><a class="table-title row-link" href="/workspace?project=${encodeURIComponent(project.id)}"><strong>${Lumitrace.escapeHtml(project.topic)}</strong><small>${Lumitrace.escapeHtml(project.id)}</small></a></td><td>${Lumitrace.stageLabel(project.stage)}</td><td>${project.collect_round} / ${project.max_collect_rounds}</td><td>${Lumitrace.formatDate(project.updated_at)}</td><td><span class="status-pill ${tone}"><i class="status-dot ${tone}"></i>${status}</span></td><td>${project.checkpoint ? `<span class="status-pill warning">1 项待审批</span>` : '<span class="muted">—</span>'}</td><td><a class="icon-button" href="/workspace?project=${encodeURIComponent(project.id)}" title="打开项目">${Lumitrace.icon("arrow", 16)}</a></td></tr>`;
  }).join("")}</tbody></table>`;
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
$("refreshProjects").addEventListener("click", loadProjects);
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

if (new URLSearchParams(window.location.search).get("new") === "1") openDrawer();
loadDefaults();
loadProjects();
