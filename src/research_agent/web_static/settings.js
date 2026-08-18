(() => {
const IS_SPA = window.location.pathname.startsWith("/app");

const $ = (id) => document.getElementById(id);
let config = null;

function value(id, content) { $(id).value = content ?? ""; }

function renderSearchKeyState() {
  const provider = $("searchProvider").value;
  const acceptsKey = provider !== "duckduckgo";
  const needsKey = ["serpapi", "tavily"].includes(provider);
  $("searchKey").disabled = !acceptsKey;
  $("searchKeyHelp").textContent = needsKey
    ? `${provider} 需要 API Key；未配置时搜索会直接报错而不静默降级。`
    : provider === "anysearch"
      ? "AnySearch API Key 可选；未配置时使用匿名额度，失败时降级到 DuckDuckGo。"
      : "DuckDuckGo 无需 Key。";
}

function renderStatus() {
  if (!config) return;
  $("configStatus").innerHTML = `<div class="detail-row"><span>模型 API</span><strong><i class="status-dot ${config.has_api_key ? "success" : "warning"}"></i> ${config.has_api_key ? "已配置" : "缺少 Key"}</strong></div><div class="detail-row"><span>搜索服务</span><strong><i class="status-dot success"></i> ${Lumitrace.escapeHtml(config.search_provider || "anysearch")}</strong></div><div class="detail-row"><span>语义检索</span><strong><i class="status-dot ${config.embedding_model ? "success" : "neutral"}"></i> ${config.embedding_model ? "已启用" : "未启用"}</strong></div>`;
}

function populateConfig() {
  value("baseUrl", config.base_url);
  $("apiKey").value = "";
  $("apiKey").placeholder = config.has_api_key ? "已配置；留空则保留当前 Key" : "请输入 API Key";
  value("model", config.model);
  value("timeout", config.timeout ?? 120);
  value("retries", config.max_retries ?? 3);
  value("temperature", config.temperature ?? 0.7);
  value("searchProvider", config.search_provider || "anysearch");
  $("searchKey").value = "";
  $("searchKey").placeholder = config.has_search_api_key ? "已配置；留空则保留当前 Key" : "请输入搜索 API Key";
  renderSearchKeyState();
  value("embeddingModel", config.embedding_model || "未配置");
  value("embeddingStatus", config.embedding_model ? "语义向量检索已启用" : "使用离线关键词检索");
  value("defaultRounds", config.default_rounds ?? 3);
  value("outputPreference", config.output_preference || "balanced");
  value("projectsDir", config.projects_dir);
  value("sourceDir", config.source_data_dir || ".data/sources");
  renderStatus();
}

function workspacePayload() {
  return {
    default_rounds: Number($("defaultRounds").value),
    output_preference: $("outputPreference").value,
    projects_dir: $("projectsDir").value.trim(),
    source_data_dir: $("sourceDir").value.trim(),
  };
}

function validateWorkspacePayload(payload) {
  if (!Number.isInteger(payload.default_rounds) || payload.default_rounds < 1 || payload.default_rounds > 5) throw new Error("采集验证轮数应为 1–5");
  if (!["fast", "balanced", "deep"].includes(payload.output_preference)) throw new Error("请选择输出偏好");
  if (!payload.projects_dir.startsWith("/")) throw new Error("项目目录必须是绝对路径");
  if (!payload.source_data_dir.startsWith("/")) throw new Error("材料目录必须是绝对路径");
}

async function saveWorkspaceConfig() {
  const button = $("saveWorkspace");
  try {
    const payload = workspacePayload();
    validateWorkspacePayload(payload);
    Lumitrace.setButtonBusy(button, true, "保存中");
    config = await Lumitrace.api("/api/config/workspace", { method: "PUT", body: JSON.stringify(payload) });
    populateConfig();
    Lumitrace.toast(config.source_restart_required ? "设置已保存；材料目录将在重启服务后生效" : "调研与存储设置已保存");
  } catch (error) {
    Lumitrace.toast(error.message, "danger");
  } finally {
    Lumitrace.setButtonBusy(button, false);
  }
}

async function loadConfig() {
  try {
    config = await Lumitrace.api("/api/config");
    populateConfig();
  } catch (error) {
    $("configStatus").innerHTML = `<div class="warning-box">配置读取失败：${Lumitrace.escapeHtml(error.message)}</div>`;
  }
}

function modelPayload() {
  return {
    base_url: $("baseUrl").value.trim(),
    api_key: $("apiKey").value.trim() || null,
    model: $("model").value.trim(),
    timeout: Number($("timeout").value),
    max_retries: Number($("retries").value),
    temperature: Number($("temperature").value),
  };
}

function validatePayload(payload) {
  if (!payload.base_url.startsWith("http://") && !payload.base_url.startsWith("https://")) throw new Error("Base URL 必须以 http:// 或 https:// 开头");
  if (!payload.model) throw new Error("请输入模型名称");
  if (!Number.isFinite(payload.timeout) || payload.timeout < 1 || payload.timeout > 600) throw new Error("超时时间应为 1–600 秒");
  if (!Number.isInteger(payload.max_retries) || payload.max_retries < 1 || payload.max_retries > 10) throw new Error("重试次数应为 1–10");
  if (!Number.isFinite(payload.temperature) || payload.temperature < 0 || payload.temperature > 2) throw new Error("温度应为 0–2");
}

async function saveModelConfig() {
  const button = $("saveModel");
  try {
    const payload = modelPayload();
    validatePayload(payload);
    Lumitrace.setButtonBusy(button, true, "保存中");
    config = await Lumitrace.api("/api/config/model", { method: "PUT", body: JSON.stringify(payload) });
    populateConfig();
    Lumitrace.toast("模型配置已保存并生效");
  } catch (error) {
    Lumitrace.toast(error.message, "danger");
  } finally {
    Lumitrace.setButtonBusy(button, false);
  }
}

async function testConnection() {
  const button = $("testConnection");
  try {
    const payload = modelPayload();
    validatePayload(payload);
    if (!payload.api_key && !config?.has_api_key) throw new Error("请先输入 API Key");
    Lumitrace.setButtonBusy(button, true, "测试中");
    const result = await Lumitrace.api("/api/config/model/test", { method: "POST", body: JSON.stringify(payload) });
    Lumitrace.toast(result.message || "连接成功");
  } catch (error) {
    Lumitrace.toast(error.message, "danger");
  } finally {
    Lumitrace.setButtonBusy(button, false);
  }
}

async function saveSearchConfig() {
  const button = $("saveSearch");
  try {
    const provider = $("searchProvider").value;
    const apiKey = $("searchKey").value.trim();
    if (["serpapi", "tavily"].includes(provider) && !apiKey && !config?.has_search_api_key) {
      throw new Error(`${provider} 需要搜索 API Key`);
    }
    Lumitrace.setButtonBusy(button, true, "保存中");
    config = await Lumitrace.api("/api/config/search", {
      method: "PUT",
      body: JSON.stringify({ provider, api_key: apiKey || null }),
    });
    populateConfig();
    Lumitrace.toast("搜索配置已保存并生效");
  } catch (error) {
    Lumitrace.toast(error.message, "danger");
  } finally {
    Lumitrace.setButtonBusy(button, false);
  }
}

async function testSearch() {
  const button = $("testSearch");
  Lumitrace.setButtonBusy(button, true, "测试中");
  try {
    const result = await Lumitrace.api("/api/config/search/test", { method: "POST" });
    Lumitrace.toast(`${result.provider}：${result.message}`);
  } catch (error) {
    Lumitrace.toast(error.message, "danger");
  } finally {
    Lumitrace.setButtonBusy(button, false);
  }
}

function init() {
  $("saveModel").addEventListener("click", saveModelConfig);
  $("testConnection").addEventListener("click", testConnection);
  $("saveWorkspace").addEventListener("click", saveWorkspaceConfig);
  $("saveSearch").addEventListener("click", saveSearchConfig);
  $("testSearch").addEventListener("click", testSearch);
  $("searchProvider").addEventListener("change", renderSearchKeyState);
  $("copyEnv").addEventListener("click", async () => {
    const template = `LLM_BASE_URL=${config?.base_url || "https://api.openai.com/v1"}\nLLM_API_KEY=\nLLM_MODEL=${config?.model || "gpt-4o"}\nLLM_TIMEOUT=${config?.timeout || 120}\nLLM_MAX_RETRIES=${config?.max_retries || 3}\nLLM_TEMPERATURE=${config?.temperature ?? 0.7}\nSEARCH_API_PROVIDER=${config?.search_provider || "anysearch"}\nSEARCH_API_KEY=\nSOURCE_EMBEDDING_BASE_URL=\nSOURCE_EMBEDDING_API_KEY=\nSOURCE_EMBEDDING_MODEL=\nMAX_COLLECT_ROUNDS=${config?.default_rounds || 3}\nOUTPUT_PREFERENCE=${config?.output_preference || "balanced"}\nPROJECTS_DIR=${config?.projects_dir || ""}\nSOURCE_DATA_DIR=${config?.source_data_dir || ""}`;
    try { await navigator.clipboard.writeText(template); Lumitrace.toast("配置模板已复制"); }
    catch (_) { Lumitrace.toast("浏览器不允许复制，请直接编辑项目中的 .env", "warning"); }
  });
  loadConfig();
}

function destroy() {
  config = null;
}

// SPA：注册视图供 router 调用；旧 /settings 页面直接初始化。
if (window.Lumitrace?.views?.register) {
  Lumitrace.views.register("settings", { init, destroy });
}
if (!IS_SPA) {
  Lumitrace.mountShell("settings");
  init();
}
})();
