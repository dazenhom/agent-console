/* Agent Console 前端逻辑（原生 JS，移动端优先）。 */
(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);

  // BASE：当前页面所在目录前缀。根路径访问("/")时为空；经 code-server /proxy/80/ 等
  // 子路径访问时为 "/proxy/80"。所有 API/WS/资源请求都加这个前缀，两种访问方式都兼容。
  const BASE = location.pathname.replace(/\/+$/, "").replace(/\/index\.html$/, "");

  const state = {
    token: localStorage.getItem("ac_token") || "",
    sessionId: localStorage.getItem("ac_session") || "",
    ws: null,
    reconnectTimer: null,
    sessions: [],
    archivedSessions: [],
    sessionView: "active",  // Sessions Tab 当前视图：active / archived
    typingEl: null,
    streamEl: null,      // 当前流式气泡的 DOM 节点
    streamText: "",      // 已累积的流式文本
    wasDisconnected: false,
    monitorWs: null,     // 监控通道 WS（会话列表实时状态）
    monitorTimer: null,
    pendingImages: [],   // 待发送的图片，每项为 { path: 绝对路径, dataUrl: base64预览 }
    tab: "overview",     // 当前激活的顶部 Tab
    taskBySession: {},   // session_id -> 最近一条 task（用于派生状态徽章/看板计数）
    toolIdMap: {},       // tool_use_id -> tool_name（用于 tool_result 反查工具名）
    histMsgs: [],        // 当前会话全量历史消息（窗口渲染用）
    histShown: 0,        // 已渲染的末尾消息条数
    heartbeatTimer: null, // WS 应用层心跳定时器
    queue: [],           // 当前会话排队待执行的指令
    drafts: {},          // sessionId -> { text: string, images: [{path, dataUrl}] }（草稿按会话隔离）
    agentGroups: {},     // Agent tool_use id -> 该子智能体卡片的 .subagent-body 元素（内部步骤归拢用）
    _histGroups: {},     // 历史渲染专用的 Agent id -> body 映射，跨批次共享以关联加载更早的卡片/结果
    subStreams: {},      // Agent id -> { bubble, text } 子智能体正在流式的气泡（各卡片独立打字机）
    subagentExpanded: false, // 全局开关：是否展开所有子智能体的思考/执行过程
  };

  // ---------------- API ----------------
  // 经 SSH 隧道/Tailscale 访问时链路偶尔抖动，单次 fetch 易报 "Failed to fetch"。
  // 这里加超时 + 自动重试：网络错误/超时/5xx 静默重试，链路恢复即自愈。
  // 只对幂等请求（GET 或显式 opts.retry）重试，避免 POST 创建类被重复执行。
  async function api(path, opts = {}) {
    const method = (opts.method || "GET").toUpperCase();
    // GET/PATCH/PUT/DELETE 语义幂等，重试安全；POST 可能有副作用（建会话/定时等），默认不重试。
    // 个别明确安全的 POST 可传 opts.retry=true 开启。
    const idempotent = method !== "POST" || opts.retry === true;
    const maxTries = idempotent ? 3 : 1;
    const timeoutMs = opts.timeoutMs || 15000;
    let lastErr;
    for (let attempt = 1; attempt <= maxTries; attempt++) {
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), timeoutMs);
      try {
        const res = await fetch(BASE + path, {
          ...opts,
          signal: ctl.signal,
          headers: {
            "Content-Type": "application/json",
            Authorization: "Bearer " + state.token,
            ...(opts.headers || {}),
          },
        });
        clearTimeout(timer);
        if (res.status === 401) { logout(); throw new Error("未授权"); }
        // 5xx 视为可重试（服务端瞬时问题）；4xx 是业务错误，直接抛不重试
        if (res.status >= 500 && attempt < maxTries) { lastErr = new Error("服务端错误 " + res.status); await _retryWait(attempt); continue; }
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "请求失败");
        return res.status === 204 ? null : res.json();
      } catch (e) {
        clearTimeout(timer);
        // 网络层失败（Failed to fetch / abort 超时）→ 幂等请求重试
        const retriable = idempotent && (e.name === "AbortError" || e.name === "TypeError" || /服务端错误/.test(e.message));
        if (retriable && attempt < maxTries) { lastErr = e; await _retryWait(attempt); continue; }
        throw e;
      }
    }
    throw lastErr || new Error("请求失败");
  }
  // 指数退避：300ms / 700ms（隧道抖动通常很快恢复）
  function _retryWait(attempt) { return new Promise((r) => setTimeout(r, attempt === 1 ? 300 : 700)); }

  // ---------------- 登录 ----------------
  $("login-btn").onclick = async () => {
    const token = $("token-input").value.trim();
    $("login-error").textContent = "";
    try {
      const r = await fetch(BASE + "/api/login", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      if (!r.ok) throw new Error("口令错误");
      state.token = token;
      localStorage.setItem("ac_token", token);
      localStorage.setItem("ac_login_ts", String(Date.now()));  // 记登录时间，用于超时重登
      enterApp();
    } catch (e) { $("login-error").textContent = e.message; }
  };
  $("token-input").addEventListener("keydown", (e) => { if (e.key === "Enter") $("login-btn").click(); });

  function logout() {
    localStorage.removeItem("ac_token");
    state.token = "";
    closeWs();
    closeMonitor();
    $("app-view").classList.add("hidden");
    $("manage-view").classList.add("hidden");
    $("login-view").classList.remove("hidden");
  }
  $("logout-btn").onclick = logout;

  // 登录态超时已关闭（用户反馈无必要）。保留 no-op 函数避免改动多处调用点。
  function loginExpired() { return false; }

  // ---------------- 顶部 Tab 导航 ----------------
  // overview / sessions / new / review / experimental。切到列表类 Tab 时刷新会话。
  // skipLoad=true 只切 UI 不拉数据（enterApp 首屏用，避免与紧随的 loadTasks/loadSessions 重复请求挤爆隧道）
  function switchTab(name, skipLoad) {
    state.tab = name;
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll(".tab-pane").forEach((p) => p.classList.toggle("active", p.dataset.pane === name));
    if (skipLoad) return;
    if (name === "overview" || name === "sessions" || name === "review") {
      loadSessions().catch(() => {});
      loadTasks();
    }
    if (name === "experimental") { loadTasks(); }
  }
  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.onclick = () => {
      // New Tab：点一下直接一键新建（走默认配置）；高级配置仍可在 New 面板里展开
      if (btn.dataset.tab === "new") { quickCreateSession(btn); return; }
      switchTab(btn.dataset.tab);
    };
  });

  // 一键新建会话：按钮禁用防抖，建成后切到 Overview 并打开详情
  async function quickCreateSession(btn) {
    if (btn.disabled) return;
    btn.disabled = true;
    toast("创建中…", "info", 3000);
    try {
      await createSession();
      switchTab("overview");
      openDetail();
      toast("新会话已创建", "success", 1600);
    } catch (err) {
      toast("创建失败：" + err.message, "error");
    } finally {
      btn.disabled = false;
    }
  }

  // 抽屉已移除——保留 closeDrawer 空实现，兼容旧调用点（createSession / openManage 等）。
  // 移动端：右栏详情作为覆盖层，closeDrawer 顺带收起它回到列表。
  function closeDrawer() {
    const dp = $("detail-panel");
    if (dp) dp.classList.remove("show");
  }
  // 移动端打开详情覆盖层
  function openDetail() {
    const dp = $("detail-panel");
    if (dp && window.matchMedia("(max-width: 900px)").matches) dp.classList.add("show");
  }
  $("detail-close-btn").onclick = () => { $("detail-panel").classList.remove("show"); };

  // 全局开关：一键展开/折叠当前会话所有子智能体的思考与执行过程
  function toggleAllSubagents() {
    state.subagentExpanded = !state.subagentExpanded;
    document.querySelectorAll("#chat .subagent").forEach((card) => {
      const body = card.querySelector(".subagent-body");
      const rbox = card.querySelector(".subagent-result");
      if (state.subagentExpanded) {
        card.classList.add("open");
        if (body) body.style.display = "";
        if (rbox && rbox.dataset.hasResult) rbox.style.display = "";
      } else {
        card.classList.remove("open");
        if (body) body.style.display = "none";
        if (rbox) rbox.style.display = "none";
      }
    });
    document.querySelectorAll("#chat .tool-group").forEach((g) => {
      const body = g.querySelector(".tool-group-body");
      g.classList.toggle("open", state.subagentExpanded);
      if (body) body.style.display = state.subagentExpanded ? "" : "none";
      updateToolGroupLabel(g);
    });
    updateSubagentToggleBtn();
  }
  function updateSubagentToggleBtn() {
    const btn = $("detail-subagent-btn");
    if (btn) {
      btn.textContent = state.subagentExpanded ? "⊟" : "⊞";
      btn.title = state.subagentExpanded ? "折叠全部子任务过程" : "展开全部子任务过程";
    }
  }
  { const b = $("detail-subagent-btn"); if (b) b.onclick = toggleAllSubagents; }

  // ---------------- PC 端左右栏拖拽调宽 ----------------
  const HUB_W_KEY = "ac_hub_left_w";
  const isDesktop = () => window.matchMedia("(min-width: 901px)").matches;

  function initHubResizer() {
    const hub = document.querySelector(".hub");
    const resizer = $("hub-resizer");
    if (!hub || !resizer) return;

    const saved = parseInt(localStorage.getItem(HUB_W_KEY) || "", 10);
    if (saved > 0) hub.style.setProperty("--hub-left-w", saved + "px");

    let startX = 0, startW = 0;

    const onMove = (e) => {
      const dx = e.clientX - startX;
      const min = 260, max = hub.clientWidth - 320 - 6;
      const w = Math.max(min, Math.min(startW + dx, max));
      hub.style.setProperty("--hub-left-w", w + "px");
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      document.body.classList.remove("hub-resizing");
      resizer.classList.remove("dragging");
      const left = hub.querySelector(".hub-left");
      if (left) localStorage.setItem(HUB_W_KEY, String(left.offsetWidth));
    };

    resizer.addEventListener("mousedown", (e) => {
      if (!isDesktop()) return;
      e.preventDefault();
      const left = hub.querySelector(".hub-left");
      startX = e.clientX;
      startW = left ? left.offsetWidth : hub.clientWidth / 2;
      document.body.classList.add("hub-resizing");
      resizer.classList.add("dragging");
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
    });

    resizer.addEventListener("dblclick", () => {
      hub.style.removeProperty("--hub-left-w");
      localStorage.removeItem(HUB_W_KEY);
    });

    window.addEventListener("resize", () => {
      if (!isDesktop()) return;
      const left = hub.querySelector(".hub-left");
      if (!left) return;
      const current = left.offsetWidth;
      const min = 260, max = hub.clientWidth - 320 - 6;
      if (max > min) {
        const clamped = Math.max(min, Math.min(current, max));
        if (clamped !== current) {
          hub.style.setProperty("--hub-left-w", clamped + "px");
          localStorage.setItem(HUB_W_KEY, String(clamped));
        }
      }
    });
  }

  // 会话搜索：按标题实时过滤 Sessions Tab 列表
  $("session-search").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    document.querySelectorAll("#session-list-all li").forEach((li) => {
      const t = (li.querySelector(".s-title") || {}).textContent || "";
      li.style.display = (!q || t.toLowerCase().includes(q)) ? "" : "none";
    });
  });

  // Sessions Tab 视图切换：活跃 / 归档
  document.querySelectorAll(".sv-btn").forEach((b) => {
    b.onclick = () => {
      state.sessionView = b.dataset.view;
      document.querySelectorAll(".sv-btn").forEach((x) => x.classList.toggle("active", x === b));
      if (state.sessionView === "archived") loadArchivedSessions();
      else fillList($("session-list-all"), state.sessions);
    };
  });

  // ---------------- 会话 ----------------
  async function loadSessions() {
    state.sessions = await api("/api/sessions");
    if (!state.sessions.length) { await createSession(); return; }
    if (!state.sessions.find((s) => s.id === state.sessionId)) state.sessionId = state.sessions[0].id;
    renderSessionLists();
    renderDashboard();
    renderKanban();
    const cur = state.sessions.find((s) => s.id === state.sessionId);
    $("session-title").textContent = cur ? cur.title : "会话";
    if (cur) markSeen(cur.id, cur.updated_at);  // 当前会话标记已读
    syncModeSelect();
  }

  // 渲染三个列表：Overview(全部) / Sessions(全部，可搜) / Review(待审视)
  function renderSessionLists() {
    fillList($("session-list"), state.sessions);
    // Sessions Tab 在「归档」视图下不用活跃列表覆盖，交给 renderArchivedSessionList
    if (state.sessionView === "archived") renderArchivedSessionList();
    else fillList($("session-list-all"), state.sessions);
    fillList($("session-list-review"), state.sessions.filter((s) => deriveState(s).key === "review"));
    const sub = $("agents-sub");
    if (sub) {
      const active = state.sessions.filter((s) => s.status === "running").length;
      sub.textContent = `${state.sessions.length} 个会话 · ${active} 个进行中`;
    }
    // 重新应用 Sessions Tab 的搜索过滤
    const sq = ($("session-search").value || "").trim().toLowerCase();
    if (sq) $("session-search").dispatchEvent(new Event("input"));
  }

  function fillList(ul, sessions, isArchived = false) {
    if (!ul) return;
    ul.innerHTML = "";
    if (!sessions.length) {
      ul.innerHTML = `<div class="entity-empty"><div class="empty-emoji">📭</div><div>这里还没有会话</div></div>`;
      return;
    }
    for (const s of sessions) ul.appendChild(renderSessionRow(s, isArchived));
  }

  // 加载并渲染归档会话（Sessions Tab「归档」视图）
  async function loadArchivedSessions() {
    try {
      state.archivedSessions = await api("/api/sessions?archived=1");
      renderArchivedSessionList();
    } catch (e) { console.error("loadArchivedSessions:", e); }
  }

  function renderArchivedSessionList() {
    fillList($("session-list-all"), state.archivedSessions, true);
    const sq = ($("session-search").value || "").trim().toLowerCase();
    if (sq) $("session-search").dispatchEvent(new Event("input"));
  }

  // 单个 Agent 行：状态徽章 + 标题 + 行摘要 + meta（时间 / workdir / 档位）
  function renderSessionRow(s, isArchived = false) {
    const st = deriveState(s);
    const li = document.createElement("li");
    li.dataset.sid = s.id;
    li.className = "st-" + st.key;
    if (s.id === state.sessionId) li.classList.add("active");

    const main = el("div", "s-main");
    const row1 = el("div", "s-row1");
    const badge = el("span", "badge " + st.badgeCls, st.label);
    const title = el("span", "s-title", escapeHtml(s.title));
    row1.append(badge, title);
    const sub = el("div", "s-sub", escapeHtml(sessionSubtitle(s)));
    const meta = el("div", "s-meta");
    meta.appendChild(el("span", null, fmtTime(s.updated_at) || ""));
    if (s.workdir) { meta.appendChild(el("span", "dot-sep", "·")); meta.appendChild(el("span", null, shortDir(s.workdir))); }
    if (s.mode) { meta.appendChild(el("span", "dot-sep", "·")); meta.appendChild(el("span", null, modeLabel(s.mode))); }
    main.append(row1, sub, meta);
    main.onclick = () => { switchSession(s.id); openDetail(); };

    const actions = el("div", "s-actions");
    if (isArchived) {
      // 归档视图：恢复 + 彻底删除（不提供速览，避免误切到已归档会话）
      const restore = el("button", "s-peek", "↩"); restore.title = "恢复到活跃列表";
      restore.onclick = (e) => { e.stopPropagation(); unarchiveSession(s.id); };
      const del = el("button", "s-del", "×"); del.title = "彻底删除";
      del.onclick = async (e) => { e.stopPropagation(); await deleteSession(s.id); };
      actions.append(restore, del);
    } else {
      const peek = el("button", "s-peek", "👁"); peek.title = "速览 / 不切会话回复";
      peek.onclick = (e) => { e.stopPropagation(); openPeek(s.id); };
      const del = el("button", "s-del", "×");
      del.onclick = async (e) => { e.stopPropagation(); await deleteSession(s.id); };
      actions.append(peek, del);
    }

    li.append(main, actions);
    return li;
  }

  // 路径缩写：只留末两段
  function shortDir(workdir) {
    const parts = String(workdir).replace(/\/$/, "").split("/");
    return parts.length > 2 ? "…/" + parts.slice(-2).join("/") : workdir;
  }
  function modeLabel(m) { return { fast: "极速", strong: "均衡", super: "最强" }[m] || m; }

  // 从 status + 最近 task 派生状态徽章（对齐设计图 Working / Review changes / Resume / Failed）
  function deriveState(s) {
    if (s.status === "running") return { key: "running", label: "Working", badgeCls: "working" };
    const t = state.taskBySession[s.id];
    if (t && t.status === "error") return { key: "failed", label: "Failed", badgeCls: "failed" };
    if (t && t.status === "success") return { key: "review", label: "Review changes", badgeCls: "review" };
    return { key: "idle", label: "Resume or archive", badgeCls: "resume" };
  }

  // 工作看板：4 个计数卡（Input / Active / Review / Failed）
  function renderDashboard() {
    const box = $("dashboard");
    if (!box) return;
    let active = 0, review = 0, failed = 0;
    for (const s of state.sessions) {
      const k = deriveState(s).key;
      if (k === "running") active++;
      else if (k === "review") review++;
      else if (k === "failed") failed++;
    }
    const cards = [
      { valCls: "", num: 0, label: "Input" },
      { valCls: active > 0 ? " mini-stat-val--active" : "", num: active, label: "Active" },
      { valCls: "", num: review, label: "Review" },
      { valCls: failed > 0 ? " mini-stat-val--failed" : "", num: failed, label: "Failed" },
    ];
    box.innerHTML = cards.map((c) =>
      `<span class="mini-stat"><span class="mini-stat-label">${c.label}</span> ` +
      `<span class="mini-stat-val${c.valCls}">${c.num}</span></span>`
    ).join("");
    const title = $("dash-title");
    if (title) title.textContent = review || failed ? `${review + failed} 项待处理` : "暂无待办";
  }

  // ---------------- 智能任务看板 ----------------
  // 单列紧凑列表：按 status 排序（进行中 → 待开始 → 已完成/已取消），每行左侧色点区分状态，
  // hover 显示编辑/删除操作，点击行主体跳转关联会话。
  async function renderKanban() {
    const list = $("kanban-list");
    if (!list) return;
    let todos;
    try { todos = await api("/api/todos"); }
    catch (e) { return; }

    // 排序权重：in_progress 在前，pending 其次，done/cancelled 最后
    const order = { in_progress: 0, pending: 1, done: 2, cancelled: 3 };
    const sorted = todos.slice().sort((a, b) => (order[a.status] ?? 1) - (order[b.status] ?? 1));

    list.innerHTML = "";
    if (!sorted.length) {
      list.innerHTML = '<div class="kanban-empty">暂无任务</div>';
      return;
    }
    for (const t of sorted) list.appendChild(renderKanbanRow(t));
  }

  // 单行看板（列表模式）：左侧状态色点 + 标题 + 单行截断的进展摘要 + hover 操作按钮
  function renderKanbanRow(t) {
    const row = document.createElement("div");
    row.className = "kanban-row";
    row.dataset.id = t.id;
    row.draggable = false; // 列表模式不需要拖拽

    // 状态点
    const dotMap = {
      pending:     { cls: "dot-pending",    html: "" },
      in_progress: { cls: "dot-inprogress", html: "" },
      done:        { cls: "dot-done",       html: "✓" },
      cancelled:   { cls: "dot-cancelled",  html: "✕" },
    };
    const dot = dotMap[t.status] || dotMap.pending;

    const hasProgress = t.progress && t.progress.trim() && t.progress !== "暂无进展信息";
    const progress = hasProgress ? t.progress : "";

    row.innerHTML = `
      <span class="kanban-dot ${dot.cls}">${dot.html}</span>
      <div class="kanban-row-main">
        <span class="kanban-row-title">${escapeHtml(t.title)}</span>
        ${progress ? `<span class="kanban-row-progress">${escapeHtml(progress)}</span>` : ""}
      </div>
      <div class="kanban-row-actions">
        <button class="kanban-act-btn btn-edit" title="编辑" data-act="edit">✎</button>
        <button class="kanban-act-btn btn-delete" title="删除" data-act="delete">🗑</button>
      </div>`;

    // 点击整行：跳转关联会话（多会话取主会话，即列表第一个）
    row.onclick = () => {
      const jumpId = (t.session_ids && t.session_ids[0]) || t.session_id;
      if (!jumpId) { toast("暂无关联会话", "info", 1500); return; }
      const sess = (state.sessions || []).find((s) => s.id === jumpId);
      if (sess) { switchTab("overview"); switchSession(sess.id); openDetail(); }
      else toast("会话不存在", "info", 1500);
    };

    // 编辑
    row.querySelector("[data-act='edit']").onclick = (e) => {
      e.stopPropagation();
      showEditTodoModal(t);
    };

    // 删除（二次确认后就地移除）
    row.querySelector("[data-act='delete']").onclick = async (e) => {
      e.stopPropagation();
      const yes = await confirmDialog(`确定删除「${t.title}」？`, { okText: "删除", danger: true });
      if (!yes) return;
      try {
        await api(`/api/todos/${t.id}`, { method: "DELETE" });
        row.remove();
        toast("已删除", "success", 1500);
      } catch (err) { toast("删除失败：" + err.message, "error"); }
    };

    return row;
  }

  // 给每个看板列绑定拖拽落点：拖入高亮、松手时若列变了就 PUT 更新状态
  function wireDropzone(container, status) {
    if (container._dropWired) return;  // 容器 DOM 常驻，只需绑定一次
    container._dropWired = true;
    container.addEventListener("dragover", (e) => {
      if (!state.dragTodoId) return;
      e.preventDefault();
      container.classList.add("kanban-col-dropzone");
    });
    container.addEventListener("dragleave", () => container.classList.remove("kanban-col-dropzone"));
    container.addEventListener("drop", async (e) => {
      e.preventDefault();
      container.classList.remove("kanban-col-dropzone");
      const id = state.dragTodoId;
      const from = state.dragTodoStatus;
      state.dragTodoId = null;
      state.dragTodoStatus = null;
      if (!id || from === status) return;
      try {
        await api(`/api/todos/${id}`, { method: "PUT", body: JSON.stringify({ status }) });
        renderKanban();
      } catch (err) { toast("状态更新失败：" + err.message, "error"); }
    });
  }

  // 防抖：活跃会话每几秒推一次 session_update，避免每次都打 /api/todos + 重建 DOM
  function debounce(fn, delay) {
    let t;
    return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), delay); };
  }
  const renderKanbanDebounced = debounce(() => renderKanban(), 2000);

  // 优先级 → 竖色条颜色。数值 priority（DB 存 INTEGER，1=⚡高优）与未来的字符串级别都支持。
  const KANBAN_PRIORITY_COLORS = { high: "#ff4d4f", medium: "#faad14", low: "#52c41a", none: "#8c8c8c" };
  function kanbanPriorityLevel(p) {
    if (typeof p === "string") return KANBAN_PRIORITY_COLORS[p] ? p : "none";
    if (p >= 2) return "high";
    if (p === 1) return "high";   // 现有 UI 把 priority=1 标为「⚡高优」，映射为高优红条
    return "none";
  }
  // 状态徽章文案 + class 后缀
  const KANBAN_BADGE = {
    pending: { text: "待开始", cls: "pending" },
    in_progress: { text: "进行中", cls: "inprogress" },
    done: { text: "已完成", cls: "done" },
    cancelled: { text: "已取消", cls: "cancelled" },
  };

  // 单张看板卡（v3）：右上角操作按钮组（编辑/刷新/删除）+ 可点击主体（跳转会话）+ 底部时间/徽章，支持拖拽换列
  function renderKanbanCard(t, status) {
    const primaryId = (t.session_ids && t.session_ids[0]) || t.session_id;
    const sess = primaryId ? state.sessions.find((s) => s.id === primaryId) : null;
    const sessCount = t.session_ids ? t.session_ids.length : (t.session_id ? 1 : 0);
    const hasProgress = t.progress && t.progress.trim();
    const progressText = hasProgress ? t.progress : "暂无进展";
    const timeText = t.progress_at ? fmtRelTime(t.progress_at) : (t.updated_at ? fmtRelTime(t.updated_at) : "");
    // 徽章按 todo 的真实 status（done 桶里可能混入 cancelled）
    const badge = KANBAN_BADGE[t.status] || KANBAN_BADGE[status] || KANBAN_BADGE.pending;
    const level = kanbanPriorityLevel(t.priority);

    const card = el("div", "kanban-card kanban-card-v3");
    card.dataset.id = t.id;
    card.draggable = true;
    card.style.borderLeftColor = KANBAN_PRIORITY_COLORS[level];
    card.innerHTML = `
      <div class="kanban-actions">
        <button class="kanban-act-btn btn-edit" title="编辑" data-act="edit">✎</button>
        ${sessCount > 0 ? `<button class="kanban-act-btn btn-refresh" title="刷新进展" data-act="refresh">↻</button>` : ""}
        <button class="kanban-act-btn btn-delete" title="删除" data-act="delete">🗑</button>
      </div>
      <div class="kanban-card-main">
        <div class="kanban-card-title">${escapeHtml(t.title)}</div>
        <div class="kanban-card-body">${escapeHtml(progressText)}</div>
      </div>
      <div class="kanban-card-footer">
        <span class="kanban-card-time">${timeText ? "🕐 " + escapeHtml(timeText) : ""}</span>
        <span class="kanban-footer-right">
          ${sessCount > 0 ? `<span class="badge-sessions" title="关联会话数">🔗${sessCount}</span>` : ""}
          <span class="kanban-badge kanban-badge--${badge.cls}">${badge.text}</span>
        </span>
      </div>`;

    const bodyEl = card.querySelector(".kanban-card-body");
    if (!hasProgress) bodyEl.classList.add("no-progress");

    // ---- 拖拽换列 ----
    card.addEventListener("dragstart", () => {
      state.dragTodoId = t.id;
      state.dragTodoStatus = status;
      card.classList.add("kanban-card-dragging");
    });
    card.addEventListener("dragend", () => card.classList.remove("kanban-card-dragging"));

    // ---- 点击卡片：跳转关联会话并高亮（多会话取主会话）----
    function doJump() {
      if (!primaryId) { toast("暂无关联会话", "info", 1500); return; }
      if (sess) { switchTab("overview"); switchSession(sess.id); openDetail(); }
      else toast("会话不存在", "info", 1500);
    }
    card.onclick = doJump;

    // ---- 操作按钮：编辑 ----
    const editBtn = card.querySelector('[data-act="edit"]');
    if (editBtn) editBtn.onclick = (e) => {
      e.stopPropagation();
      showEditTodoModal(t);
    };

    // ---- 操作按钮：刷新进展 ----
    const refreshBtn = card.querySelector('[data-act="refresh"]');
    if (refreshBtn) refreshBtn.onclick = async (e) => {
      e.stopPropagation();
      refreshBtn.disabled = true;
      refreshBtn.classList.add("is-loading");
      try {
        const res = await api(`/api/todos/${t.id}/refresh_progress?force=true`, { method: "POST", retry: true });
        if (res && res.progress) {
          bodyEl.textContent = res.progress;
          bodyEl.classList.remove("no-progress");
          if (res.progress_at) {
            const tEl = card.querySelector(".kanban-card-time");
            if (tEl) tEl.textContent = "🕐 " + fmtRelTime(res.progress_at);
          }
          toast(res.cached ? "进展无变化" : "进展已更新", "success", 1500);
        } else {
          toast(res.reason || "刷新失败", "info", 2200);
        }
      } catch (err) {
        toast("刷新失败：" + err.message, "error");
      } finally {
        refreshBtn.disabled = false;
        refreshBtn.classList.remove("is-loading");
      }
    };

    // ---- 操作按钮：删除（二次确认后就地移除并更新列计数）----
    const delBtn = card.querySelector('[data-act="delete"]');
    if (delBtn) delBtn.onclick = async (e) => {
      e.stopPropagation();
      const yes = await confirmDialog(`确定删除任务「${t.title}」？`, { okText: "删除", danger: true });
      if (!yes) return;
      try {
        await api(`/api/todos/${t.id}`, { method: "DELETE" });
        const col = card.closest(".kanban-col");
        card.remove();
        if (col) {
          const cntEl = col.querySelector(".col-count");
          if (cntEl) cntEl.textContent = Math.max(0, parseInt(cntEl.textContent || "0") - 1);
        }
        toast("已删除", "success", 1500);
      } catch (err) { toast("删除失败：" + err.message, "error"); }
    };
    return card;
  }

  // 顶部「刷新进展」：批量刷新进行中的任务，完成后重渲染看板
  async function refreshKanbanAll() {
    const btn = $("kanban-refresh-btn");
    if (btn) { btn.textContent = "刷新中…"; btn.disabled = true; }
    try {
      const res = await api("/api/kanban/refresh", { method: "POST", retry: true, timeoutMs: 120000 });
      await renderKanban();
      toast(`进展已刷新（${res.updated || 0}/${res.total || 0}）`, "success", 2000);
    } catch (e) {
      toast("刷新失败：" + e.message, "error");
    } finally {
      if (btn) { btn.textContent = "↻ 刷新进展"; btn.disabled = false; }
    }
  }

  // 新建任务弹窗：标题 + 关联会话 + 初始状态
  function showAddTodoModal() {
    const root = $("modal-root");
    root.innerHTML = "";
    const card = el("div", "modal-card");
    card.innerHTML = `
      <div class="modal-title">新建任务</div>
      <div class="entity-form" style="gap:12px">
        <label>任务标题
          <input id="nt-title" class="form-input" placeholder="输入任务名称…" />
        </label>
        <label>关联 Agent 会话
          <div class="session-selector">
            <div class="session-chips-row">
              <div class="session-chips" id="nt-session-chips"></div>
              <button type="button" class="session-add-btn" id="nt-session-add-btn">+ 添加</button>
            </div>
            <div class="session-picker-panel hidden" id="nt-session-panel">
              <input type="text" class="session-search" id="nt-session-search" placeholder="搜索会话…">
              <div class="session-picker-list" id="nt-session-list"></div>
            </div>
          </div>
        </label>
        <label>初始状态
          <select id="nt-status" class="form-select">
            <option value="pending">待开始</option>
            <option value="in_progress">进行中</option>
          </select>
        </label>
      </div>
      <div class="modal-actions">
        <button class="modal-cancel" type="button">取消</button>
        <button class="modal-ok" type="button">创建</button>
      </div>`;
    root.appendChild(card);
    root.classList.remove("hidden");
    requestAnimationFrame(() => root.classList.add("show"));
    const close = () => { root.classList.remove("show"); setTimeout(() => { root.classList.add("hidden"); root.innerHTML = ""; }, 200); };
    card.querySelector(".modal-cancel").onclick = close;
    root.onclick = (e) => { if (e.target === root) close(); };
    let selectedSessions = [];
    bindSessionChips(card, "nt", selectedSessions);
    setTimeout(() => { const t = $("nt-title"); if (t) t.focus(); }, 50);
    card.querySelector(".modal-ok").onclick = async () => {
      const title = ($("nt-title").value || "").trim();
      if (!title) { toast("请输入任务标题", "info"); return; }
      const session_ids = selectedSessions.slice();
      const status = $("nt-status").value || "pending";
      try {
        await api("/api/todos", { method: "POST", body: JSON.stringify({ title, session_ids, status }) });
        close();
        await renderKanban();
        toast("任务已创建", "success");
      } catch (e) { toast("创建失败：" + e.message, "error"); }
    };
  }

  // 编辑任务弹窗：标题 + 描述 + 优先级 + 状态，保存后 PUT 并重渲染看板
  function showEditTodoModal(t) {
    const root = $("modal-root");
    root.innerHTML = "";
    const card = el("div", "modal-card");
    const selectedIds = t.session_ids || (t.session_id ? [t.session_id] : []);
    card.innerHTML = `
      <div class="modal-title">编辑任务</div>
      <div class="entity-form" style="gap:12px">
        <label>任务标题
          <input id="et-title" class="form-input" placeholder="输入任务名称…" value="${escapeAttr(t.title)}" />
        </label>
        <label>任务描述
          <textarea id="et-desc" class="form-input" rows="3" placeholder="补充说明…">${escapeHtml(t.description || "")}</textarea>
        </label>
        <label>关联 Agent 会话
          <div class="session-selector">
            <div class="session-chips-row">
              <div class="session-chips" id="et-session-chips"></div>
              <button type="button" class="session-add-btn" id="et-session-add-btn">+ 添加</button>
            </div>
            <div class="session-picker-panel hidden" id="et-session-panel">
              <input type="text" class="session-search" id="et-session-search" placeholder="搜索会话…">
              <div class="session-picker-list" id="et-session-list"></div>
            </div>
          </div>
        </label>
        <label>优先级
          <select id="et-priority" class="form-select">
            <option value="0"${t.priority ? "" : " selected"}>普通</option>
            <option value="1"${t.priority == 1 ? " selected" : ""}>⚡高优</option>
          </select>
        </label>
        <label>状态
          <select id="et-status" class="form-select">
            <option value="pending"${t.status === "pending" ? " selected" : ""}>待开始</option>
            <option value="in_progress"${t.status === "in_progress" ? " selected" : ""}>进行中</option>
            <option value="done"${t.status === "done" ? " selected" : ""}>已完成</option>
          </select>
        </label>
      </div>
      <div class="modal-actions">
        <button class="modal-cancel" type="button">取消</button>
        <button class="modal-ok" type="button">保存</button>
      </div>`;
    root.appendChild(card);
    root.classList.remove("hidden");
    requestAnimationFrame(() => root.classList.add("show"));
    const close = () => { root.classList.remove("show"); setTimeout(() => { root.classList.add("hidden"); root.innerHTML = ""; }, 200); };
    card.querySelector(".modal-cancel").onclick = close;
    root.onclick = (e) => { if (e.target === root) close(); };
    let selectedSessions = [...selectedIds];
    bindSessionChips(card, "et", selectedSessions);
    setTimeout(() => { const el0 = $("et-title"); if (el0) el0.focus(); }, 50);
    card.querySelector(".modal-ok").onclick = async () => {
      const title = ($("et-title").value || "").trim();
      if (!title) { toast("请输入任务标题", "info"); return; }
      const description = $("et-desc").value || "";
      const priority = $("et-priority").value;
      const status = $("et-status").value;
      const session_ids = selectedSessions.slice();
      try {
        const res = await api(`/api/todos/${t.id}`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title, description, priority: parseInt(priority), status, session_ids })
        });
        close();
        await renderKanban();
        toast("已保存", "success", 1500);
      } catch (e) { toast("保存失败：" + e.message, "error"); }
    };
  }

  // 会话选择器绑定（GitHub label 风格）：chip 展示已选会话，[+ 添加] 展开浮层，
  // 浮层内可搜索、点选/取消，点浮层外关闭。
  // prefix 为弹窗 id 前缀（"nt" / "et"）；selectedSessions 为调用方持有的数组，原地增删
  function bindSessionChips(card, prefix, selectedSessions) {
    const chipsBox = card.querySelector(`#${prefix}-session-chips`);
    const addBtn   = card.querySelector(`#${prefix}-session-add-btn`);
    const panel    = card.querySelector(`#${prefix}-session-panel`);
    const search   = card.querySelector(`#${prefix}-session-search`);
    const list     = card.querySelector(`#${prefix}-session-list`);

    function renderChips() {
      chipsBox.innerHTML = selectedSessions.map((sid) => {
        const s = state.sessions.find((x) => x.id === sid);
        const name = s ? (s.title || sid.slice(0, 10)) : sid.slice(0, 10);
        return `<span class="session-chip" title="${escapeAttr(name)}"><span class="chip-name">${escapeHtml(name)}</span>` +
          `<button class="chip-remove" data-sid="${escapeAttr(sid)}" type="button">✕</button></span>`;
      }).join("");
      chipsBox.querySelectorAll(".chip-remove").forEach((btn) => {
        btn.onclick = (e) => {
          e.stopPropagation();
          const idx = selectedSessions.indexOf(btn.dataset.sid);
          if (idx > -1) selectedSessions.splice(idx, 1);
          renderChips();
          if (!panel.classList.contains("hidden")) renderList(search.value);
        };
      });
    }

    function renderList(q) {
      const kw = (q || "").toLowerCase();
      const filtered = state.sessions.filter((s) => !kw || (s.title || "").toLowerCase().includes(kw));
      list.innerHTML = filtered.length ? filtered.map((s) => {
        const selected = selectedSessions.includes(s.id);
        const name = s.title || s.id.slice(0, 10);
        return `<div class="sp-item${selected ? " selected" : ""}" data-sid="${escapeAttr(s.id)}">
          <span class="sp-check">${selected ? "✓" : ""}</span>
          <span class="sp-name" title="${escapeAttr(name)}">${escapeHtml(name)}</span>
        </div>`;
      }).join("") : `<div class="sp-empty">无匹配会话</div>`;
      list.querySelectorAll(".sp-item").forEach((item) => {
        item.onclick = () => {
          const sid = item.dataset.sid;
          const idx = selectedSessions.indexOf(sid);
          if (idx > -1) selectedSessions.splice(idx, 1);
          else selectedSessions.push(sid);
          renderChips();
          renderList(search.value);
        };
      });
    }

    addBtn.onclick = (e) => {
      e.stopPropagation();
      panel.classList.toggle("hidden");
      if (!panel.classList.contains("hidden")) {
        search.value = "";
        renderList("");
        search.focus();
      }
    };

    search.oninput = () => renderList(search.value);

    // 点浮层外关闭
    document.addEventListener("click", function closePanel(e) {
      if (!panel.contains(e.target) && e.target !== addBtn) {
        panel.classList.add("hidden");
        document.removeEventListener("click", closePanel);
      }
    });

    renderChips();
  }

  // 已读时间记录（localStorage）：用于未读标记
  function loadSeenMap() {
    try { return JSON.parse(localStorage.getItem("ac_seen") || "{}"); } catch (e) { return {}; }
  }
  function markSeen(sid, ts) {
    const m = loadSeenMap();
    m[sid] = Math.max(ts || 0, Date.now() / 1000);
    try { localStorage.setItem("ac_seen", JSON.stringify(m)); } catch (e) {}
  }

  // 会话副标题：在跑显示「当前活动」，空闲显示行摘要
  function sessionSubtitle(s) {
    if (s.status === "running") return s.activity || "运行中…";
    return s.summary || "";
  }

  // ---------------- 监控通道：所有会话状态实时更新 ----------------
  // 第二条 WS（/ws/monitor），收到 session_update 就地 patch 列表那一行，
  // 不用整体重渲染、不依赖打开抽屉轮询。弱网断开自动重连。
  function connectMonitor() {
    closeMonitor();
    if (!state.token) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}${BASE}/ws/monitor?token=${encodeURIComponent(state.token)}`;
    let ws;
    try { ws = new WebSocket(url); } catch (e) { return; }
    state.monitorWs = ws;
    ws.onopen = () => {
      // 监控通道也加心跳保活（每 25s，错开主通道的 20s）
      if (state.monitorHbTimer) clearInterval(state.monitorHbTimer);
      state.monitorHbTimer = setInterval(() => {
        if (state.monitorWs && state.monitorWs.readyState === WebSocket.OPEN) {
          try { state.monitorWs.send(JSON.stringify({ type: "ping" })); } catch (e) {}
        }
      }, 25000);
    };
    ws.onmessage = (ev) => { try { handleMonitorMessage(JSON.parse(ev.data)); } catch (e) {} };
    ws.onclose = () => {
      state.monitorWs = null;
      if (state.monitorHbTimer) { clearInterval(state.monitorHbTimer); state.monitorHbTimer = null; }
      state.monitorTimer = setTimeout(() => { if (state.token) connectMonitor(); }, 3000);
    };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  }
  function closeMonitor() {
    if (state.monitorTimer) { clearTimeout(state.monitorTimer); state.monitorTimer = null; }
    if (state.monitorHbTimer) { clearInterval(state.monitorHbTimer); state.monitorHbTimer = null; }
    if (state.monitorWs) { state.monitorWs.onclose = null; try { state.monitorWs.close(); } catch (e) {} state.monitorWs = null; }
  }

  function handleMonitorMessage(data) {
    // 看板进展异步更新：回合结束后 AI 刷新 in_progress 卡片进展，就地 patch 卡片
    if (data.type === "todo_progress_update") {
      // 兼容新列表行和旧卡片（data-id 相同）
      const el = document.querySelector(`[data-id="${data.todo_id}"]`);
      if (el) {
        const progressEl = el.querySelector(".kanban-row-progress, .kanban-card-body");
        if (progressEl && data.progress) {
          progressEl.textContent = data.progress;
          progressEl.classList.remove("no-progress");
        }
        const tEl = el.querySelector(".kanban-card-time");
        if (tEl && data.progress_at) tEl.textContent = "🕐 " + fmtRelTime(data.progress_at);
      }
      return;
    }
    // 秘书日报生成完成通知（无论企微是否启用，在线用户都能收到 toast 提示）
    if (data.type === "secretary_report") {
      toast(`📋 ${data.title} 已生成`, "success", 5000);
      return;
    }
    // 备忘录每日提醒
    if (data.type === "memo_reminder") {
      showMemoBanner(data);
      setMemoBadge(data.count || 0);
      return;
    }
    if (data.type !== "session_update") return;
    const s = state.sessions.find((x) => x.id === data.session_id);
    if (s) {
      // 更新内存里的会话对象，并就地 patch DOM（避免整体重渲染打断滚动/输入）
      s.status = data.status;
      s.activity = data.activity || "";
      s.summary = data.summary || s.summary;
      if (data.title) s.title = data.title;
      s.updated_at = data.updated_at || s.updated_at;
      patchSessionRow(s);
      renderKanbanDebounced();  // 看板卡片的关联会话名/状态可能随之变化（防抖，避免高频刷新）
      // 当前会话同步页头标题
      if (data.session_id === state.sessionId) {
        const titleEl = $('session-title');
        if (titleEl && data.title) titleEl.textContent = data.title;
      }
      // peek 面板开着且正是这个会话 → 同步刷新状态行
      if (peekState.sid === data.session_id && peekState.rerender) peekState.rerender();
    } else {
      // 列表里还没有这个会话（如定时任务/别处新建）→ 拉一次
      loadSessions().catch(() => {});
    }
  }

  // 监控通道 patch：状态可能改变徽章/计数，整行重渲染（跨 overview/sessions/review 三个列表）
  function patchSessionRow(s) {
    document.querySelectorAll(`li[data-sid="${s.id}"]`).forEach((li) => {
      const fresh = renderSessionRow(s);
      li.replaceWith(fresh);
    });
    renderDashboard();
    // review 列表成员可能因状态变化增减，简单起见重建一次该列表
    fillList($("session-list-review"), state.sessions.filter((x) => deriveState(x).key === "review"));
  }

  // ---------------- peek 速览面板：不切会话查看 + 回复 ----------------
  // 复用 modal-root 容器。展示该会话状态/摘要/最近几条消息，底部输入框直接回复
  // （经一条短命 WS 发给该会话，hub 在后台跑，列表实时反映状态）。
  async function openPeek(sid) {
    const s = state.sessions.find((x) => x.id === sid);
    const root = $("modal-root");
    root.innerHTML = "";
    const card = el("div", "modal-card peek-card");
    card.innerHTML = `
      <div class="peek-head">
        <span class="peek-title"></span>
        <button class="peek-close" type="button">×</button>
      </div>
      <div class="peek-status"></div>
      <div class="peek-body"><div class="chat-skel">${skeleton(2)}</div></div>
      <div class="peek-reply">
        <textarea class="peek-input" rows="1" placeholder="直接回复（不切会话）…"></textarea>
        <button class="peek-send" type="button">发送</button>
      </div>`;
    root.appendChild(card);
    root.classList.remove("hidden");
    requestAnimationFrame(() => root.classList.add("show"));

    card.querySelector(".peek-title").textContent = s ? s.title : "会话";
    const renderStatus = () => {
      const cur = state.sessions.find((x) => x.id === sid) || s;
      const st = card.querySelector(".peek-status");
      if (!cur) return;
      st.textContent = cur.status === "running" ? ("⏳ " + (cur.activity || "运行中…")) : ("✓ " + (cur.summary || "空闲"));
      st.className = "peek-status " + (cur.status === "running" ? "running" : "idle");
    };
    renderStatus();
    peekState.sid = sid;
    peekState.rerender = renderStatus;

    const close = () => {
      peekState.sid = null; peekState.rerender = null;
      root.classList.remove("show");
      setTimeout(() => { root.classList.add("hidden"); root.innerHTML = ""; }, 200);
    };
    card.querySelector(".peek-close").onclick = close;
    root.onclick = (e) => { if (e.target === root) close(); };

    // 拉最近消息（取末尾几条 user/assistant）
    try {
      const msgs = await api(`/api/sessions/${sid}/messages`);
      const body = card.querySelector(".peek-body");
      body.innerHTML = "";
      const tail = msgs.filter((m) => m.role === "user" || m.role === "assistant").slice(-4);
      if (!tail.length) { body.innerHTML = `<div class="peek-empty">还没有对话</div>`; }
      for (const m of tail) {
        const row = el("div", "peek-msg " + m.role);
        const txt = (m.content && m.content.text) || "";
        row.textContent = (m.role === "user" ? "你：" : "🤖 ") + txt.slice(0, 240);
        body.appendChild(row);
      }
      body.scrollTop = body.scrollHeight;
    } catch (e) {
      card.querySelector(".peek-body").innerHTML = `<div class="peek-empty">加载失败</div>`;
    }

    const inp = card.querySelector(".peek-input");
    const doSend = () => {
      const text = inp.value.trim();
      if (!text) return;
      peekReply(sid, text);
      inp.value = "";
      close();
      toast("已发送，后台执行中", "success", 1800);
    };
    card.querySelector(".peek-send").onclick = doSend;
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.shiftKey && !("ontouchstart" in window)) { e.preventDefault(); doSend(); }
    });
  }

  const peekState = { sid: null, rerender: null };

  // 不切会话发送：开一条短命 WS 到该会话，发完即走，hub 在后台跑。
  function peekReply(sid, text) {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}${BASE}/ws?token=${encodeURIComponent(state.token)}&session_id=${sid}`;
    let ws;
    try { ws = new WebSocket(url); } catch (e) { toast("发送失败", "error"); return; }
    ws.onopen = () => {
      // 标记后台，使该会话跑完会推企业微信（人不在这个会话页面）
      try { ws.send(JSON.stringify({ type: "visibility", hidden: true })); } catch (e) {}
      try { ws.send(JSON.stringify({ type: "user_message", content: text })); } catch (e) {}
      // 给服务端一点时间把回合启动（start_turn 落库+建task），再断开
      setTimeout(() => { try { ws.close(); } catch (e) {} }, 1200);
    };
    ws.onerror = () => { toast("发送失败，请重试", "error"); };
  }


  // 把当前会话的档位回填到顶栏 select
  function syncModeSelect() {
    const cur = state.sessions.find((s) => s.id === state.sessionId);
    const sel = $("mode-select");
    if (sel && cur && cur.mode) sel.value = cur.mode;
  }
  // 切换档位：持久化到会话（后端 PATCH），下一回合即生效
  $("mode-select").onchange = async (e) => {
    const mode = e.target.value;
    if (!state.sessionId) return;
    try {
      await api(`/api/sessions/${state.sessionId}/mode`, { method: "PATCH", body: JSON.stringify({ mode }) });
      const cur = state.sessions.find((s) => s.id === state.sessionId);
      if (cur) cur.mode = mode;
    } catch (err) { /* ignore，下次切会话会重新同步 */ }
  };

  async function createSession(opts = {}) {
    const body = {
      title: opts.title || ("新会话 " + new Date().toLocaleString("zh-CN", { hour: "2-digit", minute: "2-digit" })),
    };
    if (opts.workdir) body.workdir = opts.workdir;
    if (opts.mode) body.mode = opts.mode;
    const s = await api("/api/sessions", { method: "POST", body: JSON.stringify(body) });
    state.sessionId = s.id;
    localStorage.setItem("ac_session", s.id);
    await loadSessions();
    await switchSession(s.id);
    return s;
  }

  // New Tab 高级配置：自定义标题/目录/档位新建会话
  $("new-form").onsubmit = async (e) => {
    e.preventDefault();
    const title = $("nf-title").value.trim();
    const workdir = $("nf-workdir").value.trim();
    const mode = $("nf-mode").value;
    try {
      await createSession({ title, workdir, mode });
      $("new-form").reset();
      switchTab("overview");
      openDetail();
      toast("会话已创建", "success", 1600);
    } catch (err) { toast("创建失败：" + err.message, "error"); }
  };

  // Overview「高级新建」入口：切到 New 面板做自定义配置（New Tab 本身已改为一键新建）
  $("new-advanced-btn").onclick = () => switchTab("new");

  // 看板顶部按钮：批量刷新进展 / 新建任务
  $("kanban-refresh-btn").onclick = refreshKanbanAll;
  $("kanban-add-btn").onclick = showAddTodoModal;
  { const b = $("kanban-col-refresh"); if (b) b.onclick = (e) => { e.stopPropagation(); refreshKanbanAll(); }; }

  // 接续电脑/终端聊过的会话：列出 → 单击某个即接续并切过去（带完整上下文）
  $("resume-pc-btn").onclick = async () => {
    let items;
    try { items = await api("/api/import/sessions"); }
    catch (e) { toast("加载失败：" + e.message, "error"); return; }
    const avail = items.filter((it) => !it.imported);
    if (!avail.length) { toast(items.length ? "终端会话都已接续" : "没有可接续的终端会话", "info", 2200); return; }
    showResumePicker(avail);
  };

  function showResumePicker(items) {
    const root = $("modal-root");
    root.innerHTML = "";
    const card = el("div", "modal-card");
    const rows = items.map((it) => {
      const tm = new Date(it.mtime * 1000).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
      return `<button class="imp-row" data-sid="${escapeAttr(it.claude_session_id)}" type="button">
        <span class="imp-title">${escapeHtml(it.title)}</span>
        <span class="imp-meta">${it.events}条 · ${tm}</span></button>`;
    }).join("");
    card.innerHTML = `<div class="modal-msg" style="margin-bottom:12px">接续电脑会话（点一个直接接着聊）</div>
      <div class="imp-list">${rows}</div>
      <div class="modal-actions" style="margin-top:14px">
        <button class="modal-cancel" type="button">取消</button>
      </div>`;
    root.appendChild(card);
    root.classList.remove("hidden");
    requestAnimationFrame(() => root.classList.add("show"));
    const close = () => { root.classList.remove("show"); setTimeout(() => { root.classList.add("hidden"); root.innerHTML = ""; }, 200); };
    card.querySelector(".modal-cancel").onclick = close;
    card.querySelectorAll(".imp-row").forEach((btn) => {
      btn.onclick = async () => {
        const sid = btn.dataset.sid;
        close();
        try {
          const r = await api("/api/import/sessions", { method: "POST", body: JSON.stringify({ ids: [sid] }) });
          toast("已接续，可继续聊", "success");
          await loadSessions();
          if (r.session_id) { switchTab("overview"); await switchSession(r.session_id); openDetail(); }
        } catch (e) { toast("接续失败：" + e.message, "error"); }
      };
    });
    root.onclick = (e) => { if (e.target === root) close(); };
  }

  async function deleteSession(id) {
    const s = state.sessions.find((x) => x.id === id)
           || state.archivedSessions.find((x) => x.id === id);
    const yes = await confirmDialog(`确定删除会话「${s ? s.title : id}」？历史记录将一并清除。`, { okText: "删除", danger: true });
    if (!yes) return;
    try {
      await api("/api/sessions/" + id, { method: "DELETE" });
      toast("会话已删除", "success");
      delete state.drafts[id];
      if (id === state.sessionId) state.sessionId = "";
      await loadSessions();
      if (state.sessionView === "archived") await loadArchivedSessions();
      if (state.sessionId) await switchSession(state.sessionId);
    } catch (e) { toast("删除失败：" + e.message, "error"); }
  }

  // 恢复归档会话：回到活跃列表
  async function unarchiveSession(id) {
    try {
      await api(`/api/sessions/${id}/unarchive`, { method: "POST", retry: true });
      toast("已恢复", "success", 1500);
      await loadArchivedSessions();
      await loadSessions();
    } catch (e) { toast("恢复失败：" + e.message, "error"); }
  }

  // 草稿按会话隔离：切走时存当前输入框内容和待发图片，切回时恢复。
  function saveDraft(id) {
    if (!id) return;
    const text = input.value;
    const images = state.pendingImages.slice();
    if (text.trim() || images.length) state.drafts[id] = { text, images };
    else delete state.drafts[id];
  }
  function restoreDraft(id) {
    const d = state.drafts[id] || { text: "", images: [] };
    input.value = d.text || "";
    input.style.height = "auto";
    input.dispatchEvent(new Event("input"));
    state.pendingImages = (d.images || []).slice();
    renderImageTray();
  }

  async function switchSession(id) {
    const prevId = state.sessionId;
    if (prevId && prevId !== id) saveDraft(prevId);   // 存旧会话草稿
    hideTyping();
    clearStream();
    state.sessionId = id;
    state.toolIdMap = {};  // 清空工具 id 映射，避免跨会话串号
    state.agentGroups = {};  // 清空子智能体归拢映射，避免跨会话串号
    state.subStreams = {};   // 清空子智能体流式气泡，避免跨会话残留
    state.subagentExpanded = false;   // 复位子智能体展开开关，新会话默认全部折叠
    updateSubagentToggleBtn();
    state.queue = [];      // 清空上个会话的队列，等新会话 queue_update 广播刷新
    renderQueue();
    localStorage.setItem("ac_session", id);
    const cur = state.sessions.find((s) => s.id === id);
    if (cur) {
      $("session-title").textContent = cur.title;
      updateWorkdirBar(cur.workdir);
      updateSessionIdBar(cur.id);
      markSeen(cur.id, cur.updated_at);
    }
    // 高亮当前会话行（跨三个列表）
    document.querySelectorAll("li[data-sid]").forEach((li) => li.classList.toggle("active", li.dataset.sid === id));
    syncModeSelect();
    await loadHistory();
    if (prevId !== id) restoreDraft(id);      // 恢复新会话草稿（同会话不覆盖当前输入）
    connectWs();
  }

  // workdir 显示栏（点击可编辑）
  function updateWorkdirBar(workdir) {
    const bar = $("workdir-bar");
    if (!bar) return;
    if (!workdir) { bar.classList.add("hidden"); return; }
    // 只显示最后两段路径，避免太长
    const parts = workdir.replace(/\/$/, "").split("/");
    const short = parts.length > 2 ? "…/" + parts.slice(-2).join("/") : workdir;
    bar.textContent = "📂 " + short;
    bar.title = workdir;
    bar.classList.remove("hidden");
  }

  // 会话 ID 显示栏（点击可复制内部会话主键）
  function updateSessionIdBar(sid) {
    const bar = $("session-id-bar");
    if (!bar) return;
    if (!sid) { bar.classList.add("hidden"); return; }
    bar.textContent = "ID: " + sid;
    bar.title = "点击复制会话 ID：" + sid;
    bar.onclick = () => { copyText(sid).then((ok) => { if (ok) toast("已复制会话 ID", "success", 1500); }); };
    bar.classList.remove("hidden");
  }

  $("workdir-bar").onclick = async () => {
    const cur = state.sessions.find((s) => s.id === state.sessionId);
    if (!cur) return;
    const newDir = prompt("修改工作目录：", cur.workdir || "");
    if (newDir === null || newDir.trim() === cur.workdir) return;
    const wd = newDir.trim();
    if (!wd) return;
    try {
      await api(`/api/sessions/${state.sessionId}/workdir`, { method: "PATCH", body: JSON.stringify({ workdir: wd }) });
      cur.workdir = wd;
      updateWorkdirBar(wd);
      toast("工作目录已更新", "success", 1600);
    } catch (e) { toast("更新失败：" + e.message, "error"); }
  };

  // ---------------- 历史 ----------------
  // 只渲染最近 HISTORY_WINDOW 条，顶部按需「加载更早」。长会话（数百上千条 tool 输出）
  // 一次性全量同步渲染会卡死主线程数秒，这是"点进会话卡半天"的根因。
  const HISTORY_WINDOW = 80;
  async function loadHistory() {
    const chat = $("chat");
    chat.innerHTML = `<div class="chat-skel">${skeleton(3)}</div>`;
    state._histGroups = {};  // 重置历史归拢映射，跨批次（首屏 + 加载更早）共享以关联卡片与结果
    try {
      const msgs = await api(`/api/sessions/${state.sessionId}/messages`);
      chat.innerHTML = "";
      if (!msgs.length) {
        chat.innerHTML = `<div class="chat-welcome"><div class="cw-emoji">💬</div>` +
          `<div class="cw-title">开始新的对话</div>` +
          `<div class="cw-sub">输入指令，或点下方快捷指令快速开始</div></div>`;
        return;
      }
      // 暂存全量，先渲染末尾窗口；更早的通过顶部按钮按需补渲染
      state.histMsgs = msgs;
      state.histShown = Math.min(HISTORY_WINDOW, msgs.length);
      const start = msgs.length - state.histShown;
      if (start > 0) renderLoadEarlierBtn(start);
      for (let i = start; i < msgs.length; i++) appendMessageGrouped(chat, state._histGroups, msgs[i].role, msgs[i].content, msgs[i].created_at);
      scrollBottom(true);
    } catch (e) {
      chat.innerHTML = "";
      toast("加载历史失败：" + e.message, "error");
    }
  }

  // 顶部「加载更早 N 条」按钮：点一次再往前渲染一个窗口，保持滚动位置不跳。
  function renderLoadEarlierBtn(remaining) {
    const chat = $("chat");
    let btn = chat.querySelector(".load-earlier");
    if (!btn) {
      btn = el("button", "load-earlier");
      chat.prepend(btn);
    }
    btn.textContent = `↑ 加载更早消息（剩 ${remaining} 条）`;
    btn.onclick = () => {
      const msgs = state.histMsgs || [];
      const curStart = msgs.length - state.histShown;
      const newStart = Math.max(0, curStart - HISTORY_WINDOW);
      // 记录加载前的滚动高度，渲染后补偿，避免视图跳动
      const prevH = chat.scrollHeight, prevTop = chat.scrollTop;
      const frag = document.createDocumentFragment();
      // 复用 state._histGroups（跨批次共享），关联更早批次里的 Agent tool_use 与本批 tool_result
      for (let i = newStart; i < curStart; i++) {
        appendMessageGrouped(frag, state._histGroups, msgs[i].role, msgs[i].content, msgs[i].created_at);
      }
      btn.after(frag);
      state.histShown = msgs.length - newStart;
      if (newStart > 0) { btn.textContent = `↑ 加载更早消息（剩 ${newStart} 条）`; }
      else { btn.remove(); }
      chat.scrollTop = prevTop + (chat.scrollHeight - prevH);
    };
  }

  async function loadTasks() {
    try {
      const tasks = await api("/api/tasks");
      // 建 session_id -> 最近一条 task 映射（tasks 按时间倒序，首次出现即最近），供徽章/看板派生
      const map = {};
      for (const t of tasks) { if (t.session_id && !(t.session_id in map)) map[t.session_id] = t; }
      const changed = JSON.stringify(Object.keys(map).map((k) => k + map[k].status)) !==
                      JSON.stringify(Object.keys(state.taskBySession).map((k) => k + state.taskBySession[k].status));
      state.taskBySession = map;
      const ul = $("task-list");
      if (ul) {
        ul.innerHTML = "";
        for (const t of tasks) {
          const li = document.createElement("li");
          li.className = t.status;
          const dur = t.duration_ms ? (t.duration_ms / 1000).toFixed(1) + "s" : "—";
          const cost = t.cost_usd != null ? " · $" + t.cost_usd.toFixed(4) : "";
          li.innerHTML = `<div>${escapeHtml(t.summary || "")}</div>
            <div class="t-meta">${t.status} · ${dur}${cost} · ${fmtTime(t.started_at)}</div>`;
          ul.appendChild(li);
        }
      }
      // task 状态变化会影响徽章/看板，重渲染列表（仅在有会话数据时）
      if (changed && state.sessions.length) { renderSessionLists(); renderDashboard(); }
    } catch (e) { /* ignore */ }
  }
  $("refresh-tasks").onclick = loadTasks;

  // ---------------- 快捷指令 chip ----------------
  async function loadSnippets() {
    try {
      const snippets = await api("/api/snippets");
      const bar = $("snippet-bar");
      bar.innerHTML = "";
      if (!snippets || !snippets.length) { bar.classList.add("hidden"); return; }
      for (const s of snippets) {
        const chip = el("button", "snippet-chip", escapeHtml(s.label || s.id));
        chip.onclick = () => {
          const cur = input.value.trim();
          input.value = cur ? cur + "\n" + s.text : s.text;
          input.focus();
          input.dispatchEvent(new Event("input"));
        };
        bar.appendChild(chip);
      }
      bar.classList.remove("hidden");
    } catch (e) { /* ignore，快捷指令非关键路径 */ }
  }

  // ---------------- 管理面板（记忆库 / 子智能体共用） ----------------
  // kind: "memory" | "agent"，决定接口路径、字段、渲染方式
  const manage = { kind: null };
  let todoFormActive = false;  // 待办表单是否处于打开态（表单复用 manage-list 容器，故单独标记）

  function openManage(kind) {
    manage.kind = kind;
    closeDrawer();
    const titles = { memory: "记忆库", agent: "子智能体", snippets: "快捷指令", schedule: "定时任务", todos: "待办清单", memos: "备忘录", reports: "日报记录" };
    $("manage-title").textContent = titles[kind] || kind;
    $("app-view").classList.add("hidden");
    $("manage-view").classList.remove("hidden");
    showManageList();
  }
  function closeManage() {
    $("manage-view").classList.add("hidden");
    $("app-view").classList.remove("hidden");
  }
  $("open-memory-btn").onclick = () => openManage("memory");  $("open-agents-btn").onclick = () => openManage("agent");
  $("open-snippets-btn").onclick = () => openManage("snippets");
  $("open-schedules-btn").onclick = () => openManage("schedule");
  $("open-todos-btn").onclick = () => openManage("todos");
  $("open-memos-btn").onclick = () => openManage("memos");
  $("open-reports-btn").onclick = () => openManage("reports");
  $("secretary-trigger-btn").onclick = async () => {
    try {
      await api("/api/secretary/trigger", { method: "POST", body: JSON.stringify({ type: "evening" }) });
      toast("日报生成中，完成后将推送通知", "success", 3000);
    } catch (e) { toast("触发失败：" + e.message, "error"); }
  };
  $("notify-toggle").onclick = toggleNotify;
  $("voicesend-toggle").onclick = toggleVoiceSend;
  $("manage-back").onclick = () => {
    // 表单态先退回列表态，列表态再退出面板
    if (!$("manage-form").classList.contains("hidden") || todoFormActive) showManageList();
    else closeManage();
  };
  $("manage-new").onclick = () => showManageForm(null);

  const apiBase = () => manage.kind === "memory" ? "/api/memory" : manage.kind === "snippets" ? "/api/snippets" : "/api/agents";

  async function showManageList() {
    $("manage-form").classList.add("hidden");
    const listEl = $("manage-list");
    listEl.classList.remove("hidden");
    $("manage-new").classList.remove("hidden");
    listEl.innerHTML = skeleton(4);
    if (manage.kind === "schedule") { return showScheduleList(); }
    if (manage.kind === "todos") { $("manage-new").classList.remove("hidden"); return showTodoList(); }
    if (manage.kind === "memos") { $("manage-new").classList.remove("hidden"); return showMemoList(); }
    if (manage.kind === "reports") { $("manage-new").classList.add("hidden"); return showReportList(); }
    try {
      const items = await api(apiBase());
      listEl.innerHTML = "";
      if (!items.length) {
        const kindMap = { memory: ["记忆", "🧠"], agent: ["子智能体", "🤖"], snippets: ["快捷指令", "⚡"] };
        const [kindName, emoji] = kindMap[manage.kind] || ["项目", "📋"];
        listEl.innerHTML = `<div class="entity-empty"><div class="empty-emoji">${emoji}</div>` +
          `<div>还没有${kindName}</div><div class="empty-sub">点右上角「+ 新建」创建第一个</div></div>`;
        return;
      }
      for (const it of items) listEl.appendChild(renderEntity(it));
    } catch (e) {
      listEl.innerHTML = `<div class="entity-empty">加载失败：${escapeHtml(e.message)}</div>`;
    }
  }

  function renderEntity(it) {
    const li = el("li");
    // snippets 用 label 作主标题，text 作预览
    if (manage.kind === "snippets") {
      li.appendChild(el("div", "e-name", escapeHtml(it.label)));
      li.appendChild(el("div", "e-preview", escapeHtml((it.text || "").slice(0, 80))));
    } else {
      const head = el("div", "e-head");
      head.appendChild(el("span", "e-name", escapeHtml(it.name)));
      if (manage.kind === "memory" && it.type) head.appendChild(el("span", "e-tag", escapeHtml(it.type)));
      if (manage.kind === "agent" && it.model) head.appendChild(el("span", "e-tag", escapeHtml(it.model)));
      li.appendChild(head);
      if (it.description) li.appendChild(el("div", "e-desc", escapeHtml(it.description)));
      if (manage.kind === "memory" && it.preview) li.appendChild(el("div", "e-preview", escapeHtml(it.preview)));
      if (manage.kind === "agent" && it.tools && it.tools.length) li.appendChild(el("div", "e-preview", "工具：" + escapeHtml(it.tools.join(", "))));
    }
    const actions = el("div", "e-actions");
    const edit = el("button", "btn-sm", "编辑");
    const entityId = manage.kind === "snippets" ? it.id : it.name;
    edit.onclick = () => showManageForm(entityId);
    const del = el("button", "btn-sm danger", "删除");
    const delLabel = manage.kind === "snippets" ? it.label : it.name;
    del.onclick = async () => {
      const yes = await confirmDialog(`确定删除「${delLabel}」？此操作不可恢复。`, { okText: "删除", danger: true });
      if (!yes) return;
      try {
        await api(`${apiBase()}/${encodeURIComponent(entityId)}`, { method: "DELETE" });
        toast("已删除", "success");
        if (manage.kind === "snippets") loadSnippets();  // 刷新 chip 行
        await showManageList();
      } catch (e) { toast("删除失败：" + e.message, "error"); }
    };
    actions.append(edit, del);
    li.appendChild(actions);
    return li;
  }

  // ---------------- 定时任务（自有渲染，不走实体 CRUD）----------------
  function fmtSchedule(s) {
    if (s.kind === "interval") return `每 ${s.interval_min} 分钟`;
    if (s.kind === "daily") return `每天 ${s.at_hhmm}`;
    return s.kind || "";
  }
  function fmtTs(ts) {
    if (!ts) return "—";
    try { return new Date(ts * 1000).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }); }
    catch (e) { return "—"; }
  }

  async function showScheduleList() {
    const listEl = $("manage-list");
    try {
      const items = await api("/api/schedules");
      listEl.innerHTML = "";
      if (!items.length) {
        listEl.innerHTML = `<div class="entity-empty"><div class="empty-emoji">⏰</div>` +
          `<div>还没有定时任务</div><div class="empty-sub">点右上角「+ 新建」，让某个会话定时自动执行指令</div></div>`;
        return;
      }
      for (const it of items) {
        const sess = state.sessions.find((x) => x.id === it.session_id);
        const li = el("li");
        const head = el("div", "e-head");
        head.appendChild(el("span", "e-name", escapeHtml(fmtSchedule(it))));
        head.appendChild(el("span", "e-tag", it.enabled ? "启用" : "停用"));
        li.appendChild(head);
        li.appendChild(el("div", "e-desc", escapeHtml((it.prompt || "").slice(0, 80))));
        li.appendChild(el("div", "e-preview", `会话：${escapeHtml(sess ? sess.title : "(已删除)")} · 下次：${fmtTs(it.next_run)}`));
        const actions = el("div", "e-actions");
        const toggle = el("button", "btn-sm", it.enabled ? "停用" : "启用");
        toggle.onclick = async () => {
          try { await api(`/api/schedules/${it.id}`, { method: "PUT", body: JSON.stringify({ enabled: !it.enabled }) }); showScheduleList(); }
          catch (e) { toast("操作失败：" + e.message, "error"); }
        };
        const edit = el("button", "btn-sm", "编辑");
        edit.onclick = () => showScheduleForm(it.id);
        const del = el("button", "btn-sm danger", "删除");
        del.onclick = async () => {
          const yes = await confirmDialog("确定删除这条定时任务？", { okText: "删除", danger: true });
          if (!yes) return;
          try { await api(`/api/schedules/${it.id}`, { method: "DELETE" }); toast("已删除", "success"); showScheduleList(); }
          catch (e) { toast("删除失败：" + e.message, "error"); }
        };
        actions.append(toggle, edit, del);
        li.appendChild(actions);
        listEl.appendChild(li);
      }
    } catch (e) {
      listEl.innerHTML = `<div class="entity-empty">加载失败：${escapeHtml(e.message)}</div>`;
    }
  }

  async function showScheduleForm(id) {
    const form = $("manage-form");
    $("manage-list").classList.add("hidden");
    $("manage-new").classList.add("hidden");
    form.classList.remove("hidden");
    let d = { id: null, session_id: state.sessionId || (state.sessions[0] || {}).id || "", prompt: "", kind: "interval", interval_min: 60, at_hhmm: "09:00", enabled: 1 };
    if (id) {
      const all = await api("/api/schedules").catch(() => []);
      const found = all.find((x) => x.id === id);
      if (found) d = found;
    }
    const opts = state.sessions.map((s) => `<option value="${escapeAttr(s.id)}" ${s.id === d.session_id ? "selected" : ""}>${escapeHtml(s.title)}</option>`).join("");
    form.innerHTML = `
      <label>在哪个会话里执行
        <select id="sf-session">${opts}</select>
      </label>
      <label>指令（到点自动发给 Agent）
        <textarea id="sf-prompt" class="tall" placeholder="例：检查训练进度，汇总最新 loss 和 GPU 占用">${escapeHtml(d.prompt || "")}</textarea>
      </label>
      <label>触发方式
        <select id="sf-kind">
          <option value="interval" ${d.kind === "interval" ? "selected" : ""}>每隔一段时间</option>
          <option value="daily" ${d.kind === "daily" ? "selected" : ""}>每天定时</option>
        </select>
      </label>
      <label id="sf-interval-wrap">间隔分钟数
        <input id="sf-interval" type="number" min="1" value="${escapeAttr(String(d.interval_min || 60))}" />
      </label>
      <label id="sf-daily-wrap">每天几点（HH:MM，24小时制）
        <input id="sf-hhmm" value="${escapeAttr(d.at_hhmm || "09:00")}" placeholder="09:00" />
      </label>
      <div class="form-err" id="f-err"></div>
      <div class="form-actions">
        <button type="button" class="cancel">取消</button>
        <button type="submit" class="save">保存</button>
      </div>`;
    const syncKind = () => {
      const k = form.querySelector("#sf-kind").value;
      form.querySelector("#sf-interval-wrap").style.display = k === "interval" ? "" : "none";
      form.querySelector("#sf-daily-wrap").style.display = k === "daily" ? "" : "none";
    };
    syncKind();
    form.querySelector("#sf-kind").onchange = syncKind;
    form.querySelector(".cancel").onclick = (e) => { e.preventDefault(); showScheduleList(); };
    form.onsubmit = async (e) => {
      e.preventDefault();
      const body = {
        session_id: form.querySelector("#sf-session").value,
        prompt: form.querySelector("#sf-prompt").value.trim(),
        kind: form.querySelector("#sf-kind").value,
        interval_min: parseInt(form.querySelector("#sf-interval").value, 10),
        at_hhmm: form.querySelector("#sf-hhmm").value.trim(),
      };
      const errEl = form.querySelector("#f-err");
      if (!body.prompt) { errEl.textContent = "指令不能为空"; return; }
      try {
        if (id) await api(`/api/schedules/${id}`, { method: "PUT", body: JSON.stringify(body) });
        else await api("/api/schedules", { method: "POST", body: JSON.stringify(body) });
        toast("已保存", "success");
        showScheduleList();
      } catch (err) { errEl.textContent = err.message || "保存失败"; }
    };
  }

  // name 为 null 表示新建；否则拉取详情进入编辑
  async function showManageForm(name) {
    if (manage.kind === "schedule") { return showScheduleForm(name); }
    if (manage.kind === "todos") { return showTodoForm(); }
    if (manage.kind === "memos") { return showMemoForm(name); }
    const form = $("manage-form");
    const listEl = $("manage-list");
    listEl.classList.add("hidden");
    $("manage-new").classList.add("hidden");
    form.classList.remove("hidden");
    const isNew = !name;
    let data = { name: "", description: "", body: "", tools: [], model: "", prompt: "", label: "", text: "" };
    if (!isNew) {
      form.innerHTML = skeleton(3);
      try { data = await api(`${apiBase()}/${encodeURIComponent(name)}`); }
      catch (e) { form.innerHTML = `<div class="entity-empty">加载失败：${escapeHtml(e.message)}</div>`; return; }
    }
    form.innerHTML = manage.kind === "memory" ? memoryFormHtml(data, isNew)
      : manage.kind === "snippets" ? snippetFormHtml(data, isNew)
      : agentFormHtml(data, isNew);
    form.querySelector(".cancel").onclick = (e) => { e.preventDefault(); showManageList(); };
    form.onsubmit = (e) => { e.preventDefault(); submitManageForm(name, isNew); };
  }

  function memoryFormHtml(d, isNew) {
    return `
      <label>名称（英文短横线 slug，创建后不可改）
        <input id="f-name" value="${escapeAttr(d.name)}" ${isNew ? "" : "disabled"} placeholder="例：deploy-notes" />
      </label>
      <label>描述（一句话，用于检索）
        <input id="f-desc" value="${escapeAttr(d.description)}" placeholder="这条记忆讲了什么" />
      </label>
      <label>正文
        <textarea id="f-body" class="tall" placeholder="记忆内容…">${escapeHtml(d.body || "")}</textarea>
      </label>
      <div class="form-err" id="f-err"></div>
      <div class="form-actions">
        <button type="button" class="cancel">取消</button>
        <button type="submit" class="save">保存</button>
      </div>`;
  }

  function snippetFormHtml(d, isNew) {
    return `
      <label>按钮标签（显示在 chip 上）
        <input id="f-label" value="${escapeAttr(d.label || "")}" placeholder="例：查 GPU" />
      </label>
      <label>指令模板（点击填入输入框）
        <textarea id="f-snip-text" class="tall" placeholder="运行 nvidia-smi，总结当前 GPU 占用。">${escapeHtml(d.text || "")}</textarea>
      </label>
      <div class="form-err" id="f-err"></div>
      <div class="form-actions">
        <button type="button" class="cancel">取消</button>
        <button type="submit" class="save">保存</button>
      </div>`;
  }

  function agentFormHtml(d, isNew) {
    return `
      <label>名称（创建后不可改）
        <input id="f-name" value="${escapeAttr(d.name)}" ${isNew ? "" : "disabled"} placeholder="例：code-reviewer" />
      </label>
      <label>描述（何时调用此子智能体）
        <input id="f-desc" value="${escapeAttr(d.description)}" placeholder="一句话说明用途" />
      </label>
      <label>工具（逗号分隔，留空=继承全部）
        <input id="f-tools" value="${escapeAttr((d.tools || []).join(", "))}" placeholder="Read, Grep, Bash" />
      </label>
      <label>模型（留空=继承主模型）
        <input id="f-model" value="${escapeAttr(d.model)}" placeholder="如 claude-sonnet-4-6" />
      </label>
      <label>System Prompt
        <textarea id="f-prompt" class="tall" placeholder="子智能体的系统提示词…">${escapeHtml(d.prompt || "")}</textarea>
      </label>
      <div class="form-err" id="f-err"></div>
      <div class="form-actions">
        <button type="button" class="cancel">取消</button>
        <button type="submit" class="save">保存</button>
      </div>`;
  }

  async function submitManageForm(origName, isNew) {
    const errEl = $("f-err");
    errEl.textContent = "";
    const v = (id) => { const e = $(id); return e ? e.value.trim() : ""; };
    let payload, path, method;
    if (manage.kind === "snippets") {
      const label = v("f-label");
      if (!label) { errEl.textContent = "标签不能为空"; return; }
      const text = ($("f-snip-text") || {}).value || "";
      payload = { label, text };
      if (isNew) { path = apiBase(); method = "POST"; }
      else { path = `${apiBase()}/${encodeURIComponent(origName)}`; method = "PUT"; }
    } else if (manage.kind === "memory") {
      payload = { description: v("f-desc"), body: $("f-body").value };
      if (isNew) { payload.name = v("f-name"); if (!payload.name) { errEl.textContent = "名称不能为空"; return; } }
      if (isNew) { path = apiBase(); method = "POST"; }
      else { path = `${apiBase()}/${encodeURIComponent(origName)}`; method = "PUT"; }
    } else {
      payload = { description: v("f-desc"), tools: v("f-tools"), model: v("f-model"), prompt: $("f-prompt").value };
      if (isNew) { payload.name = v("f-name"); if (!payload.name) { errEl.textContent = "名称不能为空"; return; } }
      if (isNew) { path = apiBase(); method = "POST"; }
      else { path = `${apiBase()}/${encodeURIComponent(origName)}`; method = "PUT"; }
    }
    try {
      await api(path, { method, body: JSON.stringify(payload) });
      toast(isNew ? "已创建" : "已保存", "success");
      if (manage.kind === "snippets") loadSnippets();  // 刷新 chip 行
      await showManageList();
    } catch (e) { errEl.textContent = e.message; }
  }

  // ---------------- 待办清单（自有渲染，复用 manage-list 容器）----------------
  async function showTodoList() {
    todoFormActive = false;
    const listEl = $("manage-list");
    listEl.innerHTML = '<div class="entity-loading">加载中…</div>';
    try {
      const items = await api("/api/todos");
      listEl.innerHTML = "";
      if (!items.length) {
        listEl.innerHTML = '<div class="entity-empty"><div class="empty-emoji">✅</div><div>暂无待办</div></div>';
        return;
      }
      for (const it of items) {
        const li = el("li");
        const head = el("div", "e-head");
        head.appendChild(el("span", "e-name", escapeHtml(it.title)));
        const tag = it.status === "done" ? "完成" : it.status === "cancelled" ? "取消" : it.status === "in_progress" ? "进行中" : (it.priority ? "⚡高优" : "待办");
        head.appendChild(el("span", "e-tag", tag));
        li.appendChild(head);
        if (it.description) li.appendChild(el("div", "e-desc", escapeHtml(it.description)));
        const actions = el("div", "e-actions");
        if (it.status !== "done" && it.status !== "cancelled") {
          const doneBtn = el("button", "btn-sm", "完成");
          doneBtn.onclick = async () => {
            try {
              await api(`/api/todos/${it.id}`, { method: "PUT", body: JSON.stringify({ status: "done" }) });
              showTodoList();
            } catch (e) { toast("操作失败：" + e.message, "error"); }
          };
          actions.appendChild(doneBtn);
        }
        const delBtn = el("button", "btn-sm danger", "删除");
        delBtn.onclick = async () => {
          if (!confirm(`确定删除「${it.title}」？`)) return;
          try {
            await api(`/api/todos/${it.id}`, { method: "DELETE" });
            showTodoList();
          } catch (e) { toast("删除失败：" + e.message, "error"); }
        };
        actions.appendChild(delBtn);
        li.appendChild(actions);
        listEl.appendChild(li);
      }
    } catch (e) {
      listEl.innerHTML = `<div class="entity-empty">加载失败：${escapeHtml(e.message)}</div>`;
    }
  }

  function showTodoForm() {
    todoFormActive = true;
    const listEl = $("manage-list");
    listEl.innerHTML = "";

    const form = el("div", "entity-form");

    const titleInput = el("input");
    titleInput.type = "text";
    titleInput.placeholder = "待办标题（必填）";
    titleInput.className = "form-input";

    const descInput = el("textarea");
    descInput.placeholder = "详细描述（可选）";
    descInput.className = "form-input";
    descInput.rows = 3;

    const priLabel = el("label", "", "优先级：");
    const priSelect = el("select", "form-select");
    priSelect.innerHTML = '<option value="0">普通</option><option value="1">⚡ 高优</option>';

    const saveBtn = el("button", "btn-primary", "保存");
    saveBtn.onclick = async () => {
      const title = titleInput.value.trim();
      if (!title) { toast("标题不能为空", "error"); return; }
      try {
        await api("/api/todos", {
          method: "POST",
          body: JSON.stringify({ title, description: descInput.value.trim(), priority: parseInt(priSelect.value) }),
        });
        toast("待办已添加", "success");
        showTodoList();
      } catch (e) { toast("保存失败：" + e.message, "error"); }
    };

    const cancelBtn = el("button", "btn-sm", "取消");
    cancelBtn.onclick = () => showTodoList();

    form.append(titleInput, descInput, priLabel, priSelect, saveBtn, cancelBtn);
    listEl.appendChild(form);
  }

  // ---------------- 备忘录（自有渲染，复用 manage-list 容器）----------------
  function memoModeLabel(it) {
    const wd = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];
    const mode = it.remind_mode || "daily";
    const at = String(it.remind_at || "");
    switch (mode) {
      case "none": return "不提醒";
      case "weekly": return "每周" + (wd[parseInt(at, 10)] || "?");
      case "monthly": return "每月" + parseInt(at, 10) + "号";
      case "once": return "单次 " + at;
      case "deadline":
        return "截止 " + at + (it.remind_days_before ? "（提前" + it.remind_days_before + "天）" : "");
      default: return "每日";
    }
  }

  async function showMemoList() {
    todoFormActive = false;
    clearMemoBadge();
    const listEl = $("manage-list");
    listEl.innerHTML = '<div class="entity-loading">加载中…</div>';
    try {
      const items = await api("/api/memos");
      listEl.innerHTML = "";
      if (!items.length) {
        listEl.innerHTML = '<div class="entity-empty"><div class="empty-emoji">📝</div><div>暂无备忘</div><div class="empty-sub">点右上角「+ 新建」记录要提醒的事情</div></div>';
        return;
      }
      for (const it of items) {
        const li = el("li");
        const head = el("div", "e-head");
        head.appendChild(el("span", "e-name", escapeHtml(it.content)));
        head.appendChild(el("span", "e-tag", it.status === "done" ? "已完成" : "未完成"));
        if (it.last_reminded_at) {
          const todayStr = new Date().toISOString().slice(0, 10);
          const isToday = it.reminded_date === todayStr;
          const remindTag = document.createElement("span");
          remindTag.className = "e-tag" + (isToday ? " tag-warn" : "");
          remindTag.textContent = (isToday ? "🔔 今日已提醒 " : "已提醒 ") + fmtTs(it.last_reminded_at);
          head.appendChild(remindTag);
        }
        const modeTag = document.createElement("span");
        modeTag.className = "e-tag";
        modeTag.textContent = memoModeLabel(it);
        head.appendChild(modeTag);
        li.appendChild(head);
        const actions = el("div", "e-actions");
        if (it.status !== "done") {
          const doneBtn = el("button", "btn-sm", "完成");
          doneBtn.onclick = async () => {
            try {
              await api(`/api/memos/${it.id}`, { method: "PUT", body: JSON.stringify({ status: "done" }) });
              showMemoList();
            } catch (e) { toast("操作失败：" + e.message, "error"); }
          };
          actions.appendChild(doneBtn);
        }
        const editBtn = el("button", "btn-sm", "编辑");
        editBtn.onclick = () => showMemoForm(it.id);
        actions.appendChild(editBtn);
        const delBtn = el("button", "btn-sm danger", "删除");
        delBtn.onclick = async () => {
          if (!confirm("确认删除这条备忘？")) return;
          try {
            await api(`/api/memos/${it.id}`, { method: "DELETE" });
            showMemoList();
          } catch (e) { toast("删除失败：" + e.message, "error"); }
        };
        actions.appendChild(delBtn);
        li.appendChild(actions);
        listEl.appendChild(li);
      }
    } catch (e) {
      listEl.innerHTML = `<div class="entity-empty">加载失败：${escapeHtml(e.message)}</div>`;
    }
  }

  async function showMemoForm(id) {
    todoFormActive = true;
    const listEl = $("manage-list");
    listEl.innerHTML = "";
    let memo = { content: "", remind_enabled: 1, status: "active",
                 remind_mode: "daily", remind_at: "", remind_days_before: 0 };
    if (id) {
      try {
        const items = await api("/api/memos");
        memo = items.find((m) => m.id === id) || memo;
      } catch (e) { toast("加载失败：" + e.message, "error"); }
    }

    const form = el("div", "entity-form");

    const contentInput = el("textarea");
    contentInput.placeholder = "记录要提醒的事情…";
    contentInput.className = "form-input";
    contentInput.rows = 4;
    contentInput.value = memo.content || "";

    // 提醒模式 select
    const modeLabel = el("div", "form-label", "提醒方式");
    const modeSelect = document.createElement("select");
    modeSelect.className = "form-input";
    [["daily", "每天提醒"], ["weekly", "每周指定星期"], ["monthly", "每月指定日期"],
     ["once", "指定日期单次提醒"], ["deadline", "截止日提醒"], ["none", "不提醒"]]
      .forEach(([v, t]) => {
        const o = document.createElement("option");
        o.value = v; o.textContent = t;
        modeSelect.appendChild(o);
      });
    modeSelect.value = memo.remind_mode || "daily";

    // 动态参数区
    const paramBox = document.createElement("div");
    paramBox.className = "form-group";

    const weekSelect = document.createElement("select");
    weekSelect.className = "form-input";
    ["周一", "周二", "周三", "周四", "周五", "周六", "周日"].forEach((t, i) => {
      const o = document.createElement("option");
      o.value = String(i); o.textContent = t; weekSelect.appendChild(o);
    });

    const daySelect = document.createElement("select");
    daySelect.className = "form-input";
    for (let d = 1; d <= 31; d++) {
      const o = document.createElement("option");
      o.value = String(d); o.textContent = d + " 号"; daySelect.appendChild(o);
    }

    const dateInput = document.createElement("input");
    dateInput.className = "form-input"; dateInput.type = "date";

    const daysLabel = el("div", "form-label", "提前几天开始提醒");
    const daysInput = document.createElement("input");
    daysInput.className = "form-input"; daysInput.type = "number";
    daysInput.min = "0"; daysInput.placeholder = "提前几天（默认0）";

    // 回填已有值
    if (memo.remind_mode === "weekly") weekSelect.value = String(memo.remind_at || "0");
    if (memo.remind_mode === "monthly") daySelect.value = String(memo.remind_at || "1");
    if (memo.remind_mode === "once" || memo.remind_mode === "deadline") dateInput.value = memo.remind_at || "";
    daysInput.value = String(memo.remind_days_before || 0);

    function renderParams() {
      paramBox.innerHTML = "";
      const m = modeSelect.value;
      if (m === "weekly") paramBox.appendChild(weekSelect);
      else if (m === "monthly") paramBox.appendChild(daySelect);
      else if (m === "once") paramBox.appendChild(dateInput);
      else if (m === "deadline") { paramBox.appendChild(dateInput); paramBox.appendChild(daysLabel); paramBox.appendChild(daysInput); }
    }
    modeSelect.onchange = renderParams;
    renderParams();

    const saveBtn = el("button", "btn-primary", "保存");
    saveBtn.onclick = async () => {
      const content = contentInput.value.trim();
      if (!content) { toast("备忘内容不能为空", "error"); return; }
      const remind_mode = modeSelect.value;
      let remind_at = "";
      if (remind_mode === "weekly") remind_at = weekSelect.value;
      else if (remind_mode === "monthly") remind_at = daySelect.value;
      else if (remind_mode === "once" || remind_mode === "deadline") remind_at = dateInput.value;
      if ((remind_mode === "once" || remind_mode === "deadline") && !remind_at) {
        toast("请选择日期", "error"); return;
      }
      const remind_days_before = remind_mode === "deadline" ? (parseInt(daysInput.value, 10) || 0) : 0;
      const remind_enabled = remind_mode === "none" ? 0 : 1;
      const payload = { content, remind_enabled, remind_mode, remind_at, remind_days_before };
      try {
        if (id) {
          await api(`/api/memos/${id}`, { method: "PUT", body: JSON.stringify(payload) });
        } else {
          await api("/api/memos", { method: "POST", body: JSON.stringify(payload) });
        }
        toast(id ? "已保存" : "备忘已添加", "success");
        showMemoList();
      } catch (e) { toast("保存失败：" + e.message, "error"); }
    };

    const cancelBtn = el("button", "btn-sm", "取消");
    cancelBtn.onclick = () => showMemoList();

    form.append(contentInput, modeLabel, modeSelect, paramBox, saveBtn, cancelBtn);
    listEl.appendChild(form);
  }

  // ---------------- 日报记录（自有渲染，复用 manage-list 容器）----------------
  async function showReportList() {
    const listEl = $("manage-list");
    listEl.innerHTML = '<div class="entity-loading">加载中…</div>';
    try {
      const items = await api("/api/reports");
      listEl.innerHTML = "";
      if (!items.length) {
        listEl.innerHTML = '<div class="entity-empty"><div class="empty-emoji">📋</div><div>暂无日报</div></div>';
        return;
      }
      for (const it of items) {
        const li = el("li");
        const head = el("div", "e-head");
        const typeLabel = it.report_type === "evening" ? "晚报" : "早报";
        head.appendChild(el("span", "e-name", escapeHtml(`${it.report_date} ${typeLabel}`)));
        li.appendChild(head);
        const preview = el("div", "e-desc", escapeHtml((it.content || "").slice(0, 100) + "…"));
        li.appendChild(preview);
        const viewBtn = el("button", "btn-sm", "查看全文");
        viewBtn.onclick = () => showReportDetail(it);
        li.appendChild(viewBtn);
        listEl.appendChild(li);
      }
    } catch (e) {
      listEl.innerHTML = `<div class="entity-empty">加载失败：${escapeHtml(e.message)}</div>`;
    }
  }

  function showReportDetail(report) {
    const typeLabel = report.report_type === "evening" ? "晚报" : "早报";
    const root = $("modal-root");
    root.innerHTML = "";
    const card = el("div", "modal-card");
    const title = el("div", "modal-title");
    title.textContent = `${report.report_date} ${typeLabel}`;
    const body = el("div", "modal-msg");
    body.style.cssText = "text-align:left;white-space:pre-wrap;max-height:60vh;overflow-y:auto;font-size:0.9rem";
    body.textContent = report.content;
    const actions = el("div", "modal-actions");
    const closeBtn = el("button", "modal-ok");
    closeBtn.textContent = "关闭";
    actions.appendChild(closeBtn);
    card.append(title, body, actions);
    root.appendChild(card);
    root.classList.remove("hidden");
    requestAnimationFrame(() => root.classList.add("show"));
    const close = () => {
      root.classList.remove("show");
      setTimeout(() => { root.classList.add("hidden"); root.innerHTML = ""; }, 200);
    };
    closeBtn.onclick = close;
    root.onclick = (e) => { if (e.target === root) close(); };
  }

  // ---------------- 渲染 ----------------
  function el(tag, cls, html) { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }

  // ---------------- 产物预览 ----------------
  function fileUrl(path, download) {
    return `${BASE}/api/file?session_id=${encodeURIComponent(state.sessionId)}&path=${encodeURIComponent(path)}` +
      (download ? "&download=1" : "");
  }
  // 从文本里抽取图片路径（去重，最多 6 个），渲染成缩略图附在消息下方
  function attachArtifacts(node, text) {
    if (!text) return;
    const re = /([~/\w.\-]+\.(?:png|jpe?g|gif|svg|webp|bmp))/gi;
    const seen = new Set();
    let m;
    while ((m = re.exec(text)) && seen.size < 6) {
      const p = m[1];
      if (p.length < 5 || seen.has(p)) continue;  // 太短的忽略
      seen.add(p);
    }
    if (!seen.size) return;
    const wrap = el("div", "artifacts");
    for (const p of seen) {
      const img = el("img", "artifact-thumb");
      img.loading = "lazy";
      img.src = fileUrl(p);
      img.alt = p;
      img.onerror = () => img.remove();   // 路径不可读就移除，不留破图
      img.onclick = () => openImageViewer(fileUrl(p), p);
      wrap.appendChild(img);
    }
    node.appendChild(wrap);
  }
  // 全屏看大图
  function openImageViewer(src, caption) {
    const root = $("modal-root");
    root.innerHTML = "";
    const box = el("div", "img-viewer");
    box.innerHTML = `<img src="${escapeAttr(src)}" alt="" />
      <div class="img-cap">${escapeHtml(caption || "")}</div>
      <a class="img-dl" href="${escapeAttr(src + "&download=1")}" download>下载</a>`;
    root.appendChild(box);
    root.classList.remove("hidden");
    requestAnimationFrame(() => root.classList.add("show"));
    const close = () => { root.classList.remove("show"); setTimeout(() => { root.classList.add("hidden"); root.innerHTML = ""; }, 200); };
    root.onclick = (e) => { if (e.target === root || e.target === box) close(); };
  }

  // 构建一条消息的 DOM 节点（不挂载）。返回 node 或 null（system/未知类型跳过）。
  // 拆出来是为了让「加载更早」能批量 build 后一次性插入。
  // 从 assistant 气泡末尾提取 quick-reply 选项。
  // 情况1：末尾列表项均短（≤50字、≤8项），提取为选项。
  // 从 assistant 气泡末尾提取 quick-reply 选项。
  // 只在消息带有明确提问意图时触发（含问号，或前文含"请选择/以下方式/如何处理"等引导语）。
  // 情况1：带提问意图 + 末尾列表 → 列表项截标题作为选项。
  // 情况2：末尾是决策型问句 → 补充"是的""不用了"。
  function extractQuickReplies(bubble) {
    const fullText = bubble.textContent.trim();
    if (!fullText) return [];

    // 全文是否含提问意图：有问号，或有"请选择/怎么/如何/哪种/以下.*方式/你希望"等引导
    const hasQuestion = /[？?]/.test(fullText);
    const hasGuide = /请选择|你想|您想|如何处理|怎么处理|哪种|哪个|以下.*方式|你希望|您希望|该如何|想怎|要怎/.test(fullText);
    if (!hasQuestion && !hasGuide) return [];

    // 情况1：末尾列表 + 提问意图
    const lists = bubble.querySelectorAll("ul, ol");
    if (lists.length) {
      const last = lists[lists.length - 1];
      // 列表必须在 bubble 末尾（后面没有实质内容）
      let next = last.nextSibling;
      let trailingOk = true;
      while (next) {
        if (next.nodeType === 1) { trailingOk = false; break; }
        if (next.nodeType === 3 && next.textContent.trim()) { trailingOk = false; break; }
        next = next.nextSibling;
      }
      if (trailingOk) {
        const items = Array.from(last.querySelectorAll(":scope > li"));
        if (items.length && items.length <= 8) {
          const texts = items.map(li => {
            const raw = li.textContent.trim();
            const noNum = raw.replace(/^\d+[.)]\s*/, "");
            const title = noNum.split(/\s[—–\-]\s|：|:\s|\.\s/).at(0).trim();
            return title.length <= 30 ? title : title.slice(0, 30) + "…";
          });
          if (!texts.some(t => !t)) {
            const bubbleText = bubble.textContent.replace(last.textContent, "").trim();
            if (bubbleText) return texts;
          }
        }
      }
    }

    // 情况2：末尾决策型问句 → yes/no 快捷回复
    const lastSentence = fullText.split(/[。\n]/).map(s => s.trim()).filter(Boolean).at(-1) || "";
    if (!/[？?]$/.test(lastSentence)) return [];
    const decisionRe = /需要|要不要|是否|帮(你|我)|想要|继续|配置|开始|确认|可以吗|好吗|行吗|对吗|试试|使用|运行|执行|要我|要帮/;
    if (!decisionRe.test(lastSentence)) return [];
    if (fullText.length < 20) return [];
    return ["是的，请继续", "不用了，谢谢"];
  }

  function buildMessageNode(role, content, ts = null, interactive = true) {
    let node;
    if (role === "user" || role === "assistant") {
      node = el("div", "msg " + role);
      const bubble = el("div", "bubble");
      if (role === "assistant") {
        bubble.classList.add("markdown");
        bubble.innerHTML = renderMarkdown(content.text || "");
        // 复制按钮：复制原始 markdown 文本
        const copy = el("button", "msg-copy", "复制");
        copy.type = "button";
        copy.onclick = () => { copyText(content.text || "").then((ok) => { flashCopied(copy, ok); if (ok) toast("已复制到剪贴板", "success", 1500); }); };
        node.appendChild(copy);
        // Quick-reply：检测 bubble 末尾的短列表项，提取为可点击选项按钮
        const qr = extractQuickReplies(bubble);
        if (qr.length) {
          const bar = el("div", "qr-bar");
          qr.forEach(text => {
            const btn = el("button", "qr-btn", text);
            btn.type = "button";
            btn.onclick = () => { input.value = text; input.dispatchEvent(new Event("input")); input.focus(); };
            bar.appendChild(btn);
          });
          node.appendChild(bar);
        }
      } else {
        bubble.textContent = content.text || "";
        // user 消息重发按钮：点击把内容填回输入框
        const resend = el("button", "msg-resend", "重发");
        resend.type = "button";
        resend.onclick = () => { input.value = content.text || ""; input.dispatchEvent(new Event("input")); input.focus(); };
        node.appendChild(resend);
      }
      node.appendChild(bubble);
      // 产物预览：扫描文本里的图片/文件路径，渲染缩略图/可点链接
      if (role === "assistant") attachArtifacts(node, content.text || "");
      node.appendChild(el("div", "msg-time", escapeHtml(fmtClock(ts || nowTs()))));
    } else if (role === "error") {
      node = el("div", "msg error");
      node.appendChild(el("div", "bubble", escapeHtml(content.message || "出错了")));
    } else if (role === "tool_use") {
      const toolName = content.name || "";
      const isAskTool = /^Ask(Followup|User|Clarif)/i.test(toolName);
      if (toolName === "Agent") {
        const inp = content.input || {};
        const subtype = inp.subagent_type || "agent";
        const desc = String(inp.description || inp.prompt || "").slice(0, 60);
        node = el("div", "subagent");
        if (content.id) node.dataset.agentId = content.id;
        const expanded = state.subagentExpanded;
        node.innerHTML = `<div class="subagent-head">
            <span class="sap-caret">▸</span>
            <span class="subagent-icon">🤖</span>
            <span class="subagent-title">${escapeHtml(subtype)}${desc ? " · " + escapeHtml(desc) : ""}</span>
            <span class="subagent-status running">运行中</span>
          </div>
          <div class="subagent-body" style="${expanded ? "" : "display:none"}"></div>
          <div class="subagent-result" style="display:none">
            <div class="subagent-result-content"></div>
          </div>`;
        if (expanded) node.classList.add("open");
        node.querySelector(".subagent-head").addEventListener("click", function() {
          const isOpen = node.classList.toggle("open");
          node.querySelector(".subagent-body").style.display = isOpen ? "" : "none";
          const rbox = node.querySelector(".subagent-result");
          if (rbox && rbox.dataset.hasResult) rbox.style.display = isOpen ? "" : "none";
        });
      } else if (isAskTool) {
        // 问答类工具：渲染为高亮问题卡片
        node = el("div", "msg-ask");
        const inp = content.input || {};
        // 支持 AskFollowupQuestions / AskUserQuestion 的多种字段布局
        const questions = Array.isArray(inp.questions) ? inp.questions : (inp.question ? [inp.question] : []);
        const opts = inp.options || [];
        const optText = (o) => (o && typeof o === "object")
          ? (o.label ?? o.text ?? o.value ?? JSON.stringify(o))
          : String(o);
        let html = `<div class="ask-header"><span class="ask-icon">❓</span><span class="ask-title">需要确认</span></div>`;
        if (questions.length) {
          html += questions.map(q => {
            const qtext = typeof q === "string" ? q : (q.question || q.text || JSON.stringify(q));
            const qopts = typeof q === "object" ? (q.options || []) : [];
            let qhtml = `<div class="ask-question">${escapeHtml(qtext)}</div>`;
            if (qopts.length) {
              qhtml += `<div class="ask-opts">${qopts.map(o => { const t = optText(o); return `<span class="ask-opt" data-value="${escapeAttr(t)}">${escapeHtml(t)}</span>`; }).join("")}</div>`;
            }
            return qhtml;
          }).join("");
        }
        if (opts.length) {
          html += `<div class="ask-opts">${opts.map(o => { const t = optText(o); return `<span class="ask-opt" data-value="${escapeAttr(t)}">${escapeHtml(t)}</span>`; }).join("")}</div>`;
        }
        node.innerHTML = html;
        // ask-opt 仅作展示，不响应点击：作答统一走 showAskQuestionDialog 弹窗，
        // 经 permission_response 的 updated_input.answers 通道回传。
      } else {
        node = el("details", "tool");
        const inputStr = typeof content.input === "object" ? JSON.stringify(content.input, null, 2) : String(content.input ?? "");
        const brief = (content.input && (content.input.command || content.input.file_path || content.input.path || content.input.pattern || content.input.description || content.input.query)) || "";
        node.innerHTML = `<summary><span class="tag">${escapeHtml(toolName || "tool")}</span>
          <span class="summary-text">${escapeHtml(String(brief).slice(0, 80))}</span></summary>
          <pre>${escapeHtml(inputStr)}</pre>`;
      }
    } else if (role === "tool_result") {
      node = el("details", "tool");
      if (content.is_error) node.open = true;  // 出错自动展开，方便排查
      const errCls = content.is_error ? " err" : "";
      node.innerHTML = `<summary><span class="tag${errCls}">结果${content.is_error ? " ✗" : ""}</span>
        <span class="summary-text">${escapeHtml(String(content.output || "").slice(0, 80))}</span></summary>
        <pre>${escapeHtml(String(content.output || ""))}</pre>`;
    } else if (role === "result") {
      // 兜底：resume 失败的坏 result（num_turns=0 且报错，errors 含 "No conversation found"）
      // 后端一般已吞掉不下发，万一漏网也不渲染空的“$0.0000 完成”行。判定收窄到 resume
      // 失败关键字，避免误伤 agent 正常执行中产生的其他 num_turns=0 报错。
      const isResumeFail = content.num_turns === 0 && content.is_error &&
        JSON.stringify(content.errors ?? "").includes("No conversation found");
      if (isResumeFail) return null;
      const dur = content.duration_ms ? (content.duration_ms / 1000).toFixed(1) + "s" : "";
      const cost = content.cost_usd != null ? " · $" + content.cost_usd.toFixed(4) : "";
      const turns = content.num_turns ? " · " + content.num_turns + " 轮" : "";
      node = el("div", "result-line", `本回合完成 ${dur}${cost}${turns}`);
    } else if (role === "system") {
      return null; // init 信息不展示
    } else { return null; }
    return node;
  }

  function renderMessage(role, content, doScroll = true, ts = null, interactive = true) {
    const node = buildMessageNode(role, content, ts, interactive);
    if (!node) return;
    const chat = $("chat");
    node.classList.add("msg-enter");
    const welcome = chat.querySelector(".chat-welcome");
    if (welcome) welcome.remove();
    chat.appendChild(node);
    requestAnimationFrame(() => node.classList.add("msg-in"));
    if (doScroll) scrollBottom();
  }

  // 子智能体归拢：构建消息节点后按 parent 路由。父容器 parentEl（#chat），groups 为
  // Agent id -> .subagent-body 映射。历史渲染用局部 groups，实时用 state.agentGroups。
  // 老历史消息无 parent 字段时容忍缺失，正常平铺，不报错。
  function appendMessageGrouped(parentEl, groups, role, content, ts = null) {
    // 1) Agent 工具调用：建卡片挂到父容器，登记其 body 供内部步骤归拢
    if (role === "tool_use" && content.name === "Agent") {
      const node = buildMessageNode(role, content, ts, false);
      if (!node) return;
      // 嵌套 Agent：若本卡片自身带 parent 且命中某父卡片，挂进父级 body，否则挂顶层
      const targetEl = (content.parent && groups[content.parent]) ? groups[content.parent] : parentEl;
      node.classList.add("msg-enter");
      targetEl.appendChild(node);
      requestAnimationFrame(() => node.classList.add("msg-in"));
      if (content.id) groups[content.id] = node.querySelector(".subagent-body");
      return;
    }
    // 2) 子智能体的最终结果：tool_result 的 tool_use_id 命中某张卡片 → 填入结果区并收尾
    if (role === "tool_result" && content.tool_use_id && groups[content.tool_use_id]) {
      const bodyEl = groups[content.tool_use_id];
      const card = bodyEl.closest(".subagent");
      if (card) {
        const rc = card.querySelector(".subagent-result-content");
        const rbox = card.querySelector(".subagent-result");
        if (rc) { rc.classList.add("markdown"); rc.innerHTML = renderMarkdown(String(content.output || "")); }
        if (rbox) { rbox.dataset.hasResult = "1"; rbox.style.display = card.classList.contains("open") ? "" : "none"; }
        const status = card.querySelector(".subagent-status");
        if (status) { status.textContent = "✓ 完成"; status.classList.remove("running"); status.classList.add("done"); }
      }
      delete groups[content.tool_use_id];  // 注销：卡片已收尾，后续同 id 不再归拢
      return;  // 结果已入卡片，不再平铺这条 tool_result
    }
    // 3) 子智能体内部步骤：parent 命中某张卡片 → 追加到该卡片 body
    if (content.parent && groups[content.parent]) {
      const node = buildMessageNode(role, content, ts, false);
      if (node) groups[content.parent].appendChild(node);
      return;
    }
    // 4) 其余：正常平铺到父容器
    const node = buildMessageNode(role, content, ts, false);
    if (!node) return;
    const welcome = parentEl.querySelector(".chat-welcome");
    if (welcome) welcome.remove();
    node.classList.add("msg-enter");
    if (node.classList.contains("tool")) {
      appendToToolGroup(parentEl, node);
    } else {
      parentEl.appendChild(node);
    }
    requestAnimationFrame(() => node.classList.add("msg-in"));
  }

  const TOOL_GROUP_MIN = 3;

  function trailingToolGroup(parentEl) {
    let n = parentEl.lastElementChild;
    if (n && state.typingEl && n === state.typingEl) n = n.previousElementSibling;
    return (n && n.classList && n.classList.contains("tool-group")) ? n : null;
  }

  function updateToolGroupLabel(group) {
    const count = group.querySelector(".tool-group-body").children.length;
    const open = group.classList.contains("open");
    group.querySelector(".tg-label").textContent =
      (open ? "收起 " : "展开查看 ") + count + " 条工具调用";
  }

  function appendToToolGroup(parentEl, node) {
    let group = trailingToolGroup(parentEl);
    if (!group) {
      group = el("div", "tool-group open");
      group.innerHTML =
        `<div class="tool-group-head"><span class="tg-caret">▸</span>` +
        `<span class="tg-label"></span></div><div class="tool-group-body"></div>`;
      group.querySelector(".tool-group-head").addEventListener("click", () => {
        const open = group.classList.toggle("open");
        group.querySelector(".tool-group-body").style.display = open ? "" : "none";
        updateToolGroupLabel(group);
      });
      const typing = state.typingEl;
      if (typing && typing.parentElement === parentEl) parentEl.insertBefore(group, typing);
      else parentEl.appendChild(group);
    }
    const body = group.querySelector(".tool-group-body");
    body.appendChild(node);
    const count = body.children.length;
    const head = group.querySelector(".tool-group-head");
    if (count === TOOL_GROUP_MIN && !group.dataset.autofolded) {
      group.dataset.autofolded = "1";
      group.classList.remove("open");
      body.style.display = "none";
    }
    head.style.display = count >= TOOL_GROUP_MIN ? "flex" : "none";
    updateToolGroupLabel(group);
  }

  // 队列托盘：渲染排队待执行的指令，支持编辑/删除。
  function renderQueue() {
    const tray = $("queue-tray");
    if (!tray) return;
    tray.innerHTML = "";
    if (!state.queue.length) {
      tray.classList.add("hidden");
      return;
    }
    tray.classList.remove("hidden");
    for (const q of state.queue) {
      const chip = document.createElement("div");
      chip.className = "queue-chip";

      const span = document.createElement("span");
      span.className = "q-text";
      span.textContent = q.text.slice(0, 60) + (q.text.length > 60 ? "…" : "");
      chip.appendChild(span);

      const edit = document.createElement("button");
      edit.className = "q-edit";
      edit.textContent = "✎";
      edit.title = "编辑";
      edit.onclick = async () => {
        const nt = prompt("编辑排队指令：", q.text);
        if (nt == null || !nt.trim()) return;
        try {
          await api(`/api/sessions/${state.sessionId}/queue/${q.id}`, {
            method: "PATCH",
            body: JSON.stringify({ text: nt.trim() }),
          });
        } catch (e) {
          toast("编辑失败：" + e.message, "error");
        }
      };

      const del = document.createElement("button");
      del.className = "q-del";
      del.textContent = "×";
      del.title = "删除";
      del.onclick = async () => {
        try {
          await api(`/api/sessions/${state.sessionId}/queue/${q.id}`, { method: "DELETE" });
        } catch (e) {
          toast("删除失败：" + e.message, "error");
        }
      };

      chip.append(edit, del);
      tray.appendChild(chip);
    }
  }

  function flashCopied(btn, ok) {
    const old = btn.textContent;
    btn.textContent = ok ? "已复制 ✓" : "复制失败";
    btn.classList.add("copied");
    setTimeout(() => { btn.textContent = old; btn.classList.remove("copied"); }, 1500);
  }

  function showTyping() {
    if (state.typingEl) return;
    state.typingEl = el("div", "msg assistant");
    const bub = el("div", "typing", "<span></span><span></span><span></span>");
    const clock = el("span", "typing-clock", "");
    bub.appendChild(clock);
    state.typingEl.appendChild(bub);
    $("chat").appendChild(state.typingEl);
    scrollBottom();
    // 实时计时：让 4-5s 的云端推理等待可见、不显得卡死
    const t0 = Date.now();
    state.typingTimer = setInterval(() => {
      const s = (Date.now() - t0) / 1000;
      clock.textContent = "已思考 " + s.toFixed(s < 10 ? 1 : 0) + "s";
    }, 100);
  }
  function hideTyping() {
    if (state.typingTimer) { clearInterval(state.typingTimer); state.typingTimer = null; }
    if (state.typingEl) { state.typingEl.remove(); state.typingEl = null; }
  }

  // 智能滚动：用户在底部附近才自动跟随；否则不打断，亮出"新消息"pill
  function isNearBottom() {
    const c = $("chat");
    return c.scrollHeight - c.scrollTop - c.clientHeight < 120;
  }
  function scrollBottom(force = false) {
    const c = $("chat");
    if (force || isNearBottom()) {
      requestAnimationFrame(() => { c.scrollTop = c.scrollHeight; });
      hideNewPill();
    } else {
      showNewPill();
    }
  }
  function showNewPill() {
    let pill = $("new-msg-pill");
    if (!pill) {
      pill = el("button", "new-msg-pill", "新消息 ↓");
      pill.id = "new-msg-pill";
      pill.onclick = () => scrollBottom(true);
      $("chat").parentElement.appendChild(pill);
    }
    pill.classList.add("show");
  }
  function hideNewPill() { const p = $("new-msg-pill"); if (p) p.classList.remove("show"); }

  // 回顶部按钮：聊天区向上滚超过 1.5 屏时浮现
  function ensureTopBtn() {
    let btn = $("to-top-btn");
    if (!btn) {
      btn = el("button", "to-top-btn", "↑ 顶部");
      btn.id = "to-top-btn";
      btn.onclick = () => { $("chat").scrollTo({ top: 0, behavior: "smooth" }); };
      $("chat").parentElement.appendChild(btn);
    }
    return btn;
  }
  $("chat").addEventListener("scroll", () => {
    const c = $("chat");
    const btn = ensureTopBtn();
    btn.classList.toggle("show", c.scrollTop > c.clientHeight * 1.5);
  }, { passive: true });

  // ---------------- WebSocket ----------------
  function setConn(s) { const d = $("conn-dot"); d.className = "conn-dot" + (s === "ok" ? " ok" : s === "bad" ? " bad" : ""); }

  // 切后台/锁屏回来后的"对齐"：重连 + 补拉历史 + 刷会话状态。
  // 手机锁屏会挂起 JS、断开 WS，期间 Agent 推送的消息全丢，回来必须主动补。
  function resync() {
    if (!state.token || !state.sessionId) return;
    const now = Date.now();
    if (now - (state._lastResync || 0) < 3000) return;  // 节流，避免频繁切换反复刷
    state._lastResync = now;
    // WS 断了就重连
    if (!state.ws || state.ws.readyState > 1) connectWs();
    // 流式进行中不整体重渲染（会打断打字机）；否则补拉历史找回漏掉的消息
    if (!state.streamEl) loadHistory().catch(() => {});
    // 按当前会话真实状态校正按钮态（保险：即使没等到 status 消息也能自愈卡死的输入框）
    loadSessions().then(() => {
      const cur = state.sessions.find((s) => s.id === state.sessionId);
      if (cur && cur.status !== "running") setRunning(false);
    }).catch(() => {});
  }

  function closeWs() {
    if (state.reconnectTimer) clearTimeout(state.reconnectTimer);
    if (state.ws) { state.ws.onclose = null; state.ws.close(); state.ws = null; }
  }

  function connectWs() {
    closeWs();
    if (!state.sessionId) return;
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const url = `${proto}://${location.host}${BASE}/ws?token=${encodeURIComponent(state.token)}&session_id=${state.sessionId}`;
    const ws = new WebSocket(url);
    state.ws = ws;
    ws.onopen = () => {
      setConn("ok");
      reportVisibility();  // 连上即上报当前前台/后台，供后端决定回合完成是否发企业微信
      startHeartbeat();    // 应用层心跳：每 20s ping，保活穿越 Funnel+SSH 多层代理
      // 只有在"曾经断开过"之后重新连上才提示，首次连接不打扰
      if (state.wasDisconnected) { toast("已重新连接", "success", 1600); state.wasDisconnected = false; }
    };
    ws.onmessage = (ev) => handleWsMessage(JSON.parse(ev.data));
    ws.onclose = () => {
      setConn("bad");
      state.wasDisconnected = true;
      stopHeartbeat();
      // 断线自动重连（手机网络不稳）
      state.reconnectTimer = setTimeout(() => { if (state.token && state.sessionId) connectWs(); }, 2000);
    };
    ws.onerror = () => setConn("bad");
  }

  // 应用层心跳：每 20s 发 ping，让 Tailscale Funnel / SSH 隧道等中间代理认为连接活跃，
  // 避免因空闲被掐断。后端收到 ping 回 pong（pong 在 handleWsMessage 里被忽略即可）。
  function startHeartbeat() {
    stopHeartbeat();
    state.heartbeatTimer = setInterval(() => {
      if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        try { state.ws.send(JSON.stringify({ type: "ping" })); } catch (e) {}
      }
    }, 20000);
  }
  function stopHeartbeat() {
    if (state.heartbeatTimer) { clearInterval(state.heartbeatTimer); state.heartbeatTimer = null; }
  }

  // 等 WS 进入 OPEN（轮询，最多 timeoutMs）。发消息前若没连上，给重连一点时间。
  function waitWsOpen(timeoutMs) {
    return new Promise((resolve) => {
      const t0 = Date.now();
      const iv = setInterval(() => {
        if (state.ws && state.ws.readyState === WebSocket.OPEN) { clearInterval(iv); resolve(true); }
        else if (Date.now() - t0 > timeoutMs) { clearInterval(iv); resolve(false); }
      }, 120);
    });
  }

  function handleWsMessage(data) {
    if (data.type === "pong") return;  // 心跳回应，忽略
    if (data.type === "message") {
      if (data.role === "assistant_delta") {
        // 子智能体增量：parent 命中某张卡片 → 追加到其 body，不碰全局 streamEl（顶层打字机）
        const pid = data.content.parent;
        if (pid && state.agentGroups[pid]) { appendSubagentDelta(pid, data.content.text || ""); return; }
        appendDelta(data.content.text || "");
        return;
      }
      // 权威 assistant 全文：若正在流式，用 markdown 重渲染替换流式气泡；否则新建
      if (data.role === "assistant") {
        hideTyping();
        // 子智能体的最终文本：命中卡片则走分组路由，绝不 finalize 顶层 streamEl
        const pid = data.content.parent;
        if (pid && state.agentGroups[pid]) {
          finalizeSubagentDelta(pid);
          appendMessageGrouped($("chat"), state.agentGroups, data.role, data.content);
          scrollBottom();
          return;
        }
        if (state.streamEl) { finalizeStream(data.content.text || ""); return; }
      }
      if (data.role === "result" || data.role === "error") { hideTyping(); clearStream(); }
      // 记录 tool_use_id -> tool_name 映射，供 tool_result 判断
      if (data.role === "tool_use" && data.content.id) {
        state.toolIdMap[data.content.id] = data.content.name || "";
      }
      // 问答工具的 tool_result 内容是用户回答，已由 user 消息显示，跳过重复渲染
      if (data.role === "tool_result") {
        const toolName = state.toolIdMap[data.content.tool_use_id] || "";
        if (/^Ask(Followup|User|Clarif)/i.test(toolName)) { hideTyping(); clearStream(); showTyping(); return; }
      }
      appendMessageGrouped($("chat"), state.agentGroups, data.role, data.content);
      scrollBottom();
      if (data.role === "tool_use" || data.role === "tool_result") {
        hideTyping(); clearStream();
        // 问答类工具：Claude 在等用户回答，不显示"思考中"
        const isAsk = data.role === "tool_use" && /^Ask(Followup|User|Clarif)/i.test(data.content.name || "");
        if (!isAsk) showTyping();
      }
    } else if (data.type === "status") {
      if (data.status === "running") { setRunning(true); showTyping(); }
      else if (data.sync) {
        // 订阅时的状态对齐（非真实回合结束）：只解禁/复位按钮，不触发完成通知等副作用。
        // 修复：超长回合期间断线 → 回合后台跑完的 status:idle 被错过 → 重连卡在 running。
        setRunning(false); hideTyping(); clearStream();
      } else { setRunning(false); hideTyping(); clearStream(); loadTasks(); maybeNotify(data.result); document.querySelectorAll(".perm-overlay").forEach(o => o.remove()); _permQueue.length = 0; }
    } else if (data.type === "error") {
      hideTyping();
      clearStream();
      renderMessage("error", { message: data.message });
      setRunning(false);
      document.querySelectorAll(".perm-overlay").forEach(o => o.remove());
      _permQueue.length = 0;
    } else if (data.type === "queue_update") {
      state.queue = data.queue || [];
      renderQueue();
      return;
    } else if (data.type === "permission_request") {
      showPermissionDialog(data);
      return;
    }
  }

  // ---------------- 流式打字机 ----------------
  // 首个增量：隐藏 typing 点，建流式气泡（纯文本累积，不走 markdown，末尾带闪烁光标）
  function appendDelta(text) {
    if (!state.streamEl) {
      hideTyping();
      const chat = $("chat");
      const welcome = chat.querySelector(".chat-welcome");
      if (welcome) welcome.remove();
      const node = el("div", "msg assistant msg-enter");
      const bubble = el("div", "bubble streaming");
      node.appendChild(bubble);
      chat.appendChild(node);
      requestAnimationFrame(() => node.classList.add("msg-in"));
      state.streamEl = bubble;
      state.streamText = "";
    }
    state.streamText += text;
    // textContent 累积（安全，不解析 HTML）；光标用 CSS ::after，不进文本
    state.streamEl.textContent = state.streamText;
    scrollBottom();
  }

  // 收到权威全文：用 markdown 重渲染当前流式气泡，去掉光标，清空流式状态
  function finalizeStream(fullText) {
    if (!state.streamEl) { renderMessage("assistant", { text: fullText }); return; }
    const bubble = state.streamEl;
    bubble.classList.remove("streaming");
    bubble.classList.add("markdown");
    bubble.innerHTML = renderMarkdown(fullText || state.streamText || "");
    // 给这条消息补上复制按钮 + 时间戳（与 renderMessage 一致）
    const node = bubble.parentElement;
    if (node && !node.querySelector(".msg-copy")) {
      const copy = el("button", "msg-copy", "复制");
      copy.type = "button";
      const finalText = fullText || state.streamText || "";
      copy.onclick = () => { copyText(finalText).then((ok) => { flashCopied(copy, ok); if (ok) toast("已复制到剪贴板", "success", 1500); }); };
      node.insertBefore(copy, bubble);
      // Quick-reply：提取末尾短列表为选项按钮
      const qr = extractQuickReplies(bubble);
      if (qr.length && !node.querySelector(".qr-bar")) {
        const bar = el("div", "qr-bar");
        qr.forEach(text => {
          const btn = el("button", "qr-btn", text);
          btn.type = "button";
          btn.onclick = () => { input.value = text; input.dispatchEvent(new Event("input")); input.focus(); };
          bar.appendChild(btn);
        });
        node.appendChild(bar);
      }
      node.appendChild(el("div", "msg-time", escapeHtml(fmtClock(nowTs()))));
    }
    state.streamEl = null;
    state.streamText = "";
    scrollBottom();
  }

  // 清理残留的流式气泡引用（回合结束/出错/被工具调用打断时）
  function clearStream() {
    if (state.streamEl) { state.streamEl.classList.remove("streaming"); }
    state.streamEl = null;
    state.streamText = "";
  }

  // 子智能体流式：把增量追加到对应 Agent 卡片 body 内的独立气泡（与顶层打字机隔离）。
  function appendSubagentDelta(pid, text) {
    const bodyEl = state.agentGroups[pid];
    if (!bodyEl) { appendDelta(text); return; }  // body 已丢失则退回顶层，避免丢字
    let s = state.subStreams[pid];
    if (!s) {
      const node = el("div", "msg assistant msg-enter");
      const bubble = el("div", "bubble streaming");
      node.appendChild(bubble);
      bodyEl.appendChild(node);
      requestAnimationFrame(() => node.classList.add("msg-in"));
      s = state.subStreams[pid] = { bubble, text: "" };
    }
    s.text += text;
    s.bubble.textContent = s.text;
    scrollBottom();
  }

  // 子智能体流式收尾：收到权威全文时清掉临时气泡，改由 appendMessageGrouped 平铺 markdown 版本。
  function finalizeSubagentDelta(pid) {
    const s = state.subStreams[pid];
    if (!s) return;
    const node = s.bubble.parentElement;
    if (node) node.remove();
    delete state.subStreams[pid];
  }

  function setRunning(running) {
    state.running = running;
    $("cancel-btn").classList.toggle("hidden", !running);
    const inp = $("input");
    // 运行中不再锁输入：继续输入会排队执行。
    inp.disabled = false;
    $("send-btn").disabled = false;
    inp.placeholder = running ? "运行中…继续输入将排队执行" : "给 Agent 下达指令…";
  }

  // ---------------- 发送 ----------------
  const input = $("input");
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 140) + "px";
    // 字符计数：仅在内容较长（>200）时显示，避免干扰
    const cc = $("char-count");
    const n = input.value.length;
    if (n > 200) { cc.textContent = n + " 字"; cc.classList.remove("hidden"); }
    else cc.classList.add("hidden");
  });

  // 危险指令模式（发送前预检，命中弹确认；拦的是指令层误操作，非 Agent 自主决策）
  const DANGER_PATTERNS = [
    { re: /\brm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r|-rf|-fr)\b/i, name: "rm -rf 强制递归删除" },
    { re: /\bmkfs\b/i, name: "mkfs 格式化" },
    { re: /\bdd\s+if=/i, name: "dd 磁盘写入" },
    { re: /:\(\)\s*\{.*\|.*&\s*\}/, name: "fork bomb" },
    { re: /\b(kill|pkill|killall)\s+-9\b/i, name: "kill -9 强杀" },
    { re: /\bdrop\s+(table|database)\b/i, name: "DROP 数据库表" },
    { re: /\bgit\s+push\s+.*(-f\b|--force)/i, name: "git 强制推送" },
    { re: /\bchmod\s+-R\b/i, name: "chmod -R 递归改权限" },
    { re: />\s*\/(?:etc|bin|usr|boot|dev|sys|root)\b/i, name: "覆盖系统路径" },
    { re: /\brm\s+-[a-z]*\s+\/(?:\s|$|\*)/i, name: "删除根目录" },
  ];
  function dangerHit(text) {
    for (const d of DANGER_PATTERNS) if (d.re.test(text)) return d.name;
    return null;
  }

  async function send() {
    let text = input.value.trim();
    const imgs = state.pendingImages || [];
    if (!text && !imgs.length) return;
    // 危险操作预检
    const danger = dangerHit(text);
    if (danger) {
      const ok = await confirmDialog(`⚠️ 这条指令疑似包含危险操作（${danger}）。\n\n确定发送给 Agent 执行？`, { okText: "确认发送", danger: true });
      if (!ok) return;  // 取消：内容保留在输入框
    }
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
      // WS 没连上：先重连并等它 open（最多 ~3s），成功就继续发，而不是直接报错。
      // 经隧道访问时 WS 易被掐断，发消息那一刻常不是 OPEN——等一下能救回大部分。
      toast("连接中，正在发送…", "info", 2000);
      connectWs();
      const ok = await waitWsOpen(3000);
      if (!ok) { renderMessage("error", { message: "连接断开，请稍后重试" }); return; }
    }
    // 有待发图片：把路径拼进消息（tclaude 用 Read 读这些图）
    if (imgs.length) {
      const lines = imgs.map((im) => `图片：${im.path}`).join("\n");
      text = text ? `${lines}\n${text}` : `${lines}\n请查看上面的图片。`;
    }
    // 运行中发送 → 服务端入队，不本地渲染气泡也不切运行态；靠 queue_update 广播刷新托盘。
    const queued = state.running;
    if (!queued) {
      renderMessage("user", { text });
    }
    state.ws.send(JSON.stringify({ type: "user_message", content: text }));
    input.value = ""; input.style.height = "auto";
    $("char-count").classList.add("hidden");
    clearPendingImages();
    if (!queued) {
      setRunning(true); showTyping();
    }
  }

  $("send-btn").onclick = send;
  // 手机：回车换行；点发送按钮才发送。桌面：Enter 发送，Shift+Enter 换行
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !("ontouchstart" in window)) { e.preventDefault(); send(); }
  });
  $("cancel-btn").onclick = () => { if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify({ type: "cancel" })); };

  // 详情面板顶部/底部操作按钮
  $("detail-peek-btn").onclick = () => { if (state.sessionId) openPeek(state.sessionId); };
  // Resume：聚焦输入框继续对话（会话本就持续，这里相当于"继续聊"入口）
  $("act-resume").onclick = async () => {
    const sid = state.sessionId;
    if (!sid) return;
    try {
      await api("/api/sessions/" + sid + "/resume", { method: "POST" });
      toast("会话已恢复，可继续聊", "success", 1500);
    } catch (e) {
      toast("恢复失败：" + e.message, "error");
    }
    input.focus();
  };
  // Stop：中断当前回合（同 cancel-btn）
  $("act-stop").onclick = () => { if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify({ type: "cancel" })); };
  // Archive：归档=从活跃列表隐藏（不物理删除），可在 Sessions 页「归档」视图恢复
  $("act-archive").onclick = async () => {
    const sid = state.sessionId;
    if (!sid) return;
    const yes = await confirmDialog("归档该会话？可在 Sessions 页「归档」视图恢复。", { okText: "归档" });
    if (!yes) return;
    try {
      await api(`/api/sessions/${sid}/archive`, { method: "POST", retry: true });
      toast("已归档", "success", 1500);
      state.sessionId = "";
      await loadSessions();
    } catch (e) { toast("归档失败：" + e.message, "error"); }
  };

  // ---------------- 按住说话（录音 → 后端 ASR）----------------
  // 约束：getUserMedia 需要安全上下文（HTTPS 或 localhost）。HTTP + 内网 IP 下浏览器禁用麦克风。
  // 录音用 MediaRecorder（优先 ogg/opus，后端 soundfile 能解；webm/mp4 解不了→后端会报错）。
  const voice = { rec: null, stream: null, chunks: [], recording: false, starting: false, pressed: false, startY: 0, cancelled: false, mime: "", timer: null, elapsed: 0, sr: null, srText: "" };
  const VOICE_MAX_SEC = 60;
  const SpeechRec = window.SpeechRecognition || window.webkitSpeechRecognition;

  // 录音同时并行跑 Web Speech 实时识别，作为 ASR 服务失败时的兜底（无需重说一遍）
  function startSpeechFallback() {
    voice.srText = "";
    if (!SpeechRec) { voice.sr = null; return; }
    try {
      const sr = new SpeechRec();
      sr.lang = "zh-CN";
      sr.continuous = true;
      sr.interimResults = true;
      sr.onresult = (ev) => {
        let final = "";
        for (let i = 0; i < ev.results.length; i++) {
          if (ev.results[i].isFinal) final += ev.results[i][0].transcript;
        }
        if (final) voice.srText = final;
      };
      sr.onerror = () => {};   // 兜底失败静默（可能内网连不通 Google 引擎）
      voice.sr = sr;
      sr.start();
    } catch (e) { voice.sr = null; }
  }
  function stopSpeechFallback() {
    if (voice.sr) { try { voice.sr.stop(); } catch (e) {} }
  }

  function voiceAvailable() {
    const secure = window.isSecureContext || location.hostname === "localhost" || location.hostname === "127.0.0.1";
    return !!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia && window.MediaRecorder) && secure;
  }

  // 选一个 soundfile 后端能解码的录音格式：ogg/opus 最佳；webm 作兜底（后端解不了会提示）
  function pickMime() {
    const prefs = ["audio/ogg;codecs=opus", "audio/ogg", "audio/webm;codecs=opus", "audio/webm"];
    if (window.MediaRecorder && MediaRecorder.isTypeSupported) {
      for (const m of prefs) if (MediaRecorder.isTypeSupported(m)) return m;
    }
    return "";  // 让浏览器用默认（iOS 多为 mp4/aac）
  }

  // ---------------- 图片输入（拍照/选图 → 上传 workdir → 注入路径）----------------
  function initImage() {
    const btn = $("img-btn");
    const inp = $("img-input");
    if (!btn || !inp) return;
    btn.onclick = () => { if (!state.sessionId) { toast("请先选择会话", "info"); return; } inp.click(); };
    inp.onchange = async () => {
      const files = Array.from(inp.files || []);
      inp.value = "";  // 允许连续选同一张
      for (const f of files) await uploadOneImage(f);
    };
  }

  async function uploadOneImage(file) {
    if (!file || !file.type.startsWith("image/")) { toast("只能发图片", "error"); return; }
    if (file.size > 10 * 1024 * 1024) { toast("图片过大（上限 10MB）", "error"); return; }
    const tip = toast("上传中…", "info", 8000);
    try {
      const dataUrl = await new Promise((res, rej) => {
        const fr = new FileReader();
        fr.onload = () => res(fr.result);
        fr.onerror = () => rej(fr.error);
        fr.readAsDataURL(file);
      });
      const r = await api("/api/upload", {
        method: "POST",
        body: JSON.stringify({ session_id: state.sessionId, image: dataUrl, mime: file.type, name: file.name }),
      });
      state.pendingImages.push({ path: r.abs || r.path, dataUrl });
      renderImageTray();
      toast("图片已就绪，可加文字一起发送", "success", 1800);
    } catch (e) {
      toast("上传失败：" + (e.message || e), "error", 3000);
    }
  }

  // 待发图片预览条：缩略图 + 删除（全量重建，保证与 state.pendingImages 一致）
  function renderImageTray() {
    const tray = $("img-tray");
    if (!tray) return;
    tray.innerHTML = "";
    if (!state.pendingImages.length) { tray.classList.add("hidden"); return; }
    state.pendingImages.forEach((im, idx) => {
      const chip = el("div", "img-chip");
      const image = document.createElement("img");
      image.src = im.dataUrl;
      const x = el("button", "img-chip-del", "×");
      x.onclick = () => { state.pendingImages.splice(idx, 1); renderImageTray(); };
      chip.append(image, x);
      tray.appendChild(chip);
    });
    tray.classList.remove("hidden");
  }

  function clearPendingImages() {
    state.pendingImages = [];
    const tray = $("img-tray");
    if (tray) { tray.innerHTML = ""; tray.classList.add("hidden"); }
  }

  function initMic() {
    const btn = $("mic-btn");
    if (!btn) return;
    if (!(navigator.mediaDevices && window.MediaRecorder)) { btn.classList.add("hidden"); return; }
    if (!voiceAvailable()) {
      btn.classList.add("disabled");
      btn.onclick = () => toast("语音输入需通过 HTTPS 访问（当前为 HTTP）", "error", 3200);
      return;
    }
    // 点击式：点麦克风开始录音；浮层上「完成发送」「取消」结束。不依赖松手事件，永不卡死。
    btn.onclick = () => { if (voice.recording || voice.starting) return; startVoice(); };
    $("voice-done").onclick = () => { voice.cancelled = false; stopVoice(); };
    $("voice-cancel").onclick = () => { voice.cancelled = true; stopVoice(); };
  }

  function setVoiceText(t, warn) {
    const el2 = $("voice-text");
    if (el2) { el2.textContent = t; el2.classList.toggle("warn", !!warn); }
  }

  async function startVoice() {
    if (voice.recording || voice.starting) return;   // 防重入（含 getUserMedia 进行中）
    voice.starting = true;
    voice.cancelled = false; voice.chunks = [];
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (err) {
      voice.starting = false;
      const denied = err && (err.name === "NotAllowedError" || err.name === "SecurityError");
      toast(denied ? "麦克风权限被拒绝，请在浏览器设置里允许" : "无法访问麦克风：" + (err && err.name || err), "error", 3200);
      return;
    }
    voice.stream = stream;
    voice.mime = pickMime();
    let rec;
    try { rec = voice.mime ? new MediaRecorder(stream, { mimeType: voice.mime }) : new MediaRecorder(stream); }
    catch (err) { try { rec = new MediaRecorder(stream); } catch (e2) { voice.starting = false; toast("浏览器不支持录音", "error"); stopTracks(); return; } }
    voice.rec = rec;
    rec.ondataavailable = (ev) => { if (ev.data && ev.data.size) voice.chunks.push(ev.data); };
    rec.onstop = () => onRecStop();
    try {
      rec.start(); voice.recording = true; voice.starting = false;
      showVoiceOverlay(); if (navigator.vibrate) navigator.vibrate(30);
      startSpeechFallback();   // 并行跑 Web Speech 实时识别，ASR 失败时兜底
      if (voice._stopWhenReady) { voice._stopWhenReady = false; stopVoice(); }  // 启动期间已点结束
    }
    catch (err) { voice.starting = false; toast("无法开始录音", "error"); stopTracks(); }
  }

  function stopVoice() {
    if (voice.starting) {
      // 录音还在启动中（getUserMedia 未完成）就点了结束：标记，启动完成后会立即停
      voice._stopWhenReady = true;
      return;
    }
    // 无论后续识别快慢，先把浮层关掉，绝不让它挂着
    hideVoiceOverlay();
    if (!voice.recording || !voice.rec) { stopTracks(); return; }
    voice.recording = false;
    stopSpeechFallback();
    if (!voice.cancelled) toast("识别中…", "info", 1500);
    try { voice.rec.stop(); } catch (e) { onRecStop(); }   // onstop 会兜底
  }

  function stopTracks() {
    if (voice.stream) { try { voice.stream.getTracks().forEach((t) => t.stop()); } catch (e) {} voice.stream = null; }
  }

  // 录音停止后台处理：转 base64 → POST ASR → 回填输入框。浮层已在 stopVoice 关闭，这里不碰浮层。
  async function onRecStop() {
    stopTracks();
    if (voice.cancelled) { return; }
    const blob = new Blob(voice.chunks, { type: voice.mime || "audio/webm" });
    if (!blob.size) { toast("没录到声音", "info", 1500); return; }
    try {
      // 用 data URL 方式读（含 mime 前缀）让后端知道格式，不走 multipart 避开代理限制
      const dataUrl = await new Promise((res, rej) => {
        const fr = new FileReader();
        fr.onload = () => res(fr.result);
        fr.onerror = () => rej(fr.error);
        fr.readAsDataURL(blob);
      });
      const res = await fetch(BASE + "/api/asr", {
        method: "POST",
        headers: { "Content-Type": "application/json", Authorization: "Bearer " + state.token },
        body: JSON.stringify({ audio: dataUrl, mime: voice.mime || "audio/webm" }),
      });
      if (!res.ok) {
        const detail = (await res.json().catch(() => ({}))).detail || "识别失败";
        throw new Error(detail);
      }
      const data = await res.json();
      const text = (data.text || "").trim();
      if (!text || text === "<empty>") {
        // ASR 没识别到内容，看 Web Speech 兜底有没有
        const fb = (voice.srText || "").trim();
        if (fb) { fillRecognized(fb); toast("已用浏览器识别", "info", 1500); }
        else toast("没识别到内容", "info", 1500);
      } else {
        fillRecognized(text);
      }
    } catch (err) {
      // ASR 服务失败 → 用 Web Speech 实时识别的结果兜底（无需重说）
      const fb = (voice.srText || "").trim();
      if (fb) {
        fillRecognized(fb);
        toast("ASR 不可用，已用浏览器识别兜底", "info", 2600);
      } else {
        toast("语音识别失败：" + (err.message || err) + (SpeechRec ? "" : "（浏览器也不支持兜底）"), "error", 3200);
      }
    }
  }

  // 把识别文本填入输入框（语音直发开则直接发）
  function fillRecognized(text) {
    if (voiceSendEnabled()) {
      input.value = (input.value ? input.value.trimEnd() + " " : "") + text;
      send();
    } else {
      input.value = (input.value ? input.value.trimEnd() + " " : "") + text;
      input.dispatchEvent(new Event("input"));
    }
  }

  function showVoiceOverlay() {
    voice.elapsed = 0;
    setVoiceText("正在录音… 0s", false);
    $("voice-overlay").classList.remove("hidden", "transcribing");
    $("mic-btn").classList.add("recording");
    voice.timer = setInterval(() => {
      voice.elapsed++;
      if (!voice.cancelled) setVoiceText("正在录音… " + voice.elapsed + "s", false);
      if (voice.elapsed >= VOICE_MAX_SEC) { stopVoice(); }  // 超时自动停
    }, 1000);
  }
  function hideVoiceOverlay() {
    if (voice.timer) { clearInterval(voice.timer); voice.timer = null; }
    const o = $("voice-overlay"); o.classList.add("hidden"); o.classList.remove("transcribing");
    $("mic-btn").classList.remove("recording");
  }

  // ---------------- 工具函数 ----------------
  function escapeHtml(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
  function escapeAttr(s) { return escapeHtml(s == null ? "" : s); }
  function nowTs() { return Date.now() / 1000; }
  function fmtTime(ts) { if (!ts) return ""; const d = new Date(ts * 1000); return d.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }); }
  // 相对时间：「刚刚」「3小时前」「昨天」等，看板卡片底部用
  function fmtRelTime(ts) {
    if (!ts) return "";
    const diff = nowTs() - ts;
    if (diff < 60) return "刚刚";
    if (diff < 3600) return Math.floor(diff / 60) + "分钟前";
    if (diff < 86400) return Math.floor(diff / 3600) + "小时前";
    if (diff < 172800) return "昨天";
    if (diff < 604800) return Math.floor(diff / 86400) + "天前";
    return fmtTime(ts);
  }
  // 气泡时间戳：今天只显时分，跨天显月日+时分
  function fmtClock(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000), now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    return d.toLocaleString("zh-CN", sameDay
      ? { hour: "2-digit", minute: "2-digit" }
      : { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  }

  // ---------------- 轻量 Markdown 渲染（零依赖、先转义后注入白名单标签，无 XSS）----------------
  // 策略：所有原始文本一律先过 escapeHtml，再把 markdown 标记替换成固定的安全 HTML。
  // 因为用户内容已转义，注入的标签只可能来自我们自己的模板，绝不会逃逸。
  function mdInline(text) {
    // text 已转义。处理行内：行内码 → 粗 → 斜 → 链接。
    // 行内码优先：先抠出来用占位符，避免里面的 * _ 被误解析
    const codes = [];
    text = text.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return ` ${codes.length - 1} `; });
    text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    text = text.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    text = text.replace(/(^|[^_\w])_([^_\n]+)_/g, "$1<em>$2</em>");
    // 链接 [文字](url)：url 转义后 & 变 &amp;，先还原再校验，只放行 http/https
    text = text.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (m, label, url) => {
      const clean = url.replace(/&amp;/g, "&");
      return /^https?:\/\//.test(clean) ? `<a href="${escapeAttr(clean)}" target="_blank" rel="noopener">${label}</a>` : m;
    });
    text = text.replace(/ (\d+) /g, (_, i) => `<code>${codes[+i]}</code>`);
    return text;
  }

  function renderMarkdown(src) {
    const lines = String(src).replace(/\r\n?/g, "\n").split("\n");
    let html = "", i = 0;
    const esc = escapeHtml;
    while (i < lines.length) {
      let line = lines[i];
      // 围栏代码块
      const fence = line.match(/^```(\w*)\s*$/);
      if (fence) {
        const lang = fence[1] || "";
        const buf = [];
        i++;
        while (i < lines.length && !/^```\s*$/.test(lines[i])) { buf.push(lines[i]); i++; }
        i++; // 跳过收尾 ```
        const code = esc(buf.join("\n"));
        html += `<div class="code-block"><div class="cb-head"><span class="cb-lang">${esc(lang) || "code"}</span>` +
          `<button class="copy-btn" data-copy type="button">复制</button></div>` +
          `<pre><code>${code}</code></pre></div>`;
        continue;
      }
      // 标题
      const h = line.match(/^(#{1,3})\s+(.*)$/);
      if (h) { const lv = h[1].length; html += `<h${lv}>${mdInline(esc(h[2]))}</h${lv}>`; i++; continue; }
      // GFM 表格：表头行 + 分隔行(|---|---|) + 若干数据行
      if (/\|/.test(line) && i + 1 < lines.length && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[i + 1]) && /-/.test(lines[i + 1])) {
        const splitRow = (r) => r.replace(/^\s*\|/, "").replace(/\|\s*$/, "").split("|").map((c) => c.trim());
        const headers = splitRow(line);
        i += 2; // 跳过表头与分隔行
        const bodyRows = [];
        while (i < lines.length && /\|/.test(lines[i]) && lines[i].trim()) { bodyRows.push(splitRow(lines[i])); i++; }
        const th = headers.map((c) => `<th>${mdInline(esc(c))}</th>`).join("");
        const trs = bodyRows.map((cells) =>
          "<tr>" + cells.map((c) => `<td>${mdInline(esc(c))}</td>`).join("") + "</tr>").join("");
        html += `<table><thead><tr>${th}</tr></thead><tbody>${trs}</tbody></table>`;
        continue;
      }
      // 分隔线
      if (/^(\s*[-*_]){3,}\s*$/.test(line) && line.trim().length >= 3) { html += "<hr />"; i++; continue; }
      // 引用（连续多行合并）
      if (/^>\s?/.test(line)) {
        const buf = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) { buf.push(lines[i].replace(/^>\s?/, "")); i++; }
        html += `<blockquote>${mdInline(esc(buf.join(" ")))}</blockquote>`;
        continue;
      }
      // 无序列表
      if (/^\s*[-*]\s+/.test(line)) {
        const buf = [];
        while (i < lines.length && /^\s*[-*]\s+/.test(lines[i])) { buf.push(lines[i].replace(/^\s*[-*]\s+/, "")); i++; }
        html += "<ul>" + buf.map((t) => `<li>${mdInline(esc(t))}</li>`).join("") + "</ul>";
        continue;
      }
      // 有序列表
      if (/^\s*\d+\.\s+/.test(line)) {
        const buf = [];
        while (i < lines.length && /^\s*\d+\.\s+/.test(lines[i])) { buf.push(lines[i].replace(/^\s*\d+\.\s+/, "")); i++; }
        html += "<ol>" + buf.map((t) => `<li>${mdInline(esc(t))}</li>`).join("") + "</ol>";
        continue;
      }
      // 空行
      if (!line.trim()) { i++; continue; }
      // 段落（连续非空、非块级起始行合并，行内用 <br>）
      const buf = [];
      const tableAhead = (j) => /\|/.test(lines[j]) && j + 1 < lines.length && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(lines[j + 1]) && /-/.test(lines[j + 1]);
      while (i < lines.length && lines[i].trim() &&
             !/^```|^#{1,3}\s|^>\s?|^\s*[-*]\s+|^\s*\d+\.\s+/.test(lines[i]) &&
             !tableAhead(i) &&
             !(/^(\s*[-*_]){3,}\s*$/.test(lines[i]) && lines[i].trim().length >= 3)) {
        buf.push(lines[i]); i++;
      }
      if (!buf.length) { continue; }  // 当前行是表格起始，交回主循环处理
      html += `<p>${buf.map((t) => mdInline(esc(t))).join("<br />")}</p>`;
    }
    return html;
  }

  // 复制文本：优先 clipboard API，失败兜底 execCommand
  async function copyText(text) {
    try {
      if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
    } catch (e) { /* fall through */ }
    try {
      const ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      const ok = document.execCommand("copy"); ta.remove(); return ok;
    } catch (e) { return false; }
  }

  // 代码块复制按钮：事件委托（markdown 里动态生成的 [data-copy]）
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-copy]");
    if (!btn) return;
    const block = btn.closest(".code-block");
    const code = block ? block.querySelector("code") : null;
    if (code) copyText(code.textContent).then((ok) => flashCopied(btn, ok));
  });

  // 切后台回前台 / 网络恢复：自动对齐（重连 + 补历史 + 刷状态）
  document.addEventListener("visibilitychange", () => {
    reportVisibility();  // 不论切前还是切后台都上报，后端据此决定回合完成是否发企业微信
    if (document.hidden) return;
    if (state.token && loginExpired()) { logout(); return; }  // 后台回来若已过期，强制重登
    resync();
  });
  window.addEventListener("online", resync);

  // 上报页面前台/后台状态给后端（WS 连着才发；hidden=true 表示锁屏/切走）
  function reportVisibility() {
    if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
    try { state.ws.send(JSON.stringify({ type: "visibility", hidden: document.hidden })); } catch (e) {}
  }

  // ---------------- Toast 通知 ----------------
  function toast(msg, type = "info", ms = 2600) {
    const root = $("toast-root");
    if (!root) return;
    const t = el("div", "toast " + type, escapeHtml(msg));
    root.appendChild(t);
    requestAnimationFrame(() => t.classList.add("show"));
    setTimeout(() => {
      t.classList.remove("show");
      setTimeout(() => t.remove(), 250);
    }, ms);
  }

  // 备忘提醒横幅：顶部常驻，不自动消失，与短暂 toast 区分。点「查看」跳到备忘录管理页。
  function showMemoBanner(data) {
    const root = $("memo-banner-root");
    if (!root) return;
    // 已有 banner 则替换（避免堆叠）
    const existing = root.querySelector(".memo-banner");
    if (existing) existing.remove();

    const banner = el("div", "memo-banner");

    const icon = el("span", "memo-banner-icon", "📌");

    const body = el("div", "memo-banner-body");
    const titleEl = el("div", "memo-banner-title");
    titleEl.textContent = data.title || "备忘提醒";
    const previewEl = el("div", "memo-banner-preview");
    previewEl.textContent = data.preview || "";
    body.append(titleEl, previewEl);

    const actions = el("div", "memo-banner-actions");

    const viewBtn = el("button", "memo-banner-btn", "查看");
    viewBtn.onclick = () => {
      banner.remove();
      clearMemoBadge();
      switchTab("experimental");
      openManage("memos");
    };

    const closeBtn = el("button", "memo-banner-btn", "✕");
    closeBtn.onclick = () => banner.remove();

    actions.append(viewBtn, closeBtn);
    banner.append(icon, body, actions);
    root.appendChild(banner);
  }

  // 备忘录入口角标 + Experimental tab 红点：count>0 显示，否则隐藏
  function setMemoBadge(count) {
    const badge = $("memo-badge");
    const dot = $("exp-tab-dot");
    if (badge) {
      if (count > 0) {
        badge.textContent = count;
        badge.style.display = "";
      } else {
        badge.style.display = "none";
      }
    }
    if (dot) dot.style.display = count > 0 ? "" : "none";
  }

  function clearMemoBadge() {
    setMemoBadge(0);
  }

  // ---------------- 自定义确认框（替代原生 confirm）----------------
  function confirmDialog(message, { okText = "确定", cancelText = "取消", danger = false } = {}) {
    return new Promise((resolve) => {
      const root = $("modal-root");
      root.innerHTML = "";
      const card = el("div", "modal-card");
      card.innerHTML = `<div class="modal-msg">${escapeHtml(message)}</div>
        <div class="modal-actions">
          <button class="modal-cancel" type="button">${escapeHtml(cancelText)}</button>
          <button class="modal-ok${danger ? " danger" : ""}" type="button">${escapeHtml(okText)}</button>
        </div>`;
      root.appendChild(card);
      root.classList.remove("hidden");
      requestAnimationFrame(() => root.classList.add("show"));
      const close = (val) => { root.classList.remove("show"); setTimeout(() => { root.classList.add("hidden"); root.innerHTML = ""; }, 200); resolve(val); };
      card.querySelector(".modal-cancel").onclick = () => close(false);
      card.querySelector(".modal-ok").onclick = () => close(true);
      root.onclick = (e) => { if (e.target === root) close(false); };
    });
  }

  // ---------------- 权限请求弹窗 ----------------
  // Claude Code 遇到未放行的工具（Bash/Write/Edit 等）会发 control_request 等授权，
  // 后端经 WS 推 permission_request，这里弹窗让用户点允许/拒绝，回传 permission_response。
  // 用独立浮层（不占 modal-root，避免与 confirm/编辑弹窗互相顶掉），z-index 高于一切。
  // 弹窗串行化队列：同一时刻只展示一个授权弹窗，其余排队，关闭一个再弹下一个。
  const _permQueue = [];
  function showPermissionDialog(req) {
    const tool = req.tool_name || "未知工具";
    const inp = req.input || {};

    // 同一 request_id 已有弹窗（如断线重发）就不重复弹
    if (document.querySelector(`.perm-overlay[data-req="${cssEscape(req.request_id)}"]`)) return;

    // 已有弹窗在展示：入队等待，避免多个弹窗堆叠
    if (document.querySelector(".perm-overlay")) {
      if (_permQueue.some(r => r.request_id === req.request_id)) return;
      _permQueue.push(req);
      return;
    }

    // AskUserQuestion 类工具特判：不走通用 allow/deny，而是渲染问题+选项，
    // 用户选择经 updated_input.answers 回填 CLI（否则模型只能自答）。
    if (/^Ask(User|Followup|Clarif)/i.test(tool)) {
      showAskQuestionDialog(req);
      return;
    }

    // 关键入参：命令类显命令，文件类显路径，其余 JSON 截断
    let detail = inp.command || inp.file_path || inp.path;
    if (!detail) {
      try { detail = JSON.stringify(inp); } catch (e) { detail = String(inp); }
    }
    detail = String(detail || "");
    if (detail.length > 600) detail = detail.slice(0, 600) + "…";

    const overlay = el("div", "perm-overlay");
    overlay.dataset.req = req.request_id;
    overlay.style.cssText = "position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;" +
      "background:rgba(0,0,0,0.45);padding:16px";
    const card = el("div", "modal-card");
    card.innerHTML = `
      <div class="modal-title">权限请求</div>
      <div class="modal-msg" style="text-align:left">
        Agent 想执行工具 <strong>${escapeHtml(tool)}</strong>：
        <pre style="white-space:pre-wrap;word-break:break-all;max-height:40vh;overflow-y:auto;margin-top:8px;font-size:0.85rem">${escapeHtml(detail)}</pre>
      </div>
      <div class="modal-actions">
        <button class="modal-cancel" type="button">拒绝</button>
        <button class="modal-ok" type="button">允许</button>
      </div>`;
    overlay.appendChild(card);
    document.body.appendChild(overlay);

    const respond = (behavior) => {
      if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        try { state.ws.send(JSON.stringify({ type: "permission_response", request_id: req.request_id, behavior })); } catch (e) {}
      }
      overlay.remove();
      if (_permQueue.length) showPermissionDialog(_permQueue.shift());
    };
    card.querySelector(".modal-ok").onclick = () => respond("allow");
    card.querySelector(".modal-cancel").onclick = () => respond("deny");
  }

  // AskUserQuestion 专用弹窗：渲染每个问题的选项按钮，用户点选后把答案
  // 经 permission_response 的 updated_input.answers 回传 CLI（{问题文本: 选项label}）。
  function showAskQuestionDialog(req) {
    const inp = req.input || {};
    const questions = Array.isArray(inp.questions) ? inp.questions : (inp.question ? [inp.question] : []);
    const optText = (o) => (o && typeof o === "object")
      ? (o.label ?? o.text ?? o.value ?? JSON.stringify(o))
      : String(o);
    // 归一化：每个问题取出文本与选项列表
    const norm = questions.map(q => {
      const qtext = typeof q === "string" ? q : (q.question || q.text || JSON.stringify(q));
      const qopts = (typeof q === "object" ? (q.options || []) : []).map(optText);
      return { qtext, qopts };
    });

    const overlay = el("div", "perm-overlay");
    overlay.dataset.req = req.request_id;
    overlay.style.cssText = "position:fixed;inset:0;z-index:9999;display:flex;align-items:center;justify-content:center;" +
      "background:rgba(0,0,0,0.45);padding:16px";
    const card = el("div", "modal-card");
    let body = `<div class="modal-title">需要你的选择</div><div class="modal-msg" style="text-align:left">`;
    norm.forEach((n, qi) => {
      body += `<div class="ask-question" style="margin-top:${qi ? 12 : 0}px">${escapeHtml(n.qtext)}</div>`;
      body += `<div class="ask-opts" style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px">`;
      body += n.qopts.map((t, oi) =>
        `<button class="ask-opt" type="button" data-qi="${qi}" data-value="${escapeAttr(t)}" style="margin:0">${escapeHtml(t)}</button>`
      ).join("");
      body += `</div>`;
    });
    body += `</div><div class="modal-actions"><button class="modal-cancel" type="button">跳过</button><button class="modal-ok modal-confirm" type="button">确认</button></div>`;
    card.innerHTML = body;
    overlay.appendChild(card);
    document.body.appendChild(overlay);

    const send = (behavior, updated_input) => {
      if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        const msg = { type: "permission_response", request_id: req.request_id, behavior };
        if (updated_input) msg.updated_input = updated_input;
        try { state.ws.send(JSON.stringify(msg)); } catch (e) {}
      }
      overlay.remove();
      if (_permQueue.length) showPermissionDialog(_permQueue.shift());
    };

    // 多问题场景：先在此累积每道题的选择，点"确认"再一次性回传全部答案。
    // 单问题场景：点选项即提交，无需再点"确认"。
    const selectedAnswers = {};
    card.querySelectorAll(".ask-opt").forEach(btn => {
      btn.onclick = () => {
        if (norm.length === 1) {
          send("allow", { questions, answers: { [norm[0].qtext]: btn.dataset.value } });
          return;
        }
        const qi = Number(btn.dataset.qi);
        const qtext = norm[qi].qtext;
        selectedAnswers[qtext] = btn.dataset.value;
        // 高亮当前选项，取消同题其他选项的高亮（单选语义）
        card.querySelectorAll(`.ask-opt[data-qi="${qi}"]`).forEach(b => b.classList.remove("selected"));
        btn.classList.add("selected");
      };
    });
    card.querySelector(".modal-confirm").onclick = () => send("allow", { questions, answers: selectedAnswers });
    card.querySelector(".modal-cancel").onclick = () => send("deny");
  }

  // CSS 选择器里的 request_id 转义（id 含特殊字符时避免选择器报错）
  function cssEscape(s) {
    return CSS.escape(String(s));
  }

  // ---------------- 骨架屏 ----------------
  function skeleton(n = 3) {
    let s = "";
    for (let k = 0; k < n; k++) s += `<div class="skel-card"><div class="skel-line w60"></div><div class="skel-line w90"></div></div>`;
    return s;
  }

  // ---------------- 浏览器完成通知（纯前端；服务器不通外网，公网推送不可行）----------------
  function notifySupported() { return "Notification" in window; }
  function notifyEnabled() { return localStorage.getItem("ac_notify") === "1"; }

  function updateNotifyBtn() {
    const btn = $("notify-toggle");
    if (!btn) return;
    if (!notifySupported()) { btn.textContent = "🔔 完成提醒：不支持"; btn.disabled = true; return; }
    btn.textContent = "🔔 完成提醒：" + (notifyEnabled() ? "开" : "关");
    btn.classList.toggle("on", notifyEnabled());
  }

  async function toggleNotify() {
    if (!notifySupported()) { toast("当前浏览器不支持通知", "error"); return; }
    if (notifyEnabled()) {
      localStorage.setItem("ac_notify", "0");
      toast("已关闭完成提醒", "info");
    } else {
      let perm = Notification.permission;
      if (perm !== "granted") { try { perm = await Notification.requestPermission(); } catch (e) { perm = "denied"; } }
      if (perm === "granted") { localStorage.setItem("ac_notify", "1"); toast("已开启完成提醒", "success"); }
      else { localStorage.setItem("ac_notify", "0"); toast("通知权限被拒绝", "error"); }
    }
    updateNotifyBtn();
  }

  // 回合结束时：已授权 + 页面在后台 才弹通知（前台不打扰）
  function maybeNotify(result) {
    if (!notifyEnabled() || !notifySupported() || Notification.permission !== "granted") return;
    if (!document.hidden) return;
    const st = result && result.status;
    const body = st === "error" ? "任务执行出错，点击查看" : st === "cancelled" ? "任务已取消" : "Agent 已完成任务，点击查看";
    try {
      const n = new Notification("Agent Console", { body, tag: "agent-done" });
      n.onclick = () => { window.focus(); n.close(); };
      if (navigator.vibrate) navigator.vibrate(200);
    } catch (e) { /* 部分环境构造通知会抛错，静默降级 */ }
  }

  // ---------------- 语音直发开关 ----------------
  function voiceSendEnabled() { return localStorage.getItem("ac_voice_autosend") === "1"; }
  function updateVoiceSendBtn() {
    const btn = $("voicesend-toggle");
    if (!btn) return;
    btn.textContent = "🎙️ 语音直发：" + (voiceSendEnabled() ? "开" : "关");
    btn.classList.toggle("on", voiceSendEnabled());
  }
  function toggleVoiceSend() {
    const next = voiceSendEnabled() ? "0" : "1";
    localStorage.setItem("ac_voice_autosend", next);
    toast(next === "1" ? "语音识别后将自动发送" : "已关闭语音直发", next === "1" ? "success" : "info");
    updateVoiceSendBtn();
  }

  // ---------------- 启动 ----------------
  // 首屏请求串行化 + 错开 WS 连接，避免一瞬间多个 HTTP + 2 条 WS 挤爆 SSH 隧道
  // （手机经隧道访问时，首屏并发峰值正是"一开始就某些数据加载失败"的主因）。
  async function enterApp() {
    $("login-view").classList.add("hidden");
    $("app-view").classList.remove("hidden");
    updateNotifyBtn();
    updateVoiceSendBtn();
    initMic();
    initImage();
    switchTab("overview", true);  // 只切 UI，数据由下面串行加载，不重复请求
    initHubResizer();
    await loadTasks();      // 先建 taskBySession 映射，再 loadSessions 才能算对徽章/看板
    await loadSessions();
    if (state.sessionId) await switchSession(state.sessionId);  // 含 loadHistory + 第一条 WS
    loadSnippets();         // 非关键，放最后
    connectMonitor();       // 监控 WS 最后连，错开与 switchSession 里那条 WS 的建连峰值
  }

  if (state.token) {
    if (loginExpired()) {
      logout();  // 登录态过期，强制重新输口令
    } else {
      fetch(BASE + "/api/sessions", { headers: { Authorization: "Bearer " + state.token } })
        .then((r) => { if (r.ok) enterApp(); else logout(); })
        .catch(() => logout());
    }
  }
})();
