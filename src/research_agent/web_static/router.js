/* Lumitrace SPA 路由器。
 * 挂载侧边栏一次，之后在 #viewMount 内切换视图，消除整页刷新。
 * 视图内容来自后端 /api/views/{name}（从既有 HTML 切出的主内容片段），
 * 视图脚本通过 Lumitrace.views.register(name, {init, destroy}) 注册。
 */
(() => {
  const $ = (id) => document.getElementById(id);
  const VIEWS = ["research", "workspace", "materials", "results", "settings"];

  let currentView = null;   // 当前视图名
  let currentDef = null;    // 当前视图模块 { init, destroy }
  let loading = false;
  let currentController = null; // 视图片段请求的 AbortController
  const fragmentCache = new Map(); // 视图名 -> { html, title }（片段是静态的，缓存后切换零 fetch）

  function activeKey(viewName) {
    if (viewName === "research") return "home";
    return viewName;
  }

  function routeFromPath(pathname) {
    // 支持 /app/research、/app/workspace、/app/materials、/app/results、/app/settings
    const m = pathname.match(/^\/app(?:\/([a-z]+))?\/?$/);
    if (!m) return null;
    const name = m[1] || "research";
    return VIEWS.includes(name) ? name : null;
  }

  function renderSkeleton() {
    const mount = $("viewMount");
    if (!mount) return;
    mount.className = "view-loading";
    mount.innerHTML = `<div class="view-skeleton" aria-hidden="true">
      <span class="view-skeleton-bar"></span>
      <span class="view-skeleton-bar wide"></span>
      <span class="view-skeleton-bar"></span>
      <span class="view-skeleton-bar short"></span>
    </div>`;
  }

  function updateSidebarActive(viewName) {
    const items = document.querySelectorAll(".nav-item");
    items.forEach((item) => {
      const href = item.getAttribute("href") || "";
      const name = href.split("/").filter(Boolean).pop();
      const key = name === "research" ? "home" : name;
      item.classList.toggle("active", key === activeKey(viewName));
    });
  }

  function destroyCurrent() {
    if (currentDef && typeof currentDef.destroy === "function") {
      try { currentDef.destroy(); } catch (error) { console.error("[router] destroy 失败", error); }
    }
    currentDef = null;
    if (currentController) { currentController.abort(); currentController = null; }
    window.scrollTo({ top: 0, behavior: "auto" });
  }

  async function mountView(viewName, { pushState = true } = {}) {
    if (loading) return;
    if (currentView === viewName) return;
    loading = true;

    // 命中缓存时无 fetch 空窗，直接换内容，不显示骨架屏（避免闪烁）
    const cached = fragmentCache.get(viewName);
    if (!cached) renderSkeleton();

    destroyCurrent();

    currentController = new AbortController();
    try {
      const data = cached || await Lumitrace.api(`/api/views/${viewName}`, { signal: currentController.signal });
      if (!cached) fragmentCache.set(viewName, data);
      const mount = $("viewMount");
      if (!mount) return;
      document.title = `${data.title} · 溯光 Lumitrace`;

      if (pushState) {
        const query = window.location.search;
        history.pushState({ view: viewName }, "", `/app/${viewName}${query}`);
      }
      updateSidebarActive(viewName);
      const previousView = currentView;
      currentView = viewName;
      currentDef = Lumitrace.views.get(viewName);

      // 两阶段过渡：旧内容先淡出 → 同步换新内容 → 新内容淡入。
      // 不用 document.startViewTransition（回调异步，会导致 init 在 DOM
      // 就绪前访问元素抛错、页面空白）。
      const LEAVE_MS = 150;

      // 阶段 1：旧内容淡出（首次进入/骨架屏/无已渲染内容则直接跳过）
      const hasOldContent = previousView !== null && mount && mount.dataset.rendered === "1";
      if (hasOldContent) {
        mount.classList.add("view-leave");
        await new Promise((resolve) => window.setTimeout(resolve, LEAVE_MS));
      }

      // 阶段 2：同步注入新内容，并标记已渲染
      mount.classList.remove("view-leave");
      mount.className = "view-enter";
      mount.dataset.rendered = "1";
      mount.innerHTML = data.html;
      // 强制回流后移除入场类，确保 transition 生效
      void mount.offsetHeight;
      mount.classList.remove("view-enter");
      mount.classList.add("view-idle");

      // 数据初始化（此时 DOM 已注入完成）
      if (currentDef && typeof currentDef.init === "function") {
        try { await currentDef.init(mount); } catch (error) { console.error("[router] init 失败", error); }
      }
    } catch (error) {
      if (error?.message === "请求已取消") return;
      const mount = $("viewMount");
      if (mount) {
        mount.className = "";
        mount.innerHTML = `<div class="empty"><span class="empty-symbol">!</span><strong>视图加载失败</strong><p>${Lumitrace.escapeHtml(error.message)}</p></div>`;
      }
    } finally {
      loading = false;
      currentController = null;
    }
  }

  // 拦截同源导航点击：凡落在 /app/ 下的链接都走 SPA 路由，不整页跳转。
  document.addEventListener("click", (event) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    const anchor = event.target.closest("a");
    if (!anchor) return;
    const href = anchor.getAttribute("href") || "";
    if (!href || href.startsWith("http") || href.startsWith("//") || href.startsWith("#")) return;
    const target = anchor.target;
    if (target && target !== "_self") return;
    const url = new URL(href, window.location.origin);
    if (url.origin !== window.location.origin) return;
    const viewName = routeFromPath(url.pathname);
    if (!viewName) return; // 非 SPA 路径（如 / 或 /app 之外）走默认导航
    event.preventDefault();
    // 保留查询参数（如 ?project=xxx / ?new=1）
    const query = url.search;
    const clean = `/app/${viewName}${query}`;
    if (clean !== window.location.pathname + window.location.search) {
      history.pushState({ view: viewName }, "", clean);
    }
    mountView(viewName, { pushState: false });
  });

  window.addEventListener("popstate", () => {
    const viewName = routeFromPath(window.location.pathname);
    if (viewName) mountView(viewName, { pushState: false });
  });

  // 首次挂载：侧边栏 + 根据 URL 决定初始视图
  function bootstrap() {
    const initial = routeFromPath(window.location.pathname) || "research";
    Lumitrace.mountShell(activeKey(initial));
    updateSidebarActive(initial);
    mountView(initial, { pushState: false });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bootstrap);
  } else {
    bootstrap();
  }

  // 暴露编程式导航，供视图内部（如创建项目后跳转）调用
  window.Lumitrace.navigate = function (path) {
    const url = new URL(path, window.location.origin);
    const viewName = routeFromPath(url.pathname);
    if (viewName) {
      history.pushState({ view: viewName }, "", `/app/${viewName}${url.search}`);
      mountView(viewName, { pushState: false });
    } else {
      window.location.href = path;
    }
  };
})();
