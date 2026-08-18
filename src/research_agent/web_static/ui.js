const Lumitrace = (() => {
  const stageLabels = {
    init: "初始化",
    planning: "战略规划",
    await_clarification: "等待需求澄清",
    await_outline_approval: "等待提纲审批",
    sourcing: "信息源分层",
    await_source_approval: "等待源草案审批",
    collecting_and_validating: "采集验证",
    await_final_source_approval: "等待最终源审批",
    analyzing: "深度分析",
    formatting: "排版交付",
    done: "已完成",
  };

  const sourceStatusLabels = {
    created: "已创建",
    uploading: "上传中",
    quarantined: "隔离检查",
    validating: "校验中",
    needs_password: "需要密码",
    parsing: "解析中",
    ocr: "OCR 中",
    indexing: "建立索引",
    needs_review: "等待检查",
    ready: "已就绪",
    active: "已激活",
    superseded: "已替代",
    failed: "失败",
    archived: "已归档",
  };

  const icons = {
    home: '<path d="M3 11.5 12 4l9 7.5"/><path d="M5.5 10v10h13V10M9 20v-6h6v6"/>',
    workspace: '<path d="M4 5.5h6l2 2h8v11H4z"/><path d="M4 9h16"/>',
    materials: '<path d="M6 3h9l4 4v14H6z"/><path d="M15 3v5h5M9 13h7M9 17h5"/>',
    results: '<path d="M5 20V9M12 20V4M19 20v-7"/><path d="M3 20h18"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-4-4"/>',
    arrow: '<path d="m9 18 6-6-6-6"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    file: '<path d="M6 3h9l4 4v14H6z"/><path d="M15 3v5h5"/>',
    upload: '<path d="M12 16V4M7 9l5-5 5 5"/><path d="M4 15v5h16v-5"/>',
    download: '<path d="M12 4v12M7 11l5 5 5-5"/><path d="M4 20h16"/>',
    more: '<circle cx="5" cy="12" r="1"/><circle cx="12" cy="12" r="1"/><circle cx="19" cy="12" r="1"/>',
    refresh: '<path d="M20 11a8 8 0 1 0-2.3 5.7"/><path d="M20 4v7h-7"/>',
    trash: '<path d="M4 7h16"/><path d="M9 7V4h6v3"/><path d="M6 7l1 13h10l1-13"/><path d="M10 11v6M14 11v6"/>',
    close: '<path d="m6 6 12 12M18 6 6 18"/>',
  };

  function icon(name, size = 18) {
    return `<svg class="icon" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.file}</svg>`;
  }

  function sidebar(active) {
    const items = [
      ["home", "/app/research", "研究首页"],
      ["workspace", "/app/workspace", "项目工作区"],
      ["materials", "/app/materials", "材料中心"],
      ["results", "/app/results", "成果中心"],
      ["settings", "/app/settings", "设置"],
    ];
    return `
      <aside class="sidebar">
        <a class="brand" href="/" aria-label="溯光首页">
          <span class="brand-sun" aria-hidden="true"></span>
          <span><strong>溯光</strong><small>Lumitrace</small></span>
        </a>
        <a class="button primary sidebar-create" href="/app/research?new=1">${icon("plus")} 新建调研</a>
        <nav class="nav-list" aria-label="主导航">
          ${items.map(([key, href, label]) => `<a class="nav-item${active === key ? " active" : ""}" href="${href}">${icon(key)}<span>${label}</span></a>`).join("")}
        </nav>
        <div class="sidebar-spacer"></div>
        <div class="connection-card" id="connectionCard">
          <span class="status-dot neutral"></span>
          <span><strong>模型状态</strong><small>正在检查配置</small></span>
        </div>
        <div class="profile-card">
          <span class="avatar">L</span>
          <span><strong>研究员</strong><small>Local workspace</small></span>
        </div>
      </aside>`;
  }

  function mountShell(active) {
    const target = document.getElementById("sidebarMount");
    if (target) target.outerHTML = sidebar(active);
    loadConnection();
  }

  // ── 视图注册机制（SPA）─────────────────────────────────────
  // 每个视图模块调用 Lumitrace.views.register(name, { init, destroy })。
  // router.js 在注入该视图后调用 init，离开时调用 destroy，做 SSE/定时器清理。
  const _views = new Map();
  function registerView(name, def) {
    _views.set(name, def);
  }
  function getView(name) {
    return _views.get(name) || null;
  }

  async function api(path, options = {}) {
    const headers = options.body instanceof FormData ? {} : { "Content-Type": "application/json" };
    // 给每个请求挂 AbortController，避免慢请求（尤其是大产物）在导航/重试时堆积。
    // 调用方可传入外部 signal；默认不设硬超时，交给后端与浏览器。
    const controller = new AbortController();
    const externalSignal = options.signal;
    if (externalSignal) {
      if (externalSignal.aborted) controller.abort();
      else externalSignal.addEventListener("abort", () => controller.abort(), { once: true });
    }
    try {
      const response = await fetch(path, { headers, ...options, signal: controller.signal });
      const contentType = response.headers.get("content-type") || "";
      const data = contentType.includes("application/json") ? await response.json() : await response.text();
      if (!response.ok) throw new Error(data?.detail || data || `${response.status} ${response.statusText}`);
      return data;
    } catch (error) {
      if (error?.name === "AbortError") throw new Error("请求已取消");
      throw error;
    }
  }

  async function loadConnection() {
    const card = document.getElementById("connectionCard");
    if (!card) return;
    try {
      const config = await api("/api/config");
      card.querySelector(".status-dot").className = `status-dot ${config.has_api_key ? "success" : "warning"}`;
      card.querySelector("small").textContent = config.has_api_key ? `${config.model} · 已连接` : `${config.model} · 未配置 Key`;
    } catch (_) {
      card.querySelector(".status-dot").className = "status-dot danger";
      card.querySelector("small").textContent = "配置读取失败";
    }
  }

  function escapeHtml(text) {
    return String(text ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  /** 防抖：用于搜索框等高频输入，合并到最后一次触发后 delayMs 执行。 */
  function debounce(fn, delayMs = 160) {
    let timer = 0;
    return (...args) => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => fn(...args), delayMs);
    };
  }

  /** 在容器上做事件委托，按 data-* 属性分发，避免每行重复绑定监听。 */
  function delegate(container, selector, type, handler) {
    const root = typeof container === "string" ? document.querySelector(container) : container;
    if (!root) return () => {};
    const listener = (event) => {
      const target = event.target.closest(selector);
      if (target && root.contains(target)) handler(target, event);
    };
    root.addEventListener(type, listener);
    return () => root.removeEventListener(type, listener);
  }

  function renderMarkdown(text) {
    if (!text) return '<div class="empty compact"><span class="empty-symbol">◇</span><strong>暂无内容</strong></div>';
    const source = String(text).replace(/\r\n?/g, "\n");
    const lines = source.split("\n");
    const blocks = [];
    const citationNumbers = new Map();
    let paragraph = [];
    let list = null;

    const citationMarkup = (sourceId) => {
      if (!citationNumbers.has(sourceId)) citationNumbers.set(sourceId, citationNumbers.size + 1);
      const number = citationNumbers.get(sourceId);
      const projectId = selectedProject();
      const href = `/materials?project=${encodeURIComponent(projectId)}&source=${encodeURIComponent(sourceId)}`;
      return `<a class="source-citation" href="${href}" data-source-id="${sourceId}" data-citation-number="${number}" title="来源 ${sourceId}" aria-label="查看来源 ${number}：${sourceId}"><sup>${number}</sup></a>`;
    };

    const inline = (value) => {
      let marker = "LUMITRACE_MARKDOWN_TOKEN";
      while (value.includes(marker)) marker += "_";
      const tokens = [];
      const stash = (html) => `${marker}${tokens.push(html) - 1}${marker}`;
      const safeUrl = (url) => /^(?:https?:\/\/|mailto:|\/|#)/i.test(url) ? url : "#";
      let output = escapeHtml(value);
      output = output.replace(/`([^`]+)`/g, (_, code) => {
        const withCitations = code.replace(
          /\[src:\s*(src_[A-Za-z0-9_-]+)(?::v\d+)?(?:\s*,[^\]]*)?\]/gi,
          (_citation, sourceId) => citationMarkup(sourceId),
        );
        return stash(withCitations === code ? `<code>${code}</code>` : withCitations);
      });
      output = output.replace(/\[src:\s*(src_[A-Za-z0-9_-]+)(?::v\d+)?(?:\s*,[^\]]*)?\]/gi, (_, sourceId) => stash(citationMarkup(sourceId)));
      output = output.replace(/!\[([^\]]*)\]\(([^\s)]+)(?:\s+&quot;([^&]*)&quot;)?\)/g, (_, alt, url, title) => {
        const href = safeUrl(url);
        const titleAttr = title ? ` title="${title}"` : "";
        return stash(`<img src="${href}" alt="${alt}"${titleAttr} loading="lazy" />`);
      });
      output = output.replace(/\[([^\]]+)\]\(([^\s)]+)(?:\s+&quot;([^&]*)&quot;)?\)/g, (_, label, url, title) => {
        const href = safeUrl(url);
        const titleAttr = title ? ` title="${title}"` : "";
        const external = /^https?:\/\//i.test(href) ? ' target="_blank" rel="noopener noreferrer"' : "";
        return stash(`<a href="${href}"${titleAttr}${external}>${label}</a>`);
      });
      output = output
        .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
        .replace(/__(.+?)__/g, "<strong>$1</strong>")
        .replace(/~~(.+?)~~/g, "<del>$1</del>")
        .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
      return output.replace(new RegExp(`${marker}(\\d+)${marker}`, "g"), (_, index) => tokens[Number(index)]);
    };

    const splitTableRow = (row) => {
      const value = row.trim().replace(/^\|/, "").replace(/\|$/, "");
      const cells = [];
      let cell = "";
      let escaped = false;
      for (const char of value) {
        if (escaped) { cell += char; escaped = false; }
        else if (char === "\\") escaped = true;
        else if (char === "|") { cells.push(cell.trim()); cell = ""; }
        else cell += char;
      }
      cells.push(cell.trim());
      return cells;
    };
    const isTableDivider = (line) => {
      const cells = splitTableRow(line);
      return cells.length > 0 && cells.every((cell) => /^:?-{3,}:?$/.test(cell));
    };
    const flushParagraph = () => {
      if (!paragraph.length) return;
      blocks.push(`<p>${inline(paragraph.join(" "))}</p>`);
      paragraph = [];
    };
    const flushList = () => {
      if (!list) return;
      const start = list.type === "ol" && list.start !== 1 ? ` start="${list.start}"` : "";
      blocks.push(`<${list.type}${start}>${list.items.map((item) => `<li>${item}</li>`).join("")}</${list.type}>`);
      list = null;
    };
    const flushPending = () => { flushParagraph(); flushList(); };

    for (let index = 0; index < lines.length; index += 1) {
      const rawLine = lines[index];
      const line = rawLine.trimEnd();
      const fence = line.match(/^\s*(`{3,}|~{3,})\s*([\w-]+)?\s*$/);
      if (fence) {
        flushPending();
        const code = [];
        const marker = fence[1][0];
        const markerLength = fence[1].length;
        index += 1;
        while (index < lines.length && !new RegExp(`^\\s*${marker}{${markerLength},}\\s*$`).test(lines[index])) {
          code.push(lines[index]);
          index += 1;
        }
        const language = fence[2] ? ` class="language-${fence[2]}"` : "";
        blocks.push(`<pre><code${language}>${escapeHtml(code.join("\n"))}</code></pre>`);
        continue;
      }
      if (!line.trim()) { flushPending(); continue; }

      if (line.includes("|") && index + 1 < lines.length && isTableDivider(lines[index + 1])) {
        flushPending();
        const headers = splitTableRow(line);
        const dividers = splitTableRow(lines[index + 1]);
        const alignments = dividers.map((cell) => cell.startsWith(":") && cell.endsWith(":") ? "center" : cell.endsWith(":") ? "right" : "left");
        const rows = [];
        index += 2;
        while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
          rows.push(splitTableRow(lines[index]));
          index += 1;
        }
        index -= 1;
        const headerHtml = headers.map((cell, cellIndex) => `<th class="align-${alignments[cellIndex] || "left"}">${inline(cell)}</th>`).join("");
        const bodyHtml = rows.map((row) => `<tr>${headers.map((_, cellIndex) => `<td class="align-${alignments[cellIndex] || "left"}">${inline(row[cellIndex] || "")}</td>`).join("")}</tr>`).join("");
        blocks.push(`<div class="table-scroll"><table><thead><tr>${headerHtml}</tr></thead><tbody>${bodyHtml}</tbody></table></div>`);
        continue;
      }

      const heading = line.match(/^\s{0,3}(#{1,6})\s+(.+?)\s*#*$/);
      if (heading) {
        flushPending();
        const level = heading[1].length;
        blocks.push(`<h${level}>${inline(heading[2])}</h${level}>`);
        continue;
      }
      if (/^\s{0,3}(?:-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
        flushPending();
        blocks.push("<hr />");
        continue;
      }
      if (/^\s{0,3}>\s?/.test(line)) {
        flushPending();
        const quote = [];
        while (index < lines.length && /^\s{0,3}>\s?/.test(lines[index])) {
          quote.push(inline(lines[index].replace(/^\s{0,3}>\s?/, "")));
          index += 1;
        }
        index -= 1;
        blocks.push(`<blockquote>${quote.join("<br />")}</blockquote>`);
        continue;
      }

      const listItem = line.match(/^\s{0,3}([-+*]|\d+[.)])\s+(.+)$/);
      if (listItem) {
        flushParagraph();
        const type = /^\d/.test(listItem[1]) ? "ol" : "ul";
        if (list && list.type !== type) flushList();
        if (!list) list = { type, start: type === "ol" ? Number.parseInt(listItem[1], 10) : 1, items: [] };
        let content = inline(listItem[2]);
        const task = content.match(/^\[([ xX])\]\s+(.+)$/);
        if (task) content = `<input type="checkbox" disabled${task[1].toLowerCase() === "x" ? " checked" : ""} />${task[2]}`;
        list.items.push(content);
        continue;
      }
      if (list && /^\s{2,}\S/.test(rawLine)) {
        list.items[list.items.length - 1] += ` ${inline(rawLine.trim())}`;
        continue;
      }
      flushList();
      paragraph.push(line.trim());
    }
    flushPending();
    return `<div class="markdown">${blocks.join("")}</div>`;
  }

  // ── JSON 产物可读化渲染 ─────────────────────────────────────
  // 研究需求清单 / 补研任务 / 图表清单 / 验证反馈 / 任务回填 都是 JSON，
  // 直接当纯文本渲染可读性差。这里把已知的 JSON 结构转成结构化卡片/表格。

  function safeJson(text) {
    try { return JSON.parse(text); } catch (_) { return null; }
  }

  function pill(tone, label) {
    return `<span class="status-pill ${tone}">${escapeHtml(label)}</span>`;
  }

  function jsonKV(detail) {
    const rows = Object.entries(detail || {})
      .map(([k, v]) => `<div class="detail-row"><span>${escapeHtml(k)}</span><strong>${escapeHtml(String(v ?? "—"))}</strong></div>`);
    return rows.length ? `<div class="detail-list">${rows.join("")}</div>` : "";
  }

  function renderResearchRequirements(data) {
    const reqs = data.requirements || [];
    const rows = reqs.map((item) => {
      const tone = item.required ? "warning" : "neutral";
      return `<tr>
        <td><code>${escapeHtml(item.question_id)}</code></td>
        <td>${escapeHtml(item.text)}</td>
        <td>${item.required ? pill("warning", "必答") : pill("neutral", "可选")}</td>
        <td>${escapeHtml(String(item.min_supported ?? "—"))}</td>
        <td>${escapeHtml(item.min_source_tier || "不限")}</td>
        <td>${item.require_numeric ? "是" : "否"}</td>
      </tr>`;
    }).join("");
    return `<div class="json-doc">
      <div class="json-doc-head"><h3>研究需求清单</h3><span class="muted">${reqs.length} 个研究问题</span></div>
      ${data.topic ? `<p class="muted">主题：${escapeHtml(data.topic)}</p>` : ""}
      <div class="table-scroll"><table class="data-table"><thead><tr><th>ID</th><th>研究问题</th><th>必答</th><th>最低证据数</th><th>最低来源等级</th><th>需数值</th></tr></thead><tbody>${rows}</tbody></table></div>
    </div>`;
  }

  function renderResearchTasks(data) {
    const tasks = data.tasks || [];
    if (!tasks.length) return `<div class="json-doc"><div class="json-doc-head"><h3>结构化补研任务</h3></div><p class="muted">暂无补研任务。</p></div>`;
    const cards = tasks.map((t) => {
      const tone = t.priority === "critical" ? "danger" : t.status === "completed" ? "success" : t.status === "pending" ? "warning" : "neutral";
      return `<article class="json-card">
        <div class="json-card-head"><code>${escapeHtml(t.task_id)}</code>${pill(tone, escapeHtml(t.priority || t.status || "—"))}</div>
        <p>${escapeHtml(t.description)}</p>
        ${jsonKV({ 类型: t.task_type, 目标时段: t.target_period, 最低来源等级: t.min_source_tier, 独立来源数: t.required_independent_sources, 状态: t.status })}
        ${t.completion_criteria ? `<p class="muted">完成标准：${escapeHtml(t.completion_criteria)}</p>` : ""}
      </article>`;
    }).join("");
    return `<div class="json-doc"><div class="json-doc-head"><h3>结构化补研任务</h3><span class="muted">${tasks.length} 个任务</span></div>${cards}</div>`;
  }

  function renderChartManifest(data) {
    const charts = data.charts || [];
    const rows = charts.map((c) => {
      return `<tr>
        <td><code>${escapeHtml(c.id)}</code></td>
        <td>${escapeHtml(c.title || "—")}</td>
        <td>${escapeHtml(c.type || "—")}</td>
        <td>${escapeHtml(c.unit || "—")}</td>
        <td>${escapeHtml(c.as_of_date || "—")}</td>
        <td>${escapeHtml(c.source || "—")}</td>
      </tr>`;
    }).join("");
    return `<div class="json-doc">
      <div class="json-doc-head"><h3>图表清单</h3><span class="muted">${charts.length} 个图表</span></div>
      <div class="table-scroll"><table class="data-table"><thead><tr><th>ID</th><th>标题</th><th>类型</th><th>单位</th><th>日期</th><th>来源</th></tr></thead><tbody>${rows}</tbody></table></div>
    </div>`;
  }

  function renderFeedback(data) {
    const conflicts = data.conflicts || [];
    const conflictsHtml = conflicts.map((c) => {
      const values = Array.isArray(c.values) ? c.values : (c.values ? [c.values] : []);
      const resolution = c.resolution ? `<p class="muted">已解决：${escapeHtml(c.resolution)}</p>` : `<p class="muted">未解决</p>`;
      return `<article class="json-card">
        <div class="json-card-head"><strong>${escapeHtml(c.topic || "冲突")}</strong>${c.resolution ? pill("success", "已解决") : pill("danger", "未解决")}</div>
        <ul class="json-values">${values.map((v) => `<li>${escapeHtml(typeof v === "string" ? v : JSON.stringify(v))}</li>`).join("")}</ul>
        ${resolution}
      </article>`;
    }).join("");
    const gaps = (data.gap_list || []).map((g) => `<li>${escapeHtml(typeof g === "string" ? g : JSON.stringify(g))}</li>`).join("");
    return `<div class="json-doc">
      <div class="json-doc-head"><h3>验证反馈</h3>${data.round ? `<span class="muted">第 ${escapeHtml(String(data.round))} 轮</span>` : ""}</div>
      ${data.converged !== undefined ? `<p class="muted">收敛判定：${data.converged ? pill("success", "已收敛") : pill("warning", "未收敛")}</p>` : ""}
      ${data.summary ? `<div class="json-summary">${escapeHtml(data.summary)}</div>` : ""}
      ${conflicts.length ? `<h4>冲突（${conflicts.length}）</h4>${conflictsHtml}` : ""}
      ${gaps ? `<h4>缺口</h4><ul class="json-values">${gaps}</ul>` : ""}
    </div>`;
  }

  function renderTaskResults(data) {
    const results = data.results || [];
    if (!results.length) return `<div class="json-doc"><div class="json-doc-head"><h3>任务回填</h3>${data.round ? `<span class="muted">第 ${escapeHtml(String(data.round))} 轮</span>` : ""}</div><p class="muted">本轮无任务回填结果。</p></div>`;
    const cards = results.map((r) => {
      const statusTone = r.status === "completed" ? "success" : r.status === "pending" ? "warning" : "neutral";
      return `<article class="json-card">
        <div class="json-card-head"><code>${escapeHtml(r.task_id || "—")}</code>${pill(statusTone, escapeHtml(r.status || "—"))}</div>
        ${r.completion_criteria ? `<p class="muted">完成标准：${escapeHtml(r.completion_criteria)}</p>` : ""}
        ${jsonKV(r)}
      </article>`;
    }).join("");
    return `<div class="json-doc"><div class="json-doc-head"><h3>任务回填</h3>${data.round ? `<span class="muted">第 ${escapeHtml(String(data.round))} 轮</span>` : ""}</div>${cards}</div>`;
  }

  /** 根据 artifact key 渲染内容。JSON 产物走结构化渲染，其余走 Markdown。 */
  function renderArtifact(key, content) {
    const text = String(content ?? "");
    if (!text) return '<div class="empty compact"><span class="empty-symbol">◇</span><strong>暂无内容</strong></div>';

    // JSON 产物按 key 分发到对应渲染器
    const jsonRenderers = {
      research_requirements: renderResearchRequirements,
      research_tasks: renderResearchTasks,
      chart_manifest: renderChartManifest,
    };
    if (key === "research_requirements" || key === "research_tasks" || key === "chart_manifest") {
      const data = safeJson(text);
      if (data) return jsonRenderers[key](data);
    }
    // feedback_round_* 和 task_results_round_* 也按 JSON 结构化渲染
    if (key.startsWith("feedback_round_")) {
      const data = safeJson(text);
      if (data) return renderFeedback(data);
    }
    if (key.startsWith("task_results_round_")) {
      const data = safeJson(text);
      if (data) return renderTaskResults(data);
    }
    // 兜底：JSON 解析成功但无专门渲染器时，做通用美化（缩进 + 高亮），
    // 否则按 Markdown 渲染。
    const data = safeJson(text);
    if (data) {
      return `<div class="json-doc"><div class="json-doc-head"><h3>${escapeHtml(key)}</h3></div><pre class="json-pretty">${escapeHtml(JSON.stringify(data, null, 2))}</pre></div>`;
    }
    return renderMarkdown(text);
  }

  /** 让出主线程，优先使用 scheduler.yield（若浏览器支持），否则退回 macrotask。 */
  function yieldToMain() {
    if (typeof globalThis.scheduler?.yield === "function") return globalThis.scheduler.yield();
    return new Promise((resolve) => window.setTimeout(resolve, 0));
  }

  /** 异步渲染：先让出主线程让骨架屏/加载态有机会绘制，再执行同步 Markdown 解析。

   * 大文档（99KB+）的同步解析仍会阻塞一次，但骨架屏会先被 paint 出来，
   * 消除了"点击后页面瞬间白屏无响应"的观感问题。
   */
  async function renderMarkdownAsync(text) {
    await yieldToMain();
    const html = renderMarkdown(text);
    await yieldToMain();
    return html;
  }

  async function hydrateSourceCitations(root, projectId = selectedProject()) {
    const citations = [...(root?.querySelectorAll?.(".source-citation[data-source-id]") || [])];
    if (!citations.length || !projectId) return;
    try {
      const data = await api(`/api/projects/${encodeURIComponent(projectId)}/sources?include_superseded=true`);
      const sources = new Map((data.items || []).map((item) => [item.source_id, item]));
      citations.forEach((citation) => {
        const source = sources.get(citation.dataset.sourceId);
        if (!source) return;
        const number = citation.dataset.citationNumber;
        const title = source.title || source.original_filename || source.source_id;
        citation.title = `来源 ${number}：${title}\n${source.source_id} · v${source.version}`;
        citation.setAttribute("aria-label", `查看来源 ${number}：${title}`);
      });
    } catch (_) {
      // 来源标题加载失败时保留可点击编号和原始 source_id 提示。
    }
  }

  function stageLabel(stage) {
    return stageLabels[stage] || stage || "未知阶段";
  }

  function sourceStatus(status) {
    return sourceStatusLabels[status] || status;
  }

  function stageTone(project) {
    if (project.running) return "running";
    if (project.failed || project.job_status === "error") return "danger";
    if (project.stage === "done") return "success";
    if (String(project.stage).startsWith("await_")) return "warning";
    return "neutral";
  }

  function formatDate(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
  }

  function selectedProject() {
    return new URLSearchParams(window.location.search).get("project") || localStorage.getItem("lumitrace.project") || "";
  }

  function rememberProject(id) {
    if (id) localStorage.setItem("lumitrace.project", id);
  }

  function toast(message, tone = "success") {
    let region = document.querySelector(".toast-region");
    if (!region) {
      region = document.createElement("div");
      region.className = "toast-region";
      document.body.appendChild(region);
    }
    const item = document.createElement("div");
    item.className = `toast ${tone}`;
    item.innerHTML = `<span class="status-dot ${tone}"></span><span>${escapeHtml(message)}</span>`;
    region.appendChild(item);
    // 入场过渡：先插入无动画态，下一帧加 .show 触发 CSS transition。
    window.requestAnimationFrame(() => item.classList.add("show"));
    const dismiss = () => {
      item.classList.remove("show");
      window.setTimeout(() => item.remove(), 220);
    };
    item.addEventListener("click", dismiss);
    window.setTimeout(dismiss, 3600);
  }

  function setButtonBusy(button, busy, label = "处理中") {
    if (!button) return;
    if (busy) {
      button.dataset.label = button.innerHTML;
      button.innerHTML = `<span class="spinner"></span>${label}`;
      button.disabled = true;
    } else {
      button.innerHTML = button.dataset.label || button.innerHTML;
      button.disabled = false;
    }
  }

  function confirmAction({ title, message, confirmLabel = "确认", tone = "danger" }) {
    return new Promise((resolve) => {
      const backdrop = document.createElement("div");
      backdrop.className = "drawer-backdrop confirm-backdrop open";
      backdrop.innerHTML = `
        <div class="confirm-dialog" role="dialog" aria-modal="true">
          <h2>${escapeHtml(title)}</h2>
          <p>${escapeHtml(message)}</p>
          <div class="confirm-actions">
            <button class="button secondary" type="button" data-confirm="no">取消</button>
            <button class="button ${tone}" type="button" data-confirm="yes">${escapeHtml(confirmLabel)}</button>
          </div>
        </div>`;
      const previousFocus = document.activeElement;
      let settled = false;
      const finish = (value) => {
        if (settled) return;
        settled = true;
        backdrop.remove();
        document.removeEventListener("keydown", onKey, true);
        resolve(value);
        // 焦点恢复到触发元素，避免键盘用户丢失位置
        if (previousFocus && document.body.contains(previousFocus)) previousFocus.focus();
      };
      // 焦点陷阱：把 Tab 限制在对话框内
      const onKey = (event) => {
        if (!document.body.contains(backdrop)) { document.removeEventListener("keydown", onKey, true); return; }
        if (event.key === "Escape") { finish(false); return; }
        if (event.key !== "Tab") return;
        const focusables = [...backdrop.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])')]
          .filter((el) => !el.disabled && el.offsetParent !== null);
        if (!focusables.length) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
        else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      };
      backdrop.addEventListener("click", (event) => {
        if (event.target === backdrop) finish(false);
        const button = event.target.closest("[data-confirm]");
        if (button) finish(button.dataset.confirm === "yes");
      });
      document.addEventListener("keydown", onKey, true);
      document.body.appendChild(backdrop);
      backdrop.querySelector('[data-confirm="yes"]').focus();
    });
  }

  return {
    api,
    confirmAction,
    debounce,
    delegate,
    escapeHtml,
    formatDate,
    icon,
    mountShell,
    rememberProject,
    renderMarkdown,
    renderMarkdownAsync,
    renderArtifact,
    hydrateSourceCitations,
    selectedProject,
    setButtonBusy,
    sourceStatus,
    stageLabel,
    stageTone,
    toast,
    views: { register: registerView, get: getView },
  };
})();

// 顶层 const 不会自动成为 window 属性；显式挂载，供 SPA 路由与视图模块
// 通过 window.Lumitrace 访问（router.js 的 navigate、各视图的 register 判断都依赖它）。
window.Lumitrace = Lumitrace;
