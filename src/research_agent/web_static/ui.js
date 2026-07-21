const Lumitrace = (() => {
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
    close: '<path d="m6 6 12 12M18 6 6 18"/>',
  };

  function icon(name, size = 18) {
    return `<svg class="icon" width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.file}</svg>`;
  }

  function sidebar(active) {
    const items = [
      ["home", "/research", "研究首页"],
      ["workspace", "/workspace", "项目工作区"],
      ["materials", "/materials", "材料中心"],
      ["results", "/results", "成果中心"],
      ["settings", "/settings", "设置"],
    ];
    return `
      <aside class="sidebar">
        <a class="brand" href="/" aria-label="溯光首页">
          <span class="brand-sun" aria-hidden="true"></span>
          <span><strong>溯光</strong><small>Lumitrace</small></span>
        </a>
        <a class="button primary sidebar-create" href="/research?new=1">${icon("plus")} 新建调研</a>
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

  async function api(path, options = {}) {
    const headers = options.body instanceof FormData ? {} : { "Content-Type": "application/json" };
    const response = await fetch(path, { headers, ...options });
    const contentType = response.headers.get("content-type") || "";
    const data = contentType.includes("application/json") ? await response.json() : await response.text();
    if (!response.ok) throw new Error(data?.detail || data || `${response.status} ${response.statusText}`);
    return data;
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

  function renderMarkdown(text) {
    if (!text) return '<div class="empty compact"><span class="empty-symbol">◇</span><strong>暂无内容</strong></div>';
    const source = String(text).replace(/\r\n?/g, "\n");
    const lines = source.split("\n");
    const blocks = [];
    let paragraph = [];
    let list = null;

    const inline = (value) => {
      let marker = "LUMITRACE_MARKDOWN_TOKEN";
      while (value.includes(marker)) marker += "_";
      const tokens = [];
      const stash = (html) => `${marker}${tokens.push(html) - 1}${marker}`;
      const safeUrl = (url) => /^(?:https?:\/\/|mailto:|\/|#)/i.test(url) ? url : "#";
      let output = escapeHtml(value);
      output = output.replace(/`([^`]+)`/g, (_, code) => stash(`<code>${code}</code>`));
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

  function stageLabel(stage) {
    return stageLabels[stage] || stage || "未知阶段";
  }

  function sourceStatus(status) {
    return sourceStatusLabels[status] || status;
  }

  function stageTone(project) {
    if (project.running) return "running";
    if (project.stage === "done") return "success";
    if (project.job_status === "error") return "danger";
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
    window.setTimeout(() => item.remove(), 3600);
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

  return {
    api,
    escapeHtml,
    formatDate,
    icon,
    mountShell,
    rememberProject,
    renderMarkdown,
    selectedProject,
    setButtonBusy,
    sourceStatus,
    stageLabel,
    stageTone,
    toast,
  };
})();
