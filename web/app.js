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
    kanbanView: "active",   // 智能任务看板当前视图：active / archived
    typingEl: null,
    streamEl: null,      // 当前流式气泡的 DOM 节点
    streamText: "",      // 已累积的流式文本
    wasDisconnected: false,
    monitorWs: null,     // 监控通道 WS（会话列表实时状态）
    monitorTimer: null,
    pendingImages: [],   // 待发送的附件，每项为 { path, dataUrl, kind: "image"|"file", name }
    tab: "overview",     // 当前激活的顶部 Tab
    taskBySession: {},   // session_id -> 最近一条 task（用于派生状态徽章/看板计数）
    toolIdMap: {},       // tool_use_id -> tool_name（用于 tool_result 反查工具名）
    histMsgs: [],        // 当前会话已拉取的历史消息（尾部若干页，窗口渲染用）
    histShown: 0,        // 已渲染的末尾消息条数
    historySessionId: "", // histMsgs 当前归属的会话；避免跨会话误用旧快照
    histComplete: true,  // 已拉到会话最开头（false=服务端可能还有更早的可续拉）
    heartbeatTimer: null, // WS 应用层心跳定时器
    queue: [],           // 当前会话排队待执行的指令
    drafts: {},          // sessionId -> { text: string, images: [{path, dataUrl}] }（草稿按会话隔离）
    agentGroups: {},     // Agent tool_use id -> 该子智能体卡片的 .subagent-body 元素（内部步骤归拢用）
    _histGroups: {},     // 历史渲染专用的 Agent id -> body 映射，跨批次共享以关联加载更早的卡片/结果
    subStreams: {},      // Agent id -> { bubble, text } 子智能体正在流式的气泡（各卡片独立打字机）
    subagentExpanded: false, // 全局开关：是否展开所有子智能体的思考/执行过程
    searchContentSids: null, // 会话内容搜索命中的 session_id 集合（Set），null 表示未启用/未搜索
    searchDebounce: null,    // 会话内容搜索的防抖定时器
    pendingHighlight: null,  // 从搜索结果切入会话后，待在消息内高亮/跳转的查询词，用后即清
    dispatchExpanded: new Set(), // 会话列表（Overview/Sessions Tab 共用）里已展开的调度批次 plan_id（仅内存，不持久化）
    logoutPromise: null,
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
      const externalSignal = opts.signal;
      const abortFromExternal = () => ctl.abort();
      if (externalSignal) {
        if (externalSignal.aborted) ctl.abort();
        else externalSignal.addEventListener("abort", abortFromExternal, { once: true });
      }
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
        if (res.status === 401) { await logout(); throw new Error("未授权"); }
        // 5xx 视为可重试（服务端瞬时问题）；4xx 是业务错误，直接抛不重试
        if (res.status >= 500 && attempt < maxTries) { lastErr = new Error("服务端错误 " + res.status); await _retryWait(attempt); continue; }
        if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || "请求失败");
        return res.status === 204 ? null : res.json();
      } catch (e) {
        // 网络层失败（Failed to fetch / abort 超时）→ 幂等请求重试
        const retriable = idempotent && !externalSignal?.aborted
          && (e.name === "AbortError" || e.name === "TypeError" || /服务端错误/.test(e.message));
        if (retriable && attempt < maxTries) { lastErr = e; await _retryWait(attempt); continue; }
        throw e;
      } finally {
        clearTimeout(timer);
        if (externalSignal) externalSignal.removeEventListener("abort", abortFromExternal);
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

  async function logout() {
    if (state.logoutPromise) return state.logoutPromise;
    resetSwanlabFrame();
    state.logoutPromise = (async () => {
      await clearSwanlabSessionCookie(1000);
      localStorage.removeItem("ac_token");
      state.token = "";
      closeWs();
      closeMonitor();
      $("app-view").classList.add("hidden");
      $("manage-view").classList.add("hidden");
      $("arb-view").classList.add("hidden");
      $("dispatch-view").classList.add("hidden");
      $("swanlab-view").classList.add("hidden");
      $("login-view").classList.remove("hidden");
    })();
    try {
      await state.logoutPromise;
    } finally {
      state.logoutPromise = null;
    }
  }
  $("logout-btn").onclick = () => { logout(); };

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

  // 会话搜索：标题/进展即时过滤 + 会话内容异步搜索（防抖）
  const sessionSearchEl = $("session-search");
  if (sessionSearchEl) {
    sessionSearchEl.addEventListener("input", () => {
      const q = (sessionSearchEl.value || "").trim().toLowerCase();
      // 标题/进展即时过滤
      applySessionSearch();
      // 内容搜索防抖 300ms，至少 2 字
      clearTimeout(state.searchDebounce);
      state.searchDebounce = setTimeout(async () => {
        const contentCheck = $("search-in-content");
        if (!contentCheck || !contentCheck.checked || q.length < 2) {
          state.searchContentSids = null;
          applySessionSearch();
          return;
        }
        try {
          const r = await api("/api/sessions/search?q=" + encodeURIComponent(q) + "&scope=content");
          state.searchContentSids = new Set(r.session_ids || []);
        } catch (e) {
          state.searchContentSids = null;
        }
        applySessionSearch();
      }, 300);
    });
  }

  const searchClearBtn = $("session-search-clear");
  if (searchClearBtn) {
    searchClearBtn.onclick = () => {
      if ($("session-search")) $("session-search").value = "";
      state.searchContentSids = null;
      applySessionSearch();
      if ($("session-search")) $("session-search").focus();
    };
  }

  const contentToggle = $("search-in-content");
  if (contentToggle) {
    contentToggle.onchange = () => {
      if ($("session-search")) $("session-search").dispatchEvent(new Event("input"));
    };
  }

  // Sessions Tab 视图切换：活跃 / 归档
  document.querySelectorAll(".sv-btn").forEach((b) => {
    b.onclick = () => {
      state.sessionView = b.dataset.view;
      document.querySelectorAll(".sv-btn").forEach((x) => x.classList.toggle("active", x === b));
      if (state.sessionView === "archived") loadArchivedSessions();
      else { fillListGrouped($("session-list-all"), state.sessions, false); applySessionSearch(); }
    };
  });

  // ---------------- 会话 ----------------
  async function loadSessions() {
    state.sessions = await api("/api/sessions");
    if (!state.sessions.length) { await createSession(); return; }
    if (!state.sessions.find((s) => s.id === state.sessionId)) state.sessionId = state.sessions[0].id;
    const cur = state.sessions.find((s) => s.id === state.sessionId);
    // 当前会话先标记已读再渲染：未读圆点与看板「待查看」计数都不把当前会话算进去
    if (cur) markSeen(cur.id, cur.updated_at);
    renderSessionLists();
    renderDashboard();
    renderKanban();
    $("session-title").textContent = cur ? cur.title : "会话";
    updateDetailSummary(cur);
    if (cur) updateLinkedTodoBar(cur);
    syncModeSelect();
    syncEffortSelect();
  }

  // 渲染三个列表：Overview(全部 + 进行中小节) / Sessions(全部，可搜) / Review(已有成功回合)
  function renderSessionLists() {
    renderOverviewList();
    // Sessions Tab 在「归档」视图下不用活跃列表覆盖，交给 renderArchivedSessionList
    if (state.sessionView === "archived") renderArchivedSessionList();
    else fillListGrouped($("session-list-all"), state.sessions, false);
    fillList($("session-list-review"), state.sessions.filter((s) => deriveState(s).key === "done"));
    // 重新应用 Sessions Tab 的搜索过滤
    applySessionSearch();
  }

  // Overview 列表 + Agents 副标题。renderSessionLists 与 patchSessionRow 共用：
  // 状态翻转（空闲→运行中、运行中→已完成）时成员要实时增减，不能等下一次全量拉取。
  // 口径 = 全量会话 + 默认序（与 Sessions Tab 同源同序）。早先这里按「需要关注」过滤再重排，
  // 用户反馈顺序被打乱、点开一条就从列表消失（2026-09-22 回退）；运行中的会话改成顶部
  // 「进行中」小节做快捷入口——它是入口不是过滤器，同一会话在主列表里照常出现。
  function renderOverviewList() {
    const running = state.sessions.filter((s) => deriveState(s).key === "running");
    const sec = $("running-section");
    if (sec) {
      sec.classList.toggle("hidden", running.length === 0);
      // 至多几条，不值得折叠批次分组，直接用平铺 fillList
      fillList($("session-list-running"), running);
    }
    fillListGrouped($("session-list"), state.sessions, false);
    const sub = $("agents-sub");
    if (sub) sub.textContent = `共 ${state.sessions.length} 个会话`;
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

  // Overview 与 Sessions Tab 主列表共用：把同一调度批次（dispatch_plan_id）的子会话折叠成一组，
  // 其余普通会话原样渲染。sessions 已按 updated_at DESC 排序，排序即置顶优先 + 组内锚点时间倒序。
  function fillListGrouped(ul, sessions, isArchived = false) {
    if (!ul) return;
    ul.innerHTML = "";
    if (!sessions.length) {
      ul.innerHTML = `<div class="entity-empty"><div class="empty-emoji">📭</div><div>这里还没有会话</div></div>`;
      return;
    }
    // 混合排序项：普通会话取自身 updated_at，分组取组内最大 updated_at 作锚点。
    const items = [];        // [{ ts, kind: "session"|"group", ... }]
    const groupMap = {};     // plan_id -> item
    for (const s of sessions) {
      if (s.dispatch_plan_id) {
        let g = groupMap[s.dispatch_plan_id];
        if (!g) {
          g = { ts: s.updated_at, kind: "group", planId: s.dispatch_plan_id,
                title: s.dispatch_plan_title || "未命名批次", pinned: false, children: [] };
          groupMap[s.dispatch_plan_id] = g;
          items.push(g);
        }
        g.pinned = g.pinned || !!s.pinned;
        g.ts = Math.max(g.ts || 0, s.updated_at || 0);
        g.children.push(s);
      } else {
        items.push({ ts: s.updated_at, kind: "session", pinned: !!s.pinned, session: s });
      }
    }
    items.sort((a, b) => (b.pinned ? 1 : 0) - (a.pinned ? 1 : 0)
      || (b.ts || 0) - (a.ts || 0));
    for (const it of items) {
      if (it.kind === "group") {
        ul.appendChild(renderDispatchGroup(it.planId, it.title, it.children, isArchived));
      } else {
        ul.appendChild(renderSessionRow(it.session, isArchived));
      }
    }
  }

  // 渲染一个调度批次分组：头部（可点击折叠）+ 子会话 body。
  // 头部 <li> 不带 data-sid，避免被 applySessionSearch / patchSessionRow 当成普通会话行。
  function renderDispatchGroup(planId, title, children, isArchived = false) {
    const li = document.createElement("li");
    li.className = "dispatch-group";
    li.dataset.planId = planId;

    // 状态摘要：统计组内进行中数量
    const running = children.filter((c) => deriveState(c).key === "running").length;
    const expanded = state.dispatchExpanded.has(planId);

    const head = el("div", "dispatch-group-head");
    const caret = el("span", "dispatch-caret", expanded ? "▴" : "▾");
    const label = el("span", "dispatch-group-label",
      `📦 批次：${escapeHtml(title)}（${children.length}个子任务）`);
    const summary = el("span", "dispatch-group-summary", `进行中 ${running} / 共 ${children.length}`);
    // 「详情」入口：跳到智能分派详情页看该批次的拆解/验收进展，stopPropagation 避免误触发组头折叠
    const detail = el("span", "dispatch-group-detail", "详情");
    detail.onclick = (e) => { e.stopPropagation(); openDispatchView(); openDispatchDetail(planId); };
    head.append(caret, label, summary, detail);

    const body = el("div", "dispatch-group-body");
    body.style.display = expanded ? "" : "none";
    for (const c of children) body.appendChild(renderSessionRow(c, isArchived));

    head.onclick = () => {
      const nowExpanded = !state.dispatchExpanded.has(planId);
      if (nowExpanded) state.dispatchExpanded.add(planId);
      else state.dispatchExpanded.delete(planId);
      body.style.display = nowExpanded ? "" : "none";
      caret.textContent = nowExpanded ? "▴" : "▾";
    };

    li.append(head, body);
    return li;
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
    applySessionSearch();
  }

  // 在文本中高亮首个匹配的查询串（返回转义后的 HTML）
  function markText(text, q) {
    if (!q || !text) return escapeHtml(text || "");
    const lower = text.toLowerCase();
    const qi = lower.indexOf(q);
    if (qi < 0) return escapeHtml(text);
    return escapeHtml(text.slice(0, qi)) +
           "<mark>" + escapeHtml(text.slice(qi, qi + q.length)) + "</mark>" +
           escapeHtml(text.slice(qi + q.length));
  }

  // 取一条消息用于内容搜索的可比对文本（仅 user/assistant 的正文参与命中）
  function msgSearchText(m) {
    if (m.role === "user" || m.role === "assistant") return (m.content && m.content.text) || "";
    return "";
  }

  // 在消息数组里找到首个命中 q 的下标，未命中返回 -1
  function firstMatchIdx(msgs, q) {
    for (let i = 0; i < msgs.length; i++) {
      const t = msgSearchText(msgs[i]).toLowerCase();
      if (t && t.includes(q)) return i;
    }
    return -1;
  }

  // 清除聊天区里之前注入的高亮 <mark>，把文本还原（避免重复渲染叠加）
  function clearChatHighlights() {
    const chat = $("chat");
    if (!chat) return;
    chat.querySelectorAll("mark.chat-hit").forEach((m) => {
      const t = document.createTextNode(m.textContent);
      m.parentNode.replaceChild(t, m);
    });
    chat.normalize();
  }

  // 在单个元素的文本节点里包裹命中片段，返回其中第一个 <mark>（供滚动定位）
  function highlightTextInEl(root, q) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, null);
    const targets = [];
    let n;
    while ((n = walker.nextNode())) {
      if (n.parentElement && n.parentElement.closest("mark.chat-hit")) continue;
      if (n.nodeValue.toLowerCase().includes(q)) targets.push(n);
    }
    let firstMark = null;
    for (const textNode of targets) {
      const text = textNode.nodeValue;
      const lower = text.toLowerCase();
      const frag = document.createDocumentFragment();
      let idx = 0, hit;
      while ((hit = lower.indexOf(q, idx)) >= 0) {
        if (hit > idx) frag.appendChild(document.createTextNode(text.slice(idx, hit)));
        const mark = document.createElement("mark");
        mark.className = "chat-hit";
        mark.textContent = text.slice(hit, hit + q.length);
        frag.appendChild(mark);
        if (!firstMark) firstMark = mark;
        idx = hit + q.length;
      }
      if (idx < text.length) frag.appendChild(document.createTextNode(text.slice(idx)));
      textNode.parentNode.replaceChild(frag, textNode);
    }
    return firstMark;
  }

  // 高亮聊天区里所有 user/assistant 气泡内的命中，返回首个 <mark>
  function highlightChat(q) {
    clearChatHighlights();
    const chat = $("chat");
    if (!chat) return null;
    const bubbles = chat.querySelectorAll(".msg.user .bubble, .msg.assistant .bubble");
    let first = null;
    bubbles.forEach((b) => {
      const m = highlightTextInEl(b, q);
      if (!first && m) first = m;
    });
    return first;
  }

  // 高亮某个会话行的标题与摘要（首次调用会缓存原文到 dataset.raw）
  function highlightRow(li, q) {
    const t = li.querySelector(".s-title");
    const sub = li.querySelector(".s-sub");
    [t, sub].forEach((node) => {
      if (!node) return;
      if (node.dataset.raw == null) node.dataset.raw = node.textContent;
      node.innerHTML = q ? markText(node.dataset.raw, q) : escapeHtml(node.dataset.raw);
    });
  }

  // 无匹配时在列表末尾显示/移除空状态提示
  function toggleNoResult(ul, show) {
    let tip = ul.querySelector(".search-empty");
    if (show && !tip) {
      tip = document.createElement("div");
      tip.className = "search-empty entity-empty";
      tip.innerHTML = '<div class="empty-emoji">🔍</div><div>没有匹配的会话</div>';
      ul.appendChild(tip);
    } else if (!show && tip) {
      tip.remove();
    }
  }

  // 判断某会话是否命中查询：标题 / 进展(summary+activity) / 关联待办 / 会话内容
  function sessionMatchesQuery(s, q) {
    if (!q) return true;
    const inTitle = (s.title || "").toLowerCase().includes(q);
    const prog = ((s.summary || "") + " " + (s.activity || "")).toLowerCase();
    const inProg = prog.includes(q);
    const todoInfo = linkedTodoText(s);
    const inTodo = todoInfo ? todoInfo.full.toLowerCase().includes(q) : false;
    const inContent = state.searchContentSids ? state.searchContentSids.has(s.id) : false;
    return inTitle || inProg || inTodo || inContent;
  }

  // 应用 Sessions Tab 的搜索过滤 + 高亮 + 空状态
  function applySessionSearch() {
    const q = (($("session-search") && $("session-search").value) || "").trim().toLowerCase();
    const clearBtn = $("session-search-clear");
    if (clearBtn) clearBtn.classList.toggle("hidden", !q);
    const src = state.sessionView === "archived" ? (state.archivedSessions || []) : (state.sessions || []);
    const ul = $("session-list-all");
    if (!ul) return;
    let visible = 0;
    // 先处理所有普通会话行（含分组 body 内的子会话），按 data-sid 匹配
    ul.querySelectorAll("li[data-sid]").forEach((li) => {
      const s = src.find((x) => x.id === li.dataset.sid);
      const hit = s ? sessionMatchesQuery(s, q) : !q;
      li.style.display = hit ? "" : "none";
      if (hit) { visible++; highlightRow(li, q); }
    });
    // 再处理调度批次分组头：按组内可见子会话数量决定显隐/临时展开
    ul.querySelectorAll("li.dispatch-group").forEach((group) => {
      const planId = group.dataset.planId;
      const body = group.querySelector(".dispatch-group-body");
      const caret = group.querySelector(".dispatch-caret");
      const rows = body ? body.querySelectorAll("li[data-sid]") : [];
      if (q) {
        let visibleChildren = 0;
        rows.forEach((r) => { if (r.style.display !== "none") visibleChildren++; });
        if (visibleChildren > 0) {
          group.style.display = "";
          // 临时强制展开，不改 state.dispatchExpanded 的记忆值
          if (body) body.style.display = "";
          if (caret) caret.textContent = "▴";
        } else {
          group.style.display = "none";
        }
      } else {
        // 搜索清空：恢复分组头显示，展开状态按记忆值恢复
        group.style.display = "";
        const expanded = state.dispatchExpanded.has(planId);
        if (body) body.style.display = expanded ? "" : "none";
        if (caret) caret.textContent = expanded ? "▴" : "▾";
      }
    });
    toggleNoResult(ul, !!q && visible === 0);
  }

  // 单个 Agent 行：状态徽章 + 标题 + 行摘要 + meta（时间 / workdir / 档位）
  function renderSessionRow(s, isArchived = false) {
    const st = deriveState(s);
    const li = document.createElement("li");
    li.dataset.sid = s.id;
    li.className = "st-" + st.key;
    if (s.id === state.sessionId) li.classList.add("active");
    if (s.pinned) li.classList.add("pinned");

    const main = el("div", "s-main");
    const row1 = el("div", "s-row1");
    const title = el("span", "s-title", escapeHtml(s.title));
    // done 是常态：每条跑完的会话都挂个绿"已完成"只是噪声，silent 状态不画徽章
    // （失败/被中断、运行中、未开始/已取消仍照常显示）
    if (!st.silent) row1.append(el("span", "badge " + st.badgeCls, st.label));
    row1.append(title);
    const seen = loadSeenMap()[s.id] || 0;
    if (s.updated_at > seen && s.id !== state.sessionId) {
      row1.prepend(el("span", "s-dot unread"));
    }
    const sub = el("div", "s-sub", escapeHtml(sessionSubtitle(s)));
    const meta = el("div", "s-meta");
    meta.appendChild(el("span", null, fmtTime(s.updated_at) || ""));
    // 轮次（user_turns）与上回合耗时：回答"这条会话做了多少事"，信息量高于 workdir
    if (s.user_turns > 0) { meta.appendChild(el("span", "dot-sep", "·")); meta.appendChild(el("span", null, `${s.user_turns} 轮`)); }
    const durTxt = fmtTurnDur(s.last_duration_ms);
    if (durTxt) { meta.appendChild(el("span", "dot-sep", "·")); meta.appendChild(el("span", null, durTxt)); }
    if (s.workdir) { meta.appendChild(el("span", "dot-sep", "·")); meta.appendChild(el("span", null, shortDir(s.workdir))); }
    if (s.mode) { meta.appendChild(el("span", "dot-sep", "·")); meta.appendChild(el("span", null, modeLabel(s.mode))); }
    if (s.engine === "codex") { meta.appendChild(el("span", "dot-sep", "·")); meta.appendChild(el("span", "engine-badge", "Codex")); }
    const todoInfo = linkedTodoText(s);  // s.linked_todo_titles 的展示口径集中在辅助函数中
    let todoEl = null;
    if (todoInfo) {
      todoEl = el("div", "s-todo", "📋 " + escapeHtml(todoInfo.brief));
      todoEl.title = todoInfo.full;
    }
    if (todoEl) main.append(row1, sub, todoEl, meta);
    else main.append(row1, sub, meta);
    main.onclick = () => {
      const q = (($("session-search") && $("session-search").value) || "").trim().toLowerCase();
      state.pendingHighlight = (q.length >= 2 && sessionMatchesQuery(s, q)) ? q : null;
      switchSession(s.id);
      openDetail();
    };

    const actions = el("div", "s-actions");
    if (isArchived) {
      // 归档视图：恢复 + 彻底删除（不提供速览，避免误切到已归档会话）
      const restore = el("button", "s-peek", "↩"); restore.title = "恢复到活跃列表";
      restore.onclick = (e) => { e.stopPropagation(); unarchiveSession(s.id); };
      const del = el("button", "s-del", "×");
      if (s.linked_todo_count > 0) { del.classList.add("s-del-locked"); del.title = "已被看板任务关联，无法删除"; }
      else { del.title = "彻底删除"; }
      del.onclick = async (e) => { e.stopPropagation(); await deleteSession(s.id); };
      actions.append(restore, del);
    } else {
      const pin = el("button", "s-pin" + (s.pinned ? " pinned" : ""), s.pinned ? "📌" : "📍");
      pin.title = s.pinned ? "取消置顶" : "置顶到列表顶部";
      pin.onclick = async (e) => { e.stopPropagation(); await togglePinSession(s.id, !!s.pinned); };
      const peek = el("button", "s-peek", "👁"); peek.title = "速览 / 不切会话回复";
      peek.onclick = (e) => { e.stopPropagation(); openPeek(s.id); };
      const del = el("button", "s-del", "×");
      if (s.linked_todo_count > 0) { del.classList.add("s-del-locked"); del.title = "已被看板任务关联，无法删除"; }
      del.onclick = async (e) => { e.stopPropagation(); await deleteSession(s.id); };
      actions.append(pin, peek, del);
    }

    li.append(main, actions);
    return li;
  }

  // 路径缩写：只留末两段
  function shortDir(workdir) {
    const parts = String(workdir).replace(/\/$/, "").split("/");
    return parts.length > 2 ? "…/" + parts.slice(-2).join("/") : workdir;
  }
  // 上回合耗时的 meta 短文案：不足 2 分钟按秒直显，往上换算成分钟，避免 3600s 这类读不动的长数字
  function fmtTurnDur(ms) {
    const sec = Math.round((ms || 0) / 1000);
    if (sec <= 0) return "";
    if (sec < 120) return sec + "s";
    return Math.max(1, Math.round(sec / 60)) + "m";
  }
  const CODEX_MODELS = ["gpt-5.6-sol","gpt-5.6-terra","gpt-5.6-luna","gpt-5.5","gpt-5.4","gpt-5.3-codex","gpt-5.1-codex","gpt-5.1-codex-mini","glm-5.2-ioa","hy3-ioa","gpt-6-astra","deepseek-v4-pro-ioa","deepseek-v4-flash-ioa","deepseek-v4.1-flash","hy4-preview-ioa"];
  const CLAUDE_MODELS = [
    "claude-glm-5.3[1m]",
    "claude-sonnet-5","claude-sonnet-5[1m]",
    "claude-sonnet-4-6","claude-sonnet-4-6[1m]",
    "claude-opus-5","claude-opus-5[1m]",
    "claude-opus-4-8","claude-opus-4-8[1m]",
    "claude-opus-4-7","claude-opus-4-7[1m]",
    "claude-opus-4-6","claude-opus-4-6[1m]",
    "claude-haiku-4-5","claude-hy3","opusplan",
    "claude-glm-5.2","claude-glm-5.2[1m]",
    "claude-glm-5.3",
    "claude-glm-5.3-flash[1m]",
    "claude-kimi-k3[1m]",
    "claude-deepseek-v4.1-flash[1m]",
    "claude-hy4-preview[1m]",
    "claude-deepseek-v4-pro","claude-deepseek-v4-pro[1m]",
    "claude-deepseek-v4-flash","claude-deepseek-v4-flash[1m]",
  ];
  const EFFORTS = ["low", "medium", "high", "xhigh", "max"];
  // 倍率数据来自 2026-08-03 用户截图，采集档位 High。
  // claude-opus-5[1m]、claude-sonnet-4-6 为截图未覆盖的同价推断值。
  // 以下模型截图未覆盖，故倍率表缺席（保留模型定义，查不到时不显示徽章）：
  // claude-haiku-4-5、claude-hy3、opusplan、claude-glm-5.2、claude-glm-5.2[1m]、
  // gpt-5.1-codex、gpt-5.1-codex-mini、glm-5.2-ioa、hy3-ioa。
  // Gemini-3.5-Flash 在 CLAUDE_MODELS/CODEX_MODELS 中均无对应模型 ID，本次不接入。
  const RATE_TABLE = {
    "claude-opus-5":                    3.33,
    "claude-opus-5[1m]":                3.33,
    "claude-sonnet-5":                  1.33,
    "claude-sonnet-5[1m]":              1.33,
    "claude-sonnet-4-6[1m]":            2.00,
    "claude-sonnet-4-6":                2.00,
    "claude-opus-4-8[1m]":              3.33,
    "claude-opus-4-8":                  3.33,
    "claude-opus-4-7[1m]":              3.33,
    "claude-opus-4-7":                  3.33,
    "claude-opus-4-6[1m]":              3.33,
    "claude-opus-4-6":                  3.33,
    "claude-deepseek-v4-flash":         0.05,
    "claude-deepseek-v4-flash[1m]":     0.05,
    "claude-deepseek-v4-pro":           0.13,
    "claude-deepseek-v4-pro[1m]":       0.13,
    "gpt-5.6-sol":                      3.47,
    "gpt-5.6-terra":                    1.39,
    "gpt-5.6-luna":                     0.14,
    "gpt-5.5":                          3.31,
    "gpt-5.4":                          1.65,
    "gpt-5.3-codex":                    1.25,
  };
  const RATE_BASE = 1.33;
  // 目标循环成本上限默认值：来自后端 config.GOAL_MAX_COST_USD（enterApp 拉 /api/config 覆盖）。
  // 初值 20 仅兜底：接口 404/失败时不阻塞，UI 文案退回 20。
  let GOAL_COST_DEFAULT = 20;
  function modeRateStr(m) {
    const rate = RATE_TABLE[m];
    if (!rate) return null;
    return `${rate}x`;
  }
  function modeLabel(m) {
    const labels = {
      "claude-glm-5.2": "GLM 5.2",
      "claude-glm-5.2[1m]": "GLM 5.2 (1M)",
      "claude-glm-5.3": "GLM 5.3",
      "claude-glm-5.3[1m]": "GLM 5.3 (1M)",
      "claude-glm-5.3-flash[1m]": "GLM 5.3 Flash (1M)",
      "claude-kimi-k3[1m]": "Kimi K3 (1M)",
      "claude-deepseek-v4.1-flash[1m]": "DeepSeek V4.1 Flash 长文",
      "claude-hy4-preview[1m]": "HY4 Preview (1M)",
      "claude-sonnet-4-6": "Sonnet 4.6",
      "claude-sonnet-4-6[1m]": "Sonnet 4.6 (1M)",
      "claude-opus-5": "Opus 5",
      "claude-opus-5[1m]": "Opus 5 (1M)",
      "claude-opus-4-8": "Opus 4.8",
      "claude-opus-4-8[1m]": "Opus 4.8 (1M)",
      "claude-opus-4-7": "Opus 4.7",
      "claude-opus-4-7[1m]": "Opus 4.7 (1M)",
      "claude-opus-4-6": "Opus 4.6",
      "claude-opus-4-6[1m]": "Opus 4.6 (1M)",
      "claude-haiku-4-5": "Haiku 4.5",
      "claude-hy3": "HY3",
      "opusplan": "Opus Plan",
      "claude-sonnet-5": "Sonnet 5",
      "claude-sonnet-5[1m]": "Sonnet 5 长文",
      "claude-deepseek-v4-pro": "DeepSeek V4 Pro",
      "claude-deepseek-v4-pro[1m]": "DeepSeek V4 Pro 长文",
      "claude-deepseek-v4-flash": "DeepSeek V4 Flash",
      "claude-deepseek-v4-flash[1m]": "DeepSeek V4 Flash 长文",
      // codex 模型
      "gpt-5.6-sol": "GPT-5.6 Sol",
      "gpt-5.6-terra": "GPT-5.6 Terra",
      "gpt-5.6-luna": "GPT-5.6 Luna",
      "gpt-5.5": "GPT-5.5",
      "gpt-5.4": "GPT-5.4",
      "gpt-5.3-codex": "GPT-5.3 Codex",
      "gpt-5.1-codex": "GPT-5.1 Codex",
      "gpt-5.1-codex-mini": "GPT-5.1 Mini",
      "glm-5.2-ioa": "GLM 5.2 (IOA)",
      "hy3-ioa": "HY3",
      "gpt-6-astra": "GPT-6 Astra",
      "deepseek-v4-pro-ioa": "DeepSeek V4 Pro (IOA)",
      "deepseek-v4-flash-ioa": "DeepSeek V4 Flash (IOA)",
      "deepseek-v4.1-flash": "DeepSeek V4.1 Flash",
      "hy4-preview-ioa": "HY4 Preview",
      // 兼容旧档位标签
      "fast": "极速", "strong": "均衡", "super": "最强"
    };
    return labels[m] || m;
  }
  function effortLabel(e) {
    const labels = { low: "低", medium: "中", high: "高", xhigh: "极高", max: "最大" };
    return labels[e] || e;
  }

  // 从 status + 回合结局派生状态徽章。优先级：运行中 > last_outcome（本回合结局，
  // 服务端回合结束落库）> 最近一条 task（老会话在 last_outcome 落地前没有该字段，
  // 用 /api/sessions 批量补的 last_task_status 兜底）。key: running/failed/done/idle。
  function deriveState(s) {
    if (s.status === "running") return { key: "running", label: "运行中", badgeCls: "working" };
    const oc = s.last_outcome || (s.last_task_status === "error" ? "error"
              : s.last_task_status === "success" ? "success" : "");
    if (oc === "error")       return { key: "failed",  label: "失败",   badgeCls: "failed" };
    if (oc === "interrupted") return { key: "failed",  label: "被中断", badgeCls: "failed" };
    if (oc === "success")     return { key: "done",    label: "已完成", badgeCls: "done", silent: true };
    // 用户主动停止：不算失败也不需要查看，灰色静态呈现，但别误标成"未开始"
    if (oc === "cancelled")   return { key: "idle",   label: "已取消", badgeCls: "resume" };
    return { key: "idle", label: "未开始", badgeCls: "resume" };
  }

  // 工作看板：4 个计数卡（进行中 / 待查看 / 失败 / 空闲）。
  // 「待查看」= 已完成且更新时间晚于本地已读记录（loadSeenMap），即"哪些跑完了我还没看"。
  function renderDashboard() {
    const box = $("dashboard");
    if (!box) return;
    const seenMap = loadSeenMap();
    let active = 0, unseen = 0, failed = 0;
    for (const s of state.sessions) {
      const k = deriveState(s).key;
      if (k === "running") active++;
      else if (k === "failed") failed++;
      else if (k === "done" && s.updated_at > (seenMap[s.id] || 0)) unseen++;
    }
    const idle = state.sessions.length - active - failed - unseen;
    const cards = [
      { valCls: active > 0 ? " mini-stat-val--active" : "", num: active, label: "进行中" },
      { valCls: "", num: unseen, label: "待查看" },
      { valCls: failed > 0 ? " mini-stat-val--failed" : "", num: failed, label: "失败" },
      { valCls: "", num: idle, label: "空闲" },
    ];
    box.innerHTML = cards.map((c) =>
      `<span class="mini-stat"><span class="mini-stat-label">${c.label}</span> ` +
      `<span class="mini-stat-val${c.valCls}">${c.num}</span></span>`
    ).join("");
    const title = $("dash-title");
    if (title) title.textContent = (unseen + failed) ? `${unseen + failed} 项待处理` : "暂无待办";
  }

  // ---------------- 智能任务看板 ----------------
  // 单列紧凑列表：按 status 排序（进行中 → 待开始 → 已完成/已取消），每行左侧色点区分状态，
  // hover 显示编辑/删除操作，点击行主体跳转关联会话。
  async function renderKanban() {
    const list = $("kanban-list");
    if (!list) return { ok: false, items: null };
    const archived = state.kanbanView === "archived";
    const refreshBtn = $("kanban-refresh-btn");
    const addBtn = $("kanban-add-btn");
    if (refreshBtn) {
      refreshBtn.disabled = archived;
      refreshBtn.title = archived ? "归档视图不可刷新进展" : "";
    }
    if (addBtn) {
      addBtn.disabled = archived;
      addBtn.title = archived ? "请返回活跃任务后新建" : "新建任务";
    }
    const cleanBtn = $("kanban-clean-btn");
    if (cleanBtn) {
      cleanBtn.textContent = archived ? "🧹 清空归档" : "🧹 清理";
      cleanBtn.title = archived ? "彻底删除全部归档任务" : "归档已完成/已取消的任务";
    }
    let todos;
    try { todos = await api(archived ? "/api/todos?archived=1" : "/api/todos"); }
    catch (e) { return { ok: false, items: null }; }

    // 排序权重：in_progress 在前，pending 其次，done/cancelled 最后
    const order = { in_progress: 0, pending: 1, done: 2, cancelled: 3 };
    const sorted = todos.slice()
      .filter(t => t.status !== "triage")  // 待分诊项走独立收件箱，不混入主看板
      .sort((a, b) => (order[a.status] ?? 1) - (order[b.status] ?? 1));

    list.innerHTML = "";
    if (!sorted.length) {
      list.innerHTML = `<div class="kanban-empty">${archived ? "暂无归档任务" : "暂无任务"}</div>`;
    } else {
      const visibleItems = archived ? sorted : sorted.slice(0, 3);
      for (const t of visibleItems) list.appendChild(renderKanbanRow(t, archived));
      if (!archived && sorted.length > 3) {
        const showAll = el("button", "kanban-show-all");
        showAll.type = "button";
        showAll.textContent = `查看全部 ${sorted.length} 项`;
        showAll.onclick = () => showKanbanSheet(sorted, showAll);
        list.appendChild(showAll);
      }
    }
    if (!archived) {
      await renderStallInbox();
      const triageResult = await renderTriageInbox();
      return { ...triageResult, kanbanItems: sorted };
    }
    const stallBox = $("stall-inbox");
    if (stallBox) {
      stallBox.classList.add("hidden");
      stallBox.innerHTML = "";
    }
    const triageBox = $("triage-inbox");
    if (triageBox) {
      triageBox.classList.add("hidden");
      triageBox.innerHTML = "";
    }
    return { ok: true, items: [], kanbanItems: sorted };
  }

  // 停滞事项收件箱（Stall Watch，H3 增强）：目标循环耗尽/暂停/卡死、待办无进展、
  // 分派失败/卡死等"停在半路"的告警，等人工 继续/稍后/跳过
  async function renderStallInbox() {
    const box = $("stall-inbox");
    if (!box) return { ok: false, items: null };
    let items;
    try { items = await api("/api/stalls"); }
    catch (e) { return { ok: false, items: null }; }
    if (!items || !items.length) {
      box.classList.add("hidden");
      box.innerHTML = "";
      return { ok: true, items: [] };
    }
    box.classList.remove("hidden");
    box.innerHTML = `<div class="stall-head">⏳ 待跟进 (${items.length})</div>`;
    for (const it of items.slice(0, 3)) box.appendChild(renderStallRow(it));
    if (items.length > 3) {
      const showAll = el("button", "stall-show-all");
      showAll.type = "button";
      showAll.textContent = `查看全部 ${items.length} 项`;
      showAll.onclick = () => showStallSheet(items, showAll);
      box.appendChild(showAll);
    }
    return { ok: true, items };
  }

  // 停滞事项完整列表：复用待分诊 sheet 的骨架（modal-root + 独立滚动 + Escape 关闭）
  function showStallSheet(items, opener) {
    const root = $("modal-root");
    if (!root) return;
    if (root._sheetOwner && root._sheetOwner.close) root._sheetOwner.close({ suppressFocus: true });
    let closed = false;
    let closeBtn = null;
    const owner = {};
    const onKeydown = (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        close();
      }
    };
    const close = (opts = {}) => {
      if (closed) return;
      closed = true;
      if (root.onclick === onBackdropClick) root.onclick = null;
      root.removeEventListener("keydown", onKeydown);
      root.classList.remove("show");
      setTimeout(() => {
        if (root._sheetOwner !== owner) return;
        root.classList.add("hidden");
        root.innerHTML = "";
        delete root._sheetOwner;
        if (!opts.suppressFocus) {
          const focusTarget = (opener && opener.isConnected)
            ? opener
            : document.querySelector("#stall-inbox .stall-show-all");
          if (focusTarget) focusTarget.focus();
        }
      }, 200);
    };
    const onBackdropClick = (e) => { if (e.target === root) close(); };
    const render = (currentItems) => {
      if (closed) return;
      if (!currentItems.length) {
        close();
        return;
      }
      const shouldFocusClose = root.contains(document.activeElement);
      root.innerHTML = "";
      const card = el("div", "modal-card triage-sheet-card");
      card.setAttribute("role", "dialog");
      card.setAttribute("aria-modal", "true");
      card.setAttribute("aria-label", `全部待跟进事项，共 ${currentItems.length} 项`);
      card.innerHTML = `
        <div class="triage-sheet-head">
          <div class="modal-title">待跟进 (${currentItems.length})</div>
          <button class="triage-sheet-close" type="button" aria-label="关闭待跟进列表">关闭</button>
        </div>
        <div class="triage-sheet-list"></div>`;
      const list = card.querySelector(".triage-sheet-list");
      for (const it of currentItems) {
        list.appendChild(renderStallRow(it, {
          onResolved: async () => {
            // 动作完成后重拉全量，就地重渲染剩余项
            let fresh;
            try { fresh = await api("/api/stalls"); } catch (e) { fresh = null; }
            if (!closed && fresh) render(fresh);
          },
        }));
      }
      root.appendChild(card);
      closeBtn = card.querySelector(".triage-sheet-close");
      closeBtn.onclick = close;
      if (shouldFocusClose) closeBtn.focus();
    };
    render(items);
    if (closed) return;
    owner.close = close;
    root._sheetOwner = owner;
    root.onclick = onBackdropClick;
    root.addEventListener("keydown", onKeydown);
    root.classList.remove("hidden");
    requestAnimationFrame(() => {
      if (closed) return;
      root.classList.add("show");
      if (closeBtn && closeBtn.isConnected) closeBtn.focus();
    });
  }

  // 单条停滞事项：标题 + 停滞时长徽章 + 说明 + 三个操作按钮（继续/稍后/跳过）。
  // continue 会建 worktree+隔离会话（分钟级），远超前端默认 15s 超时，必须显式放宽到 120s。
  function renderStallRow(it, opts = {}) {
    const row = document.createElement("div");
    row.className = "stall-row";
    row.dataset.id = it.id;
    const idleSec = it.idle_sec || 0;
    const badge = idleSec >= 86400
      ? `已停滞 ${Math.max(1, Math.round(idleSec / 86400))} 天`
      : `已停滞 ${Math.max(1, Math.round(idleSec / 3600))} 小时`;
    // 成本熔断额外挂一枚徽章：同为"耗尽"，这一类的下一步动作不同（续跑要定新预算）
    const costBadge = it.kind === "goal_cost_capped"
      ? `<span class="stall-badge">💰 成本上限</span>` : "";
    const detail = (it.detail || "").trim();
    row.innerHTML = `
      <div class="stall-row-main">
        <div class="stall-row-top">
          <span class="stall-row-title">${escapeHtml(it.title)}</span>
          <span class="stall-badge">${escapeHtml(badge)}</span>
          ${costBadge}
        </div>
        ${detail ? `<div class="stall-row-reason">${escapeHtml(detail)}</div>` : ""}
      </div>
      <div class="stall-row-actions">
        <button class="kanban-act-btn btn-edit" type="button" title="继续推进" aria-label="继续推进" data-act="continue">▶ 继续</button>
        <button class="kanban-act-btn btn-edit" type="button" title="稍后提醒（24 小时）" aria-label="稍后提醒" data-act="snooze">⏰ 稍后</button>
        <button class="kanban-act-btn btn-delete" type="button" title="跳过并不再提醒" aria-label="跳过并不再提醒" data-act="skip">✕ 跳过</button>
      </div>`;

    let inFlight = false;
    const actionButtons = row.querySelectorAll("[data-act]");
    const setBusy = (busy) => {
      inFlight = busy;
      row.setAttribute("aria-busy", String(busy));
      actionButtons.forEach((button) => { button.disabled = busy; });
    };
    const finish = async (successText) => {
      toast(successText, "success", 1500);
      try { await renderKanban(); } catch (e) { /* 刷新失败不影响动作结果 */ }
      if (opts.onResolved) await opts.onResolved();
    };
    const post = async (endpoint, body, timeoutMs) => {
      if (inFlight) return false;
      setBusy(true);
      try {
        await api(`/api/stalls/${it.id}/${endpoint}`, {
          method: "POST", body: JSON.stringify(body || {}), ...(timeoutMs ? { timeoutMs } : {}),
        });
      } catch (e) {
        if (row.isConnected) setBusy(false);
        toast(`操作失败：${e.message}`, "error");
        return false;
      }
      return true;
    };
    row.querySelector("[data-act='continue']").onclick = async () => {
      // 成本熔断的续跑会开新成本窗口（已花额度重新计），先问用户新上限；留空 = 不改上限
      const body = {};
      if (it.kind === "goal_cost_capped") {
        // related 缺失（后端成本取数失败时整块退化为空）时不能硬编码一个上限兜底：
        // GOAL_MAX_COST_USD 是可配 env，写成 20 只在默认配置下巧合正确，会拿着错数字
        // 诱导用户定预算；此时改说「未知」，输入框也不预填（留空 = 不改上限，后端沿用原值）。
        const rel = it.related || {};
        const hasLimit = rel.cost_limit != null && String(rel.cost_limit) !== "";
        const limitText = hasLimit ? `上限 $${rel.cost_limit}` : "上限未知";
        const spentText = rel.spent_usd != null && !Number.isNaN(Number(rel.spent_usd))
          ? `$${Number(rel.spent_usd).toFixed(2)}` : "未知";
        const ans = prompt(
          `新的成本上限（美元）。该会话本成本窗口已花 ${spentText} / ${limitText}。`
          + `留空则${hasLimit ? `沿用 $${rel.cost_limit}` : "不设新上限"}：`,
          hasLimit ? String(rel.cost_limit) : "");
        if (ans === null) return; // 取消不动
        if (ans.trim() !== "") {
          const v = Number(ans);
          if (!(v >= 0)) { toast("上限需为非负数字", "error"); return; }
          body.max_cost_usd = v;
        }
      }
      // 建会话/worktree 是分钟级操作，120s 超时兜底（前端默认仅 15s）
      if (await post("continue", body, 120000)) await finish("已继续推进");
    };
    row.querySelector("[data-act='snooze']").onclick = async () => {
      if (await post("snooze", { hours: 24 })) await finish("已稍后提醒（24 小时后再见）");
    };
    row.querySelector("[data-act='skip']").onclick = async () => {
      if (await post("skip", {})) await finish("已跳过，不再提醒");
    };
    return row;
  }

  // 待分诊收件箱（H3）：秘书晚报后分诊出的待跟进事项，等人工派单/转任务/忽略
  async function renderTriageInbox() {
    const box = $("triage-inbox");
    if (!box) return { ok: false, items: null };
    let items;
    try { items = await api("/api/triage"); }
    catch (e) { return { ok: false, items: null }; }
    if (!items || !items.length) {
      box.classList.add("hidden");
      box.innerHTML = "";
      return { ok: true, items: [] };
    }
    box.classList.remove("hidden");
    box.innerHTML = `<div class="triage-head">🔔 待分诊 (${items.length})</div>`;
    for (const t of items.slice(0, 3)) box.appendChild(renderTriageRow(t));
    if (items.length > 3) {
      const showAll = el("button", "triage-show-all");
      showAll.type = "button";
      showAll.textContent = `查看全部 ${items.length} 项`;
      showAll.onclick = () => showTriageSheet(items, showAll);
      box.appendChild(showAll);
    }
    return { ok: true, items };
  }

  // 待分诊完整列表：复用 modal-root，列表独立滚动，避免把 Overview 下方 Agent 列表顶走。
  function showTriageSheet(items, opener) {
    const root = $("modal-root");
    if (!root) return;
    if (root._sheetOwner && root._sheetOwner.close) root._sheetOwner.close({ suppressFocus: true });
    let closed = false;
    let closeBtn = null;
    const owner = {};
    const onKeydown = (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        close();
      }
    };
    const close = (opts = {}) => {
      if (closed) return;
      closed = true;
      if (root.onclick === onBackdropClick) root.onclick = null;
      root.removeEventListener("keydown", onKeydown);
      root.classList.remove("show");
      setTimeout(() => {
        if (root._sheetOwner !== owner) return;
        root.classList.add("hidden");
        root.innerHTML = "";
        delete root._sheetOwner;
        if (!opts.suppressFocus) {
          const focusTarget = (opener && opener.isConnected)
            ? opener
            : document.querySelector("#triage-inbox .triage-show-all");
          if (focusTarget) focusTarget.focus();
        }
      }, 200);
    };
    const onBackdropClick = (e) => { if (e.target === root) close(); };
    const render = (currentItems) => {
      if (closed) return;
      if (!currentItems.length) {
        close();
        return;
      }
      const shouldFocusClose = root.contains(document.activeElement);
      root.innerHTML = "";
      const card = el("div", "modal-card triage-sheet-card");
      card.setAttribute("role", "dialog");
      card.setAttribute("aria-modal", "true");
      card.setAttribute("aria-label", `全部待分诊事项，共 ${currentItems.length} 项`);
      card.innerHTML = `
        <div class="triage-sheet-head">
          <div class="modal-title">待分诊 (${currentItems.length})</div>
          <button class="triage-sheet-close" type="button" aria-label="关闭待分诊列表">关闭</button>
        </div>
        <div class="triage-sheet-list"></div>`;
      const list = card.querySelector(".triage-sheet-list");
      for (const t of currentItems) {
        list.appendChild(renderTriageRow(t, {
          onResolved: async (nextItems) => {
            if (!closed) render(nextItems);
          },
        }));
      }
      root.appendChild(card);
      closeBtn = card.querySelector(".triage-sheet-close");
      closeBtn.onclick = close;
      if (shouldFocusClose) closeBtn.focus();
    };
    render(items);
    if (closed) return;
    owner.close = close;
    root._sheetOwner = owner;
    root.onclick = onBackdropClick;
    root.addEventListener("keydown", onKeydown);
    root.classList.remove("hidden");
    requestAnimationFrame(() => {
      if (closed) return;
      root.classList.add("show");
      if (closeBtn && closeBtn.isConnected) closeBtn.focus();
    });
  }

  // 单条待分诊事项：标题 + 置信度 + 建议动作徽章 + 理由 + 三个操作按钮
  function renderTriageRow(t, opts = {}) {
    const row = document.createElement("div");
    row.className = "triage-row";
    row.dataset.id = t.id;
    const conf = Math.round((t.confidence || 0) * 100);
    const action = t.suggested_action || "triage";
    const reason = (t.description || "").trim();
    row.innerHTML = `
      <div class="triage-row-main">
        <div class="triage-row-top">
          <span class="triage-row-title">${escapeHtml(t.title)}</span>
          <span class="triage-badge">${escapeHtml(action)}</span>
          <span class="triage-conf">${conf}%</span>
        </div>
        ${reason ? `<div class="triage-row-reason">${escapeHtml(reason)}</div>` : ""}
      </div>
      <div class="triage-row-actions">
        <button class="kanban-act-btn btn-edit" type="button" title="派单开工" aria-label="派单开工" data-act="dispatch">▶ 派单</button>
        <button class="kanban-act-btn btn-edit" type="button" title="转为普通任务" aria-label="转为普通任务" data-act="to_todo">→ 任务</button>
        <button class="kanban-act-btn btn-delete" type="button" title="忽略" aria-label="忽略" data-act="ignore">🗑 忽略</button>
      </div>`;

    let inFlight = false;
    const actionButtons = row.querySelectorAll("[data-act]");
    const setBusy = (busy) => {
      inFlight = busy;
      row.setAttribute("aria-busy", String(busy));
      actionButtons.forEach((button) => { button.disabled = busy; });
    };
    const resolve = async (endpoint, successText, failText) => {
      if (inFlight) return;
      setBusy(true);
      try {
        await api(`/api/triage/${t.id}/${endpoint}`, { method: "POST" });
      } catch (e) {
        if (row.isConnected) setBusy(false);
        toast(`${failText}：${e.message}`, "error");
        return;
      }
      let refreshed;
      try {
        refreshed = await renderKanban();
      } catch (e) {
        refreshed = { ok: false, items: null };
      }
      if (!refreshed || !refreshed.ok) {
        toast("操作已提交，但列表刷新失败，请手动刷新确认", "error");
        return;
      }
      toast(successText, "success", 1500);
      if (opts.onResolved) await opts.onResolved(refreshed.items);
    };
    row.querySelector("[data-act='dispatch']").onclick = () => resolve("dispatch", "已派单开工", "派单失败");
    row.querySelector("[data-act='to_todo']").onclick = () => resolve("to_todo", "已转为任务", "转任务失败");
    row.querySelector("[data-act='ignore']").onclick = () => resolve("ignore", "已忽略", "忽略失败");
    return row;
  }

  // 活跃任务完整列表：首页仅保留三条预览，完整交互放在可滚动 sheet 中。
  function showKanbanSheet(items, opener) {
    const root = $("modal-root");
    if (!root) return;
    if (root._sheetOwner && root._sheetOwner.close) root._sheetOwner.close({ suppressFocus: true });
    let closed = false;
    let closeBtn = null;
    const owner = {};
    const onKeydown = (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        close();
      }
    };
    const close = (opts = {}) => {
      if (closed) return;
      closed = true;
      if (root.onclick === onBackdropClick) root.onclick = null;
      root.removeEventListener("keydown", onKeydown);
      root.classList.remove("show");
      setTimeout(() => {
        if (root._sheetOwner !== owner) return;
        root.classList.add("hidden");
        root.innerHTML = "";
        delete root._sheetOwner;
        if (opts.afterClose) {
          opts.afterClose();
          return;
        }
        if (!opts.suppressFocus) {
          const focusTarget = (opener && opener.isConnected)
            ? opener
            : document.querySelector("#kanban-list .kanban-show-all");
          if (focusTarget) focusTarget.focus();
        }
      }, 200);
    };
    const onBackdropClick = (e) => { if (e.target === root) close(); };
    const render = (currentItems) => {
      if (closed) return;
      if (!currentItems.length) {
        close();
        return;
      }
      const shouldFocusClose = root.contains(document.activeElement);
      root.innerHTML = "";
      const card = el("div", "modal-card kanban-sheet-card");
      card.setAttribute("role", "dialog");
      card.setAttribute("aria-modal", "true");
      card.setAttribute("aria-label", `全部任务，共 ${currentItems.length} 项`);
      card.innerHTML = `
        <div class="kanban-sheet-head">
          <div class="modal-title">全部任务 (${currentItems.length})</div>
          <button class="kanban-sheet-close" type="button" aria-label="关闭全部任务列表">关闭</button>
        </div>
        <div class="kanban-sheet-list"></div>`;
      const list = card.querySelector(".kanban-sheet-list");
      for (const t of currentItems) {
        list.appendChild(renderKanbanRow(t, false, {
          onChanged: async (refreshed) => {
            if (!closed) render(refreshed.kanbanItems || []);
          },
          onExitSheet: (afterClose) => close({ afterClose }),
        }));
      }
      root.appendChild(card);
      closeBtn = card.querySelector(".kanban-sheet-close");
      closeBtn.onclick = close;
      if (shouldFocusClose) closeBtn.focus();
    };
    render(items);
    if (closed) return;
    owner.close = close;
    root._sheetOwner = owner;
    root.onclick = onBackdropClick;
    root.addEventListener("keydown", onKeydown);
    root.classList.remove("hidden");
    requestAnimationFrame(() => {
      if (closed) return;
      root.classList.add("show");
      if (closeBtn && closeBtn.isConnected) closeBtn.focus();
    });
  }

  // ---------------- 看板卡片 → 只读会话内容预览 sheet ----------------
  // 与 peek 速览面板的分工：peek 带输入框、能直接往会话里发消息（会改会话状态），
  // 卡片预览是纯回放——"点一下看看这个任务做到哪了"不该顺手改到会话，故不复用 peek，
  // 本弹层内不存在任何输入框（硬护栏见下方 TRANSCRIPT_INTERACTIVE_SEL）。
  // 正文复用实时会话同一套消息构建器 appendMsgWithPreview，气泡/markdown/工具卡样式一致。
  const TRANSCRIPT_WINDOW = 200;  // 首屏条数。绝不传 limit=0：大会话全量拉会打爆前端。

  // 只读预览里必须摘掉的交互按钮：这两类都会往**主输入框**写内容（重发把指令填回输入框、
  // qr 快捷回复同理），点一下就把预览变成编辑态并抢走焦点。
  // ⚠️ 以后新增任何"写主输入框"的按钮，必须同步加进这个选择器。
  const TRANSCRIPT_INTERACTIVE_SEL = ".msg-resend, .qr-btn";

  function stripInteractiveBtns(root) {
    root.querySelectorAll(TRANSCRIPT_INTERACTIVE_SEL).forEach((b) => b.remove());
    // 摘空后的快捷栏留着会多出一道空行（.qr-bar 自带 margin-top）
    root.querySelectorAll(".qr-bar").forEach((bar) => { if (!bar.children.length) bar.remove(); });
  }

  // 关联会话 id 解析：session_ids（多关联）优先，回落旧的单值 session_id。
  function todoSessionIds(t) {
    if (!t) return [];
    return (t.session_ids && t.session_ids.length) ? t.session_ids : (t.session_id ? [t.session_id] : []);
  }

  // 会话元信息：活跃会话在 state.sessions 里已有（且带 hub 合并的运行态），命中就不打网络，
  // 归档会话列表未加载时才回落到 /api/sessions/{sid}/meta。
  async function fetchSessionMeta(sid) {
    const local = (state.sessions || []).find((s) => s.id === sid)
               || (state.archivedSessions || []).find((s) => s.id === sid);
    if (local) {
      return {
        id: sid,
        title: local.title || "",
        status: local.status || "idle",
        summary: local.summary || "",
        archived: local.archived ? 1 : 0,
        workdir: local.workdir || "",
        engine: local.engine || "claude",
        updated_at: local.updated_at || null,
      };
    }
    try {
      return await api(`/api/sessions/${encodeURIComponent(sid)}/meta`);
    } catch (e) {
      return null;
    }
  }

  // sid 为 null 表示"这个任务还没关联任何会话"——仍开 sheet，走空态 + 关联入口，
  // 保证"点开卡片一定看得到东西"，而不是又弹一个编辑框。
  async function showSessionTranscript(sid, opts = {}) {
    const root = $("modal-root");
    if (!root) return;
    if (root._sheetOwner && root._sheetOwner.close) root._sheetOwner.close({ suppressFocus: true });
    let closed = false;
    let closeBtn = null;
    const owner = {};
    const onKeydown = (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        close();
      }
    };
    const close = (o = {}) => {
      if (closed) return;
      closed = true;
      if (root.onclick === onBackdropClick) root.onclick = null;
      root.removeEventListener("keydown", onKeydown);
      root.classList.remove("show");
      setTimeout(() => {
        if (root._sheetOwner !== owner) return;
        root.classList.add("hidden");
        root.innerHTML = "";
        delete root._sheetOwner;
        if (o.afterClose) {
          o.afterClose();
          return;
        }
        if (!o.suppressFocus) {
          const focusTarget = (opts.opener && opts.opener.isConnected)
            ? opts.opener
            : document.querySelector("#kanban-list .kanban-show-all");
          if (focusTarget) focusTarget.focus();
        }
      }, 200);
    };
    const onBackdropClick = (e) => { if (e.target === root) close(); };

    const ids = todoSessionIds(opts.todo);
    const initialSid = sid || ids[0] || null;
    const shouldFocusClose = root.contains(document.activeElement);
    root.innerHTML = "";
    const card = el("div", "modal-card transcript-card");
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "true");
    card.innerHTML = `
      <div class="transcript-head">
        <span class="transcript-title"></span>
        <span class="transcript-badges"></span>
        <button class="transcript-close" type="button" aria-label="关闭会话预览">关闭</button>
      </div>
      <div class="transcript-tabs hidden"></div>
      <div class="transcript-body"></div>
      <div class="transcript-foot">
        <button class="transcript-open" type="button">打开完整会话 ↗</button>
      </div>`;
    root.appendChild(card);
    const titleEl = card.querySelector(".transcript-title");
    const badgesEl = card.querySelector(".transcript-badges");
    const tabsEl = card.querySelector(".transcript-tabs");
    const body = card.querySelector(".transcript-body");
    const openBtn = card.querySelector(".transcript-open");
    closeBtn = card.querySelector(".transcript-close");
    closeBtn.onclick = close;

    // sheet 局部状态：正文、是否拉到最早、以及本 sheet 独立的 Agent 归拢 Map。
    // groups 必须是局部的——传 state.agentGroups 会把预览重建出的子智能体 body
    // 登记进实时会话的归拢表，之后 WS 增量就会写进这个即将被销毁的节点。
    const transcript = { sid: initialSid, msgs: [], complete: false, groups: {} };

    const renderBadges = (meta) => {
      badgesEl.innerHTML = "";
      if (!meta) return;
      if (meta.archived) badgesEl.appendChild(el("span", "transcript-badge", "已归档"));
      const running = meta.status === "running";
      const st = el("span", "transcript-status " + (running ? "running" : "idle"),
        running ? "运行中" : "空闲");
      badgesEl.appendChild(st);
    };

    // 翻页：拿当前最早一条的 created_at 作 before 游标续拉更早一页。
    // 不用 renderLoadEarlierBtn——它写死 $("chat") 与 state.hist*，复用会把预览的
    // 翻页窗口污染进实时会话；这里只照抄它的 scrollTop 补偿写法。
    const makeEarlierBtn = () => {
      const btn = el("button", "transcript-earlier", "↑ 加载更早消息");
      btn.type = "button";
      btn.onclick = async () => {
        if (btn.disabled) return;
        const msgs = transcript.msgs;
        if (transcript.complete || !msgs.length || !msgs[0].created_at) { btn.remove(); return; }
        const targetSid = transcript.sid;
        btn.disabled = true;
        // 游标带上 id 作次级键：同秒消息只按 created_at 翻页会跨页静默丢消息
        const bId = msgs[0].id ? `&before_id=${encodeURIComponent(msgs[0].id)}` : "";
        let older;
        try {
          older = await api(`/api/sessions/${encodeURIComponent(targetSid)}/messages`
            + `?limit=${TRANSCRIPT_WINDOW}&before=${msgs[0].created_at}${bId}`);
        } catch (e) {
          if (!closed && btn.isConnected) btn.disabled = false;
          toast("加载更早消息失败：" + e.message, "error");
          return;
        }
        if (closed || transcript.sid !== targetSid) return;
        if (!older.length) {
          transcript.complete = true;
          btn.remove();
          return;
        }
        if (older.length < TRANSCRIPT_WINDOW) transcript.complete = true;
        const prevH = body.scrollHeight, prevTop = body.scrollTop;
        // 用**已连接**的 staging 容器承接这批旧消息：appendMessageGrouped 对游离
        // fragment 有兜底分支会去查实时 #chat 的 DOM（换了会话又查不到就会漏渲染
        // 子智能体结果）。挂进 body 保证 isConnected，搬完再把包装层摘掉。
        const staging = el("div", "transcript-staging");
        body.insertBefore(staging, btn.nextSibling);
        for (const m of older) {
          appendMsgWithPreview(staging, transcript.groups, m.role, m.content, m.created_at);
        }
        stripInteractiveBtns(staging);
        while (staging.firstChild) body.insertBefore(staging.firstChild, staging);
        staging.remove();
        // 游标必须同步前移：msgs[0] 是下一次翻页的 before，不更新会反复拉同一页。
        transcript.msgs = older.concat(transcript.msgs);
        if (transcript.complete) btn.remove();
        else btn.disabled = false;
        body.scrollTop = prevTop + (body.scrollHeight - prevH);
      };
      return btn;
    };

    const renderBody = () => {
      body.innerHTML = "";
      if (!transcript.msgs.length) {
        body.innerHTML = `<div class="entity-empty">这个会话还没有消息</div>`;
        return;
      }
      if (!transcript.complete) body.appendChild(makeEarlierBtn());
      for (const m of transcript.msgs) {
        appendMsgWithPreview(body, transcript.groups, m.role, m.content, m.created_at);
      }
      stripInteractiveBtns(body);
      // 预览默认停在最新一条（"这个任务做到哪了"）。
      body.scrollTop = body.scrollHeight;
    };

    const loadTranscript = async (targetSid) => {
      transcript.sid = targetSid;
      transcript.msgs = [];
      transcript.complete = false;
      transcript.groups = {};
      body.innerHTML = `<div class="chat-skel">${skeleton(3)}</div>`;
      const meta = await fetchSessionMeta(targetSid);
      if (closed || transcript.sid !== targetSid) return;
      const label = (meta && meta.title) || targetSid.slice(0, 10);
      titleEl.textContent = label;
      titleEl.title = label;
      card.setAttribute("aria-label", `会话预览：${label}`);
      renderBadges(meta);
      let msgs;
      try {
        msgs = await api(`/api/sessions/${encodeURIComponent(targetSid)}/messages?limit=${TRANSCRIPT_WINDOW}`);
      } catch (e) {
        if (closed || transcript.sid !== targetSid) return;
        body.innerHTML = `<div class="entity-empty">加载失败：${escapeHtml(e.message)}</div>`;
        return;
      }
      if (closed || transcript.sid !== targetSid) return;
      transcript.msgs = Array.isArray(msgs) ? msgs : [];
      transcript.complete = transcript.msgs.length < TRANSCRIPT_WINDOW;
      renderBody();
    };

    // 多会话 chip 条：点 chip 原地换正文，不关 sheet（关掉再开会闪一次）。
    if (ids.length > 1) {
      tabsEl.classList.remove("hidden");
      ids.forEach((cid, i) => {
        const chip = el("button", "transcript-chip" + (cid === initialSid ? " active" : ""));
        chip.type = "button";
        const local = (state.sessions || []).find((s) => s.id === cid)
                   || (state.archivedSessions || []).find((s) => s.id === cid);
        const chipLabel = (local && local.title) || cid.slice(0, 10);
        chip.title = chipLabel;
        chip.innerHTML = (i === 0 ? `<span class="transcript-chip-main">主</span>` : "")
          + `<span class="transcript-chip-name">${escapeHtml(chipLabel)}</span>`;
        chip.onclick = () => {
          if (transcript.sid === cid) return;
          tabsEl.querySelectorAll(".transcript-chip").forEach((c) => c.classList.remove("active"));
          chip.classList.add("active");
          loadTranscript(cid);
        };
        tabsEl.appendChild(chip);
      });
    }

    // goal 任务：sheet 头部给一个跳转目标循环详情的入口（复用现有 openGoalDetail，
    // 不在这里重画 goal_iterations）。
    const scheduleId = (opts.todo && opts.todo.dispatched_schedule_id)
      ? opts.todo.dispatched_schedule_id.trim() : "";
    if (scheduleId) {
      tabsEl.classList.remove("hidden");
      const goalBtn = el("button", "transcript-goal", "🎯 目标循环");
      goalBtn.type = "button";
      goalBtn.onclick = () => close({ afterClose: () => openGoalDetail(scheduleId) });
      tabsEl.appendChild(goalBtn);
    }

    if (initialSid) {
      openBtn.onclick = () => {
        const target = transcript.sid;
        close({ suppressFocus: true, afterClose: () => doJumpToSessionEnsured(target) });
      };
      loadTranscript(initialSid);
    } else {
      // 空态：没有关联会话。这里只给"去关联"的入口，编辑弹窗要用户再点一下才开，
      // 不再像以前那样点卡片直接落到编辑框里。
      openBtn.classList.add("hidden");
      titleEl.textContent = (opts.todo && opts.todo.title) || "任务";
      card.setAttribute("aria-label", `任务预览：${titleEl.textContent}`);
      body.innerHTML = `
        <div class="transcript-empty">
          <div class="transcript-empty-emoji">🔗</div>
          <div class="transcript-empty-title">这个任务还没有关联 Agent 会话</div>
          <div class="transcript-empty-sub">关联之后，点卡片就能直接看到会话在做什么</div>
          <button class="transcript-link-btn" type="button">关联 Agent 会话</button>
        </div>`;
      body.querySelector(".transcript-link-btn").onclick = () => {
        const t = opts.todo;
        close({ suppressFocus: true, afterClose: () => showEditTodoModal(t) });
      };
    }

    owner.close = close;
    root._sheetOwner = owner;
    root.onclick = onBackdropClick;
    root.addEventListener("keydown", onKeydown);
    root.classList.remove("hidden");
    requestAnimationFrame(() => {
      if (closed) return;
      root.classList.add("show");
      if (shouldFocusClose && closeBtn && closeBtn.isConnected) closeBtn.focus();
    });
  }

  // 看板行主体点击入口：有会话 → 只读内容预览；无会话 → 空态预览（内带关联入口）。
  function openTodoTranscript(t, opener) {
    const ids = todoSessionIds(t);
    showSessionTranscript(ids[0] || null, { opener, todo: t });
  }

  // 归档任务：从活跃看板隐藏（不物理删除），归档视图里可恢复
  async function refreshKanbanAfterTodoMutation(successText) {
    let refreshed;
    try {
      refreshed = await renderKanban();
    } catch (e) {
      refreshed = { ok: false, items: null };
    }
    if (!refreshed || !Array.isArray(refreshed.kanbanItems)) {
      toast("操作已提交，但看板刷新失败，请稍后手动刷新", "info", 3000);
      return { ok: false, submitted: true, refreshed: null };
    }
    toast(successText, "success", 1500);
    return { ok: true, submitted: true, refreshed };
  }

  async function archiveTodo(id) {
    try {
      await api(`/api/todos/${id}/archive`, { method: "POST" });
    } catch (e) {
      toast("归档失败：" + e.message, "error");
      return { ok: false, submitted: false };
    }
    return refreshKanbanAfterTodoMutation("已归档");
  }
  async function unarchiveTodo(id) {
    try {
      await api(`/api/todos/${id}/unarchive`, { method: "POST" });
    } catch (e) {
      toast("恢复失败：" + e.message, "error");
      return { ok: false, submitted: false };
    }
    return refreshKanbanAfterTodoMutation("已恢复");
  }
  async function deleteTodo(id) {
    try {
      await api(`/api/todos/${id}`, { method: "DELETE" });
    } catch (e) {
      toast("删除失败：" + e.message, "error");
      return { ok: false, submitted: false };
    }
    return refreshKanbanAfterTodoMutation("已删除");
  }
  async function cleanupTodos(scope) {
    let res;
    try {
      res = await api("/api/todos/bulk_cleanup", { method: "POST", body: JSON.stringify({ scope }) });
    } catch (e) {
      toast("清理失败：" + e.message, "error");
      return { ok: false, submitted: false };
    }
    const message = res.skipped > 0
      ? `已清理 ${res.affected} 项，${res.skipped} 项因关联会话运行中已跳过`
      : `已清理 ${res.affected} 项`;
    return refreshKanbanAfterTodoMutation(message);
  }

  // 单行看板（列表模式）：左侧状态色点 + 标题 + 单行截断的进展摘要 + hover 操作按钮
  function renderKanbanRow(t, isArchived = false, opts = {}) {
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
    // 无关联会话时给一条轻量提示，引导用户去编辑里关联 Agent
    const sessCount = (t.session_ids && t.session_ids.length) || (t.session_id ? 1 : 0);

    // .kanban-row-main 带 tabindex="-1"：它是会话预览的 opener，关闭预览时要把焦点还回来；
    // 纯 div 默认不可聚焦，opener.focus() 会静默失效导致焦点掉到 body。不进 Tab 序。
    row.innerHTML = `
      <span class="kanban-dot ${dot.cls}">${dot.html}</span>
      <div class="kanban-row-main" tabindex="-1">
        <span class="kanban-row-title">${escapeHtml(t.title)}</span>
        ${progress ? `<span class="kanban-row-progress">${escapeHtml(progress)}</span>`
          : (sessCount ? "" : `<span class="kanban-row-hint">＋ 点击关联 Agent</span>`)}
      </div>
      <div class="kanban-row-actions">
        ${sessCount ? `<button class="kanban-act-btn btn-edit" type="button" title="打开会话" aria-label="打开会话" data-act="goto">↗</button>` : ""}
        ${isArchived
          ? `<button class="kanban-act-btn btn-edit" type="button" title="恢复" aria-label="恢复任务" data-act="unarchive">↩</button>
             <button class="kanban-act-btn btn-delete" type="button" title="删除" aria-label="删除任务" data-act="delete">🗑</button>`
          : `<button class="kanban-act-btn btn-edit" type="button" title="编辑" aria-label="编辑任务" data-act="edit">✎</button>
             <button class="kanban-act-btn btn-edit" type="button" title="归档" aria-label="归档任务" data-act="archive">🗄</button>
             <button class="kanban-act-btn btn-delete" type="button" title="删除" aria-label="删除任务" data-act="delete">🗑</button>`}
      </div>`;

    let inFlight = false;
    const actionButtons = row.querySelectorAll("[data-act]");
    const setBusy = (busy) => {
      inFlight = busy;
      row.setAttribute("aria-busy", String(busy));
      actionButtons.forEach((button) => { button.disabled = busy; });
    };
    const exitSheet = (next) => {
      if (opts.onExitSheet) opts.onExitSheet(next);
      else next();
    };
    const handleMutation = async (mutation) => {
      if (inFlight) return;
      setBusy(true);
      const result = await mutation();
      if (!result || !result.ok) {
        // 请求未提交时允许重试；已提交但刷新失败则维持禁用，避免重复提交。
        if (!result || !result.submitted) setBusy(false);
        return;
      }
      if (opts.onChanged) await opts.onChanged(result.refreshed);
    };

    // 点击行主体：打开只读会话内容预览（无关联会话则给空态 + 关联入口）。
    // 只挂在 .kanban-row-main 上，给右侧操作按钮留出整条不响应跳转的空白区。
    const mainEl = row.querySelector(".kanban-row-main");
    mainEl.onclick = () => exitSheet(() => openTodoTranscript(t, mainEl));

    // ↗ 打开会话：保留原来的"直接跳到会话"老路径（预览里的「打开完整会话」也走它）
    const gotoBtn = row.querySelector("[data-act='goto']");
    if (gotoBtn) gotoBtn.onclick = (e) => {
      e.stopPropagation();
      exitSheet(() => jumpToTodoSession(t, () => toast("暂无关联会话", "info", 1500)));
    };

    // 编辑
    const editBtn = row.querySelector("[data-act='edit']");
    if (editBtn) editBtn.onclick = (e) => {
      e.stopPropagation();
      exitSheet(() => showEditTodoModal(t));
    };

    // 归档 / 恢复
    const archiveBtn = row.querySelector("[data-act='archive']");
    if (archiveBtn) archiveBtn.onclick = (e) => {
      e.stopPropagation();
      handleMutation(() => archiveTodo(t.id));
    };
    const unarchiveBtn = row.querySelector("[data-act='unarchive']");
    if (unarchiveBtn) unarchiveBtn.onclick = (e) => {
      e.stopPropagation();
      handleMutation(() => unarchiveTodo(t.id));
    };

    // 删除需二次确认；在 sheet 内先退出，避免与确认弹窗嵌套。
    row.querySelector("[data-act='delete']").onclick = (e) => {
      e.stopPropagation();
      if (inFlight) return;
      exitSheet(async () => {
        const yes = await confirmDialog(`确定删除「${t.title}」？`, { okText: "删除", danger: true });
        if (!yes) return;
        await handleMutation(() => deleteTodo(t.id));
      });
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

  // 看板卡片/行点击跳转策略：
  //   "picker"  = 多会话时弹选择器让用户选（默认）
  //   "primary" = 总是跳主会话（session_ids[0]），忽略其它关联
  // 改此值即可切换整体行为，无需改下面的调用点。
  const KANBAN_JUMP_MODE = "picker";

  // 统一处理看板任务的会话跳转。t 为 todo 对象。
  // onNoSession 可选：无关联会话时的回调（行视图传打开编辑）。
  // 跳转决策集中在此处 + KANBAN_JUMP_MODE，两处调用点只调本函数，换策略无需改调用点。
  function jumpToTodoSession(t, onNoSession) {
    const ids = todoSessionIds(t);
    if (ids.length === 0) {
      if (onNoSession) onNoSession();
      return;
    }
    // 单会话，或开关设为 primary：直接跳第一个
    if (ids.length === 1 || KANBAN_JUMP_MODE === "primary") {
      doJumpToSessionEnsured(ids[0]);
      return;
    }
    // 多会话 + picker 模式：弹选择器
    showSessionJumpPicker(ids);
  }

  // 实际执行跳转到某个会话（带存在性校验）。
  // 关键点：归档会话列表默认没加载，只查内存列表会把"已归档但看板还关联着"的会话
  // 误判成"会话不存在"（实测 6/10 张卡片点开是死路），故 miss 时先补拉一次归档列表再判。
  async function doJumpToSessionEnsured(sid) {
    if (!sid) return;
    const findLocal = () => (state.sessions || []).find((s) => s.id === sid)
                         || (state.archivedSessions || []).find((s) => s.id === sid);
    let sess = findLocal();
    if (!sess) {
      // loadArchivedSessions 自身吞异常（内部只 console.error），catch 兜住未来改成
      // 会抛出时的情形，避免跳转入口因为一次列表拉取失败而静默失效。
      await loadArchivedSessions().catch(() => {});
      sess = findLocal();
    }
    if (sess) { switchTab("overview"); switchSession(sess.id); openDetail(); }
    else toast("会话不存在", "info", 1500);
  }

  // 多会话跳转选择器：列出候选会话，点击某个即跳转并关闭。复用 modal-root / sp-item 样式。
  function showSessionJumpPicker(sessionIds) {
    const root = $("modal-root");
    root.innerHTML = "";
    const card = el("div", "modal-card");
    const items = sessionIds.map((sid, i) => {
      const s = (state.sessions || []).find((x) => x.id === sid)
             || (state.archivedSessions || []).find((x) => x.id === sid);
      const name = s ? (s.title || sid.slice(0, 10)) : sid.slice(0, 10);
      const primary = i === 0;  // 第一个为主会话
      return `<div class="sp-item jump-item" data-sid="${escapeAttr(sid)}">
        ${primary ? `<span class="chip-primary-tag">主</span>` : `<span class="sp-check"></span>`}
        <span class="sp-name" title="${escapeAttr(name)}">${escapeHtml(name)}</span>
      </div>`;
    }).join("");
    card.innerHTML = `
      <div class="modal-title">跳转到会话</div>
      <div class="jump-picker-list">${items}</div>
      <div class="modal-actions">
        <button class="modal-cancel" type="button">取消</button>
      </div>`;
    root.appendChild(card);
    root.classList.remove("hidden");
    requestAnimationFrame(() => root.classList.add("show"));
    const close = () => { root.classList.remove("show"); setTimeout(() => { root.classList.add("hidden"); root.innerHTML = ""; }, 200); };
    card.querySelector(".modal-cancel").onclick = close;
    root.onclick = (e) => { if (e.target === root) close(); };
    card.querySelectorAll(".jump-item").forEach((item) => {
      item.onclick = () => { close(); doJumpToSessionEnsured(item.dataset.sid); };
    });
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
        <div class="form-field">
          <span class="field-label">关联 Agent 会话</span>
          <div class="session-selector">
            <div class="session-chips-row">
              <div class="session-chips" id="nt-session-chips"></div>
              <button type="button" class="session-add-btn" id="nt-session-add-btn">+ 添加</button>
            </div>
            <div class="session-picker-panel hidden" id="nt-session-panel">
              <input type="text" class="session-search" id="nt-session-search" placeholder="搜索会话…">
              <div class="session-picker-list" id="nt-session-list"></div>
              <button type="button" class="sp-done-btn" id="nt-session-done-btn">完成 ✓</button>
            </div>
          </div>
        </div>
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
    const close = () => { destroyPicker(); root.classList.remove("show"); setTimeout(() => { root.classList.add("hidden"); root.innerHTML = ""; }, 200); };
    card.querySelector(".modal-cancel").onclick = close;
    root.onclick = (e) => { if (e.target === root) close(); };
    let selectedSessions = [];
    const destroyPicker = bindSessionChips(card, "nt", selectedSessions);
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
    const selectedIds = (t.session_ids && t.session_ids.length)
      ? t.session_ids
      : (t.session_id ? [t.session_id] : []);
    card.innerHTML = `
      <div class="modal-title">编辑任务</div>
      <div class="entity-form" style="gap:12px">
        <label>任务标题
          <input id="et-title" class="form-input" placeholder="输入任务名称…" value="${escapeAttr(t.title)}" />
        </label>
        <label>任务描述
          <textarea id="et-desc" class="form-input" rows="3" placeholder="补充说明…">${escapeHtml(t.description || "")}</textarea>
        </label>
        <div class="form-field">
          <span class="field-label">关联 Agent 会话</span>
          <div class="session-selector">
            <div class="session-chips-row">
              <div class="session-chips" id="et-session-chips"></div>
              <button type="button" class="session-add-btn" id="et-session-add-btn">+ 添加</button>
            </div>
            <div class="session-picker-panel hidden" id="et-session-panel">
              <input type="text" class="session-search" id="et-session-search" placeholder="搜索会话…">
              <div class="session-picker-list" id="et-session-list"></div>
              <button type="button" class="sp-done-btn" id="et-session-done-btn">完成 ✓</button>
            </div>
          </div>
        </div>
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
    const close = () => { destroyPicker(); root.classList.remove("show"); setTimeout(() => { root.classList.add("hidden"); root.innerHTML = ""; }, 200); };
    card.querySelector(".modal-cancel").onclick = close;
    root.onclick = (e) => { if (e.target === root) close(); };
    let selectedSessions = [...selectedIds];
    const destroyPicker = bindSessionChips(card, "et", selectedSessions);
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
    const doneBtn  = card.querySelector(`#${prefix}-session-done-btn`);

    // session 状态 → 小圆点 class（沿用看板 dotMap 的色系，见 renderKanbanRow）
    function sessionDotCls(s) {
      if (!s) return "dot-pending";
      if (s.status === "running") return "dot-inprogress";
      const key = deriveState(s).key;
      if (key === "failed") return "dot-cancelled";
      if (key === "done") return "dot-done";
      return "dot-pending";
    }

    function renderChips() {
      chipsBox.innerHTML = selectedSessions.map((sid, i) => {
        const s = state.sessions.find((x) => x.id === sid);
        const name = s ? (s.title || sid.slice(0, 10)) : sid.slice(0, 10);
        // 第一个为主会话（看板跳转/进展刷新都取它），加「主」标记与 primary-chip 样式
        const primary = i === 0;
        return `<span class="session-chip${primary ? " primary-chip" : ""}" title="${escapeAttr(name)}">` +
          `${primary ? `<span class="chip-primary-tag">主</span>` : ""}` +
          `<span class="chip-name">${escapeHtml(name)}</span>` +
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
      // 已选置顶：便于确认与取消，其余保持原顺序
      filtered.sort((a, b) => (selectedSessions.includes(b.id) ? 1 : 0) - (selectedSessions.includes(a.id) ? 1 : 0));
      list.innerHTML = filtered.length ? filtered.map((s) => {
        const selected = selectedSessions.includes(s.id);
        const name = s.title || s.id.slice(0, 10);
        return `<div class="sp-item${selected ? " selected" : ""}" data-sid="${escapeAttr(s.id)}">
          <span class="sp-check">${selected ? "✓" : ""}</span>
          <span class="sp-dot ${sessionDotCls(s)}"></span>
          <span class="sp-name" title="${escapeAttr(name)}">${escapeHtml(name)}</span>
        </div>`;
      }).join("") : `<div class="sp-empty">无匹配会话</div>`;
      list.querySelectorAll(".sp-item").forEach((item) => {
        item.onclick = (e) => {
          // 必须在 renderList 重建 DOM 前阻断冒泡：否则本节点被销毁后事件冒到 document，
          // onOutsideClick 因 e.target 已脱离 panel 而误判为外部点击关闭浮层。
          e.stopPropagation();
          const sid = item.dataset.sid;
          const idx = selectedSessions.indexOf(sid);
          if (idx > -1) selectedSessions.splice(idx, 1);
          else selectedSessions.push(sid);
          renderChips();
          renderList(search.value);
        };
      });
    }

    // 点浮层外关闭：监听器只绑一次，关闭时一并解绑，避免每次开合都堆积监听器。
    // 注意：panel 内点击（尤其点列表项）会触发 renderList 重建 DOM，e.target 随即脱离 panel，
    // 若仅靠 panel.contains(e.target) 判断会误判为"点在外面"而关闭。故在 panel 上拦截冒泡（见下方 panel.onclick）。
    function onOutsideClick(e) {
      if (!panel.contains(e.target) && e.target !== addBtn) closePanel();
    }
    function closePanel() {
      panel.classList.add("hidden");
      document.removeEventListener("click", onOutsideClick);
    }
    function openPanel() {
      search.value = "";
      renderList("");
      panel.classList.remove("hidden");
      search.focus();
      // 先解绑再绑，确保全局只有一个监听器
      document.removeEventListener("click", onOutsideClick);
      document.addEventListener("click", onOutsideClick);
    }

    addBtn.onclick = (e) => {
      e.stopPropagation();
      if (panel.classList.contains("hidden")) openPanel();
      else closePanel();
    };

    // 拦截 panel 内部点击的冒泡：列表项点击会 renderList 重建 DOM，若冒泡到 document，
    // onOutsideClick 里 e.target 已脱离 panel 会被误判为外部点击而关闭浮层。用捕获阶段兜底。
    panel.addEventListener("click", (e) => { e.stopPropagation(); });

    search.oninput = () => renderList(search.value);
    if (doneBtn) doneBtn.onclick = (e) => { e.stopPropagation(); closePanel(); };

    renderChips();

    // 供调用方在关闭弹窗时解绑：弹窗直接销毁（root.innerHTML = ""）不走 closePanel，
    // 需显式清理，否则 onOutsideClick 会残留在 document 上
    return function destroy() {
      document.removeEventListener("click", onOutsideClick);
    };
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

  // 会话副标题：在跑显示「耗时 + 当前活动」（卡住给 ⚠️ 提示，stuck 是 WS 里一直在推、
  // 之前从没被用上的现成信号），空闲显示行摘要
  function sessionSubtitle(s) {
    if (s.status === "running") {
      const mins = (typeof s.elapsed === "number" && s.elapsed > 0) ? Math.max(1, Math.round(s.elapsed / 60)) : null;
      const head = s.stuck
        ? `⚠️ ${mins ? mins + "m " : ""}无输出`
        : (mins ? `⏳ ${mins}m` : "⏳ 运行中");
      return head + (s.activity ? " · " + s.activity : "");
    }
    return s.summary || "";
  }

  // 从 session 对象派生关联待办文案，列表行与详情页共用同一口径。
  function linkedTodoText(s) {
    const titles = (s && s.linked_todo_titles) || [];
    if (!titles.length) return null;
    return {
      titles,
      brief: titles[0] + (titles.length > 1 ? " +" + (titles.length - 1) : ""),
      full: titles.join(" / "),
    };
  }

  // 常驻运行中提示条：composer 上方，展示当前会话是否在跑、跑了多久、卡没卡住。
  // 数据源优先 WS 实时 turn_progress，没收到过时首屏兜底走 GET /api/progress。
  // 计时本地自增：后端 turn_progress 首推在 300s、之后每 1800s 才推一次，纯事件驱动
  // 会让"运行中 N分"在两次推送之间僵死半小时。所以收到一次进度就记下基准
  // （elapsed + 收到时刻），由 _runBarTimer 每秒按真实流逝时间重算文字。
  let _runBarState = null;   // { elapsed, stuck, activity, at }  at = 收到该 elapsed 的本地时刻
  let _runBarTimer = null;

  function renderRunBar() {
    const bar = $("run-bar");
    if (!bar) return;
    if (!_runBarState) { bar.classList.add("hidden"); bar.textContent = ""; return; }
    const st = _runBarState;
    // 基准 elapsed 加上"收到之后又过去的时间"，得到当前真实耗时
    const live = (st.elapsed || 0) + (Date.now() - st.at) / 1000;
    const mins = Math.max(0, Math.round(live / 60));
    bar.classList.remove("hidden");
    bar.classList.toggle("stuck", !!st.stuck);
    const activity = st.activity ? " · " + st.activity : "";
    bar.textContent = st.stuck
      ? `⏳ 运行中 ${mins}分 · 最近 10 分钟无新输出${activity}`
      : `⏳ 运行中 ${mins}分${activity}`;
  }

  function updateRunBar(info) {
    if (!info) {
      _runBarState = null;
      if (_runBarTimer) { clearInterval(_runBarTimer); _runBarTimer = null; }
      renderRunBar();
      return;
    }
    // activity 可能本次没带（只推了 elapsed/stuck），沿用上一次的，避免文字忽然掉一截
    const prevActivity = _runBarState && _runBarState.activity;
    _runBarState = {
      elapsed: info.elapsed || 0,
      stuck: !!info.stuck,
      activity: info.activity !== undefined ? info.activity : prevActivity,
      at: Date.now(),
    };
    renderRunBar();
    if (!_runBarTimer) _runBarTimer = setInterval(renderRunBar, 1000);
  }

  // 首屏/刷新兜底：WS 进度事件在页面刚加载时可能还没收到过，主动拉一次快照对齐当前会话的 run-bar。
  async function primeRunBarFromApi() {
    try {
      const list = await api("/api/progress");
      const mine = (list || []).find((p) => p.session_id === state.sessionId);
      updateRunBar(mine || null);
    } catch (e) { /* 静默：run-bar 只是体验增强，拉不到就留空 */ }
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
    if (data.type === "turn_done") {
      showTurnDoneBanner(data.session_id, data.status, data.kind);
      if (data.session_id !== state.sessionId) maybeNotify({ status: data.status, kind: data.kind, sessionId: data.session_id });
      return;
    }
    if (data.type === "turn_progress") {
      const s = state.sessions.find((x) => x.id === data.session_id);
      if (s) {
        s.status = "running";
        s.elapsed = data.elapsed;
        s.stuck = !!data.stuck;
        // 不再用"已运行 N 分钟"覆盖 activity：耗时由 subtitle 的 elapsed 现算展示，
        // 保留上次真实活动标签（工具调用/回复摘要），卡住改由 stuck 标记驱动 ⚠️ 文案
        patchSessionRow(s);
      }
      return;
    }
    if (data.type === "agent_notify") {
      toast(`📣 ${data.title}${data.text ? "：" + data.text : ""}`, data.level === "error" ? "error" : "info", 6000);
      return;
    }
    // 目标循环状态变更（达成/终止）：目标视图开着就就地刷新，并给一条 toast
    if (data.type === "goal_update") {
      const done = data.goal_status === "done";
      toast(done ? "🎯 目标已达成" : "⏹️ 目标循环终止", done ? "success" : "info", 5000);
      if (!$("goal-view").classList.contains("hidden")) {
        if (_goalCurrentId && _goalCurrentId === data.schedule_id) loadGoalDetail(_goalCurrentId);
        else if (!_goalCurrentId) showGoalList();
      }
      return;
    }
    // 目标循环阶段实时进展（进新一轮/转验收/复位）：详情态就地刷新该目标，列表态刷新整个列表（推送为主，轮询兜底）
    if (data.type === "goal_progress") {
      if (!$("goal-view").classList.contains("hidden")) {
        if (_goalCurrentId && _goalCurrentId === data.schedule_id) loadGoalDetail(_goalCurrentId);
        else if (!_goalCurrentId) showGoalList();
      }
      return;
    }
    // 背对背仲裁阶段实时进展（A/B 各自完成/进入仲裁/收尾）：正打开该仲裁详情就重新拉取渲染
    if (data.type === "arbitration_progress") {
      if (!$("arb-view").classList.contains("hidden") && _arbCurrentId && _arbCurrentId === data.arb_id) {
        pollArbitration(_arbCurrentId);
      }
      return;
    }
    // 分派子任务阶段实时进展（转验收 / 落 done-failed）：正打开该 plan 详情就重拉重渲染，
    // 且无论详情态与否都刷新会话列表对应批次组摘要（推送为主，pollDispatch 的 60s 封顶轮询保留兜底）
    if (data.type === "dispatch_progress") {
      if (!$("dispatch-view").classList.contains("hidden") && _dispatchCurrentPlanId && _dispatchCurrentPlanId === data.plan_id) {
        openDispatchDetail(_dispatchCurrentPlanId);
      }
      const grp = document.querySelector(`li.dispatch-group[data-plan-id="${data.plan_id}"]`);
      if (grp) {
        const row = grp.querySelector(".dispatch-group-body li[data-sid]");
        if (row) refreshDispatchGroupSummary(row);
      }
      return;
    }
    // 停滞事项新增告警（Stall Watch 扫描发现新停滞）：toast 提示 + 收件箱即时刷新
    if (data.type === "stall_alert") {
      toast(`⏳ ${data.count || 1} 件事停在半路`, "info", 6000);
      renderStallInbox();
      return;
    }
    if (data.type !== "session_update") return;
    const s = state.sessions.find((x) => x.id === data.session_id);
    if (s) {
      // 更新内存里的会话对象，并就地 patch DOM（避免整体重渲染打断滚动/输入）
      s.status = data.status;
      s.activity = data.activity || "";
      if (typeof data.elapsed === "number") s.elapsed = data.elapsed;
      s.stuck = !!data.stuck;
      s.summary = data.summary || s.summary;
      if (data.title) s.title = data.title;
      // 回合结局：不重拉 /api/sessions 也能就地翻转徽章（已完成/失败/被中断）
      if (data.last_outcome) s.last_outcome = data.last_outcome;
      // 轮次/上回合耗时同批推送：列表 meta 的「N 轮 · 上回合耗时」不用等全量拉取才更新
      if (typeof data.user_turns === "number") s.user_turns = data.user_turns;
      if (typeof data.last_duration_ms === "number") s.last_duration_ms = data.last_duration_ms;
      s.updated_at = data.updated_at || s.updated_at;
      patchSessionRow(s);
      renderKanbanDebounced();  // 看板卡片的关联会话名/状态可能随之变化（防抖，避免高频刷新）
      // 当前会话同步页头标题与详情摘要
      if (data.session_id === state.sessionId) {
        const titleEl = $('session-title');
        if (titleEl && data.title) titleEl.textContent = data.title;
        updateDetailSummary(s);
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
      // 若该行属于某个折叠分组，同步刷新其分组头的「进行中N/共M」计数
      // （否则展开前的摘要会停留在上次整页渲染的旧值，直到下次 renderSessionLists）
      refreshDispatchGroupSummary(fresh);
    });
    renderDashboard();
    renderOverviewList();  // 状态翻转要反映到主列表行与「进行中」小节（如空闲→运行中）
    // review 列表成员可能因状态变化增减，简单起见重建一次该列表
    fillList($("session-list-review"), state.sessions.filter((x) => deriveState(x).key === "done"));
  }

  // 根据某个子会话行所在的分组 body，重算并更新该分组头的状态摘要。
  // 计数口径与 renderDispatchGroup 一致（deriveState().key === "running"）。
  // 行不在任何分组内则静默跳过；每处命中各自更新所在分组，互不干扰。
  function refreshDispatchGroupSummary(rowEl) {
    const body = rowEl.closest(".dispatch-group-body");
    if (!body) return;
    const summary = body.parentElement.querySelector(".dispatch-group-summary");
    if (!summary) return;
    const rows = body.querySelectorAll("li[data-sid]");
    let running = 0;
    for (const r of rows) {
      const sess = state.sessions.find((x) => x.id === r.dataset.sid);
      if (sess && deriveState(sess).key === "running") running++;
    }
    summary.textContent = `进行中 ${running} / 共 ${rows.length}`;
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

    // 拉最近消息（取末尾几条 user/assistant）。显式 limit=0 拉全量：长回合尾部可能
    // 堆着几百条 tool 消息，只取尾页会把真正的最后几条对话截掉。
    try {
      const msgs = await api(`/api/sessions/${sid}/messages?limit=0`);
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
    if (!sel || !cur) return;
    if (cur.engine === "codex") {
      // codex 会话：用 codex 模型列表重建 options，可自由切换。
      sel.innerHTML = CODEX_MODELS.map((id) => `<option value="${id}">${modeLabel(id)}</option>`).join("");
      sel.disabled = false;
      let mode = cur.mode;
      if (!CODEX_MODELS.includes(mode)) {
        // 脏值/claude 模型残留：默认首个 codex 模型并持久化。
        mode = CODEX_MODELS[0];
        api(`/api/sessions/${cur.id}/mode`, { method: "PATCH", body: JSON.stringify({ mode }) }).catch(() => {});
        cur.mode = mode;
      }
      sel.value = mode;
      const hint = $("mode-price-hint");
      if (hint) hint.textContent = modeRateStr(sel.value) || "";
      return;
    }
    // 旧档位迁移映射（与后端 session_hub 的 _LEGACY_MAP 保持一致）
    const legacyMap = {
      fast: "claude-haiku-4-5",
      strong: "claude-sonnet-5",
      super: "claude-opus-5[1m]",
    };
    // 重建 claude 模型 options（切回 claude 会话时覆盖 codex 会话残留的 options）
    sel.innerHTML = CLAUDE_MODELS.map((id) => `<option value="${id}">${modeLabel(id)}</option>`).join("");
    let mode = cur.mode;
    if (legacyMap[mode]) {
      // DB 存量旧会话：这些值不是任何 <option>，自动迁移到对应新模型 ID
      mode = legacyMap[mode];
      api(`/api/sessions/${cur.id}/mode`, { method: "PATCH", body: JSON.stringify({ mode }) }).catch(() => {});
      cur.mode = mode;
    }
    if (!CLAUDE_MODELS.includes(mode)) {
      // 脏值/codex 模型残留：默认首个 claude 模型并持久化。
      mode = CLAUDE_MODELS[0];
      api(`/api/sessions/${cur.id}/mode`, { method: "PATCH", body: JSON.stringify({ mode }) }).catch(() => {});
      cur.mode = mode;
    }
    if (mode) sel.value = mode;
    sel.disabled = false;
    const hint = $("mode-price-hint");
    if (hint) hint.textContent = modeRateStr(sel.value) || "";
  }
  // 切换档位：持久化到会话（后端 PATCH），下一回合即生效
  $("mode-select").onchange = async (e) => {
    const mode = e.target.value;
    if (!state.sessionId) return;
    try {
      await api(`/api/sessions/${state.sessionId}/mode`, { method: "PATCH", body: JSON.stringify({ mode }) });
      const cur = state.sessions.find((s) => s.id === state.sessionId);
      if (cur) cur.mode = mode;
      const hint = $("mode-price-hint");
      if (hint) hint.textContent = modeRateStr(mode) || "";
    } catch (err) { /* ignore，下次切会话会重新同步 */ }
  };

  // 把当前会话的推理强度回填到顶栏 select；codex 会话不支持 effort，置灰。
  function syncEffortSelect() {
    const cur = state.sessions.find((s) => s.id === state.sessionId);
    const sel = $("effort-select");
    if (!sel || !cur) return;
    sel.value = cur.effort || "medium";
    sel.disabled = cur.engine === "codex";
  }
  // 切换推理强度：持久化到会话（后端 PATCH），下一回合即生效
  $("effort-select").onchange = async (e) => {
    const effort = e.target.value;
    if (!state.sessionId) return;
    try {
      await api(`/api/sessions/${state.sessionId}/effort`, { method: "PATCH", body: JSON.stringify({ effort }) });
      const cur = state.sessions.find((s) => s.id === state.sessionId);
      if (cur) cur.effort = effort;
    } catch (err) {
      toast("切换失败：" + err.message, "error");
      syncEffortSelect();  // 失败回滚 select 显示
    }
  };

  // 把当前会话的底层 Agent 回填到详情栏 select；有消息后置灰（回合已开始不可切）
  function syncEngineSelect() {
    const cur = state.sessions.find((s) => s.id === state.sessionId);
    const sel = $("engine-select");
    if (!sel || !cur) return;
    sel.value = cur.engine || "claude";
    sel.disabled = !!(state.histMsgs && state.histMsgs.length > 0);
  }
  // 切换底层 Agent：仅在无消息时可切，持久化到会话（后端 PATCH）
  $("engine-select").onchange = async (e) => {
    const engine = e.target.value;
    if (!state.sessionId) return;
    try {
      await api(`/api/sessions/${state.sessionId}/engine`, { method: "PATCH", body: JSON.stringify({ engine }) });
      const cur = state.sessions.find((s) => s.id === state.sessionId);
      if (cur) cur.engine = engine;
      syncModeSelect();  // 切到 codex 需把模型档位置灰
    } catch (err) {
      toast("切换失败：" + err.message, "error");
      syncEngineSelect();  // 失败回滚 select 显示
    }
  };

  async function createSession(opts = {}) {
    const body = {
      title: opts.title || ("新会话 " + new Date().toLocaleString("zh-CN", { hour: "2-digit", minute: "2-digit" })),
    };
    if (opts.workdir) body.workdir = opts.workdir;
    if (opts.mode) body.mode = opts.mode;
    if (opts.effort) body.effort = opts.effort;
    if (opts.engine) body.engine = opts.engine;
    if (opts.isolate) body.isolate = true;
    const s = await api("/api/sessions", { method: "POST", body: JSON.stringify(body) });
    if (s.isolate_notice) toast(s.isolate_notice, "warn", 4000);
    state.sessionId = s.id;
    localStorage.setItem("ac_session", s.id);
    await loadSessions();
    await switchSession(s.id);
    return s;
  }

  // New Tab 高级配置：自定义标题/目录/档位新建会话
  $("nf-mode").onchange = (e) => {
    const hint = $("nf-mode-price-hint");
    if (hint) hint.textContent = modeRateStr(e.target.value) || "";
  };
  // 目标选「本系统」时强制隔离：自动勾上并禁用手动取消（走 agent-console 仓库的 worktree）
  const nfTarget = $("nf-target");
  if (nfTarget) nfTarget.onchange = (e) => {
    const self = e.target.value === "self";
    const iso = $("nf-isolate");
    if (self) { iso.checked = true; iso.disabled = true; }
    else { iso.disabled = false; }
  };
  $("new-form").onsubmit = async (e) => {
    e.preventDefault();
    const title = $("nf-title").value.trim();
    let workdir = $("nf-workdir").value.trim();
    const mode = $("nf-mode").value;
    const effort = $("nf-effort").value;
    const engine = $("nf-engine").value;
    let isolate = $("nf-isolate").checked;
    // 「本系统」：workdir 走 sentinel，后端解析到 agent-console 仓库根，并强制隔离
    if (nfTarget && nfTarget.value === "self") { workdir = "@self"; isolate = true; }
    try {
      await createSession({ title, workdir, mode, effort, engine, isolate });
      $("new-form").reset();
      $("nf-isolate").disabled = false;  // reset 不会清除 disabled，手动复位
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
  $("kanban-archive-toggle").onclick = (e) => {
    state.kanbanView = state.kanbanView === "archived" ? "active" : "archived";
    e.currentTarget.classList.toggle("active", state.kanbanView === "archived");
    e.currentTarget.textContent = state.kanbanView === "archived" ? "🗄 返回" : "🗄 归档";
    renderKanban();
  };
  $("kanban-clean-btn").onclick = async (e) => {
    const button = e.currentTarget;
    const archived = state.kanbanView === "archived";
    const scope = archived ? "purge_archived" : "archive_finished";
    let items;
    try {
      items = await api(archived ? "/api/todos?archived=1" : "/api/todos");
    } catch (err) {
      toast("清理失败：" + err.message, "error");
      return;
    }
    const n = scope === "archive_finished"
      ? items.filter(t => (t.status === "done" || t.status === "cancelled") && !t.archived).length
      : items.filter(t => t.archived && t.status !== "triage").length;
    if (!n) {
      toast("没有可清理的任务", "info", 1800);
      return;
    }
    const root = $("modal-root");
    if (root._sheetOwner && root._sheetOwner.close) root._sheetOwner.close({ suppressFocus: true });
    const yes = archived
      ? await confirmDialog(`确定彻底删除 ${n} 项归档任务？此操作无法恢复。`, { danger: true })
      : await confirmDialog(`确定归档 ${n} 项已完成/已取消的任务？`);
    if (!yes) return;
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = "清理中…";
    try {
      await cleanupTodos(scope);
    } finally {
      button.disabled = false;
      button.textContent = originalText;
    }
  };
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
    if (s && s.linked_todo_count > 0) {
      toast("该会话已被智能看板任务关联，无法删除。请先在看板中解除关联。", "info", 2500);
      return;
    }
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
    } catch (e) {
      // 409（会话被看板任务关联）后端 detail 已含完整说明，直接原样提示，比“删除失败：”前缀更清晰
      if (e.message && e.message.includes("无法删除")) toast(e.message, "info", 2500);
      else toast("删除失败：" + e.message, "error");
    }
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

  async function togglePinSession(id, pinned) {
    try {
      await api(`/api/sessions/${id}/${pinned ? "unpin" : "pin"}`, { method: "POST" });
      await loadSessions();
    } catch (e) { toast((pinned ? "取消置顶失败：" : "置顶失败：") + e.message, "error"); }
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
    // 重复点当前会话时不要重新拉历史、清空正文和重连 WS。
    // 搜索命中需要扩大历史窗口并高亮时仍走完整加载流程。
    if (prevId === id && state.historySessionId === id && !state.pendingHighlight) {
      const cur = state.sessions.find((s) => s.id === id);
      if (cur) {
        $("session-title").textContent = cur.title;
        updateDetailSummary(cur);
        updateWorkdirBar(cur.workdir);
        updateSessionIdBar(cur.id);
        updateLinkedTodoBar(cur);
        markSeen(cur.id, cur.updated_at);
        // 标记已读后只摘掉这一行的未读圆点：整表重建会把刚点开的行重画、滚动位置也跳回顶部。
        // 不限容器按 data-sid 全局选——同一会话可能同时出现在主列表/进行中/Review/归档里，
        // session id 是 uuid 截断全局唯一，不会误伤别的行。
        document.querySelectorAll(`li[data-sid="${cur.id}"] .s-dot.unread`).forEach((d) => d.remove());
        renderDashboard();  // 「待查看」计数要实时减
      }
      document.querySelectorAll("li[data-sid]").forEach((li) => li.classList.toggle("active", li.dataset.sid === id));
      syncModeSelect();
      syncEffortSelect();
      if (!state.ws || state.ws.readyState > 1) connectWs();
      primeRunBarFromApi();
      return;
    }
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
      updateDetailSummary(cur);
      updateWorkdirBar(cur.workdir);
      updateSessionIdBar(cur.id);
      updateLinkedTodoBar(cur);
      markSeen(cur.id, cur.updated_at);
      // 同第一次点击分支：只摘圆点不整表重建，保住刚点开那行的位置与滚动条；同样全局选所有列表
      document.querySelectorAll(`li[data-sid="${cur.id}"] .s-dot.unread`).forEach((d) => d.remove());
      renderDashboard();  // 「待查看」计数要实时减
    }
    // 高亮当前会话行（跨三个列表）
    document.querySelectorAll("li[data-sid]").forEach((li) => li.classList.toggle("active", li.dataset.sid === id));
    syncModeSelect();
    syncEffortSelect();
    await loadHistory();
    if (prevId !== id) restoreDraft(id);      // 恢复新会话草稿（同会话不覆盖当前输入）
    connectWs();
    primeRunBarFromApi();
  }

  // 详情页头摘要：标题下方、meta 上方的 #detail-summary，素材与列表行 s-sub 同一份
  function updateDetailSummary(s) {
    const sumEl = $("detail-summary");
    if (!sumEl) return;
    sumEl.textContent = (s && s.summary) || "";
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

  function updateLinkedTodoBar(s) {
    const bar = $("linked-todo-bar");
    if (!bar) return;
    bar.classList.remove("expanded");
    const info = linkedTodoText(s);
    if (!info) {
      bar.classList.add("hidden");
      bar.textContent = "";
      bar.onclick = null;
      return;
    }
    const text = document.createElement("span");
    text.className = "todo-bar-text";
    text.textContent = "📋 " + info.brief;
    const toggle = document.createElement("span");
    toggle.className = "todo-bar-toggle";
    toggle.textContent = "展开";
    bar.replaceChildren(text, toggle);
    bar.title = info.full;
    bar.onclick = () => {
      const expanded = bar.classList.toggle("expanded");
      text.textContent = "📋 " + (expanded ? info.titles.join("\n") : info.brief);
      toggle.textContent = expanded ? "收起" : "展开";
    };
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
  const HISTORY_WINDOW = 200;  // 首屏渲染最近 N 条，"加载更早"每次再往前 N 条

  // 消息表是 append-only；实时 WS 消息没有数据库 id，因此用末尾几条的 role/content
  // 判断静默校验结果是否真的变化。相同则保留现有 DOM，不制造“切回页面又刷新”的闪烁。
  function sameHistorySnapshot(a, b) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
    const start = Math.max(0, a.length - 3);
    for (let i = start; i < a.length; i++) {
      if (a[i].role !== b[i].role) return false;
      if (JSON.stringify(a[i].content || {}) !== JSON.stringify(b[i].content || {})) return false;
    }
    return true;
  }

  // 把 WS 已确认落库的完整消息同步进内存快照。这样切后台回来再向服务器校验时，
  // 若只是核对到相同内容，就不会因为 histMsgs 仍是旧长度而误判并重建整个聊天区。
  function recordLiveHistory(role, content, extra = null) {
    if (state.historySessionId !== state.sessionId) return;
    state.histMsgs.push({ role, content, created_at: nowTs(), _live: true, ...(extra || {}) });
    state.histShown = Math.min(state.histMsgs.length, Math.max(0, state.histShown) + 1);
  }

  function isSameLiveMessage(msg, role, content) {
    return !!msg && msg.role === role
      && JSON.stringify(msg.content || {}) === JSON.stringify(content || {});
  }

  // 当前页本地先画了 user 气泡，后端出队/其它标签页又广播同一条 user 时，
  // 不应让内存快照重复计数，否则下一次静默校验会误判为“历史变化”而重绘。
  function recordWsHistory(role, content) {
    if (state.historySessionId !== state.sessionId) return;
    if (role === "user") {
      const last = state.histMsgs[state.histMsgs.length - 1];
      if (last && last._liveLocal && isSameLiveMessage(last, role, content)) {
        delete last._liveLocal;
        last._live = true;
        return;
      }
    }
    recordLiveHistory(role, content);
  }

  function renderHistorySnapshot(msgs, { preserveScroll = false } = {}) {
    const chat = $("chat");
    const hadContent = preserveScroll && !chat.querySelector(".chat-skel");
    const wasNearBottom = hadContent ? isNearBottom() : true;
    const previousTop = hadContent ? chat.scrollTop : 0;
    const previousCount = state.historySessionId === state.sessionId ? state.histMsgs.length : 0;
    const previousShown = state.historySessionId === state.sessionId ? state.histShown : 0;
    chat.innerHTML = "";
    state._histGroups = {};
    // 整屏重建后 state.agentGroups/subStreams 里的节点引用全部悬空；不清空会让后续
    // WS tool_result/流式增量写进孤儿节点（用户看到的新卡片永远"运行中"）。这里先
    // 清，再在下方渲染循环末尾从新 DOM 重绑，保证任何离开本函数的路径 Map 都与 DOM 一致。
    state.agentGroups = {};
    state.subStreams = {};
    state.histMsgs = msgs;
    state.historySessionId = state.sessionId;
    if (!msgs.length) {
      state.histShown = 0;
      syncEngineSelect();   // 无消息：底层 Agent 可切
      chat.innerHTML = `<div class="chat-welcome"><div class="cw-emoji">💬</div>` +
        `<div class="cw-title">开始新的对话</div>` +
        `<div class="cw-sub">输入指令，或点下方快捷指令快速开始</div></div>`;
      return;
    }
    syncEngineSelect();   // 有消息：底层 Agent 置灰
    const q = state.pendingHighlight;
    // 静默补消息时保留用户已展开的“更早消息”窗口，不退回只看末尾 200 条。
    let shown = preserveScroll
      ? Math.min(msgs.length, Math.max(HISTORY_WINDOW, previousShown + Math.max(0, msgs.length - previousCount)))
      : Math.min(HISTORY_WINDOW, msgs.length);
    let hitIdx = -1;
    if (q) {
      hitIdx = firstMatchIdx(msgs, q);
      if (hitIdx >= 0) shown = Math.max(shown, msgs.length - hitIdx);
    }
    state.histShown = shown;
    // 本地还有未渲染的旧消息，或服务端可能还有更早的可续拉（翻页未到头）时显示按钮
    if (shown < msgs.length || !state.histComplete) renderLoadEarlierBtn();
    const start = msgs.length - shown;
    for (let i = start; i < msgs.length; i++) {
      appendMsgWithPreview(chat, state._histGroups, msgs[i].role, msgs[i].content, msgs[i].created_at);
    }
    // 重绑：把重建出的、尚未收尾的 Agent 卡片登记回实时归拢 Map（tool_use id -> 卡片 body），
    // 正在跑的子智能体后续 WS 增量/结果才能写进新 DOM。已收尾（hasResult）的不登记，
    // 与 appendMessageGrouped 收尾后 delete 的语义对齐，避免结果被二次覆盖。
    chat.querySelectorAll(".subagent[data-agent-id]").forEach((card) => {
      if (card.querySelector(".subagent-result[data-has-result]")) return;
      const body = card.querySelector(".subagent-body");
      if (body) state.agentGroups[card.dataset.agentId] = body;
    });
    // 自愈兜底：卡片仍标"运行中"但其后已出现顶层 result 行（回合已收尾），说明结果
    // 消息丢失/未落库。标中性"已结束（结果未记录）"而非谎报运行中；result 行在
    // 窗口外时判据落空，保持"运行中"——宁可保守也不误报完成。
    const resultLines = chat.querySelectorAll(".result-line");
    if (resultLines.length) {
      chat.querySelectorAll(".subagent-status.running").forEach((status) => {
        const card = status.closest(".subagent");
        if (!card) return;
        const ended = Array.from(resultLines).some((rl) =>
          card.compareDocumentPosition(rl) & Node.DOCUMENT_POSITION_FOLLOWING);
        if (ended) {
          status.textContent = "已结束（结果未记录）";
          status.classList.remove("running");
          status.classList.add("stale");
        }
      });
    }
    if (q) {
      const firstMark = highlightChat(q);
      state.pendingHighlight = null;
      if (firstMark) requestAnimationFrame(() => firstMark.scrollIntoView({ block: "center", behavior: "smooth" }));
      else scrollBottom(true);
    } else if (hadContent && !wasNearBottom) {
      // 用户正在翻旧消息时，后台校验发现新内容也不要把阅读位置拽到底部。
      requestAnimationFrame(() => {
        chat.scrollTop = Math.min(previousTop, Math.max(0, chat.scrollHeight - chat.clientHeight));
      });
    } else {
      scrollBottom(true);
    }
  }

  async function loadHistory({ silent = false, preserveScroll = false } = {}) {
    const chat = $("chat");
    const reqSid = state.sessionId;   // 快照：请求返回后若已切换会话则丢弃结果
    const previous = state.historySessionId === reqSid ? state.histMsgs : null;
    if (!silent) chat.innerHTML = `<div class="chat-skel">${skeleton(3)}</div>`;
    try {
      // 搜索命中要从全量历史里定位首个匹配并展开高亮，这个少见路径仍拉全量（limit=0）；
      // 常规首屏只拉尾部一页（服务端默认 limit=200），"加载更早"再按 before 游标续拉，
      // 避免单会话 12MB 级历史每次打开/切回都全量拉取。
      const wantAll = !!state.pendingHighlight;
      let msgs = await api(`/api/sessions/${reqSid}/messages${wantAll ? "?limit=0" : ""}`);
      if (reqSid !== state.sessionId) return;   // 用户已切走，勿覆盖新会话正文
      const pageLen = msgs.length;
      // 已点过"加载更早"时本页只是尾部一页：把上一份快照里更早翻页拉来的消息拼回
      // 前面（消息 append-only、旧前缀不变），否则已展开的窗口会被这次刷新收起。
      if (previous && previous.length > pageLen && pageLen) {
        const earliest = msgs[0].created_at || 0;
        const older = previous.filter((m) => (m.created_at || 0) < earliest);
        if (older.length) msgs = older.concat(msgs);
      }
      // 到头判定：拉了全量或尾页不满一页 = 更早方向没有更多可续拉；翻过页到过头要
      // 保住（同会话刷新只升不降），切会话时 previous 为空走全量/单页逻辑重置。
      if (wantAll || pageLen < HISTORY_WINDOW) state.histComplete = true;
      else if (!previous) state.histComplete = false;
      if (silent && previous && sameHistorySnapshot(previous, msgs)) {
        // 用服务端版本替换含 _live 临时项的快照，但保留正在浏览的 DOM 和滚动位置。
        state.histMsgs = msgs;
        state.historySessionId = reqSid;
        syncEngineSelect();
        return;
      }
      // 刚发送的本地气泡已显示、但服务端请求还没来得及落库时，远端快照可能短暂落后。
      // 此时宁可保留当前内容，等下次 WS/前台校验追平，也不要让用户看到消息闪退。
      if (silent && previous && msgs.length < previous.length && previous.some((m) => m._live)) return;
      // 翻页展开会把合并后的总长拉平，上面的长度比较盖不住"服务端缺最新乐观气泡"的
      // 场景：再按内容比对一次，本地 _live 尾巴没出现在返回里就按落后处理。
      if (silent && previous && previous.some((m) => m._live) &&
          !previous.filter((m) => m._live).every((m) =>
            msgs.some((x) => isSameLiveMessage(x, m.role, m.content)))) return;
      renderHistorySnapshot(msgs, { preserveScroll: silent || preserveScroll });
    } catch (e) {
      if (reqSid !== state.sessionId) return;   // 用户已切走，勿覆盖新会话正文
      if (silent) return;                       // 后台静默校验失败不打断当前阅读
      state.pendingHighlight = null;
      chat.innerHTML = "";
      toast("加载历史失败：" + e.message, "error");
      state.histMsgs = [];
      state.historySessionId = reqSid;
      syncEngineSelect();
    }
  }

  function renderLoadEarlierBtn() {
    const chat = $("chat");
    const remaining = (state.histMsgs || []).length - state.histShown;
    if (remaining <= 0 && state.histComplete) {
      const old = chat.querySelector(".load-earlier");
      if (old) old.remove();
      return;
    }
    let btn = chat.querySelector(".load-earlier");
    if (!btn) {
      btn = el("button", "load-earlier");
      chat.prepend(btn);
    }
    btn.disabled = false;
    btn.textContent = remaining > 0 ? `↑ 加载更早消息（剩 ${remaining} 条）` : "↑ 加载更早消息";
    btn.onclick = async () => {
      const msgs = state.histMsgs || [];
      // 第一层：本地已拉取但未渲染的旧消息直接扩窗（纯本地，快且不打服务端）
      if (state.histShown < msgs.length) {
        const curStart = msgs.length - state.histShown;
        const newStart = Math.max(0, curStart - HISTORY_WINDOW);
        const prevH = chat.scrollHeight, prevTop = chat.scrollTop;
        const frag = document.createDocumentFragment();
        for (let i = newStart; i < curStart; i++) {
          appendMsgWithPreview(frag, state._histGroups, msgs[i].role, msgs[i].content, msgs[i].created_at);
        }
        btn.after(frag);
        state.histShown = msgs.length - newStart;
        renderLoadEarlierBtn();
        chat.scrollTop = prevTop + (chat.scrollHeight - prevH);
        return;
      }
      if (state.histComplete || !msgs.length || !msgs[0].created_at) { renderLoadEarlierBtn(); return; }
      // 第二层：本地已渲染到头，按当前最早一条的 created_at 作 before 游标续拉更早一页
      // （带上该条的 id 作次级游标，同秒多条的边界才不会被吞掉）
      const reqSid = state.sessionId;
      const bId = msgs[0].id ? `&before_id=${encodeURIComponent(msgs[0].id)}` : "";
      btn.disabled = true;
      try {
        const older = await api(`/api/sessions/${reqSid}/messages?limit=${HISTORY_WINDOW}&before=${msgs[0].created_at}${bId}`);
        if (reqSid !== state.sessionId) return;   // 用户已切走，丢弃结果
        if (state.histMsgs !== msgs) { renderLoadEarlierBtn(); return; }   // 快照已被刷新替换，勿往重建后的 DOM 插
        if (!older.length) { state.histComplete = true; renderLoadEarlierBtn(); return; }
        if (older.length < HISTORY_WINDOW) state.histComplete = true;
        const prevH = chat.scrollHeight, prevTop = chat.scrollTop;
        const frag = document.createDocumentFragment();
        for (const m of older) {
          appendMsgWithPreview(frag, state._histGroups, m.role, m.content, m.created_at);
        }
        btn.after(frag);
        state.histMsgs = older.concat(msgs);
        state.histShown += older.length;
        chat.scrollTop = prevTop + (chat.scrollHeight - prevH);
        renderLoadEarlierBtn();
      } catch (e) {
        toast("加载更早消息失败：" + e.message, "error");
        btn.disabled = false;
      }
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
          if (t.resolved_model) {
            const mline = document.createElement('div');
            mline.className = 't-meta';
            mline.textContent = '实际模型：' + t.resolved_model;
            li.appendChild(mline);
          }
          ul.appendChild(li);
        }
      }
      // task 状态变化会影响徽章/看板，重渲染列表（仅在有会话数据时）
      if (changed && state.sessions.length) { renderSessionLists(); renderDashboard(); }
    } catch (e) { /* ignore */ }
  }
  $("refresh-tasks").onclick = loadTasks;

  // ---------------- 快捷指令 chip ----------------
  // 拉取快捷指令渲染成 chip 行：点击把 s.text 追加进指定 textarea。
  // composer 与仲裁/分派/目标循环弹窗共用，避免各处重复取数逻辑。
  async function renderSnippetChipsInto(barEl, textareaEl) {
    if (!barEl || !textareaEl) return;
    try {
      const snippets = await api("/api/snippets");
      barEl.innerHTML = "";
      if (!snippets || !snippets.length) { barEl.classList.add("hidden"); return; }
      for (const s of snippets) {
        const chip = el("button", "snippet-chip", escapeHtml(s.label || s.id));
        chip.onclick = () => {
          const cur = textareaEl.value.trim();
          textareaEl.value = cur ? cur + "\n" + s.text : s.text;
          textareaEl.focus();
          textareaEl.dispatchEvent(new Event("input"));
        };
        barEl.appendChild(chip);
      }
      barEl.classList.remove("hidden");
    } catch (e) { /* ignore，快捷指令非关键路径 */ }
  }
  async function loadSnippets() { await renderSnippetChipsInto($("snippet-bar"), input); }

  // ---------------- 管理面板（记忆库 / 子智能体共用） ----------------
  // kind: "memory" | "agent"，决定接口路径、字段、渲染方式
  const manage = { kind: null };
  let todoFormActive = false;  // 待办表单是否处于打开态（表单复用 manage-list 容器，故单独标记）

  function openManage(kind) {
    manage.kind = kind;
    closeDrawer();
    const titles = { memory: "记忆库", agent: "子智能体", skills: "技能", snippets: "快捷指令", schedule: "定时任务", todos: "待办清单", memos: "备忘录", reports: "日报记录", artifacts: "产出物" };
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
  $("open-skills-btn").onclick = () => openManage("skills");
  $("open-snippets-btn").onclick = () => openManage("snippets");
  $("open-schedules-btn").onclick = () => openManage("schedule");
  $("open-todos-btn").onclick = () => openManage("todos");
  $("open-memos-btn").onclick = () => openManage("memos");
  $("open-reports-btn").onclick = () => openManage("reports");
  const openArtifactsBtn = document.getElementById('open-artifacts-btn');
  if (openArtifactsBtn) openArtifactsBtn.onclick = () => openManage('artifacts');

  // ---------------- 背对背仲裁 + 智能分派：文本输入弹窗 ----------------
  // 复用 modal-root 风格（同 confirmDialog），返回 Promise<string|null>。
  function promptText(title, placeholder, okText = "确定", checkboxLabel = "", opts = {}) {
    // checkboxLabel 非空时额外渲染一个勾选框，并以 { text, checked } 形式 resolve；
    // 否则保持旧行为，直接 resolve 文本字符串（背对背仲裁等调用方依赖此签名）。
    // opts.snippets / opts.attachments 打开时，在弹窗内渲染快捷指令 chip 行与附件按钮，
    // 附件用弹窗局部 pending 数组（绝不碰全局 state.pendingImages，避免污染 composer 草稿），
    // 提交时按 send() 同规则把附件路径前置到返回文本；不传 opts 时旧行为完全不变。
    return new Promise((resolve) => {
      const root = $("modal-root");
      root.innerHTML = "";
      const card = el("div", "modal-card");
      const cbHtml = checkboxLabel
        ? `<label class="checkbox-label" style="margin-top:2px">
             <input type="checkbox" id="pt-checkbox" />
             <span>${escapeHtml(checkboxLabel)}</span>
           </label>`
        : "";
      const snippetHtml = opts.snippets ? `<div id="pt-snippet-bar" class="snippet-bar hidden"></div>` : "";
      const attachHtml = opts.attachments
        ? `<div style="display:flex">
             <button id="pt-attach-btn" class="img-btn" type="button" title="上传附件">&#128206;</button>
             <input id="pt-file-input" type="file" multiple hidden />
           </div>
           <div id="pt-tray" class="img-tray hidden"></div>`
        : "";
      card.innerHTML = `<div class="modal-title">${escapeHtml(title)}</div>
        <div class="entity-form" style="gap:12px">
          <textarea id="pt-input" class="form-input tall" rows="4" placeholder="${escapeAttr(placeholder)}"></textarea>
          ${snippetHtml}
          ${attachHtml}
          ${cbHtml}
        </div>
        <div class="modal-actions">
          <button class="modal-cancel" type="button">取消</button>
          <button class="modal-ok" type="button">${escapeHtml(okText)}</button>
        </div>`;
      root.appendChild(card);
      root.classList.remove("hidden");
      requestAnimationFrame(() => root.classList.add("show"));
      const close = (val) => { root.classList.remove("show"); setTimeout(() => { root.classList.add("hidden"); root.innerHTML = ""; }, 200); resolve(val); };
      setTimeout(() => { const t = $("pt-input"); if (t) t.focus(); }, 50);
      // 快捷指令 / 附件（局部 pending，随弹窗销毁）
      const ptPending = [];
      if (opts.snippets) renderSnippetChipsInto(card.querySelector("#pt-snippet-bar"), card.querySelector("#pt-input"));
      if (opts.attachments) {
        const attachBtn = card.querySelector("#pt-attach-btn");
        const fileInp = card.querySelector("#pt-file-input");
        const tray = card.querySelector("#pt-tray");
        attachBtn.onclick = () => fileInp.click();
        fileInp.onchange = async () => {
          const files = Array.from(fileInp.files || []);
          fileInp.value = "";
          for (const f of files) await uploadFileTo(f, ptPending, tray);
        };
      }
      card.querySelector(".modal-cancel").onclick = () => close(null);
      card.querySelector(".modal-ok").onclick = () => {
        const v = ($("pt-input").value || "").trim();
        if (!v) return;
        const lines = attachmentLines(ptPending);
        const finalText = lines ? `${lines}\n${v}` : v;
        if (checkboxLabel) close({ text: finalText, checked: !!($("pt-checkbox") && $("pt-checkbox").checked) });
        else close(finalText);
      };
      root.onclick = (e) => { if (e.target === root) close(null); };
    });
  }

  // ---------------- 背对背双执行 + 综合仲裁 ----------------
  let _arbPollTimer = null;
  let _arbCurrentId = null;  // 详情态正在看的仲裁 id，用于 arbitration_progress 就地刷新
  const _arbWaitStart = {};  // 各仲裁首次被看到处于 running 态的本地时间戳，用于纯前端自算「已用时」（避免依赖服务器/浏览器时钟一致性）
  function openArbView() {
    stopArbPoll();
    $("app-view").classList.add("hidden");
    $("arb-view").classList.remove("hidden");
    showArbList();
  }
  function closeArbView() {
    stopArbPoll();
    $("arb-view").classList.add("hidden");
    $("app-view").classList.remove("hidden");
  }
  function stopArbPoll() { if (_arbPollTimer) { clearTimeout(_arbPollTimer); _arbPollTimer = null; } }

  async function showArbList() {
    stopArbPoll();
    _arbCurrentId = null;
    const body = $("arb-body");
    body.innerHTML = '<div class="entity-loading">加载中…</div>';
    try {
      const items = await api("/api/arbitrations");
      body.innerHTML = "";
      if (!items.length) {
        body.innerHTML = '<div class="entity-empty"><div class="empty-emoji">⚖</div><div>还没有仲裁记录</div><div class="empty-sub">点右上角「+ 新问题」发起第一次</div></div>';
        return;
      }
      const ul = el("ul", "entity-list");
      for (const it of items) {
        const li = el("li");
        const head = el("div", "e-head");
        head.appendChild(el("span", "e-name", escapeHtml((it.question || "").slice(0, 60) || "（无标题）")));
        const stat = el("span", "e-tag", escapeHtml(it.status || ""));
        if (it.status === "error") stat.style.color = "var(--red)";
        head.appendChild(stat);
        li.appendChild(head);
        li.appendChild(el("div", "e-desc", escapeHtml(fmtTime(it.created_at))));
        li.style.cursor = "pointer";
        li.onclick = () => openArbDetail(it.id);
        ul.appendChild(li);
      }
      body.appendChild(ul);
    } catch (e) {
      body.innerHTML = `<div class="entity-empty">加载失败：${escapeHtml(e.message)}</div>`;
    }
  }

  async function openArbitrationInput() {
    const question = await promptText("背对背仲裁", "输入要交给两位工程师背对背作答的问题…", "开始", "", { snippets: true, attachments: true });
    if (!question) return;
    try {
      const r = await api("/api/arbitrate", { method: "POST", body: JSON.stringify({ question, session_id: state.sessionId || null }) });
      openArbView();
      openArbDetail(r.id);
    } catch (e) { toast("发起失败：" + e.message, "error"); }
  }

  function openArbDetail(id) {
    $("arb-view").classList.remove("hidden");
    $("app-view").classList.add("hidden");
    pollArbitration(id);
  }

  async function pollArbitration(id) {
    stopArbPoll();
    _arbCurrentId = id;
    try {
      const data = await api("/api/arbitrations/" + encodeURIComponent(id));
      renderArbitration(data);
      if (data.status === "running") {
        _arbPollTimer = setTimeout(() => pollArbitration(id), 3000);
      }
    } catch (e) {
      $("arb-body").innerHTML = `<div class="entity-empty">加载失败：${escapeHtml(e.message)}</div>`;
    }
  }

  function renderArbitration(data) {
    const body = $("arb-body");
    const stage = data.stage || "";
    // 纯前端自算「已用时」：首次看到 running 态记下本地时间戳，收尾后清掉（不依赖 created_at / 时钟对齐）
    const aid = data.id || _arbCurrentId;
    if (data.status === "running") {
      if (aid && !_arbWaitStart[aid]) _arbWaitStart[aid] = Date.now();
    } else if (aid) {
      delete _arbWaitStart[aid];
    }
    const elapsed = (aid && _arbWaitStart[aid]) ? Math.max(0, Math.round((Date.now() - _arbWaitStart[aid]) / 1000)) : 0;
    const elapsedSuffix = elapsed ? `（已用时 ${elapsed}s）` : "";
    // A/B 尚在生成阶段（含旧记录 stage 为空的 running 态）：两卡各自"并行作答中"，结果到位即显示
    const abPhase = ["", "pending", "running_ab", "a_done", "b_done"].includes(stage);
    const aWaiting = !data.result_a && abPhase ? `方案 A 并行作答中…${elapsedSuffix}` : "";
    const bWaiting = !data.result_b && abPhase ? `方案 B 并行作答中…${elapsedSuffix}` : "";
    // 仲裁结论卡：未出结论时——仲裁阶段显示"综合仲裁中…"，否则（A/B 还没都好）显示"等待方案 A/B 完成…"
    let finalWaiting = "";
    if (!data.verdict && stage !== "done") {
      finalWaiting = stage === "arbitrating" ? "综合仲裁中…" : "等待方案 A/B 完成…";
    }
    const col = (title, sub, text, waitingText) => {
      const placeholder = waitingText ? `<div class="arb-waiting">${escapeHtml(waitingText)}</div>` : `<div class="arb-text">${escapeHtml(text || "（无内容）")}</div>`;
      return `<div class="arb-col">
        <div class="arb-col-title">${escapeHtml(title)}</div>
        <div class="arb-col-sub">${escapeHtml(sub || "")}</div>
        ${placeholder}
      </div>`;
    };
    body.innerHTML = `
      <div class="arb-question">❓ ${escapeHtml(data.question || "")}</div>
      <div class="arb-columns">
        ${col("方案 A（Claude）", data.model_a, data.result_a, aWaiting)}
        ${col("方案 B（Codex）", data.model_b, data.result_b, bWaiting)}
        ${col("综合仲裁结论", data.arbiter_model, data.verdict, finalWaiting)}
      </div>
      ${data.status === "error" ? `<div class="form-err">仲裁失败：${escapeHtml(data.error || "")}</div>` : ""}`;
  }

  $("open-arb-btn").onclick = openArbView;
  $("arb-back").onclick = closeArbView;
  $("arb-new").onclick = openArbitrationInput;

  // ---------------- 角色化动态调度（智能分派） ----------------
  let _dispatchPollTimer = null;
  let _dispatchCurrentPlanId = null;  // 详情态正在看的 plan id，用于 dispatch_progress 就地刷新（比照 _arbCurrentId）
  function stopDispatchPoll() { if (_dispatchPollTimer) { clearTimeout(_dispatchPollTimer); _dispatchPollTimer = null; } }
  function openDispatchView() {
    stopDispatchPoll();
    $("app-view").classList.add("hidden");
    $("dispatch-view").classList.remove("hidden");
    showDispatchList();
  }
  function closeDispatchView() {
    stopDispatchPoll();
    $("dispatch-view").classList.add("hidden");
    $("app-view").classList.remove("hidden");
  }

  async function showDispatchList() {
    stopDispatchPoll();
    _dispatchCurrentPlanId = null;  // 回列表态清空，避免列表态下误触发详情刷新（比照 goal 的 _goalCurrentId）
    const body = $("dispatch-body");
    body.innerHTML = '<div class="entity-loading">加载中…</div>';
    try {
      const items = await api("/api/dispatch/plans");
      body.innerHTML = "";
      if (!items.length) {
        body.innerHTML = '<div class="entity-empty"><div class="empty-emoji">🧭</div><div>还没有分派记录</div><div class="empty-sub">点右上角「+ 新需求」发起第一次</div></div>';
        return;
      }
      const ul = el("ul", "entity-list");
      for (const it of items) {
        const li = el("li");
        const head = el("div", "e-head");
        head.appendChild(el("span", "e-name", escapeHtml((it.title || "").slice(0, 60) || "（无标题）")));
        head.appendChild(el("span", "e-tag", escapeHtml((it.subtask_count || 0) + " 个子任务")));
        li.appendChild(head);
        li.appendChild(el("div", "e-desc", escapeHtml(fmtTime(it.created_at))));
        li.style.cursor = "pointer";
        li.onclick = () => openDispatchDetail(it.plan_id);
        ul.appendChild(li);
      }
      body.appendChild(ul);
    } catch (e) {
      body.innerHTML = `<div class="entity-empty">加载失败：${escapeHtml(e.message)}</div>`;
    }
  }

  async function openDispatchDetail(planId) {
    stopDispatchPoll();
    _dispatchCurrentPlanId = planId;
    $("dispatch-view").classList.remove("hidden");
    $("app-view").classList.add("hidden");
    $("dispatch-body").innerHTML = '<div class="entity-loading">加载中…</div>';
    try {
      const subtasks = await api("/api/dispatch/" + encodeURIComponent(planId));
      renderDispatchPlan({ plan_id: planId, subtasks });
      const anyRunning = subtasks.some((s) => s.session_status === "running");
      if (anyRunning) pollDispatch(planId, 0);
    } catch (e) {
      $("dispatch-body").innerHTML = `<div class="entity-empty">加载失败：${escapeHtml(e.message)}</div>`;
    }
  }

  async function openDispatchInput() {
    const res = await promptText("智能分派", "描述一个较大的需求，会自动拆解成子任务并派给最合适的引擎/模型…", "分派", "困难任务（验收走背对背仲裁验证）", { snippets: true, attachments: true });
    if (!res) return;
    const request = res.text;
    const needArbitration = !!res.checked;
    $("dispatch-view").classList.remove("hidden");
    $("app-view").classList.add("hidden");
    $("dispatch-body").innerHTML = '<div class="entity-loading">规划并分派中…</div>';
    try {
      const data = await api("/api/dispatch", { method: "POST", body: JSON.stringify({ request, session_id: state.sessionId || null, need_arbitration: needArbitration }) });
      renderDispatchPlan(data);
      _dispatchCurrentPlanId = data.plan_id || null;  // 记住当前详情 plan，供 dispatch_progress 就地刷新
      // 子会话在各自后台跑，轮询几次刷新状态徽章
      if (data.plan_id) pollDispatch(data.plan_id, 0);
    } catch (e) {
      $("dispatch-body").innerHTML = `<div class="entity-empty">分派失败：${escapeHtml(e.message)}</div>`;
    }
  }

  async function pollDispatch(planId, tries) {
    stopDispatchPoll();
    if (tries >= 20) return;
    _dispatchPollTimer = setTimeout(async () => {
      try {
        const subtasks = await api("/api/dispatch/" + encodeURIComponent(planId));
        renderDispatchPlan({ plan_id: planId, subtasks });
        // 子会话仍在跑，或已进入/未离开完成判定流程（dispatched/verifying）都继续轮询，
        // 直到全部落到终态（done/failed/error）。
        const active = subtasks.some((s) =>
          s.session_status === "running" || s.status === "dispatched" || s.status === "verifying");
        if (active) pollDispatch(planId, tries + 1);
      } catch (e) { /* 静默：下次进入面板再刷 */ }
    }, 3000);
  }

  function _dispatchFriendlyName(engine, model) {
    if (engine === "codex") return "Codex";
    const m = (model || "").toLowerCase();
    if (m.includes("opus")) return "Opus 4.8";
    if (m.includes("sonnet")) return "Sonnet 5";
    return model || "Claude";
  }

  // 子任务完成判定状态 → [徽章文案, 徽章样式类]。dispatched 不加徽章，沿用下方 session_status 展示。
  const DISPATCH_STATUS_LABEL = {
    verifying: ["判定中", "tag-run"],
    done: ["已完成", "tag-good"],
    failed: ["判定失败", "tag-warn"],
    error: ["出错", "tag-warn"],
  };

  function renderDispatchPlan(data) {
    const body = $("dispatch-body");
    const planId = data.plan_id;
    const subtasks = data.subtasks || [];
    if (!subtasks.length) {
      body.innerHTML = '<div class="entity-empty"><div class="empty-emoji">🧭</div><div>没有拆解出子任务</div><div class="empty-sub">换个更具体的需求再试</div></div>';
      return;
    }
    body.innerHTML = "";
    const ul = el("ul", "entity-list");
    for (const st of subtasks) {
      const li = el("li");
      if (st.status === "error") li.classList.add("error");
      const head = el("div", "e-head");
      head.appendChild(el("span", "e-name", escapeHtml(st.title || "（无标题）")));
      const badge = el("span", "dispatch-badge dispatch-" + (st.category || "dev"), escapeHtml("派给：" + _dispatchFriendlyName(st.engine, st.model)));
      head.appendChild(badge);
      // 困难任务标记：验收走背对背双评委仲裁，用徽章提示用户该子任务走高规格验证。
      if (st.need_arbitration) head.appendChild(el("span", "e-tag tag-run", "⚖仲裁验证"));
      // 完成判定徽章：verifying/done/failed/error 各自成态；dispatched 阶段不显示，交给下方 session_status。
      const sm = DISPATCH_STATUS_LABEL[st.status];
      if (sm) head.appendChild(el("span", "e-tag " + sm[1], escapeHtml(sm[0])));
      li.appendChild(head);
      if (st.instruction) li.appendChild(el("div", "e-desc", escapeHtml(st.instruction.slice(0, 160))));
      // dispatched（或未知）阶段仍展示子会话运行态；已进入判定流程则用徽章表达。
      if (!sm || st.status === "dispatched") {
        li.appendChild(el("div", "e-desc", escapeHtml("子会话：" + (st.session_status || "待启动"))));
      }
      // done/failed 附带验收反馈文本（verdict 只是 done/failed 代码，已由徽章表达，此处展示可读反馈）。
      if ((st.status === "done" || st.status === "failed") && st.feedback) {
        li.appendChild(el("div", "e-desc", escapeHtml(st.feedback)));
      }
      // failed/error 是死态，给一个"重派"按钮：新起子会话重跑同一子任务，成功后回到轮询刷新。
      if (st.status === "failed" || st.status === "error") {
        const retryBtn = el("button", "e-tag tag-run", "重派");
        retryBtn.style.cursor = "pointer";
        retryBtn.onclick = async (e) => {
          e.stopPropagation();
          retryBtn.disabled = true;
          try {
            await api(`/api/dispatch/${encodeURIComponent(planId)}/subtasks/${encodeURIComponent(st.id)}/retry`, { method: "POST" });
            toast("已重派", "success");
            pollDispatch(planId, 0);
          } catch (err) {
            retryBtn.disabled = false;
            toast("重派失败：" + err.message, "error");
          }
        };
        li.appendChild(retryBtn);
      }
      if (st.child_session_id) {
        li.style.cursor = "pointer";
        li.onclick = () => { closeDispatchView(); switchSession(st.child_session_id); };
      }
      ul.appendChild(li);
    }
    body.appendChild(ul);
  }

  $("open-dispatch-btn").onclick = openDispatchView;
  $("dispatch-back").onclick = closeDispatchView;
  $("dispatch-new").onclick = openDispatchInput;

  // ---------------- 目标循环（H2）：列表 + 详情两态，仿智能分派/背对背仲裁 ----------------
  const GOAL_STATUS_LABEL = {
    running: ["等待下一轮", ""],
    producing: ["执行中", "tag-run"],
    verifying: ["验收中", "tag-run"],
    done: ["已完成", "tag-good"],
    exhausted: ["已耗尽停止", "tag-warn"],
  };
  function fmtGoalStatus(status) {
    return GOAL_STATUS_LABEL[status] || [status || "-", ""];
  }
  // 精简目标反馈：剥离"理由："等前缀、删掉内联长绝对路径/反引号路径，让结论句前置；兜底原文
  function simplifyFeedback(s) {
    if (!s) return "";
    const raw = String(s).trim();
    let t = raw;
    t = t.replace(/^\s*(理由|原因|说明|结论|反馈)\s*[:：]\s*/, "");
    t = t.replace(/`[^`]*\/[^`]*`/g, "");
    t = t.replace(/\/[\w.\-\/]{12,}/g, "");
    t = t.replace(/经?在?工作目录\s*实际核实\s*[，,、]?/g, "");
    t = t.replace(/\s{2,}/g, " ").replace(/^[\s，,、。.·-]+/, "").trim();
    return t || raw;
  }
  // 列表分组：按互斥优先级把每个目标循环归入唯一组，保证 KPI 各组之和 == 总数。
  // 返回徽章 class / 进度条 modifier / 排序 rank（进行中→已耗尽→已停用→已达成）
  function goalGroup(it) {
    const st = it.goal_status;
    if (it.enabled && (st === "running" || st === "producing" || st === "verifying"))
      return { group: "active", badgeCls: "tag-progress", meterCls: "is-active", rank: 0 };
    if (st === "done") return { group: "done", badgeCls: "tag-good", meterCls: "is-done", rank: 3 };
    if (st === "exhausted") return { group: "exhausted", badgeCls: "tag-warn", meterCls: "is-exhausted", rank: 1 };
    return { group: "disabled", badgeCls: "tag-warn", meterCls: "is-disabled", rank: 2 };
  }
  let _goalPollTimer = null;
  let _goalCurrentId = null;  // 详情态正在看的目标循环 id（列表态为 null），用于 goal_update 就地刷新
  let _goalSummaryCache = "";  // 「生成总结」结果缓存：模块级存活，WS 全量重建列表后仍能重渲，不随 goal-body 抹掉
  function stopGoalPoll() { if (_goalPollTimer) { clearTimeout(_goalPollTimer); _goalPollTimer = null; } }
  function openGoalView() {
    stopGoalPoll();
    $("app-view").classList.add("hidden");
    $("goal-view").classList.remove("hidden");
    showGoalList();
  }
  function closeGoalView() {
    stopGoalPoll();
    $("goal-view").classList.add("hidden");
    $("app-view").classList.remove("hidden");
  }

  // 单张目标循环卡片：状态左色条（is-<group>）+ 标题/徽章/进度/反馈；未完成组附「继续」按钮。
  function buildGoalCard(it) {
    const g = goalGroup(it);
    const sess = state.sessions.find((x) => x.id === it.session_id);
    const [label] = fmtGoalStatus(it.goal_status);
    // 来源：分诊派单标记在会话标题里（也兜底查 prompt），sess 可能不存在
    const isTriage = (it.prompt || "").includes("[自动派单]") || (sess && (sess.title || "").includes("[自动派单]"));
    // 标题优先用会话标题（prompt 首行是"项目路径…"抓不到重点），prompt 兜底
    const titleText = ((sess && sess.title) ? sess.title : (it.prompt || "（无标题）")).replace(/\s+/g, " ").trim().slice(0, 60);
    const pct = it.goal_status === "done"
      ? 100
      : (it.max_iterations ? Math.min(100, Math.round((it.iter_count || 0) / it.max_iterations * 100)) : 0);
    const card = el("div", "goal-card is-" + g.group,
      `<div class="goal-card-head">` +
        `<span class="goal-card-title">${escapeHtml(titleText)}</span>` +
        (isTriage ? `<span class="e-tag goal-badge-triage">分诊</span>` : "") +
        `<span class="e-tag ${g.badgeCls}">${escapeHtml(label)}</span>` +
      `</div>` +
      `<div class="goal-meter">` +
        `<div class="goal-meter-track"><div class="goal-meter-fill ${g.meterCls}" style="width:${pct}%"></div></div>` +
        `<div class="goal-card-meta">迭代 ${escapeHtml(String(it.iter_count || 0))}/${escapeHtml(String(it.max_iterations || 10))} · ${escapeHtml(it.last_run ? fmtRelTime(it.last_run) : "未运行")}</div>` +
      `</div>` +
      (it.last_feedback ? `<div class="goal-card-feedback">${escapeHtml(simplifyFeedback(it.last_feedback))}</div>` : ""));
    card.onclick = () => openGoalDetail(it.id);
    // 未完成（耗尽/停用）：加「继续」入口，保留历史续跑（stopPropagation 防误触发卡片跳详情）
    if (g.group === "exhausted" || g.group === "disabled") {
      const cont = el("button", "mini-btn goal-continue-btn", "继续");
      cont.onclick = (e) => { e.stopPropagation(); openGoalContinueForm(it); };
      card.appendChild(cont);
    }
    return card;
  }

  async function showGoalList() {
    stopGoalPoll();
    _goalCurrentId = null;
    const body = $("goal-body");
    body.innerHTML = '<div class="entity-loading">加载中…</div>';
    try {
      const all = await api("/api/schedules");
      const items = all.filter((s) => s.kind === "goal");
      body.innerHTML = "";
      if (!items.length) {
        body.innerHTML = '<div class="entity-empty"><div class="empty-emoji">🎯</div><div>还没有目标循环</div><div class="empty-sub">点右上角「+ 新目标」，设一个完成标准让 Agent 自迭代到达成</div></div>';
        return;
      }
      // 总览区：完成率大数字 + 进度条 + 各状态计数卡（各组互斥，和恒等于总数）
      const total = items.length;
      const counts = { active: 0, done: 0, exhausted: 0, disabled: 0 };
      for (const it of items) counts[goalGroup(it).group]++;
      const donePct = total ? Math.round(counts.done / total * 100) : 0;
      const overview = el("div", "goal-overview");
      const rate = el("div", "stat-card goal-stat-done",
        `<div class="stat-ico">✓</div>` +
        `<div class="stat-num">${donePct}%</div>` +
        `<div class="stat-label">完成率 ${counts.done}/${total}</div>` +
        `<div class="goal-meter"><div class="goal-meter-track"><div class="goal-meter-fill is-done" style="width:${donePct}%"></div></div></div>`);
      overview.appendChild(rate);
      for (const [grp, ico, lbl, n] of [
        ["active", "▶", "进行中", counts.active],
        ["exhausted", "⚠", "未完成(耗尽)", counts.exhausted],
        ["disabled", "⏸", "已停用", counts.disabled],
        ["done", "✓", "已完成", counts.done],
      ]) {
        overview.appendChild(el("div", "stat-card goal-stat-" + grp,
          `<div class="stat-ico">${ico}</div><div class="stat-num">${n}</div><div class="stat-label">${escapeHtml(lbl)}</div>`));
      }
      body.appendChild(overview);
      // 总结面板：仅当缓存非空才渲染（WS 全量重建列表后靠 _goalSummaryCache 保住），可折叠
      if (_goalSummaryCache) {
        const panel = el("div", "goal-summary-panel",
          `<div class="goal-summary-head">🧠 总览小结<span class="goal-summary-toggle">收起</span></div>` +
          `<div class="goal-summary-body">${escapeHtml(_goalSummaryCache)}</div>`);
        panel.querySelector(".goal-summary-head").onclick = () => {
          panel.classList.toggle("collapsed");
          panel.querySelector(".goal-summary-toggle").textContent = panel.classList.contains("collapsed") ? "展开" : "收起";
        };
        body.appendChild(panel);
      }
      // 分节：进行中 / 未完成(耗尽+停用合并) / 已完成，各节独立 grid，节内按创建时间倒序，空节不渲染
      for (const sec of [
        { label: "▶ 进行中", groups: ["active"] },
        { label: "⏹ 未完成", groups: ["exhausted", "disabled"] },
        { label: "✅ 已完成", groups: ["done"] },
      ]) {
        const secItems = items
          .filter((it) => sec.groups.includes(goalGroup(it).group))
          .sort((a, b) => (b.created_at || 0) - (a.created_at || 0));
        if (!secItems.length) continue;
        const section = el("div", "goal-section");
        section.appendChild(el("div", "goal-rounds-head", `${escapeHtml(sec.label)}（${secItems.length}）`));
        const grid = el("div", "goal-grid");
        for (const it of secItems) grid.appendChild(buildGoalCard(it));
        section.appendChild(grid);
        body.appendChild(section);
      }
    } catch (e) {
      body.innerHTML = `<div class="entity-empty">加载失败：${escapeHtml(e.message)}</div>`;
    }
  }

  // 「生成总结」：调后端一次性 LLM 汇总所有目标循环完成情况，缓存到 _goalSummaryCache 后重渲列表。
  async function genGoalSummary() {
    const btn = $("goal-summary-btn");
    if (!btn || btn.disabled) return;
    const orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = "生成中…";
    try {
      const res = await api("/api/goals/summary", { method: "POST", timeoutMs: 120000 });
      if (res && res.ok) {
        _goalSummaryCache = res.summary || "";
        if (!_goalCurrentId) showGoalList();
        else toast("已生成总结", "success");
      } else {
        toast((res && res.summary) || "总结生成失败", "info", 4000);
      }
    } catch (e) {
      toast("总结生成失败：" + (e.message || ""), "info", 4000);
    } finally {
      btn.disabled = false;
      btn.textContent = orig;
    }
  }

  // 续跑弹层：保留历史追加轮数继续迭代（区别于详情页「重新启动」的从头重跑）。抄 openGoalForm 的 modal 结构。
  async function openGoalContinueForm(it) {
    const root = $("modal-root");
    root.innerHTML = "";
    // 拉末轮反馈作只读展示：与后端同规则——过滤 verdict 非空、取 iter_no 最大那条的 feedback
    let lastReason = "";
    try {
      const its = await api(`/api/schedules/${encodeURIComponent(it.id)}/iterations`);
      const scored = (its || []).filter((r) => (r.verdict || "").trim());
      if (scored.length) {
        const last = scored.reduce((a, b) => ((b.iter_no || 0) >= (a.iter_no || 0) ? b : a));
        lastReason = (last.feedback || "").trim();
      }
    } catch (e) { /* 拉不到反馈不阻断续跑 */ }
    const card = el("div", "modal-card");
    card.innerHTML = `<div class="modal-title">继续目标循环</div>
      <div class="entity-form" style="gap:12px">
        ${lastReason ? `<div class="goal-field"><div class="goal-field-label">未完成原因（末轮验收反馈）</div><div class="goal-text">${escapeHtml(lastReason)}</div></div>` : ""}
        <label>目标（可调整后继续）
          <textarea id="gc-prompt" class="form-input tall" rows="3">${escapeHtml(it.prompt || "")}</textarea>
        </label>
        <label>完成标准
          <textarea id="gc-stop" class="form-input tall" rows="3">${escapeHtml(it.stop_condition || "")}</textarea>
        </label>
        <label>验收命令（可选，每轮在会话工作区里跑，退出码+输出作为客观信号喂给验收员）
          <input id="gc-verify" class="form-input" type="text" placeholder="例：python3 -m py_compile server/*.py" value="${escapeAttr(it.verify_command || "")}" />
        </label>
        <label>执行模式
          <select id="gc-mode">
            <option value="solo" ${(it.exec_mode || "solo") === "solo" ? "selected" : ""}>solo — 单 Agent 裸执行（默认）</option>
            <option value="team" ${it.exec_mode === "team" ? "selected" : ""}>team — 走 /console-dev 四角流水线</option>
          </select>
        </label>
        <label>追加轮数（1-50）
          <input id="gc-add" class="form-input" type="number" min="1" max="50" value="3" />
        </label>
        <label>成本上限（美元，留空不改）
          <input id="gc-maxcost" class="form-input" type="number" min="0" step="0.01" placeholder="默认 ${GOAL_COST_DEFAULT}" value="${it.max_cost_usd ? escapeAttr(String(it.max_cost_usd)) : ''}" />
        </label>
      </div>
      <div class="form-err" id="gc-err"></div>
      <div class="modal-actions">
        <button class="modal-cancel" type="button">取消</button>
        <button class="modal-ok" type="button">继续</button>
      </div>`;
    root.appendChild(card);
    root.classList.remove("hidden");
    requestAnimationFrame(() => root.classList.add("show"));
    const close = () => { root.classList.remove("show"); setTimeout(() => { root.classList.add("hidden"); root.innerHTML = ""; }, 200); };
    card.querySelector(".modal-cancel").onclick = close;
    root.onclick = (e) => { if (e.target === root) close(); };
    card.querySelector(".modal-ok").onclick = async () => {
      const errEl = card.querySelector("#gc-err");
      const add = parseInt(card.querySelector("#gc-add").value, 10);
      if (!(add >= 1 && add <= 50)) { errEl.textContent = "追加轮数需在 1-50 之间"; return; }
      const payload = {
        prompt: card.querySelector("#gc-prompt").value.trim(),
        stop_condition: card.querySelector("#gc-stop").value.trim(),
        verify_command: card.querySelector("#gc-verify").value.trim(),
        exec_mode: card.querySelector("#gc-mode").value,
        add_iterations: add,
        max_cost_usd: card.querySelector("#gc-maxcost").value === "" ? undefined : parseFloat(card.querySelector("#gc-maxcost").value),
      };
      try {
        await api(`/api/schedules/${encodeURIComponent(it.id)}/continue`, { method: "POST", body: JSON.stringify(payload) });
        toast("已继续，将在下一轮调度中重新迭代", "success");
        close();
        showGoalList();
      } catch (e) { errEl.textContent = e.message || "继续失败"; }
    };
  }

  async function openGoalDetail(id) {
    stopGoalPoll();
    $("goal-view").classList.remove("hidden");
    $("app-view").classList.add("hidden");
    $("goal-body").innerHTML = '<div class="entity-loading">加载中…</div>';
    await loadGoalDetail(id);
  }

  async function loadGoalDetail(id) {
    stopGoalPoll();
    _goalCurrentId = id;
    try {
      // 直接拉 /api/goals/{id}：返回体即 schedule 本体 + iterations（每轮迭代历史）+ subtasks（planned 子任务）
      const sch = await api("/api/goals/" + encodeURIComponent(id));
      renderGoalDetail(sch);
      if (["running", "producing", "verifying"].includes(sch.goal_status) && sch.enabled) {
        // goal_progress 推送为实时刷新主力，这个轮询只作 WS 断线时的兜底
        _goalPollTimer = setTimeout(() => loadGoalDetail(id), 3000);
      }
    } catch (e) {
      $("goal-body").innerHTML = `<div class="entity-empty">加载失败：${escapeHtml(e.message)}</div>`;
    }
  }

  // 目标循环「当前进展」横条的阶段文案：running/producing 都在生成，verifying 在验收，终态用状态标签本身
  function goalPhaseText(status) {
    if (status === "running" || status === "producing") return "生成中…";
    if (status === "verifying") return "验收中…";
    return fmtGoalStatus(status)[0];
  }

  function renderGoalDetail(sch) {
    const body = $("goal-body");
    const rounds = sch.iterations || [];
    const subtasks = sch.subtasks || [];
    const sess = state.sessions.find((x) => x.id === sch.session_id);
    const [label, cls] = fmtGoalStatus(sch.goal_status);
    const isTerminal = sch.goal_status === "done" || sch.goal_status === "exhausted";
    // 当前进展横条：非终态时点亮，planned 模式带上当前子任务标题
    let progressBar = "";
    if (!isTerminal) {
      let sub = "";
      if (sch.active_subtask_id) {
        const st = subtasks.find((x) => x.id === sch.active_subtask_id);
        if (st && st.title) sub = ` · ${escapeHtml(st.title)}`;
      }
      progressBar = `<div class="goal-progress-bar">
        <span>第 ${sch.iter_count || 0}/${sch.max_iterations || 10} 轮 · ${escapeHtml(goalPhaseText(sch.goal_status))}</span>
        <span class="goal-progress-sub">${sub}</span>
      </div>`;
    }
    // producing 阶段：把关联会话的实时活动摆到详情最上方，并给一个跳到该会话实时视图的入口
    let producingLine = "";
    if (sch.goal_status === "producing") {
      const act = sess
        ? (sess.status === "running" ? (sess.activity || "运行中…") : (sess.activity || sess.summary || "空闲"))
        : "(会话已删除)";
      producingLine = `<div class="goal-field"><div class="goal-field-label">🚀 正在执行</div><div class="goal-text${sess ? " goal-session-link" : ""}"${sess ? ' id="goal-producing-jump"' : ""}>${escapeHtml(act)}</div></div>`;
    }
    body.innerHTML = `
      ${progressBar}
      ${producingLine}
      <div class="goal-meta-row">
        <span class="e-tag ${cls}">${escapeHtml(label)}</span>
        <span class="e-tag">${sch.enabled ? "启用" : "已停用"}</span>
        <span class="e-tag">迭代 ${sch.iter_count || 0}/${sch.max_iterations || 10}</span>
      </div>
      <div class="goal-field"><div class="goal-field-label">🎯 目标</div><div class="goal-text">${escapeHtml(sch.prompt || "")}</div></div>
      <div class="goal-field"><div class="goal-field-label">✅ 完成标准</div><div class="goal-text">${escapeHtml(sch.stop_condition || "")}</div></div>
      ${sch.verify_command ? `<div class="goal-field"><div class="goal-field-label">🧪 验收命令</div><div class="goal-text">${escapeHtml(sch.verify_command)}</div></div>` : ""}
      <div class="goal-field"><div class="goal-field-label">⚙️ 执行模式</div><div class="goal-text">${sch.exec_mode === "team" ? "team（/console-dev 四角流水线）" : "solo（单 Agent）"}</div></div>
      <div class="goal-field"><div class="goal-field-label">💰 成本上限</div><div class="goal-text">${sch.max_cost_usd > 0 ? "$" + escapeHtml(String(sch.max_cost_usd)) : `默认 $${GOAL_COST_DEFAULT}`}</div></div>
      <div class="goal-field"><div class="goal-field-label">💬 最新反馈</div><div class="goal-text">${escapeHtml(sch.last_feedback || "（暂无）")}</div></div>
      <div class="goal-field"><div class="goal-field-label">📍 所在会话</div><div class="goal-text goal-session-link" id="goal-session-link">${escapeHtml(sess ? sess.title : "(已删除)")}</div></div>
      <div class="goal-actions">
        ${sess ? '<button class="btn-sm" id="goal-goto-session">查看会话</button>' : ""}
        <button class="btn-sm" id="goal-edit">编辑</button>
        ${!isTerminal ? `<button class="btn-sm danger" id="goal-stop">${sch.enabled ? "终止" : "重新启动"}</button>` : ""}
        <button class="btn-sm danger" id="goal-delete">删除</button>
      </div>
      <div class="goal-rounds-head">每轮进展（${rounds.length}）</div>
      <ul class="entity-list goal-rounds"></ul>`;
    const ul = body.querySelector(".goal-rounds");
    if (!rounds.length) {
      ul.innerHTML = '<li class="e-empty-row">还没有完成任何一轮验收</li>';
    } else {
      // rounds 按时间升序给出连续编号（第 N 次迭代），编号与原始 iter_no 解耦。
      // 只有整个循环仍在跑、且是最新一轮，才算真正"进行中"；
      // 循环已终止（耗尽/达成/暂停）后仍是 producing/verifying 的行，是进程重启/中断残留的僵尸行，标"已中断"。
      const loopActive = !!sch.enabled && ["running", "producing", "verifying"].includes(sch.goal_status);
      const sorted = rounds.slice().sort((a, b) => (a.started_at || 0) - (b.started_at || 0));
      const lastIdx = sorted.length - 1;
      sorted.forEach((it, idx) => {
        const li = el("li");
        const inProgressRow = it.status === "producing" || it.status === "verifying";
        const isCurrent = inProgressRow && loopActive && idx === lastIdx;
        const interrupted = inProgressRow && !isCurrent;
        const bad = it.status === "error";
        const roundDone = it.verdict === "done";
        const head = el("div", "e-head");
        head.appendChild(el("span", "e-name", `第 ${idx + 1} 次迭代 · ${fmtTime(it.started_at)}`));
        let tag, tagCls;
        if (isCurrent) { tag = it.status === "verifying" ? "验收中" : "执行中"; tagCls = "tag-progress"; }
        else if (interrupted) { tag = "已中断"; tagCls = "tag-muted"; }
        else if (bad) { tag = "异常"; tagCls = "tag-warn"; }
        else if (roundDone) { tag = "达成"; tagCls = "tag-good"; }
        else if (it.verdict === "exhausted") { tag = "已耗尽"; tagCls = "tag-warn"; }
        else { tag = "继续迭代"; tagCls = ""; }
        head.appendChild(el("span", "e-tag " + tagCls, escapeHtml(tag)));
        head.appendChild(el("span", "e-arrow", "▸"));
        head.addEventListener("click", () => li.classList.toggle("open"));
        li.appendChild(head);
        // 收起态：一行反馈/阶段预览。展开态：本轮产出摘要 + 元信息 + 跳转会话。
        const reason = isCurrent ? goalPhaseText(sch.goal_status)
          : interrupted ? (it.feedback || "该轮进程中断，未留下验收结论")
          : (it.feedback || "");
        if (reason) li.appendChild(el("div", "e-desc", escapeHtml(reason)));
        const bodyEl = el("div", "e-body");
        const excerpt = interrupted && !it.produced_excerpt
          ? "该轮进程中断，未留下产出"
          : (it.produced_excerpt || "（本轮无产出摘要）");
        bodyEl.appendChild(el("pre", "goal-round-excerpt", escapeHtml(excerpt)));
        let metaText = `原始迭代号 #${it.iter_no}`;
        if (it.task) {
          if (it.task.duration_ms) metaText += ` · 耗时 ${(it.task.duration_ms / 1000).toFixed(1)}s`;
          if (it.task.cost_usd != null) metaText += ` · $${it.task.cost_usd.toFixed(4)}`;
        }
        bodyEl.appendChild(el("div", "e-desc", escapeHtml(metaText)));
        if (sess) {
          const jump = el("button", "btn-sm", "查看会话");
          jump.onclick = (e) => { e.stopPropagation(); closeGoalView(); switchSession(sess.id); };
          bodyEl.appendChild(jump);
        }
        li.appendChild(bodyEl);
        ul.appendChild(li);
      });
    }
    if (sess) body.querySelector("#goal-goto-session").onclick = () => { closeGoalView(); switchSession(sess.id); };
    if (sess) { const pj = body.querySelector("#goal-producing-jump"); if (pj) pj.onclick = () => { closeGoalView(); switchSession(sess.id); }; }
    body.querySelector("#goal-edit").onclick = () => openGoalForm(sch);
    const stopBtn = body.querySelector("#goal-stop");
    if (stopBtn) stopBtn.onclick = async () => {
      const turningOn = !sch.enabled;
      const yes = await confirmDialog(turningOn ? "重新启动这个目标循环？将从第 1 轮重新开始迭代。" : "确定终止这个目标循环？",
        { okText: turningOn ? "重新启动" : "终止", danger: !turningOn });
      if (!yes) return;
      try {
        await api(`/api/schedules/${sch.id}`, { method: "PUT", body: JSON.stringify({ enabled: turningOn }) });
        toast(turningOn ? "已重新启动" : "已终止", "success");
        loadGoalDetail(sch.id);
      } catch (e) { toast("操作失败：" + e.message, "error"); }
    };
    body.querySelector("#goal-delete").onclick = async () => {
      const yes = await confirmDialog("确定删除这个目标循环？（会话本身不受影响）", { okText: "删除", danger: true });
      if (!yes) return;
      try {
        await api(`/api/schedules/${sch.id}`, { method: "DELETE" });
        toast("已删除", "success");
        showGoalList();
      } catch (e) { toast("删除失败：" + e.message, "error"); }
    };
  }

  function openGoalForm(existing) {
    stopGoalPoll();
    const root = $("modal-root");
    root.innerHTML = "";
    const d = existing || { session_id: state.sessionId || (state.sessions[0] || {}).id || "", prompt: "", stop_condition: "", max_iterations: 10, verify_command: "", exec_mode: "solo", max_cost_usd: 0 };
    const opts = state.sessions.map((s) => `<option value="${escapeAttr(s.id)}" ${s.id === d.session_id ? "selected" : ""}>${escapeHtml(s.title)}</option>`).join("");
    const mode = d.exec_mode === "team" ? "team" : "solo";
    const card = el("div", "modal-card goal-modal");
    card.innerHTML = `<div class="modal-title">${existing ? "编辑目标循环" : "新目标循环"}</div>
      <div class="entity-form goal-form">
        <label class="gf-full">在哪个会话里执行
          <select id="gf-session">${opts}</select>
        </label>
        <label class="gf-full">目标（每轮迭代都会带着这个目标去推进）
          <textarea id="gf-prompt" class="form-input" rows="3" placeholder="例：把 web 前端的目标循环入口做成列表+详情两态">${escapeHtml(d.prompt || "")}</textarea>
        </label>
        <div id="gf-snippet-bar" class="snippet-bar hidden gf-full"></div>
        <div class="gf-full" style="display:flex">
          <button id="gf-attach-btn" class="img-btn" type="button" title="上传附件">&#128206;</button>
          <input id="gf-file-input" type="file" multiple hidden />
        </div>
        <div id="gf-tray" class="img-tray hidden gf-full"></div>
        <label class="gf-full">完成标准（自然语言，独立小模型据此验收每轮产出）
          <textarea id="gf-stop" class="form-input" rows="3" placeholder="例：node --check 通过，且样式与现有页面一致">${escapeHtml(d.stop_condition || "")}</textarea>
        </label>
        <label class="gf-full">验收命令（可选，每轮在会话工作区里跑，退出码+输出作为客观信号喂给验收员）
          <input id="gf-verify" class="form-input" type="text" placeholder="例：python3 -m py_compile server/*.py" value="${escapeAttr(d.verify_command || "")}" />
        </label>
        <label>执行模式
          <select id="gf-mode">
            <option value="solo" ${mode === "solo" ? "selected" : ""}>solo — 单 Agent 裸执行（默认）</option>
            <option value="team" ${mode === "team" ? "selected" : ""}>team — 走 /console-dev 四角流水线</option>
          </select>
        </label>
        <label>最大迭代轮数（1-100）
          <input id="gf-maxiter" class="form-input" type="number" min="1" max="100" value="${escapeAttr(String(d.max_iterations || 10))}" />
        </label>
        <label>成本上限（美元，留空用默认）<input id="gf-maxcost" class="form-input" type="number" min="0" step="0.01" placeholder="默认 ${GOAL_COST_DEFAULT}" value="${d.max_cost_usd ? escapeAttr(String(d.max_cost_usd)) : ''}" /></label>
      </div>
      <div class="form-err" id="gf-err"></div>
      <div class="modal-actions">
        <button class="modal-cancel" type="button">取消</button>
        <button class="modal-ok" type="button">${existing ? "保存" : "创建"}</button>
      </div>`;
    root.appendChild(card);
    root.classList.remove("hidden");
    requestAnimationFrame(() => root.classList.add("show"));
    const close = () => { root.classList.remove("show"); setTimeout(() => { root.classList.add("hidden"); root.innerHTML = ""; }, 200); };
    card.querySelector(".modal-cancel").onclick = () => { close(); if (existing) loadGoalDetail(existing.id); };
    root.onclick = (e) => { if (e.target === root) { close(); if (existing) loadGoalDetail(existing.id); } };
    // 快捷指令 / 附件（局部 pending，随弹窗销毁，不碰全局 composer 草稿）
    const gfPending = [];
    renderSnippetChipsInto(card.querySelector("#gf-snippet-bar"), card.querySelector("#gf-prompt"));
    const gfFileInp = card.querySelector("#gf-file-input");
    const gfTray = card.querySelector("#gf-tray");
    card.querySelector("#gf-attach-btn").onclick = () => gfFileInp.click();
    gfFileInp.onchange = async () => {
      const files = Array.from(gfFileInp.files || []);
      gfFileInp.value = "";
      for (const f of files) await uploadFileTo(f, gfPending, gfTray);
    };
    // —— 成本上限自动预填（公式A：模型×模式×轮数×冗余；可改，用户改过后停止联动）——
    const LEGACY_MODEL_MAP = { fast: "claude-haiku-4-5", strong: "claude-sonnet-5", super: "claude-opus-5[1m]" };
    const maxCostEl = card.querySelector("#gf-maxcost");
    let costTouched = !!existing && Number(d.max_cost_usd) > 0;  // 编辑已有且已设值：保留、不预填、不联动
    function suggestCost() {
      const sess = state.sessions.find((s) => s.id === card.querySelector("#gf-session").value);
      const model = sess ? (LEGACY_MODEL_MAP[sess.mode] || sess.mode) : "";
      const rate = RATE_TABLE[model];                     // 查不到（未采集模型/脏值）→ 系数 1
      const priceFactor = rate ? rate / RATE_BASE : 1;    // 相对 sonnet-5 归一：sonnet 1 / opus-5≈2.5 / sol≈2.6
      const team = card.querySelector("#gf-mode").value === "team" ? 4 : 1;
      const iter = Math.max(1, parseInt(card.querySelector("#gf-maxiter").value, 10) || 1);
      return Math.max(2, Math.ceil(0.5 * priceFactor * team * iter * 1.5)); // 向上取整到$1、下限$2
    }
    function refreshCost() { if (!costTouched) maxCostEl.value = String(suggestCost()); }
    if (!costTouched) refreshCost();
    maxCostEl.addEventListener("input", () => { costTouched = true; });
    card.querySelector("#gf-session").addEventListener("change", refreshCost);
    card.querySelector("#gf-mode").addEventListener("change", refreshCost);
    card.querySelector("#gf-maxiter").addEventListener("input", refreshCost);
    card.querySelector(".modal-ok").onclick = async () => {
      const errEl = card.querySelector("#gf-err");
      const body = {
        session_id: card.querySelector("#gf-session").value,
        prompt: card.querySelector("#gf-prompt").value.trim(),
        kind: "goal",
        stop_condition: card.querySelector("#gf-stop").value.trim(),
        verify_command: card.querySelector("#gf-verify").value.trim(),
        exec_mode: card.querySelector("#gf-mode").value,
        max_iterations: parseInt(card.querySelector("#gf-maxiter").value, 10),
        max_cost_usd: card.querySelector("#gf-maxcost").value === "" ? 0 : parseFloat(card.querySelector("#gf-maxcost").value),
      };
      if (!body.prompt) { errEl.textContent = "目标不能为空"; return; }
      if (!body.stop_condition) { errEl.textContent = "完成标准不能为空"; return; }
      // 附件路径按 send() 同规则前置到目标文本
      const _lines = attachmentLines(gfPending);
      if (_lines) body.prompt = `${_lines}\n${body.prompt}`;
      try {
        if (existing) {
          await api(`/api/schedules/${existing.id}`, { method: "PUT", body: JSON.stringify(body) });
          toast("已保存", "success");
          close();
          loadGoalDetail(existing.id);
        } else {
          const created = await api("/api/schedules", { method: "POST", body: JSON.stringify(body) });
          toast("已创建", "success");
          close();
          openGoalDetail(created.id);
        }
      } catch (e) { errEl.textContent = e.message || "保存失败"; }
    };
  }

  $("open-goal-btn").onclick = openGoalView;
  const goalLoopHero = $("goal-loop-btn");
  if (goalLoopHero) goalLoopHero.onclick = openGoalView;
  $("goal-back").onclick = () => { if (_goalCurrentId) showGoalList(); else closeGoalView(); };
  $("goal-new").onclick = () => openGoalForm(null);
  $("goal-summary-btn").onclick = genGoalSummary;

  // ---------------- 任务运行统一视图：观测四套机制（目标循环/智能分派/背对背仲裁/分流）的发起。
  // 纯只读展示，只消费 GET /api/work_items[/{wid}]，绝不发起任何写操作。仿智能分派的列表+详情两态。
  const WORK_ITEM_ORIGIN_LABEL = {
    goal: "🎯 目标循环",
    dispatch: "🧭 智能分派",
    arbiter: "⚖ 背对背仲裁",
    triage: "🔀 分流",
  };
  // work_item 聚合状态 → [徽章文案, 徽章样式类]。pending/running/done/exhausted/error 覆盖四套机制的全部落态。
  const WORK_ITEM_STATUS_LABEL = {
    pending: ["待启动", ""],
    running: ["运行中", "tag-run"],
    done: ["已完成", "tag-good"],
    exhausted: ["已耗尽", "tag-warn"],
    error: ["出错", "tag-warn"],
  };
  function fmtWorkItemStatus(status) {
    return WORK_ITEM_STATUS_LABEL[status] || [status || "-", ""];
  }

  function openWorkItemsView() {
    $("app-view").classList.add("hidden");
    $("work-items-view").classList.remove("hidden");
    showWorkItemsList();
  }
  function closeWorkItemsView() {
    $("work-items-view").classList.add("hidden");
    $("app-view").classList.remove("hidden");
  }

  async function showWorkItemsList() {
    const body = $("work-items-body");
    body.innerHTML = '<div class="entity-loading">加载中…</div>';
    const origin = $("work-items-origin").value;
    const status = $("work-items-status").value;
    const qs = new URLSearchParams();
    if (origin) qs.set("origin", origin);
    if (status) qs.set("status", status);
    qs.set("limit", "50");
    try {
      const items = await api("/api/work_items?" + qs.toString());
      body.innerHTML = "";
      if (!items.length) {
        body.innerHTML = '<div class="entity-empty"><div class="empty-emoji">🗂</div><div>没有任务运行记录</div><div class="empty-sub">换个筛选条件，或等四套机制发起后再看</div></div>';
        return;
      }
      const ul = el("ul", "entity-list");
      for (const it of items) {
        const [label, cls] = fmtWorkItemStatus(it.status);
        const li = el("li");
        if (it.status === "error") li.classList.add("error");
        const head = el("div", "e-head");
        head.appendChild(el("span", "e-name", escapeHtml((it.summary || "").slice(0, 60) || "（无摘要）")));
        head.appendChild(el("span", "e-tag " + cls, escapeHtml(label)));
        li.appendChild(head);
        li.appendChild(el("div", "e-desc",
          `${escapeHtml(WORK_ITEM_ORIGIN_LABEL[it.origin] || it.origin || "-")} · ${escapeHtml(it.topology || "-")} · ${escapeHtml(fmtTime(it.created_at))}`));
        li.style.cursor = "pointer";
        li.onclick = () => openWorkItemDetail(it.id);
        ul.appendChild(li);
      }
      body.appendChild(ul);
    } catch (e) {
      body.innerHTML = `<div class="entity-empty">加载失败：${escapeHtml(e.message)}</div>`;
    }
  }

  async function openWorkItemDetail(wid) {
    const body = $("work-items-body");
    body.innerHTML = '<div class="entity-loading">加载中…</div>';
    try {
      const it = await api("/api/work_items/" + encodeURIComponent(wid));
      renderWorkItemDetail(it);
    } catch (e) {
      body.innerHTML = `<div class="entity-empty">加载失败：${escapeHtml(e.message)}</div>`;
    }
  }

  function renderWorkItemDetail(it) {
    const body = $("work-items-body");
    body.innerHTML = "";
    const back = el("button", "mini-btn", "← 返回列表");
    back.style.marginBottom = "10px";
    back.onclick = showWorkItemsList;
    body.appendChild(back);

    const [label, cls] = fmtWorkItemStatus(it.status);
    const ul = el("ul", "entity-list");
    const li = el("li");
    if (it.status === "error") li.classList.add("error");
    const head = el("div", "e-head");
    head.appendChild(el("span", "e-name", escapeHtml((it.summary || "").slice(0, 80) || "（无摘要）")));
    head.appendChild(el("span", "e-tag " + cls, escapeHtml(label)));
    li.appendChild(head);
    li.appendChild(el("div", "e-desc",
      `来源：${escapeHtml(WORK_ITEM_ORIGIN_LABEL[it.origin] || it.origin || "-")} · 拓扑：${escapeHtml(it.topology || "-")}`));
    li.appendChild(el("div", "e-desc",
      `隔离：${escapeHtml(it.isolation || "-")} · 验收：${escapeHtml(it.verify_mode || "-")}`));
    li.appendChild(el("div", "e-desc",
      `创建：${escapeHtml(fmtTime(it.created_at))} · 更新：${escapeHtml(fmtTime(it.updated_at))}`));
    ul.appendChild(li);
    body.appendChild(ul);

    // 关联来源摘要：后端按 origin 尽力反查，related 为 null 表示无关联或反查失败。
    body.appendChild(el("div", "exp-sub-head", "关联来源"));
    renderWorkItemRelated(body, it.related);
  }

  function renderWorkItemRelated(body, related) {
    if (!related) {
      body.appendChild(el("div", "entity-empty", "无法关联到具体来源"));
      return;
    }
    const ul = el("ul", "entity-list");
    if (related.kind === "schedule") {
      // 目标循环 / 分流：进度快照。
      const [label, cls] = fmtGoalStatus(related.goal_status);
      const li = el("li");
      const head = el("div", "e-head");
      head.appendChild(el("span", "e-name", "循环进度"));
      head.appendChild(el("span", "e-tag " + cls, escapeHtml(label)));
      if (!related.enabled) head.appendChild(el("span", "e-tag tag-warn", "已停用"));
      li.appendChild(head);
      li.appendChild(el("div", "e-desc",
        `迭代 ${related.iter_count || 0}/${related.max_iterations || 10}`));
      ul.appendChild(li);
    } else if (related.kind === "dispatch_subtasks") {
      // 智能分派：子任务清单，复用子任务判定状态徽章。
      const subs = related.subtasks || [];
      if (!subs.length) {
        body.appendChild(el("div", "entity-empty", "没有子任务"));
        return;
      }
      for (const st of subs) {
        const li = el("li");
        if (st.status === "error") li.classList.add("error");
        const head = el("div", "e-head");
        head.appendChild(el("span", "e-name", escapeHtml(st.title || "（无标题）")));
        const sm = DISPATCH_STATUS_LABEL[st.status];
        if (sm) head.appendChild(el("span", "e-tag " + sm[1], escapeHtml(sm[0])));
        li.appendChild(head);
        if (st.session_status) li.appendChild(el("div", "e-desc", escapeHtml("子会话：" + st.session_status)));
        ul.appendChild(li);
      }
    } else if (related.kind === "arbitration") {
      // 背对背仲裁：结论。
      const li = el("li");
      const head = el("div", "e-head");
      head.appendChild(el("span", "e-name", "仲裁结论"));
      head.appendChild(el("span", "e-tag", escapeHtml(related.status || "-")));
      li.appendChild(head);
      if (related.verdict) li.appendChild(el("div", "e-desc", escapeHtml(related.verdict)));
      ul.appendChild(li);
    } else {
      body.appendChild(el("div", "entity-empty", "无法关联到具体来源"));
      return;
    }
    body.appendChild(ul);
  }

  $("open-work-items-btn").onclick = openWorkItemsView;
  $("work-items-back").onclick = closeWorkItemsView;
  $("work-items-refresh").onclick = showWorkItemsList;
  $("work-items-origin").onchange = showWorkItemsList;
  $("work-items-status").onchange = showWorkItemsList;


  // SwanLab iframe 面板
  const SWANLAB_DEFAULT = "@Speech_Model/zhihangxu_ct_exp";
  let swanlabLoadTimer = null;
  let swanlabLoadSeq = 0;
  let swanlabSessionAbort = null;

  function setSwanlabStatus(kind, message) {
    const status = $("swanlab-status");
    status.classList.toggle("hidden", kind === "ready");
    status.classList.toggle("error", kind === "error");
    $("swanlab-status-text").textContent = message || "";
    $("swanlab-retry-btn").classList.toggle("hidden", kind !== "error");
  }

  function clearSwanlabTimer() {
    if (swanlabLoadTimer) {
      clearTimeout(swanlabLoadTimer);
      swanlabLoadTimer = null;
    }
  }

  function resetSwanlabFrame() {
    swanlabLoadSeq++;
    clearSwanlabTimer();
    if (swanlabSessionAbort) {
      swanlabSessionAbort.abort();
      swanlabSessionAbort = null;
    }
    $("swanlab-frame").src = "about:blank";
    setSwanlabStatus("loading", "正在连接 SwanLab…");
  }

  async function clearSwanlabSessionCookie(timeoutMs = 1000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      await fetch(BASE + "/api/swanlab/session/cookie", {
        method: "DELETE",
        keepalive: true,
        signal: controller.signal,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ public_base: BASE }),
      });
    } catch (_) {
      // best effort：即使链路已断，也必须继续完成本地 logout/关闭。
    } finally {
      clearTimeout(timer);
    }
  }

  async function loadSwanlab(path) {
    path = (path || "").trim();
    const seq = ++swanlabLoadSeq;
    clearSwanlabTimer();
    if (!path) {
      setSwanlabStatus("error", "请输入 SwanLab 项目路径");
      return;
    }
    const frame = $("swanlab-frame");
    const goBtn = $("swanlab-go-btn");
    if (swanlabSessionAbort) swanlabSessionAbort.abort();
    const sessionAbort = new AbortController();
    swanlabSessionAbort = sessionAbort;
    setSwanlabStatus("loading", "正在建立安全会话…");
    goBtn.disabled = true;
    frame.src = "about:blank";
    try {
      const session = await api("/api/swanlab/session", {
        method: "POST",
        retry: true,
        timeoutMs: 15000,
        signal: sessionAbort.signal,
        body: JSON.stringify({ path, public_base: BASE }),
      });
      if (seq !== swanlabLoadSeq) return;
      if (!session || !session.url) throw new Error("服务端未返回 SwanLab 地址");
      setSwanlabStatus("loading", "正在加载 SwanLab 项目…");
      frame.src = session.url;
      swanlabLoadTimer = setTimeout(() => {
        if (seq !== swanlabLoadSeq) return;
        setSwanlabStatus("error", "SwanLab 在 20 秒内未完成加载，请重试或检查服务状态。");
      }, 20000);
    } catch (e) {
      if (seq === swanlabLoadSeq) {
        setSwanlabStatus("error", "无法打开 SwanLab：" + e.message);
      }
    } finally {
      if (swanlabSessionAbort === sessionAbort) swanlabSessionAbort = null;
      if (seq === swanlabLoadSeq) goBtn.disabled = false;
    }
  }

  function openSwanlab() {
    $("app-view").classList.add("hidden");
    $("swanlab-view").classList.remove("hidden");
    const input = $("swanlab-url-input");
    if (!input.value.trim()) input.value = SWANLAB_DEFAULT;
    loadSwanlab(input.value);
  }
  function closeSwanlab() {
    resetSwanlabFrame();
    $("swanlab-view").classList.add("hidden");
    $("app-view").classList.remove("hidden");
    clearSwanlabSessionCookie(1000);
  }
  $("open-swanlab-btn").onclick = openSwanlab;
  $("swanlab-back").onclick = closeSwanlab;
  $("swanlab-go-btn").onclick = () => loadSwanlab($("swanlab-url-input").value);
  $("swanlab-retry-btn").onclick = () => loadSwanlab($("swanlab-url-input").value);
  $("swanlab-url-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter") $("swanlab-go-btn").click();
  });
  window.addEventListener("message", (event) => {
    const frame = $("swanlab-frame");
    if (
      event.origin !== location.origin
      || event.source !== frame.contentWindow
      || !event.data
      || typeof event.data !== "object"
    ) return;
    if (event.data.type === "swanlab-ready") {
      clearSwanlabTimer();
      setSwanlabStatus("ready", "");
    } else if (event.data.type === "swanlab-error") {
      clearSwanlabTimer();
      setSwanlabStatus("error", event.data.message || "SwanLab 页面加载失败，请重试。");
    }
  });
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

  const apiBase = () => manage.kind === "memory" ? "/api/memory" : manage.kind === "snippets" ? "/api/snippets" : manage.kind === "skills" ? "/api/skills" : "/api/agents";

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
    if (manage.kind === "artifacts") {
      const newBtn = document.getElementById('manage-new');
      if (newBtn) newBtn.classList.add('hidden');
      return showArtifactList();
    }
    try {
      const items = await api(apiBase());
      listEl.innerHTML = "";
      if (!items.length) {
        const kindMap = { memory: ["记忆", "🧠"], agent: ["子智能体", "🤖"], skills: ["技能", "🧩"], snippets: ["快捷指令", "⚡"] };
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
    if (s.kind === "goal") return `目标循环 (${s.iter_count || 0}/${s.max_iterations || 10})`;
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
        if (it.kind === "goal" && it.goal_status) {
          head.appendChild(el("span", "e-tag", it.goal_status));
        }
        li.appendChild(head);
        li.appendChild(el("div", "e-desc", escapeHtml((it.prompt || "").slice(0, 80))));
        const previewText = it.kind === "goal"
          ? `会话：${escapeHtml(sess ? sess.title : "(已删除)")} · 迭代 ${it.iter_count || 0}/${it.max_iterations || 10} · 状态：${escapeHtml(it.goal_status || "-")}`
          : `会话：${escapeHtml(sess ? sess.title : "(已删除)")} · 下次：${fmtTs(it.next_run)}`;
        li.appendChild(el("div", "e-preview", previewText));
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
    let d = { id: null, session_id: state.sessionId || (state.sessions[0] || {}).id || "", prompt: "", kind: "interval", interval_min: 60, at_hhmm: "09:00", stop_condition: "", max_iterations: 10, enabled: 1 };
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
          <option value="goal" ${d.kind === "goal" ? "selected" : ""}>目标循环（自迭代直到达成）</option>
        </select>
      </label>
      <label id="sf-interval-wrap">间隔分钟数
        <input id="sf-interval" type="number" min="1" value="${escapeAttr(String(d.interval_min || 60))}" />
      </label>
      <label id="sf-daily-wrap">每天几点（HH:MM，24小时制）
        <input id="sf-hhmm" value="${escapeAttr(d.at_hhmm || "09:00")}" placeholder="09:00" />
      </label>
      <label id="sf-stop-wrap">完成标准（自然语言，验收员据此判断是否达成）
        <textarea id="sf-stop" class="tall" placeholder="例：README 里新增「快速开始」章节，且 pytest 全部通过">${escapeHtml(d.stop_condition || "")}</textarea>
      </label>
      <label id="sf-maxiter-wrap">最大迭代轮数（1-100）
        <input id="sf-maxiter" type="number" min="1" max="100" value="${escapeAttr(String(d.max_iterations || 10))}" />
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
      form.querySelector("#sf-stop-wrap").style.display = k === "goal" ? "" : "none";
      form.querySelector("#sf-maxiter-wrap").style.display = k === "goal" ? "" : "none";
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
      if (body.kind === "goal") {
        body.stop_condition = form.querySelector("#sf-stop").value.trim();
        body.max_iterations = parseInt(form.querySelector("#sf-maxiter").value, 10);
      }
      const errEl = form.querySelector("#f-err");
      if (!body.prompt) { errEl.textContent = "指令不能为空"; return; }
      if (body.kind === "goal" && !body.stop_condition) { errEl.textContent = "完成标准不能为空"; return; }
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
      : manage.kind === "skills" ? skillFormHtml(data, isNew)
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

  function skillFormHtml(d, isNew) {
    return `
      <label>名称（创建后不可改）
        <input id="f-name" value="${escapeAttr(d.name)}" ${isNew ? "" : "disabled"} placeholder="例：devops-team" />
      </label>
      <label>描述（何时触发此技能）
        <input id="f-desc" value="${escapeAttr(d.description)}" placeholder="一句话说明用途与触发场景" />
      </label>
      <label>正文（SKILL.md 主体，网页里发 /名称 即注入为 prompt）
        <textarea id="f-body" class="tall" placeholder="技能的编排指令 / 提示词…">${escapeHtml(d.body || "")}</textarea>
      </label>
      ${!isNew && d.files && d.files.length ? `<div class="agents-sub">其他文件：${escapeHtml(d.files.join(", "))}</div>` : ""}
      <div class="form-err" id="f-err"></div>
      <div class="form-actions">
        <button type="button" class="cancel">取消</button>
        <button type="submit" class="save">保存</button>
      </div>`;
  }

  function agentFormHtml(d, isNew) {    return `
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
    } else if (manage.kind === "skills") {
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
      const hasCleanable = items.some(it => it.status === "done" || it.status === "cancelled");
      listEl.innerHTML = "";
      if (hasCleanable) {
        const cleanBtn = el("button", "btn-sm danger", "🧹 一键清理");
        cleanBtn.onclick = async () => {
          const n = items.filter(it => it.status === "done" || it.status === "cancelled").length;
          if (!(await confirmDialog(`确定归档 ${n} 项已完成/已取消的待办？`))) return;
          try {
            const res = await api("/api/todos/bulk_cleanup", {
              method: "POST",
              body: JSON.stringify({ scope: "archive_finished" }),
            });
            toast(`已清理 ${res.affected} 项`, "success");
            showTodoList();
          } catch (e) {
            toast("清理失败：" + e.message, "error");
          }
        };
        listEl.appendChild(cleanBtn);
      }
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
    let items;
    try {
      items = await api("/api/memos");
    } catch (e) {
      listEl.innerHTML = `<div class="entity-empty">加载失败：${escapeHtml(e.message)}</div>`;
      return;
    }
    listEl.innerHTML = "";

    const active = items.filter(m => m.status !== "done");
    const done = items.filter(m => m.status === "done");

    if (!active.length && !done.length) {
      listEl.innerHTML = '<div class="entity-empty"><div class="empty-emoji">📝</div><div>暂无备忘</div><div class="empty-sub">点右上角「+ 新建」记录要提醒的事情</div></div>';
      return;
    }

    const today = new Date();
    const todayStr = today.getFullYear() + "-" +
      String(today.getMonth() + 1).padStart(2, "0") + "-" +
      String(today.getDate()).padStart(2, "0");

    function renderMemoItem(it, isDone) {
      const li = el("li");
      const head = el("div", "e-head");
      head.appendChild(el("span", "e-name", escapeHtml(it.content)));

      // 今日已提醒高亮
      if (it.last_reminded_at) {
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
      if (!isDone) {
        const doneBtn = el("button", "btn-sm memo-done-btn", "✓ 完成");
        doneBtn.onclick = async () => {
          try {
            await api(`/api/memos/${it.id}`, { method: "PUT", body: JSON.stringify({ status: "done" }) });
            showMemoList();
            refreshMemoBadge();
          } catch (e) { toast("操作失败：" + e.message, "error"); }
        };
        const editBtn = el("button", "btn-sm", "编辑");
        editBtn.onclick = () => showMemoForm(it.id);
        actions.append(doneBtn, editBtn);
      }
      const delBtn = el("button", "btn-sm danger", "🗑");
      delBtn.onclick = async () => {
        if (!(await confirmDialog("确认删除这条备忘？", { okText: "删除", danger: true }))) return;
        try {
          await api(`/api/memos/${it.id}`, { method: "DELETE" });
          showMemoList();
          refreshMemoBadge();
        } catch (e) { toast("删除失败：" + e.message, "error"); }
      };
      actions.appendChild(delBtn);
      li.appendChild(actions);
      return li;
    }

    // 渲染 active 组
    active.forEach(it => listEl.appendChild(renderMemoItem(it, false)));

    // 渲染 done 组（折叠）
    if (done.length) {
      const toggle = document.createElement("div");
      toggle.className = "memo-done-toggle";
      let expanded = false;
      toggle.textContent = "已完成 " + done.length + " 条 ▾";

      const doneBody = document.createElement("div");
      doneBody.className = "memo-done-body";
      doneBody.style.display = "none";
      done.forEach(it => doneBody.appendChild(renderMemoItem(it, true)));

      toggle.onclick = () => {
        expanded = !expanded;
        doneBody.style.display = expanded ? "" : "none";
        toggle.textContent = "已完成 " + done.length + " 条 " + (expanded ? "▴" : "▾");
      };

      listEl.append(toggle, doneBody);
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

    // 内容输入
    const contentInput = el("textarea");
    contentInput.placeholder = "记录要提醒的事情…";
    contentInput.className = "form-input";
    contentInput.rows = 4;
    contentInput.value = memo.content || "";

    // 提醒模式 chip
    const chipLabel = el("div", "form-label", "提醒方式");
    const chipDefs = [
      { label: "每天", value: "daily" },
      { label: "每周", value: "weekly" },
      { label: "每月", value: "monthly" },
      { label: "单次", value: "once" },
      { label: "截止日", value: "deadline" },
      { label: "不提醒", value: "none" },
    ];
    let currentMode = memo.remind_mode || "daily";

    const chipRow = document.createElement("div");
    chipRow.className = "memo-chip-row";

    const chipBtns = chipDefs.map(def => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "memo-chip" + (def.value === currentMode ? " active" : "");
      btn.textContent = def.label;
      btn.onclick = () => {
        chipBtns.forEach(c => c.classList.remove("active"));
        btn.classList.add("active");
        currentMode = def.value;
        renderParams();
      };
      chipRow.appendChild(btn);
      return btn;
    });

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

    // 快捷日期按钮
    function makeDateQuick() {
      const row = document.createElement("div");
      row.className = "memo-date-quick";
      const now = new Date();
      function localDate(d) {
        return d.getFullYear() + "-" +
          String(d.getMonth() + 1).padStart(2, "0") + "-" +
          String(d.getDate()).padStart(2, "0");
      }
      const today = new Date(now);
      const tomorrow = new Date(now); tomorrow.setDate(now.getDate() + 1);
      const weekend = new Date(now);
      const daysToSun = (7 - now.getDay()) % 7 || 7;
      weekend.setDate(now.getDate() + daysToSun);

      [["今天", localDate(today)], ["明天", localDate(tomorrow)], ["本周末", localDate(weekend)]].forEach(([label, val]) => {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "memo-chip";
        btn.textContent = label;
        btn.onclick = () => { dateInput.value = val; };
        row.appendChild(btn);
      });
      return row;
    }

    // 回填已有值
    if (memo.remind_mode === "weekly") weekSelect.value = String(memo.remind_at || "0");
    if (memo.remind_mode === "monthly") daySelect.value = String(memo.remind_at || "1");
    if (memo.remind_mode === "once" || memo.remind_mode === "deadline") dateInput.value = memo.remind_at || "";
    daysInput.value = String(memo.remind_days_before || 0);

    function renderParams() {
      paramBox.innerHTML = "";
      if (currentMode === "weekly") paramBox.appendChild(weekSelect);
      else if (currentMode === "monthly") paramBox.appendChild(daySelect);
      else if (currentMode === "once") { paramBox.appendChild(makeDateQuick()); paramBox.appendChild(dateInput); }
      else if (currentMode === "deadline") { paramBox.appendChild(makeDateQuick()); paramBox.appendChild(dateInput); paramBox.appendChild(daysLabel); paramBox.appendChild(daysInput); }
    }
    renderParams();

    const saveBtn = el("button", "btn-primary", "保存");
    saveBtn.onclick = async () => {
      const content = contentInput.value.trim();
      if (!content) { toast("备忘内容不能为空", "error"); return; }
      let remind_at = "";
      if (currentMode === "weekly") remind_at = weekSelect.value;
      else if (currentMode === "monthly") remind_at = daySelect.value;
      else if (currentMode === "once" || currentMode === "deadline") remind_at = dateInput.value;
      if ((currentMode === "once" || currentMode === "deadline") && !remind_at) {
        toast("请选择日期", "error"); return;
      }
      const remind_days_before = currentMode === "deadline" ? (parseInt(daysInput.value, 10) || 0) : 0;
      const remind_enabled = currentMode === "none" ? 0 : 1;
      const payload = { content, remind_enabled, remind_mode: currentMode, remind_at, remind_days_before };
      try {
        if (id) {
          await api(`/api/memos/${id}`, { method: "PUT", body: JSON.stringify(payload) });
        } else {
          await api("/api/memos", { method: "POST", body: JSON.stringify(payload) });
        }
        toast(id ? "已保存" : "备忘已添加", "success");
        showMemoList();
        refreshMemoBadge();
      } catch (e) { toast("保存失败：" + e.message, "error"); }
    };

    const cancelBtn = el("button", "btn-sm", "取消");
    cancelBtn.onclick = () => showMemoList();

    form.append(contentInput, chipLabel, chipRow, paramBox, saveBtn, cancelBtn);
    listEl.appendChild(form);
    setTimeout(() => contentInput.focus(), 50);
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

  async function showArtifactList() {
    const list = document.getElementById('manage-list');
    if (!list) return;
    list.innerHTML = '<div class="loading">加载中…</div>';
    let items;
    try {
      items = await api('/api/artifacts');
    } catch (e) {
      list.innerHTML = '<div class="empty-hint">加载失败，请重试</div>';
      return;
    }
    if (!items || !items.length) {
      list.innerHTML = '<div class="empty-hint">暂无产出物</div>';
      return;
    }
    list.innerHTML = '';
    items.forEach(it => {
      const card = document.createElement('div');
      card.className = 'entity-card';
      card.style.cursor = 'pointer';
      const title = document.createElement('div');
      title.className = 'e-name';
      if (it.favicon) title.textContent = it.favicon + ' ' + (it.title || it.url);
      else title.textContent = it.title || it.url;
      const desc = document.createElement('div');
      desc.className = 'e-desc';
      desc.textContent = it.description || '';
      const meta = document.createElement('div');
      meta.className = 'e-meta';
      const ts = it.published_at || it.created_at;
      meta.textContent = ts ? new Date(ts * 1000).toLocaleString('zh-CN') : '';
      card.appendChild(title);
      if (it.description) card.appendChild(desc);
      card.appendChild(meta);
      card.onclick = () => window.open(it.url, '_blank', 'noopener,noreferrer');
      list.appendChild(card);
    });
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

  function el(tag, cls, html) { const e = document.createElement(tag); if (cls) e.className = cls; if (html != null) e.innerHTML = html; return e; }

  // ---------------- 产物预览 ----------------
  function fileUrl(path, download) {
    return `${BASE}/api/file?session_id=${encodeURIComponent(state.sessionId)}&path=${encodeURIComponent(path)}` +
      (download ? "&download=1" : "") +
      (state.token ? `&token=${encodeURIComponent(state.token)}` : "");
  }
  // 从文本里抽取图片路径，插到提及该图片的段落/标题后面；找不到锚点则 append 到末尾
  function attachArtifacts(node, text) {
    if (!text) return;
    // 跳过已在气泡内联渲染的路径（避免重复：内联 img + artifact 缩略图）
    const alreadyInline = new Set(
      Array.from(node.querySelectorAll('img.msg-img')).map(i => i.alt).filter(Boolean)
    );
    const re = /([~/\w.\-]+\.(?:png|jpe?g|gif|svg|webp|bmp))/gi;
    const paths = [];
    const seen = new Set();
    let m;
    while ((m = re.exec(text)) && seen.size < 6) {
      const p = m[1];
      if (p.length < 5 || seen.has(p) || alreadyInline.has(p)) continue;
      seen.add(p);
      paths.push(p);
    }
    if (!paths.length) return;

    // bubble 元素（node 是 .msg.assistant，bubble 是其内第一个 .bubble.markdown）
    const bubble = node.querySelector('.bubble.markdown') || node;

    for (const p of paths) {
      const fname = p.split('/').pop();  // 只取文件名用于段落匹配
      const img = el('img', 'artifact-thumb');
      img.loading = 'lazy';
      img.src = fileUrl(p);
      img.alt = p;
      img.onerror = () => { img.remove(); };
      img.onclick = () => openImageViewer(fileUrl(p), p);

      // 在 bubble 内找第一个文本含文件名（或路径）的块级元素，插到其后
      const blocks = Array.from(bubble.querySelectorAll('p, h1, h2, h3, li, td'));
      const anchor = blocks.find(el => el.textContent.includes(fname) || el.textContent.includes(p));
      if (anchor) {
        anchor.insertAdjacentElement('afterend', img);
      } else {
        // 没找到锚点：append 到 bubble 末尾
        bubble.appendChild(img);
      }
    }
  }
  // 全屏看大图
  function openImageViewer(src, caption) {
    const root = $("modal-root");
    root.innerHTML = "";
    const box = el("div", "img-viewer");
    const dlUrl = src.includes('?') ? src + '&download=1' : src + '?download=1';
    box.innerHTML = `<img src="${escapeAttr(src)}" alt="" />
      <div class="img-cap">${escapeHtml(caption || "")}</div>
      <a class="img-dl" href="${escapeAttr(dlUrl)}" download>下载</a>`;
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

  // 子智能体卡片头部汇总元信息：模型名 · 时长 · token 消耗 · 工具调用次数
  function fmtAgentDuration(ms) {
    if (ms == null) return "";
    const s = ms / 1000;
    if (s < 60) return s.toFixed(1) + "s";
    if (s < 3600) {
      const m = Math.floor(s / 60);
      const sec = Math.floor(s % 60);
      return m + "m" + String(sec).padStart(2, "0") + "s";
    }
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    return h + "h" + String(m).padStart(2, "0") + "m";
  }

  function fmtAgentTokens(n) {
    if (n == null) return "";
    if (n >= 1000) return (n / 1000).toFixed(1) + "k tok";
    return n + " tok";
  }

  // 用 dataset.model 作为模型名的单一事实来源：subagent_done 无模型字段时保留已有值。
  function updateSubagentMeta(card, meta) {
    if (!meta) return;
    const span = card.querySelector(".subagent-meta");
    if (!span) return;
    if (meta.model) span.dataset.model = meta.model;  // resolvedModel 比启动时配置更权威
    const parts = [];
    if (span.dataset.model) parts.push(modeLabel(span.dataset.model));
    const dur = fmtAgentDuration(meta.duration_ms);
    if (dur) parts.push(dur);
    const tok = fmtAgentTokens(meta.tokens);
    if (tok) parts.push(tok);
    if (meta.tool_uses != null) parts.push(meta.tool_uses + " tools");
    span.textContent = parts.join(" · ");
  }

  function buildMessageNode(role, content, ts = null, interactive = true) {
    let node;
    if (role === "user" || role === "assistant") {
      node = el("div", "msg " + role);
      const bubble = el("div", "bubble");
      if (role === "assistant") {
        bubble.classList.add("markdown");
        // 文件路径 HTML：走 /api/preview 内联沙箱预览；整页 HTML 文档：直接内联沙箱预览
        const _t = content.text || "";
        const _hp = htmlFilePath(_t);
        if (_hp) {
          bubble.innerHTML = htmlSrcEmbedBlock(_hp);
        } else if (looksLikeHtmlDoc(_t)) {
          bubble.innerHTML = htmlEmbedBlock(_t);
        } else {
          bubble.innerHTML = renderMarkdown(_t);
        }
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
        const _ut = content.text || "";
        const _uhp = htmlFilePath(_ut);
        if (_uhp) {
          // 路径消息：显示为文件名片，预览由 assistant 侧渲染
          const fname = _uhp.split("/").pop();
          bubble.classList.add("html-path-chip");
          bubble.innerHTML = `<span class="html-path-icon">📄</span><span class="html-path-name">${escapeHtml(fname)}</span><span class="html-path-full">${escapeHtml(_uhp)}</span>`;
        } else {
          bubble.textContent = _ut;
        }
        // user 消息重发按钮：点击把内容填回输入框
        const resend = el("button", "msg-resend", "重发");
        resend.type = "button";
        resend.onclick = () => { input.value = _ut; input.dispatchEvent(new Event("input")); input.focus(); };
        node.appendChild(resend);
      }
      node.appendChild(bubble);
      // 产物预览：扫描文本里的图片/文件路径，渲染缩略图/可点链接
      if (role === "assistant") attachArtifacts(node, content.text || "");
      node.appendChild(el("div", "msg-time", escapeHtml(fmtClock(ts || nowTs()))));
    } else if (role === "error") {
      node = el("div", "msg error");
      const emsg = content.message || "出错了";
      node.appendChild(el("div", "bubble", escapeHtml(emsg)));
      // 回合以「用户手动停止 / 取消 / 超时 / 卡死」结尾时，给一排快捷操作，省得用户自己打字续跑：
      // 「继续」复用发消息通道发一条“请继续”，「新建会话」按当前会话的引擎/目录/模型另起一个。
      if (interactive && /手动停止|已取消|超时|卡死|已终止/.test(emsg)) {
        const bar = el("div", "qr-bar");
        const contBtn = el("button", "qr-btn", "继续");
        contBtn.type = "button";
        contBtn.onclick = () => {
          input.value = "请继续";
          input.dispatchEvent(new Event("input"));
          send();
        };
        const newBtn = el("button", "qr-btn", "新建会话");
        newBtn.type = "button";
        newBtn.onclick = async () => {
          const cur = (state.sessions || []).find((s) => s.id === state.sessionId) || {};
          try {
            await createSession({ workdir: cur.workdir, mode: cur.mode, effort: cur.effort, engine: cur.engine });
            toast("已新建会话", "success", 1600);
          } catch (err) { toast("新建失败：" + err.message, "error"); }
        };
        bar.appendChild(contBtn);
        bar.appendChild(newBtn);
        node.appendChild(bar);
      }
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
            <span class="subagent-meta"></span>
          </div>
          <div class="subagent-body" style="${expanded ? "" : "display:none"}"></div>
          <div class="subagent-result" style="display:none">
            <div class="subagent-result-content"></div>
          </div>`;
        if (expanded) node.classList.add("open");
        // 启动时配置的模型名先落到卡片上；之后以 resolvedModel 更新（dataset.model 为单一事实来源）。
        const metaEl = node.querySelector(".subagent-meta");
        if (content.model && metaEl) {
          metaEl.dataset.model = content.model;
          metaEl.textContent = modeLabel(content.model);
        }
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
      if (content.agent_meta) {
        // 后端只给子智能体收尾 tool_result 塞 agent_meta（session_hub 按 tool_use_result.agentId
        // 判定），是确定性标记，条件必须严格：宽了会把普通工具结果误渲染成卡片。
        // 走到这说明对应 Agent 卡片（tool_use）不在当前渲染窗口（200 条尾部翻页，
        // developer 的 tool_use 与 tool_result 间隔可到 ~200 条），直接合成一张
        // 已完成卡片，让结果可见而不是折叠成普通工具结果块。
        const meta = content.agent_meta;
        node = el("div", "subagent");
        if (content.tool_use_id) node.dataset.agentId = content.tool_use_id;
        node.innerHTML = `<div class="subagent-head">
            <span class="sap-caret">▸</span>
            <span class="subagent-icon">🤖</span>
            <span class="subagent-title">${escapeHtml(String(meta.agent_type || "agent"))}</span>
            <span class="subagent-status done">✓ 完成</span>
            <span class="subagent-meta"></span>
          </div>
          <div class="subagent-body" style="display:none"></div>
          <div class="subagent-result" data-has-result="1" style="display:none">
            <div class="subagent-result-content markdown"></div>
          </div>`;
        const rcEl = node.querySelector(".subagent-result-content");
        if (rcEl) rcEl.innerHTML = renderMarkdown(String(content.output || ""));
        updateSubagentMeta(node, meta);
        // 结构对齐 Agent 卡片；内部步骤不在本窗口，body 恒空——展开时跳过空 body 的显隐。
        node.querySelector(".subagent-head").addEventListener("click", function() {
          const isOpen = node.classList.toggle("open");
          const bodyEl = node.querySelector(".subagent-body");
          if (bodyEl && bodyEl.children.length) bodyEl.style.display = isOpen ? "" : "none";
          const rbox = node.querySelector(".subagent-result");
          if (rbox && rbox.dataset.hasResult) rbox.style.display = isOpen ? "" : "none";
        });
      } else {
        node = el("details", "tool");
        if (content.is_error) node.open = true;  // 出错自动展开，方便排查
        const errCls = content.is_error ? " err" : "";
        node.innerHTML = `<summary><span class="tag${errCls}">结果${content.is_error ? " ✗" : ""}</span>
          <span class="summary-text">${escapeHtml(String(content.output || "").slice(0, 80))}</span></summary>
          <pre>${escapeHtml(String(content.output || ""))}</pre>`;
      }
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
      // API 4xx（典型：上下文顶格被网关 400 拒绝）以 role=result + is_error 落幕而非
      // role=error，上面 error 分支的快捷栏匹配不到。复用同一套 qr-bar 给自救入口：
      // 压缩上下文 / 新建会话。
      if (interactive && content.is_error && /API Error: 4\d\d/.test(content.result || "")) {
        const bar = el("div", "qr-bar");
        const compactBtn = el("button", "qr-btn", "压缩上下文");
        compactBtn.type = "button";
        compactBtn.onclick = () => requestCompact();
        const newBtn = el("button", "qr-btn", "新建会话");
        newBtn.type = "button";
        newBtn.onclick = async () => {
          const cur = (state.sessions || []).find((s) => s.id === state.sessionId) || {};
          try {
            await createSession({ workdir: cur.workdir, mode: cur.mode, effort: cur.effort, engine: cur.engine });
            toast("已新建会话", "success", 1600);
          } catch (err) { toast("新建失败：" + err.message, "error"); }
        };
        bar.appendChild(compactBtn);
        bar.appendChild(newBtn);
        node.appendChild(bar);
      }
    } else if (role === "system") {
      return null; // init 信息不展示
    } else if (role === "compact") {
      // 上下文压缩标记：居中分割线 + 灰字标签，点击展开摘要全文
      node = el("div", "msg-compact");
      const dividerText = (content && content.pre_tokens != null && content.post_tokens != null)
        ? `— 上下文已压缩 · ${content.pre_tokens}→${content.post_tokens} tokens —`
        : "— 以上上下文已压缩 —";
      const divider = el("div", "compact-divider", dividerText);
      const summary = el("div", "compact-summary");
      summary.textContent = (content && content.summary) || "";
      divider.onclick = () => node.classList.toggle("open");
      node.appendChild(divider);
      node.appendChild(summary);
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

  // 历史消息渲染包装：遇到 user 消息是 HTML 路径时，自动在其后补一条 assistant 预览节点
  function appendMsgWithPreview(parentEl, groups, role, content, ts) {
    appendMessageGrouped(parentEl, groups, role, content, ts);
    if (role === "user") {
      const _hp = htmlFilePath(content.text || "");
      if (_hp) {
        const previewNode = buildMessageNode("assistant", { text: _hp }, ts, false);
        if (previewNode) {
          previewNode.classList.add("msg-enter", "msg-in");
          parentEl.appendChild(previewNode);
        }
      }
    }
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
    // 2) 子智能体的最终结果：tool_result 的 tool_use_id 命中某张卡片 → 填入结果区并收尾。
    //    两级查找：先查 groups（常规路径），未命中再按 data-agent-id 查 DOM 兜底——
    //    Map 与 DOM 偶发脱节（重绘丢绑/跨窗口边界）时结果仍能落到真实卡片上，
    //    而不是被吞掉。已收尾的卡片不再覆盖，等价于下方 delete 的注销语义。
    if (role === "tool_result" && content.tool_use_id) {
      let card = null;
      const bodyEl = groups[content.tool_use_id];
      if (bodyEl) {
        card = bodyEl.closest(".subagent");
      } else {
        // parentEl 为游离 fragment（加载更早）时其内卡片不在 #chat 里，但那种路径 groups
        // 本身覆盖跨批次关联；这里只在已连接容器（实时 #chat）里兜底查。
        const root = (parentEl && parentEl.isConnected) ? parentEl : $("chat");
        const found = root.querySelector(`.subagent[data-agent-id="${CSS.escape(content.tool_use_id)}"]`);
        if (found && !found.querySelector(".subagent-result[data-has-result]")) card = found;
      }
      if (card) {
        const rc = card.querySelector(".subagent-result-content");
        const rbox = card.querySelector(".subagent-result");
        if (rc) { rc.classList.add("markdown"); rc.innerHTML = renderMarkdown(String(content.output || "")); }
        if (rbox) { rbox.dataset.hasResult = "1"; rbox.style.display = card.classList.contains("open") ? "" : "none"; }
        const status = card.querySelector(".subagent-status");
        if (status) { status.textContent = "✓ 完成"; status.classList.remove("running"); status.classList.remove("stale"); status.classList.add("done"); }
        if (content.agent_meta) updateSubagentMeta(card, content.agent_meta);
        delete groups[content.tool_use_id];  // 注销：卡片已收尾，后续同 id 不再归拢
        return;  // 结果已入卡片，不再平铺这条 tool_result
      }
    }
    // 3) 子智能体内部步骤：parent 命中某张卡片 → 追加到该卡片 body
    if (content.parent && groups[content.parent]) {
      const node = buildMessageNode(role, content, ts, false);
      if (node) groups[content.parent].appendChild(node);
      return;
    }
    // 4) 其余：正常平铺到父容器
    // result 的 API 4xx 自救栏只在实时通道可交互；历史重放仍保持 interactive=false。
    const node = buildMessageNode(role, content, ts, role === "result" && groups === state.agentGroups);
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

  const TOOL_GROUP_MIN = 2;

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
    // 优先用当前会话的进度快照反推起始时间（切后台/刷新后计时不归零）；没有快照时退回 Date.now()。
    const curSess = (state.sessions || []).find((s) => s.id === state.sessionId);
    const t0 = (curSess && typeof curSess.elapsed === "number" && curSess.elapsed > 0)
      ? Date.now() - curSess.elapsed * 1000
      : Date.now();
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
    // 流式进行中不校验（会打断打字机）；否则静默核对历史。
    // 内容没变化时完全不碰 DOM；有漏消息才更新，并尽量保持原阅读位置。
    if (!state.streamEl) loadHistory({ silent: true, preserveScroll: true }).catch(() => {});
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
      if (data.role === "status") {
        // 纯瞬时状态提示（如 codex 续跑心跳）：toast 展示即可，不落聊天气泡、不动打字机状态。
        toast(data.content && data.content.text || "", "info", 6000);
        return;
      }
      if (data.role === "subagent_progress" || data.role === "subagent_done") {
        // 子智能体实时进度/完成：只刷新卡片头部元信息，不进历史快照（否则会触发整屏重绘）。
        const c = data.content || {};
        const bodyEl = state.agentGroups[c.parent];
        if (bodyEl) {
          const card = bodyEl.closest(".subagent");
          if (card) updateSubagentMeta(card, c);
        }
        return;
      }
      if (data.role === "assistant_delta") {
        // 子智能体增量：parent 命中某张卡片 → 追加到其 body，不碰全局 streamEl（顶层打字机）
        const pid = data.content.parent;
        if (pid && state.agentGroups[pid]) { appendSubagentDelta(pid, data.content.text || ""); return; }
        appendDelta(data.content.text || "");
        return;
      }
      // 除增量/瞬时状态外，message 事件均对应后端已落库的完整消息。
      recordWsHistory(data.role, data.content);
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
      if (data.status === "running") {
        // 新回合开始：清掉上一回合遗留的 elapsed，否则 showTyping 会拿上轮的旧值起算计时
        // （PROGRESS_FIRST_SEC 默认 300s，没到这个阈值前不会有 turn_progress 事件来刷新它）。
        const curSess = state.sessions.find((s) => s.id === state.sessionId);
        if (curSess) curSess.elapsed = 0;
        setRunning(true); updateRunBar({ elapsed: 0 }); showTyping();
      }
      else if (data.sync) {
        // 订阅时的状态对齐（非真实回合结束）：只解禁/复位按钮，不触发完成通知等副作用。
        // 修复：超长回合期间断线 → 回合后台跑完的 status:idle 被错过 → 重连卡在 running。
        setRunning(false); updateRunBar(null); hideTyping(); clearStream();
      } else { setRunning(false); updateRunBar(null); hideTyping(); clearStream(); loadTasks(); maybeNotify({ ...data.result, sessionId: state.sessionId }); document.querySelectorAll(".perm-overlay").forEach(o => o.remove()); _permQueue.length = 0; }
    } else if (data.type === "turn_done") {
      maybeNotify({ status: data.status, kind: data.kind, sessionId: data.session_id || state.sessionId });
      return;
    } else if (data.type === "turn_progress") {
      const mins = Math.max(1, Math.round((data.elapsed || 0) / 60));
      if (!data.session_id || data.session_id === state.sessionId) {
        const cur = state.sessions.find((s) => s.id === state.sessionId);
        if (cur) {
          cur.status = "running";
          cur.elapsed = data.elapsed;
          cur.stuck = !!data.stuck;
        }
        updateRunBar({ elapsed: data.elapsed, stuck: data.stuck, activity: cur && cur.activity });
      }
      toast(data.stuck ? `回合已运行 ${mins} 分钟，暂时没有新输出` : `回合仍在运行，已持续 ${mins} 分钟`, data.stuck ? "error" : "info", 6000);
      return;
    } else if (data.type === "error") {
      hideTyping();
      clearStream();
      renderMessage("error", { message: data.message });
      setRunning(false);
      updateRunBar(null);
      document.querySelectorAll(".perm-overlay").forEach(o => o.remove());
      _permQueue.length = 0;
    } else if (data.type === "queue_update") {
      state.queue = data.queue || [];
      renderQueue();
      return;
    } else if (data.type === "permission_request") {
      showPermissionDialog(data);
      return;
    } else if (data.type === "compacting") {
      toast("正在压缩上下文…", "info", 3000);
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
    const _finalText = fullText || state.streamText || "";
    const _hp = htmlFilePath(_finalText);
    if (_hp) {
      bubble.innerHTML = htmlSrcEmbedBlock(_hp);
    } else if (looksLikeHtmlDoc(_finalText)) {
      bubble.innerHTML = htmlEmbedBlock(_finalText);
    } else {
      bubble.innerHTML = renderMarkdown(_finalText);
    }
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
    // 若气泡仍停留在纯文本流式态（未经过 finalizeStream），补做 markdown 重渲染，
    // 否则本地音频等富文本控件永远不会出现（appendDelta 只写 textContent）。
    if (state.streamEl && state.streamText) {
      finalizeStream(state.streamText);
      return;
    }
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
    const cb = $("compact-btn");
    if (cb) cb.disabled = running;  // 回合进行中不可压缩
    const resumeBtn = $("act-resume");
    if (resumeBtn) resumeBtn.disabled = running;  // 运行中进程活着，无需恢复/重连
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
    // 压缩进行中：直接挡掉，不发 WS（此刻后端 is_running 为真，发出去只会被入队，语义混乱）
    if (state.compacting) { toast("压缩中，请稍候…", "info", 2000); return; }
    // /compact：真实上下文压缩，走独立接口而非正常发消息
    if (text === "/compact") {
      input.value = ""; input.style.height = "auto";
      $("char-count").classList.add("hidden");
      await requestCompact();
      return;
    }
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
    // 有待发附件：把路径拼进消息（tclaude 用 Read 读这些文件）
    if (imgs.length) {
      const lines = attachmentLines(imgs);
      text = text ? `${lines}\n${text}` : `${lines}\n请查看上面的附件。`;
    }
    // 运行中发送 → 服务端入队，不本地渲染气泡也不切运行态；靠 queue_update 广播刷新托盘。
    const queued = state.running;
    if (!queued) {
      renderMessage("user", { text });
      recordLiveHistory("user", { text }, { _liveLocal: true });
      // 如果是 HTML 文件路径，立即在 assistant 侧插入预览块（不等 agent 回复）
      const _previewPath = htmlFilePath(text);
      if (_previewPath) {
        renderMessage("assistant", { text: _previewPath, _htmlPreview: true });
      }
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
  const compactBtn = $("compact-btn");
  if (compactBtn) compactBtn.onclick = requestCompact;

  // 压缩上下文：概括整段对话、重置会话，摘要作前缀注入下一条消息续接。
  // compacting 提示与 compact 分割线由后端经 WS 广播，这里只负责发起与失败提示。
  async function requestCompact() {
    if (!state.sessionId) { toast("请先选择会话", "info", 1500); return; }
    if (state.running) { toast("回合进行中，稍后再压缩", "info", 2000); return; }
    if (state.compacting) return;  // 防重复触发
    // 压缩期间锁 UI：输入框/发送按钮/压缩按钮全禁用，避免用户在等待窗口发消息触发后端竞态。
    state.compacting = true;
    const inp = $("input");
    const sendBtn = $("send-btn");
    const cb = $("compact-btn");
    inp.disabled = true; sendBtn.disabled = true; if (cb) cb.disabled = true;
    const prevPlaceholder = inp.placeholder;
    inp.placeholder = "压缩中，请稍候…";
    try {
      // 原生 /compact 是一次真实回合。后端 COMPACT_TIMEOUT 默认 300s 超时并给出真实
      // 原因，前端放宽到 300s+20s 余量，保证用户先看到后端的诊断信息、不抢先中止。
      const res = await api(`/api/sessions/${state.sessionId}/compact`, { method: "POST", timeoutMs: 320000 });
      if (res && res.noop) {
        const noopMsg = {
          empty: "对话为空，无需压缩",
          resume_failed: "上下文已失效，已自动重置，请再发消息后重试压缩",
        }[res.reason] || "历史太短，无需压缩";
        toast(noopMsg, "info", 2000);
      }
    } catch (e) {
      // 409（回合进行中/会话为空等）与其它错误经 api() 抛出，统一 toast 出来。
      toast("压缩失败：" + e.message, "error");
    } finally {
      state.compacting = false;
      inp.disabled = false; sendBtn.disabled = false;
      inp.placeholder = prevPlaceholder;
      // 压缩按钮的可用性交回 setRunning 的统一口径（运行中禁用、空闲启用）。
      if (cb) cb.disabled = state.running;
    }
  }
  const memoQuickBtn = $("memo-quick-btn");
  if (memoQuickBtn) memoQuickBtn.onclick = openMemoQuickPanel;
  // 手机：回车换行；点发送按钮才发送。桌面：Enter 发送，Shift+Enter 换行
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !("ontouchstart" in window)) { e.preventDefault(); send(); }
  });
  $("cancel-btn").onclick = () => { if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send(JSON.stringify({ type: "cancel" })); };

  // 详情面板顶部/底部操作按钮
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
  // 背对背仲裁 / 分派子任务：用得少，收进「更多」面板，点⋯弹出选项
  $("detail-more-btn").onclick = () => {
    showActionSheet([
      { label: "⚖ 背对背仲裁", onClick: openArbitrationInput },
      { label: "🧭 分派子任务", onClick: openDispatchView },
    ]);
  };

  function showActionSheet(items) {
    const root = $("modal-root");
    root.innerHTML = "";
    const card = el("div", "modal-card sheet-card");
    card.innerHTML = items.map((it, i) =>
      `<button class="sheet-option" type="button" data-i="${i}">${escapeHtml(it.label)}</button>`
    ).join("") + `<button class="sheet-option sheet-cancel" type="button">取消</button>`;
    root.appendChild(card);
    root.classList.remove("hidden");
    requestAnimationFrame(() => root.classList.add("show"));
    const close = () => { root.classList.remove("show"); setTimeout(() => { root.classList.add("hidden"); root.innerHTML = ""; }, 200); };
    card.querySelectorAll(".sheet-option[data-i]").forEach((btn) => {
      btn.onclick = () => { close(); items[+btn.dataset.i].onClick(); };
    });
    card.querySelector(".sheet-cancel").onclick = close;
    root.onclick = (e) => { if (e.target === root) close(); };
  }

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
    // 附件按钮：支持任意文件（图片缩略图预览，其他显示图标+文件名）
    const fileBtn = $("file-btn");
    const fileInp = $("file-input");
    if (fileBtn && fileInp) {
      fileBtn.onclick = () => { if (!state.sessionId) { toast("请先选择会话", "info"); return; } fileInp.click(); };
      fileInp.onchange = async () => {
        const files = Array.from(fileInp.files || []);
        fileInp.value = "";  // 允许连续选同一文件
        for (const f of files) await uploadOneFile(f);
      };
    }
  }

  // 聊天区拖拽上传：拖入文件即上传为待发附件
  function initDrop() {
    const chatEl = $("chat") || $("app-view");
    if (!chatEl) return;
    chatEl.addEventListener("dragover", (e) => {
      e.preventDefault();
      chatEl.classList.add("drag-over");
    });
    chatEl.addEventListener("dragleave", (e) => {
      if (!chatEl.contains(e.relatedTarget)) chatEl.classList.remove("drag-over");
    });
    chatEl.addEventListener("drop", async (e) => {
      e.preventDefault();
      chatEl.classList.remove("drag-over");
      if (!state.sessionId) { toast("请先选择会话", "info"); return; }
      const files = Array.from(e.dataTransfer && e.dataTransfer.files || []);
      for (const f of files) await uploadOneFile(f);
    });
  }

  async function uploadOneImage(file) {
    if (!file || !file.type.startsWith("image/")) { toast("只能发图片", "error"); return; }
    if (file.size > 100 * 1024 * 1024) { toast("图片过大（上限 100MB）", "error"); return; }
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
      state.pendingImages.push({ path: r.abs || r.path, dataUrl, kind: "image", name: r.name || file.name });
      renderImageTray();
      toast("图片已就绪，可加文字一起发送", "success", 1800);
    } catch (e) {
      toast("上传失败：" + (e.message || e), "error", 3000);
    }
  }

  // 上传任意文件（含图片）到全局 UPLOAD_DIR，落库到指定 pending 数组并重渲染其 tray。
  // composer 用全局 state.pendingImages/#img-tray；仲裁/分派/目标循环弹窗传各自局部数组/tray，互不污染。
  async function uploadFileTo(file, pendingArr, trayEl) {
    if (!file) return;
    if (file.size > 100 * 1024 * 1024) { toast("文件过大（上限 100MB）", "error"); return; }
    const isImage = (file.type || "").startsWith("image/");
    const tip = toast("上传中…", "info", 8000);
    try {
      const dataUrl = await new Promise((res, rej) => {
        const fr = new FileReader();
        fr.onload = () => res(fr.result);
        fr.onerror = () => rej(fr.error);
        fr.readAsDataURL(file);
      });
      const b64 = String(dataUrl).split(",")[1] || "";
      const r = await api("/api/upload", {
        method: "POST",
        body: JSON.stringify({
          session_id: state.sessionId || "",
          file: b64,
          mime: file.type || "application/octet-stream",
          name: file.name,
        }),
      });
      pendingArr.push({
        path: r.abs || r.path,
        dataUrl: isImage ? dataUrl : null,
        kind: isImage ? "image" : "file",
        name: r.name || file.name,
      });
      renderTrayInto(pendingArr, trayEl);
      toast("文件已就绪，可加文字一起发送", "success", 1800);
    } catch (e) {
      toast("上传失败：" + (e.message || e), "error", 3000);
    }
  }

  // composer 专用：要求已选会话，落到全局 pending + #img-tray
  async function uploadOneFile(file) {
    if (!file) return;
    if (!state.sessionId) { toast("请先选择会话", "info"); return; }
    await uploadFileTo(file, state.pendingImages, $("img-tray"));
  }

  // 待发附件预览条：缩略图 + 删除（全量重建，保证与 pending 数组一致）
  function renderTrayInto(pendingArr, trayEl) {
    if (!trayEl) return;
    trayEl.innerHTML = "";
    if (!pendingArr.length) { trayEl.classList.add("hidden"); return; }
    pendingArr.forEach((im, idx) => {
      const del = () => { pendingArr.splice(idx, 1); renderTrayInto(pendingArr, trayEl); };
      if (im.kind === "image" || im.dataUrl) {
        const chip = el("div", "img-chip");
        const image = document.createElement("img");
        image.src = im.dataUrl;
        const x = el("button", "img-chip-del", "×");
        x.onclick = del;
        chip.append(image, x);
        trayEl.appendChild(chip);
      } else {
        const chip = el("div", "file-chip");
        const icon = el("span", null, "📄");
        const nameEl = el("span", "file-chip-name");
        nameEl.textContent = im.name || "文件";
        nameEl.title = im.name || "";
        const x = el("button", "img-chip-del", "×");
        x.onclick = del;
        chip.append(icon, nameEl, x);
        trayEl.appendChild(chip);
      }
    });
    trayEl.classList.remove("hidden");
  }

  function renderImageTray() { renderTrayInto(state.pendingImages, $("img-tray")); }

  // 把待发附件按 send() 的规则拼成前置文本行（图片：<path> / 文件：<name>（<path>）），空数组返回 ""。
  function attachmentLines(pendingArr) {
    if (!pendingArr || !pendingArr.length) return "";
    return pendingArr.map((im) =>
      im.kind === "image" || im.dataUrl
        ? `图片：${im.path}`
        : `文件：${im.name}（${im.path}）`
    ).join("\n");
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
  // 注入到预览 iframe 里的高度上报脚本，配合外层 message 监听实现自适应高度
  const _EMBED_REPORTER = "<scr"+"ipt>(function(){function p(){try{var h=Math.max(document.documentElement.scrollHeight,(document.body||{}).scrollHeight||0);parent.postMessage({__embedHeight:h},'*');}catch(e){}}window.addEventListener('load',p);window.addEventListener('resize',p);if(window.ResizeObserver){try{new ResizeObserver(p).observe(document.documentElement);}catch(e){}}p();setTimeout(p,300);setTimeout(p,1200);})();</scr"+"ipt>";
  // 判断一段文本是否是完整 HTML 文档（用于对话栏内联预览）
  function looksLikeHtmlDoc(text) {
    const t = String(text).trim();
    return /^<!doctype html/i.test(t) || /^<html[\s>]/i.test(t)
        || /^<svg[\s>]/i.test(t);
  }
  // 把原始 HTML 包成沙箱 iframe 预览块，附带「查看源码」切换
  function htmlEmbedBlock(rawHtml) {
    return `<div class="html-embed">
    <div class="cb-head"><span class="cb-lang">HTML 预览</span>
      <button class="html-embed-full" type="button" title="全屏">⛶ 全屏</button>
      <button class="html-embed-src" type="button">查看源码</button></div>
    <iframe sandbox="allow-scripts" srcdoc="${escapeAttr(rawHtml + _EMBED_REPORTER)}" loading="lazy"></iframe>
    <pre class="html-embed-source"><code>${escapeHtml(rawHtml)}</code></pre>
  </div>`;
  }
  // 预览接口 URL：把工作区内的 HTML 文件路径转成后端 /api/preview 地址
  function previewUrl(path) {
    return `${BASE}/api/preview?path=${encodeURIComponent(path)}` +
      (state.token ? `&token=${encodeURIComponent(state.token)}` : "");
  }
  const _HTML_FILE_RE = /^(\/[^\s'"(){}<>]+\.html?)$/i;
  // 判断一段文本是否就是一个绝对路径的 HTML 文件（用于文件预览）
  function htmlFilePath(text) {
    const t = String(text || "").trim();
    const m = _HTML_FILE_RE.exec(t);
    return m ? m[1] : null;
  }
  // 把 HTML 文件路径包成沙箱 iframe 预览块（走后端 /api/preview 加载）
  function htmlSrcEmbedBlock(path) {
    const fname = path.split("/").pop();
    return `<div class="html-embed">
    <div class="cb-head">
      <span class="cb-lang">📄 ${escapeHtml(fname)}</span>
      <span class="html-embed-path" title="${escapeAttr(path)}">${escapeHtml(path)}</span>
      <button class="html-embed-full" type="button" title="全屏">⛶</button>
    </div>
    <iframe sandbox="allow-scripts allow-same-origin allow-forms" src="${escapeAttr(previewUrl(path))}" loading="lazy"></iframe>
  </div>`;
  }
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
  const _LOCAL_IMG_RE = /^[~/][^\s]*\.(?:png|jpe?g|gif|svg|webp|bmp)$/i;
  const _LOCAL_AUDIO_RE = /^[~/][^\s]*\.(?:wav|mp3|flac|ogg|m4a|aac)$/i;
  function mdInline(text) {
    // text 已转义。处理行内：行内码 → 粗 → 斜 → 链接。
    // 行内码优先：先抠出来用占位符，避免里面的 * _ 被误解析
    const codes = [];
    text = text.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return ` ${codes.length - 1} `; });
    text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    text = text.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    text = text.replace(/(^|[^_\w])_([^_\n]+)_/g, "$1<em>$2</em>");
    // 行内图片 ![alt](url)：http/https 直接用，本地路径走 fileUrl API
    text = text.replace(/!\[([^\]]*)]\(([^)\s]+)\)/g, (m, alt, url) => {
      const clean = url.replace(/&amp;/g, "&");
      if (/^https?:\/\//.test(clean))
        return `<img class="msg-img" src="${escapeAttr(clean)}" alt="${alt}" loading="lazy" />`;
      if (_LOCAL_IMG_RE.test(clean))
        return `<img class="msg-img" src="${escapeAttr(fileUrl(clean))}" alt="${escapeAttr(alt || clean)}" loading="lazy" />`;
      return m;
    });
    text = text.replace(/(^|\s)(https?:\/\/[^\s)]+?\.(?:png|jpe?g|gif|webp)(?:\?[^\s)]*)?)(?=\s|$)/gi, (m, pre, url) => {
      const clean = url.replace(/&amp;/g, "&");
      return `${pre}<img class="msg-img" src="${escapeAttr(clean)}" alt="" loading="lazy" />`;
    });
    // 本地图片路径（绝对路径或 ~/ 开头），转成 API 地址内联渲染；路径后允许紧跟中英文标点
    text = text.replace(/(?<![/~\w.\-])([~/][^\s\uff0c\u3002\uff1a:!?\uff08\u3010\u300c\uff09\u3011\u300d,]*\.(?:png|jpe?g|gif|svg|webp|bmp))(?=[,\s\uff09\u3011\u300d\uff0c\u3002\uff1a:!?\uff08\u3010\u300c]|$)/gi, (m, p) => {
      const cleanPath = p.replace(/&amp;/g, '&');
      return `<img class="msg-img" src="${escapeAttr(fileUrl(cleanPath))}" alt="${escapeAttr(cleanPath)}" loading="lazy" />`;
    });
    // \u672c\u5730\u97f3\u9891\u8def\u5f84\uff08\u7edd\u5bf9\u8def\u5f84\u6216 ~/ \u5f00\u5934\uff09\uff0c\u8f6c\u6210 API \u5730\u5740\u5185\u8054\u6e32\u67d3\uff1b\u8def\u5f84\u540e\u5141\u8bb8\u7d27\u8ddf\u4e2d\u82f1\u6587\u6807\u70b9
    text = text.replace(/(?<![/~\w.\-])([~/][^\s\uff0c\u3002\uff1a:!?\uff08\u3010\u300c\uff09\u3011\u300d,]*\.(?:wav|mp3|flac|ogg|m4a|aac))(?=[,\s\uff09\u3011\u300d\uff0c\u3002\uff1a:!?\uff08\u3010\u300c]|$)/gi, (m, p) => {
      const cleanPath = p.replace(/&amp;/g, '&');
      return `<audio class="msg-audio" controls preload="metadata" src="${escapeAttr(fileUrl(cleanPath, false))}"></audio><span class="msg-audio-path">${escapeHtml(cleanPath)}</span>`;
    });
    // 链接 [文字](url)：url 转义后 & 变 &amp;，先还原再校验，只放行 http/https
    text = text.replace(/\[([^\]]+)]\(([^)\s]+)\)/g, (m, label, url) => {
      const clean = url.replace(/&amp;/g, "&");
      return /^https?:\/\//.test(clean) ? `<a href="${escapeAttr(clean)}" target="_blank" rel="noopener">${label}</a>` : m;
    });
    // 还原行内码：本地图片路径渲染为图片，否则还原为 <code>
    text = text.replace(/ (\d+) /g, (_, idx) => {
      const c = codes[+idx];
      const cleanC = c ? c.replace(/&amp;/g, '&') : c;
      if (cleanC && _LOCAL_IMG_RE.test(cleanC.trim()))
        return `<img class="msg-img" src="${escapeAttr(fileUrl(cleanC.trim()))}" alt="${escapeAttr(cleanC.trim())}" loading="lazy" />`;
      if (cleanC && _LOCAL_AUDIO_RE.test(cleanC.trim()))
        return `<audio class="msg-audio" controls preload="metadata" src="${escapeAttr(fileUrl(cleanC.trim(), false))}"></audio><span class="msg-audio-path">${escapeHtml(cleanC.trim())}</span>`;
      return `<code>${c}</code>`;
    });
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
        // html/htm 围栏：直接内联沙箱预览，而非纯代码块
        if (/^html?$/i.test(lang)) { html += htmlEmbedBlock(buf.join("\n")); continue; }
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
    const img = e.target.closest("img.msg-img");
    if (img) { openImageViewer(img.src, img.getAttribute("alt") || img.src); return; }
    // HTML 预览块「全屏」
    if (e.target.classList.contains("html-embed-full")) {
      const embed = e.target.closest(".html-embed");
      const iframe = embed && embed.querySelector("iframe");
      if (iframe && iframe.requestFullscreen) iframe.requestFullscreen().catch(() => {});
      return;
    }
    // HTML 预览块「查看源码 / 查看预览」切换
    if (e.target.classList.contains("html-embed-src")) {
      const embed = e.target.closest(".html-embed");
      const iframe = embed.querySelector("iframe");
      const pre = embed.querySelector("pre");
      const showing = iframe.style.display !== "none";
      iframe.style.display = showing ? "none" : "";
      pre.style.display = showing ? "" : "none";
      e.target.classList.toggle("on", showing);
      e.target.textContent = showing ? "查看预览" : "查看源码";
      return;
    }
    const btn = e.target.closest("[data-copy]");
    if (!btn) return;
    const block = btn.closest(".code-block");
    const code = block ? block.querySelector("code") : null;
    if (code) copyText(code.textContent).then((ok) => flashCopied(btn, ok));
  });

  // 预览 iframe 上报高度：自适应 iframe 高度（上限窗口高 80%）
  window.addEventListener("message", (e) => {
    const d = e.data;
    if (!d || typeof d.__embedHeight !== "number") return;
    const iframes = document.querySelectorAll(".html-embed iframe");
    for (const f of iframes) {
      if (f.contentWindow === e.source) {
        const max = Math.floor(window.innerHeight * 0.9);
        f.style.minHeight = "0";
        f.style.height = Math.min(Math.ceil(d.__embedHeight) + 4, max) + "px";
        break;
      }
    }
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

  // 快速备忘浮层：从输入区 📝 按钮唤起，底部弹出卡片，chip 选提醒周期，一键保存
  function openMemoQuickPanel() {
    // 遮罩
    const overlay = document.createElement("div");
    overlay.className = "memo-quick-overlay";

    // 卡片
    const card = document.createElement("div");
    card.className = "memo-quick-card";

    // 标题
    const title = document.createElement("div");
    title.className = "memo-quick-title";
    title.textContent = "📝 快速备忘";

    // textarea
    const ta = document.createElement("textarea");
    ta.className = "form-input memo-quick-ta";
    ta.placeholder = "记录要提醒的事情...";
    ta.rows = 3;

    // chip 行
    const chipRow = document.createElement("div");
    chipRow.className = "memo-quick-chips";

    const chipDefs = [
      { label: "每天", mode: "daily", at: "" },
      { label: "本周", mode: "weekly", at: String((new Date().getDay() + 6) % 7) },
      { label: "本月", mode: "monthly", at: String(new Date().getDate()) },
      { label: "不提醒", mode: "none", at: "" },
    ];
    let selectedChip = chipDefs[0];

    const chips = chipDefs.map(def => {
      const btn = document.createElement("button");
      btn.className = "memo-chip" + (def === selectedChip ? " active" : "");
      btn.textContent = def.label;
      btn.type = "button";
      btn.onclick = () => {
        chips.forEach(c => c.classList.remove("active"));
        btn.classList.add("active");
        selectedChip = def;
      };
      chipRow.appendChild(btn);
      return btn;
    });

    // 按钮行
    const btnRow = document.createElement("div");
    btnRow.className = "memo-quick-btns";

    const saveBtn = document.createElement("button");
    saveBtn.className = "btn-primary";
    saveBtn.textContent = "保存";
    saveBtn.type = "button";
    saveBtn.onclick = async () => {
      const content = ta.value.trim();
      if (!content) { toast("备忘内容不能为空", "error"); return; }
      const remind_enabled = selectedChip.mode === "none" ? 0 : 1;
      try {
        await api("/api/memos", {
          method: "POST",
          body: JSON.stringify({
            content,
            remind_mode: selectedChip.mode,
            remind_at: selectedChip.at,
            remind_days_before: 0,
            remind_enabled,
          }),
        });
        toast("已添加备忘", "success");
        overlay.remove();
        refreshMemoBadge();
      } catch (e) {
        toast("保存失败：" + e.message, "error");
      }
    };

    const cancelBtn = document.createElement("button");
    cancelBtn.className = "btn-sm";
    cancelBtn.textContent = "取消";
    cancelBtn.type = "button";
    cancelBtn.onclick = () => overlay.remove();

    btnRow.append(saveBtn, cancelBtn);
    card.append(title, ta, chipRow, btnRow);
    overlay.appendChild(card);
    document.body.appendChild(overlay);

    // 点遮罩关闭
    overlay.addEventListener("click", e => { if (e.target === overlay) overlay.remove(); });

    // 自动 focus
    setTimeout(() => ta.focus(), 50);
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

    if (data.memos && data.memos.length) {
      const memoList = document.createElement("div");
      memoList.className = "memo-banner-list";
      data.memos.forEach(m => {
        const item = document.createElement("div");
        item.className = "memo-banner-item";

        const txt = document.createElement("span");
        txt.className = "memo-banner-item-text";
        txt.textContent = m.content;

        const doneBtn = document.createElement("button");
        doneBtn.className = "memo-banner-check";
        doneBtn.textContent = "✓";
        doneBtn.title = "标记完成";
        doneBtn.onclick = async () => {
          try {
            await api("/api/memos/" + m.id, {
              method: "PUT",
              body: JSON.stringify({ status: "done" }),
            });
            item.remove();
            // 若无剩余 memo 则移除整个 banner
            if (!memoList.children.length) banner.remove();
            refreshMemoBadge();
          } catch (e) {
            toast("操作失败", "error");
          }
        };

        item.append(txt, doneBtn);
        memoList.appendChild(item);
      });
      body.appendChild(memoList);
    }

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

  // 回合完成横幅：前台在线也不错过完成通知（对齐 maybeNotify 去掉 document.hidden 门）。
  function showTurnDoneBanner(sid, status, kind) {
    const root = $("memo-banner-root");
    if (!root) return;
    const existing = root.querySelector(".turn-done-banner");
    if (existing) existing.remove();
    const banner = el("div", "turn-done-banner" + (status === "error" ? " is-error" : ""));
    const icon = el("span", "turn-done-banner-icon", status === "error" ? "⚠️" : kind === "stuck" ? "⏳" : "✅");
    const body = el("div", "turn-done-banner-body");
    const label = kind === "stuck" ? "长时间无新输出" : status === "error" ? "回合出错" : kind === "resume" ? "后台续跑已完成" : "回合已完成";
    body.textContent = label;
    const closeBtn = el("button", "turn-done-banner-close", "✕");
    closeBtn.onclick = () => banner.remove();
    banner.append(icon, body, closeBtn);
    banner.onclick = (e) => {
      if (e.target === closeBtn) return;
      if (sid && sid !== state.sessionId) switchSession(sid);
      banner.remove();
    };
    root.appendChild(banner);
    setTimeout(() => { if (banner.parentElement) banner.remove(); }, 8000);
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

  // 拉取备忘列表，算出「今日仍待提醒」条数刷新角标（进入 App 时初始化、增删改后同步）
  async function refreshMemoBadge() {
    try {
      const today = new Date();
      const todayStr = today.getFullYear() + "-" +
        String(today.getMonth() + 1).padStart(2, "0") + "-" +
        String(today.getDate()).padStart(2, "0");
      const items = await api("/api/memos");
      const count = items.filter(m =>
        m.status === "active" &&
        m.remind_enabled == 1 &&
        m.reminded_date !== todayStr
      ).length;
      setMemoBadge(count);
    } catch (e) {
      // 静默失败
    }
  }

  // ---------------- 自定义确认框（替代原生 confirm）----------------
  function confirmDialog(message, { okText = "确定", cancelText = "取消", danger = false } = {}) {
    return new Promise((resolve) => {
      const root = $("modal-root");
      root.innerHTML = "";
      root._sheetOwner = null;
      const card = el("div", "modal-card");
      card.innerHTML = `<div class="modal-msg">${escapeHtml(message)}</div>
        <div class="modal-actions">
          <button class="modal-cancel" type="button">${escapeHtml(cancelText)}</button>
          <button class="modal-ok${danger ? " danger" : ""}" type="button">${escapeHtml(okText)}</button>
        </div>`;
      root.appendChild(card);
      root.classList.remove("hidden");
      requestAnimationFrame(() => root.classList.add("show"));
      const close = (val) => { root.classList.remove("show"); setTimeout(() => { root.classList.add("hidden"); root.innerHTML = ""; delete root._sheetOwner; }, 200); resolve(val); };
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

  // 回合结束时：只要已授权就弹通知，前台也不能吞掉用户明确开启的完成提醒。
  function maybeNotify(result) {
    if (!notifyEnabled() || !notifySupported() || Notification.permission !== "granted") return;
    const st = result && result.status;
    const kind = result && result.kind;
    const body = kind === "stuck" ? "任务长时间没有新输出，点击查看" : st === "error" ? "任务执行出错，点击查看" : st === "cancelled" ? "任务已取消" : "Agent 已完成任务，点击查看";
    // tag 按会话区分：不同会话各自独立提醒，避免后一条覆盖前一条未读通知。
    const tag = "agent-done-" + ((result && result.sessionId) || state.sessionId || "");
    try {
      const n = new Notification("Agent Console", { body, tag });
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
    initDrop();
    switchTab("overview", true);  // 只切 UI，数据由下面串行加载，不重复请求
    // 拉后端目标循环默认成本上限（单一数据源）；fire-and-forget，失败静默保持兜底 20
    api("/api/config").then((cfg) => {
      if (cfg && cfg.goal_max_cost_usd > 0) GOAL_COST_DEFAULT = cfg.goal_max_cost_usd;
    }).catch(() => {});
    initHubResizer();
    await loadTasks();      // 先建 taskBySession 映射，再 loadSessions 才能算对徽章/看板
    await loadSessions();
    if (state.sessionId) await switchSession(state.sessionId);  // 含 loadHistory + 第一条 WS
    primeRunBarFromApi();
    loadSnippets();         // 非关键，放最后
    connectMonitor();       // 监控 WS 最后连，错开与 switchSession 里那条 WS 的建连峰值
    refreshMemoBadge();     // 初始化备忘角标（今日仍待提醒条数）
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
