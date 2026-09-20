# 方案：会话展示关联待办（需求1）+ 会话置顶（需求2）

状态：analyst 产出，待 developer 实现。行号基于 commit d422e2d。
修订 2026-09-14：按用户追加要求，**详情页联动展示关联待办**由"本轮不做"改为**本轮必做**，
方案见 §4（默认单行截断 + 点击展开，复用 goal-summary-panel 的 class-toggle 范式）；
§5 风险表与 §7 清单（阶段 B2）已同步。
本文件只写方案，不含代码。所有行号均已用 Read/Grep 核实，不引用图谱措辞。

---

## 0. 先说结论：两个需求在前端强耦合，必须按本文顺序改

两需求的前端落点**完全重叠**：都要改 `web/app.js` 的 `renderSessionRow(s, isArchived)`
（app.js:633-684），需求1加内容到 `main` 里、需求2加按钮到 `actions` 里。
且两者都要往 `server/db.py:list_sessions()`（db.py:414-467）里加字段。

**强制改动顺序：先做需求2（置顶），再做需求1（关联待办）。**
理由：需求2要改 `list_sessions` 的 `ORDER BY`（db.py:420）这一行，属于结构性改动；
需求1只是在已有的批量 JOIN 段落（db.py:422-437）里多带一个字段，是纯增量。
反过来做会让需求1的 JOIN 代码被需求2的排序改动夹在中间二次编辑。

---

## 1. 现状核实（两需求共用的事实）

### 1.1 数据结构：会话→待办是**多对多表 + 一个反向主会话字段**

- `todos` 表（db.py:98-107）有 `session_id` 字段 = **主会话**（"primary"），
  语义见 `set_todo_sessions()` 注释（db.py:794）："覆盖式设置任务关联会话；
  同步把主会话（列表第一个）写回 todos.session_id"。
- `todo_sessions` 表（db.py:109-114）是真正的多对多关联表，
  `PRIMARY KEY (todo_id, session_id)`，两侧都有索引（db.py:115-116）。
- **`sessions` 表没有任何字段指回 todo**（已核实 20 列全量，见 §2.1），
  所以会话侧想知道自己关联了哪些 todo，只能走 `todo_sessions` 反查。
- 老数据回填：`init_db` 会把 `todos.session_id` 幂等灌进关联表（db.py:369-376），
  所以**关联表是全集**，不需要再 UNION `todos.session_id`。这点很重要，
  否则 developer 会写出多余的 UNION。

### 1.2 已有接口能查到什么

| 位置 | 函数 | 方向 | 是否可直接用 |
|---|---|---|---|
| db.py:470-477 | `todos_linked_to_session(session_id)` | session → todo **标题列表** | 语义正好，但是单会话（N+1） |
| db.py:422-437 | `list_sessions` 内联批量 JOIN | session → **关联数量** `linked_todo_count` | **这就是要扩的地方** |
| db.py:785-790 | `list_todo_session_ids(todo_id)` | todo → sessions | 反方向，不用 |
| db.py:769-781 | `list_todos` 内联批量 | todo → `session_ids` | 反方向，不用 |

**关键：`list_sessions` 里已经存在一段"避免 N+1 的批量 JOIN"**（db.py:422-437），
它已经 JOIN 了 `todo_sessions` + `todos` 并按 `ts.session_id` 分组，
只是只 `COUNT(*)` 出了数量、没取标题。需求1**不需要新写查询，只需把这条 SQL 的
投影从 COUNT 扩成 COUNT + 标题聚合**。别新建接口、别新建函数。

### 1.3 前端渲染：单一行渲染函数，三个列表 + 一个分组共用

`renderSessionRow(s, isArchived = false)`（app.js:633-684）是**唯一**的会话行渲染器，
5 处调用（已核实计数）：
- `fillList()` app.js:383 —— 供 review 列表（app.js:366）与归档列表（app.js:469）
- `fillListGrouped()` app.js:418 —— 供 Overview（app.js:362）与 Sessions 主列表（app.js:365）
- `renderDispatchGroup()` app.js:446 —— 分组内子会话
- `patchSessionRow()` app.js:1981 —— WS 推送后**整行重渲染**

行内结构（app.js:640-655）：
```
li.st-<state>                       ← className 带状态，li 自身是 flex（style.css:186-192）
├── div.s-main  (flex:1, min-width:0, 纵向 flex, gap 3px)   style.css:200
│   ├── div.s-row1   徽章 + 标题（+ 未读红点 prepend）       style.css:201
│   ├── div.s-sub    行摘要，:empty 自动隐藏                 style.css:203-204
│   └── div.s-meta   时间 · workdir · 档位 · Codex 徽章      style.css:205-206
└── div.s-actions  (flex:0 0 auto)  👁速览 + ×删除           style.css:207-213
```

**卡片拥挤度判断**：`.s-meta` 是 `display:flex; flex-wrap: wrap`（style.css:205），
已经天然支持换行，目前挂 2~4 个 span。**有预留空间，追加一个 chip 不会撑破布局**。
移动端 `.s-title` 是 `flex: 1 1 100%` 强制换行到徽章下方（style.css:1057），
`.s-sub` 允许 2 行（style.css:1058）。

---

## 2. 需求2：会话置顶（先做）

### 2.1 后端：新增 `pinned` 字段

已核实生产库 `sessions` 现有 20 列，**无 `pinned`**：
`id,title,claude_session_id,workdir,status,created_at,updated_at,mode,summary,
is_secretary,title_auto,archived,worktree_branch,is_worktree,worktree_base,engine,
effort,pending_compact_summary,compacted_at,codex_usage_baseline`

**落点：`server/db.py:287` 之后追加一行**（紧跟 `codex_usage_baseline` 那行）：
```
_add_col("sessions", "pinned INTEGER DEFAULT 0")
```
范式说明（务必照抄 `codex_usage_baseline` 的写法，**不要**照抄上面 `archived` 的写法）：
- db.py:260-285 那批用的是 `if "xxx" not in cols:` 前置判断，`cols` 快照取自 db.py:259；
- db.py:287 起改用**裸调 `_add_col`**，因为 `_add_col` 自身已吞掉 duplicate column
  （db.py:253-258，注释明确说是为双进程 80/8800 竞态设计）。
- 新字段跟 `codex_usage_baseline` 一样裸调即可，**别加 `if not in cols`**——
  `cols` 是 db.py:259 的旧快照，混用会让后续读者误判。

### 2.2 后端：排序

**落点：`server/db.py:420`**，把
`rows = _query(base + " ORDER BY updated_at DESC", ())`
改成 `ORDER BY pinned DESC, updated_at DESC`。

⚠️ **老库兼容陷阱**：`pinned` 对老库是 ALTER 出来的，存量行值为 `0`（有 DEFAULT 0），
不会是 NULL——因为 `_add_col` 走 `ADD COLUMN ... DEFAULT 0`，SQLite 会把存量行填 0。
但为了和本文件其它字段的防御风格一致（db.py:419 对 `archived` 就写了 `IS NULL` 兜底），
建议写成 `ORDER BY (pinned IS 1) DESC, updated_at DESC` 或
`COALESCE(pinned,0) DESC, updated_at DESC`。**二选一，developer 挑一个并在注释里说明**。

### 2.3 ⚠️ 后端排序改了还不够：前端会把顺序重排掉（**最容易漏的坑**）

已核实：`fillListGrouped()`（app.js:388-421）**不信任后端顺序**，它自己重算：
- app.js:397-412 把会话摊成 `items`，普通会话取 `ts = s.updated_at`（app.js:410），
  分组取组内首个成员的 `updated_at` 作锚点（app.js:403）；
- app.js:413 `items.sort((a, b) => (b.ts || 0) - (a.ts || 0))` —— **纯按 updated_at 降序重排**。

所以**只改后端 ORDER BY，Overview 和 Sessions 主列表（占两个主要入口）置顶会完全不生效**，
只有 review 列表和归档列表（走 `fillList`，app.js:376-384，不重排）会生效。
这是本需求最大的实现风险。

**改法（app.js:388-421 内）**：
1. app.js:410 普通会话项加 `pinned: !!s.pinned`；
2. 分组项（app.js:403-404）加 `pinned:` = 组内**任一**子会话 pinned（建议用
   `g.pinned = g.pinned || !!s.pinned`，在 app.js:408 push 子会话时累积）；
3. app.js:413 的比较器改为**先比 pinned 再比 ts**：
   `items.sort((a,b) => (b.pinned?1:0)-(a.pinned?1:0) || (b.ts||0)-(a.ts||0));`

注：`||` 短路在这里安全，因为第一项为 0 时才落到第二项，正是想要的 tie-break。

`fillList()`（app.js:376-384）保持原样即可——它按入参数组顺序渲染，
review 列表的入参来自 `state.sessions.filter(...)`（app.js:366），
而 `state.sessions` 来自 `GET /api/sessions`（app.js:347），已经是后端排好的序。

### 2.4 后端：toggle 接口

**落点：`server/main.py:286` 之后**（紧跟 `unarchive_session` 结束、`resume_session` 之前）。

照抄 archive/unarchive 这对接口的范式（main.py:273-286）：两个独立的 POST，
各自先 `db.get_session(sid)` 判 404、再 `db.update_session(sid, ...)`、
返回 `{"ok": True, "pinned": ...}`。

```
POST /api/sessions/{sid}/pin     → update_session(sid, pinned=1) → {"ok":True,"pinned":True}
POST /api/sessions/{sid}/unpin   → update_session(sid, pinned=0) → {"ok":True,"pinned":False}
```

**为什么用两个接口而不是一个 toggle**：项目内既有的"切换态"全是成对的显式接口——
sessions 的 archive/unarchive（main.py:273-286）、todos 的 archive/unarchive
（main.py:1056-1069）。成对接口幂等（重复 pin 不会翻回来），移动端弱网重试安全。
`api()` 的 `retry: true`（app.js:2380 用法）在 toggle 语义下会翻错方向。
**必须用成对接口。**

⚠️ **`update_session` 副作用**：db.py:497-502 会**无条件 `fields["updated_at"] = _now()`**。
即 pin/unpin 会顺手把会话的 `updated_at` 刷成现在，导致该会话在二级排序里也跳到最前、
且列表里的"时间"列（app.js:651）变成刚刚。
- 对 pin 来说副作用基本无害（本来就要排最前）；
- 但 **unpin 会让一个很老的会话诡异地跳到活跃列表顶部**，这是可感知的 bug。
- **要求 developer 处理**：unpin 走一条不刷 `updated_at` 的写入路径。
  最小改动是在 db.py 新增一个 `set_session_pinned(sid, pinned)`，
  内部直接 `_exec("UPDATE sessions SET pinned=? WHERE id=?", ...)` 不碰 updated_at，
  两个接口都走它。**不要**去改 `update_session` 的通用行为（19 处调用，见 §5）。

### 2.5 前端：交互与渲染

**交互选型：在 `.s-actions` 里加一个图钉按钮，直接点击切换。不做长按菜单。**
依据：
- 项目内**没有任何长按/touchstart 手势代码**（已 Grep `touchstart|longpress`，
  只有 app.js:2081/5728 的 `"ontouchstart" in window` 特性嗅探，与手势无关）。
  引入长按是新范式，成本高且移动端易与列表滚动冲突。
- 既有产品语言就是"行内小图标按钮直接点"：会话行的 👁/×（app.js:674-678）、
  看板行的 ✎/🗄/🗑（app.js:1216-1218）。图钉按钮与之一致。
- 详情页底部 `#act-archive`（index.html:309）那种大按钮是"当前会话"级操作，
  而置顶要在列表里对任意行操作，不适合放详情页。

**落点：`web/app.js:663-680`（`const actions = el("div","s-actions")` 块内）**
- 活跃分支（app.js:673-680，`else` 分支）：在 `peek` 之前插入 pin 按钮，
  即 `actions.append(pin, peek, del)`（app.js:679）。
  放最左是因为置顶是"状态类"操作，且 × 删除应保持在最右（肌肉记忆/防误触）。
- 归档分支（app.js:664-672）：**不加 pin 按钮**。归档会话不在活跃列表里，
  置顶无意义；且 `list_sessions` 的归档视图是另一次查询（main.py:117 `archived_only`）。
- 按钮形态：`el("button", "s-pin" + (s.pinned ? " pinned" : ""), s.pinned ? "📌" : "📍")`，
  `title` 分别为 "取消置顶" / "置顶到列表顶部"。
  （emoji 二选一由 developer 定，但**必须 pinned/未 pinned 视觉可区分**，
  参考 `.s-del-locked` 用 opacity 区分态的做法，style.css:1947-1951。）
- `onclick` 必须 `e.stopPropagation()`——因为点击整行会 `switchSession + openDetail`
  （app.js:656-661），不阻断会误切会话。同 app.js:675/678 的既有写法。

**调用的刷新逻辑**：点击后 `await api(...)` 然后 `await loadSessions()`
（loadSessions 在 app.js:346-358，会重拉 + `renderSessionLists()`）。
参考 `unarchiveSession()`（app.js:2378-2382）的写法新增一个
`async function togglePinSession(id, pinned)`，建议紧邻它落地（app.js:2376 附近）。
⚠️ 不要用 `retry: true`（见 §2.4 的幂等讨论——成对接口下其实可以带，
但若 developer 最终实现成单 toggle 接口则绝对不能带；统一起见建议不带）。

**另一个额外渲染点（可选，建议做）**：置顶的会话在行上要有可见标记，
否则用户不知道为什么它在最前。最省事的做法是给 `li` 加一个 class：
app.js:637-638 附近加 `if (s.pinned) li.classList.add("pinned");`，
CSS 里用一条 `.session-list li.pinned { ... }` 做左侧或背景轻标记。
**注意不要覆盖 `li.st-*` 的 `border-left-color`**（style.css:195-198）——
那是状态色，语义冲突。建议改 `background` 或加 `::after` 角标。

### 2.6 CSS 落点

**`web/style.css:213` 之后**（紧跟 `.s-del:hover`）。
- `.s-pin` 必须并入既有的 `.s-peek, .s-del` 复合选择器（style.css:208-211）
  才能继承尺寸/padding/字号，**否则三个按钮高度不齐**。
  即把 style.css:208 改成 `.s-peek, .s-del, .s-pin {`。
- 同理**移动端触控热区** style.css:1061 的 `.s-peek, .s-del { padding: 5px 7px; ... }`
  也要加上 `.s-pin`，否则手机上图钉按钮热区偏小。**这行极易漏，明确要求改。**
- 新增 `.s-pin:hover { color: var(--accent); background: var(--bg-3); }`
  与 `.s-pin.pinned { color: var(--accent); }`（参考 style.css:212 的 hover 范式）。

---

## 3. 需求1：会话上展示关联待办（后做）

### 3.1 基数判断：**实测是严格 1:1，但代码必须容忍 N**

生产库实测（`data/console.db`，2026-09-14）：
- `sessions` 315 行，`todos` 8 行，`todo_sessions` 仅 **6 行**；
- 按 session 分组的 todo 数分布：**`todos/session = 1` 的有 6 个 session，没有任何 >1 的**
  （含归档 todo 一起算也是全 1）；
- 关联 todo 的标题长度：min 6 / 平均 13.8 / max 30 字符。

结论：
- **展示单条 title 即可**，不需要做省略/tooltip 的多条折叠 UI；
- 但数据模型允许 N（`todo_sessions` 主键是复合键，`set_todo_sessions` 明确支持
  多会话关联，反向天然也能多 todo 指向同一 session），
  所以**渲染要写成"取第一条 + 有多条时缀 `+N`"**，不能假设只有一条就直接取 `[0]` 而不管其余。
- 标题最长 30 字符，配合 `.s-meta` 的 `flex-wrap: wrap`（style.css:205）不会撑破，
  但仍建议 CSS 加 `max-width` + `text-overflow: ellipsis` 防未来长标题。

### 3.2 后端：扩已有批量 JOIN，不加接口

**落点：`server/db.py:427-437`**（`list_sessions` 内那段 `link_rows` 查询）。

现状 SQL（db.py:428-431）已经 JOIN 好了，只需把投影从 `COUNT(*)` 扩成
`COUNT(*)` + 标题聚合。推荐用 SQLite 内置的 `GROUP_CONCAT`：
```
SELECT ts.session_id, COUNT(*) AS c, GROUP_CONCAT(t.title, '\x1f') AS titles
  FROM todo_sessions ts JOIN todos t ON t.id = ts.todo_id
 WHERE ts.session_id IN (...) AND (t.archived = 0 OR t.archived IS NULL)
 GROUP BY ts.session_id
```
然后 db.py:434-437 的回填循环里，除 `linked_todo_count` 外再补
`d["linked_todo_titles"] = <split 后的 list>`（未关联时 `[]`）。

设计要点与理由：
1. **必须复用同一条查询**，不要另起一条。这是需求明确的"避免 N+1"，
   而且 db.py:422 的注释已经把这段定性为"批量补…避免 N+1"，扩它最自然。
2. **过滤条件保持 `t.archived = 0 OR t.archived IS NULL` 不动**。
   实测 6 条关联里有 2 条的 todo 已归档（`音频去重验证`、
   `口语顺滑+格式化输出 管线数据筛选`），沿用现有条件意味着
   **归档 todo 不在会话上展示**——这与 `linked_todo_count` 的口径一致
   （也与 db.py:471 的注释"归档任务不锁定会话"一致）。口径统一比多显信息重要。
3. **分隔符不要用逗号**。`GROUP_CONCAT` 默认分隔符是 `,`，而 todo 标题里完全可能含逗号
   （实测标题含空格和 `+`，如 `口语顺滑+格式化输出 管线数据筛选`），用 `,` 会切错。
   用 `\x1f`(ASCII Unit Separator) 这类不可能出现在标题里的字符。
   **或者**更稳的做法：不用 GROUP_CONCAT，改成不带 GROUP BY 的明细查询
   （`SELECT ts.session_id, t.title FROM ... WHERE ... IN (...)`），
   在 Python 侧用 `setdefault(...).append(...)` 聚合成 list —— 这正是 db.py:769-781
   `list_todos` 补 `session_ids` 的既有范式，**与项目风格更一致，且没有分隔符风险**。
   **推荐后者**；此时 `COUNT` 可直接由 `len(titles)` 得出，
   但**为了不动 `linked_todo_count` 的现有语义，建议保留原 COUNT 查询不变、
   另加一条明细查询**，或用一条明细查询同时算出两者（developer 二选一，需保证
   `linked_todo_count` 数值与改动前完全一致，见 §6 验证）。
4. `GROUP_CONCAT` 的**顺序不保证**。若走 GROUP_CONCAT 方案，"第一条"是不确定的；
   明细查询方案可以 `ORDER BY ts.created_at` 让"第一条"= 最早关联（与
   db.py:787 `list_todo_session_ids` 的 `ORDER BY created_at` 口径一致）。**再一条选后者的理由。**

**接口层无需改动**：`GET /api/sessions`（main.py:115-117）直接 `return db.list_sessions(...)`，
新字段自动随 dict 序列化出去。**不要新增接口。**

### 3.3 前端渲染落点

**落点：`web/app.js:650-655`（`.s-meta` 段）之后、`main.append(...)` 之前。**

在 `meta` 拼装完（app.js:651-654）之后、app.js:655 `main.append(row1, sub, meta)` 之前，
新增一个独立的 `.s-todo` 行，并把它加进 append：
`main.append(row1, sub, meta, todoEl)` —— 只在有关联时创建，无关联时不 append。

**为什么放在 `.s-meta` 之后而不是塞进 `.s-meta` 里**：
- `.s-meta` 是 `color: var(--muted-2)` 的 11px 最弱视觉层（style.css:205），
  用来放时间/路径/档位这类"元信息"。关联待办是**业务信息**（用户明确说"方便查找"），
  塞进 meta 会被淹没。
- 独立一行可以用 `.kanban-row-hint`（style.css:1886-1890，`color: var(--accent)` + `opacity .75`）
  那一档的强调色，与看板的视觉语言呼应——用户脑子里"待办"就是看板那套颜色。
- 另一个备选是放进 `.s-row1` 做成 chip 跟在标题后（app.js:644），
  但 `.s-row1` 在移动端标题已经 `flex:1 1 100%` 强制换行（style.css:1057），
  再塞东西会挤成三行。**不选。**

**渲染内容**（对应 §3.1 的 1:1 但容忍 N）：
```
📋 <第一条 title>            // 仅一条
📋 <第一条 title> +2         // 多条时缀 +N（N = count - 1）
```
- 文本必须过 `escapeHtml()`（app.js:6123），与 app.js:643/649 的既有做法一致。
  ⚠️ 注意 `el()` 的第三参是 `innerHTML`（app.js:4758），**不转义就是 XSS**，
  而 todo 标题是用户输入。
- `title` 属性挂**全部标题**（`titles.join(' / ')`，用 `escapeAttr()` app.js:6124），
  这样多条时 hover 可看全，省掉专门的 tooltip 组件。
- 判空：`const titles = s.linked_todo_titles || [];` 再 `if (titles.length)` 才创建。
  **必须容忍字段缺失**——`patchSessionRow`（app.js:1981）重渲染时用的是
  `state.sessions` 里的对象，而 WS 推送（app.js:1953-1962）只覆盖部分字段，
  不会带 `linked_todo_titles`；好在它是**原地改同一个对象**（app.js:1954 `const s = find(...)`），
  原有字段保留，所以不会丢。但 `|| []` 兜底仍必须写。

### 3.4 搜索联动（建议一并做，成本 2 行）

`sessionMatchesQuery(s, q)`（app.js:579-586）目前只匹配标题/进展/内容。
用户的动机原文是"**方便查找**"，那么让关联待办标题也能被搜到才算闭环。
**落点 app.js:581-585**：加一项
`const inTodo = (s.linked_todo_titles || []).join(" ").toLowerCase().includes(q);`
并并入 app.js:585 的 return。

⚠️ 若做了这一步，注意 `highlightRow()`（app.js:555-563）只高亮 `.s-title` 和 `.s-sub`
两个节点，新的 `.s-todo` 不会被高亮——**这是可接受的**（命中但不高亮，不是 bug），
但如果 developer 想加高亮，必须同时处理 `dataset.raw` 缓存逻辑（app.js:560），
否则反复搜索会把 `<mark>` 标签吃进 `raw` 里。**建议本轮不加高亮，只加匹配。**

### 3.5 CSS 落点

**`web/style.css:204` 之后**（紧跟 `.s-sub:empty`）新增：
```
.s-todo { font-size: 11.5px; color: var(--accent); opacity: .8;
          overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
```
命名依据：与 `.s-main/.s-row1/.s-title/.s-sub/.s-meta/.s-actions` 同前缀
（style.css:200-207），符合既有 `s-` 命名习惯。
移动端（style.css:1053-1062 那段 media query）可选加一条
`.s-todo { font-size: 12px; }` 与 `.s-sub`/`.s-meta` 的移动端放大一致（style.css:1058-1059）。

---

## 4. 需求1（续）：详情页联动展示关联待办（本轮必做）

> 修订记录（2026-09-14，用户追加需求）：本节原判断是"本轮不做详情页"。
> 用户明确要求**详情页也要联动显示关联待办**，并附加约束：
> "显示的时候做的灵活一些，可以调整显示的文字多少，不然可能会覆盖别的内容"。
> 该约束正是原判断里"新增常驻 bar 会挤压 detail-head"这一顾虑的正面解法——
> 不是不显示，而是**默认只占一行、可折叠展开**。本节即该方案，**升级为本轮必做**。
> 原"留作 follow-up"的结论作废；§7 改动清单已相应追加阶段 B2。

### 4.1 详情页当前布局（已核实）

`#detail-panel`（index.html:257-312）自上而下是 5 个 flex 兄弟：

```
section.hub-right#detail-panel                     移动端 style.css:1065-1069 是全屏覆盖层
├── div.detail-head        flex:0 0 auto           style.css:227
│   ├── div.detail-head-row                        style.css:228  display:flex; align-items:center
│   │   ├── div.detail-title#session-title         style.css:229  flex:1; min-width:0; ellipsis
│   │   └── div.detail-head-btns  🧩子任务/⋯更多/←返回   style.css:230-235
│   └── div.detail-meta                            style.css:238  display:flex; flex-wrap:wrap; gap:8px
│       ├── span#workdir-bar.workdir-bar.hidden    style.css:239  max-width:70%; ellipsis; 可点改目录
│       ├── span#session-id-bar.sid-bar.hidden     style.css:241  ellipsis; 可点复制
│       └── select#engine-select                   style.css:243-249
├── main#chat.chat                          ← 聊天正文，会话主体
├── div#snippet-bar / #img-tray / #queue-tray / #run-bar   四个 .hidden 条
├── footer.composer                         ← 输入区
└── div.detail-actions                      ← Resume / Archive / 压缩
```

关键事实：
1. **`.detail-meta` 已是 `flex-wrap: wrap`**（style.css:238），和列表侧 `.s-meta` 一样
   天然支持换行，追加一个子元素不会撑破布局。
2. **`.detail-meta` 里现存的两个 span 全部自带 ellipsis 截断**
   （`.workdir-bar` style.css:239、`.sid-bar` style.css:241），
   即"长内容单行截断"已经是本行的既有视觉契约——新元素照做即可，不引入新范式。
3. 移动端 `.detail-head` 变 `flex-direction: column; gap: 6px`（style.css:1070-1073），
   `.detail-head-btns` 被 `order: -1` 提到最上（style.css:1075）。
   `.detail-meta` 在移动端**没有额外规则**，继承桌面的 wrap 行为。
4. `📋` emoji 在详情页**未被占用**（detail-head 现有 🧩 / ⋯ / ← / 📂，底部 ▷ / 🗄 / 📉），
   与列表侧 §3.3 用的 `📋` 一致，跨列表/详情语义统一。

### 4.2 复用哪个既有折叠范式：`goal-summary-panel`（不发明新 UI）

已 Grep 全量核实，项目里"默认收起/可展开"共有 4 类实现，选型如下：

| 既有范式 | 位置 | 实现方式 | 是否可复用 |
|---|---|---|---|
| `.goal-summary-panel.collapsed` | app.js:3218-3227 / style.css:722-726 | 容器 toggle `collapsed` class + CSS `.collapsed .body{display:none}` + 头部文字在"展开/收起"间切换 | **✅ 选它** |
| `.tool-group` / `.subagent` 的 `open` class | app.js:214 / 4984 / 5054 / 5162 | toggle `open` + 手动改 `style.display` | ❌ 混用 class 与 inline style，为聊天流设计，重 |
| `-webkit-line-clamp` 纯 CSS 截断 | style.css:203（`.s-sub`）/ 712 / 1198 | 纯 CSS，**不可交互展开** | 只作为"收起态"的截断手段 |
| `title` 属性原生 tooltip | app.js:2461/2471、style.css:239 | 浏览器原生 | 作为补充，**不作为主方案** |

**为什么不用纯 hover tooltip 作主方案**：移动端（本项目详情页在 ≤900px 是全屏覆盖层，
是主要使用场景之一）**没有 hover**，`title` 在手机上完全不可达。项目里 `title` 一直只做
桌面端的锦上添花（如 `.sid-bar` 既能点击复制、又有 title 说明），从不承载唯一信息路径。
所以**主方案必须是点击展开**，`title` 只做桌面端 hover 的额外便利。

**选 `goal-summary-panel` 范式的理由**：
- 它是项目里唯一一个"**纯 class toggle + CSS 控制显隐 + 提示文字同步切换**"的干净实现，
  没有 inline style 污染，重渲染也不会残留状态；
- 它的头部文字（`.goal-summary-toggle`，"展开"/"收起"，app.js:3224）就是用户要的
  "可调整显示的文字多少"的显式开关，且**用户已经在目标循环页面见过这个交互**，无学习成本；
- 它已经处理了"标题行始终可见、正文可隐"这一正是本需求所需的结构。

### 4.3 详情页展示方案（三态：无关联 / 收起 / 展开）

在 `.detail-meta` 内新增**一个** `<span id="linked-todo-bar" class="todo-bar hidden">`，
作为 `.detail-meta` 的第 3 个子元素（与 workdir/sid 同级，共享 wrap 与 gap）。

**三态定义**

| 态 | 触发 | 渲染 | 占位 |
|---|---|---|---|
| 无关联 | `titles.length === 0` | 加 `.hidden`，文本清空 | **0**（`display:none !important`，style.css:50） |
| 收起（默认） | 有关联，未点击 | `📋 <第一条 title>`（多条时缀 `+N`）+ `展开` | **一行，`max-width` 受限 + ellipsis** |
| 展开 | 点击 bar | 多条逐行全文 + `收起`，`white-space: pre-wrap; word-break: break-word` | 独占 meta 一整行，随内容增高 |

**默认收起态的宽度控制（这是"不覆盖别的内容"的关键）**

`.todo-bar` 收起态给 `max-width: 42%`。同行 `.workdir-bar` 已有 `max-width: 70%`
（style.css:239），两者相加可能超 100% → 由 `.detail-meta` 的 `flex-wrap: wrap`
自动换行，**不会溢出，最坏是 meta 变两行**，这与 `.s-meta` 在列表里的行为一致，可接受。
配 `overflow:hidden; text-overflow:ellipsis; white-space:nowrap`，
与 `.workdir-bar` / `.sid-bar` **完全同一套截断写法**。

> ⚠️ **不要给收起态用 `-webkit-line-clamp`**。`line-clamp` 需要 `display:-webkit-box`，
> 会与本行 flex item 的 `display:inline-flex` 打架。`.workdir-bar` / `.sid-bar` 用的就是
> `white-space:nowrap + ellipsis` 而不是 clamp（对比 `.s-sub` style.css:203 是在纵向 flex
> 的 `.s-main` 里才用 clamp）——**照抄同行邻居，不要照抄列表行**。

**展开态**

点击 bar 时 `bar.classList.toggle("expanded")`，CSS：
```
.todo-bar.expanded { max-width: 100%; flex: 1 1 100%; white-space: pre-wrap;
                     word-break: break-word; overflow: visible; text-overflow: clip; }
```
`flex: 1 1 100%` 让它展开时独占 meta 的一整行（范式已存在：移动端 `.s-title` 就是用
`flex: 1 1 100%` 强制独占行，style.css:1057），这样展开只**向下**推 chat 区一点，
不会横向覆盖 workdir / engine-select。

展开态文本 = 全部标题逐行（`titles.join("\n")`，靠 `pre-wrap` 成多行），前缀仍是 `📋`，
尾部 toggle 文字从 `展开` 切成 `收起`（完全照 app.js:3224 的做法）。
toggle **只要有关联就显示**，与 `.goal-summary-toggle` 的无条件显示一致（不做"是否被截断"的探测，
那需要量 `scrollWidth`，是新范式）。

**为什么不做成 detail-head-btns 里的图标按钮 + 弹出 sheet**：
项目确有 `showActionSheet()`（app.js:5766-5782）可复用，但 sheet 是**动作选择器**
（选项 → 执行操作），不是信息展示器；把只读信息塞进 action sheet 是语义误用。
且 `.detail-head-btns` 已有 3 个按钮，移动端 `order:-1` 提到首行后再加第 4 个会挤。**不选。**

### 4.4 前端落点（⚠️ 两处 switchSession 分支都要改）

**新增 HTML —— `web/index.html:269` 之后、`<select id="engine-select">`（index.html:270）之前**：
```
<span id="linked-todo-bar" class="todo-bar hidden"></span>
```
放在 `#session-id-bar` 之后、`#engine-select` 之前，理由：`#engine-select` 是本行**唯一的
交互控件**（下拉框），保持它在 meta 行末尾的固定位置，避免新元素展开成整行时把下拉框挤到诡异位置。

**新增 update 函数 —— `web/app.js:2474` 之后**（紧跟 `updateSessionIdBar` 结束、
`$("workdir-bar").onclick`（app.js:2476）之前），命名 `updateLinkedTodoBar(s)`：
- 与 `updateWorkdirBar`（app.js:2453-2463）/ `updateSessionIdBar`（app.js:2466-2474）
  **同一范式**：`const bar = $("linked-todo-bar"); if (!bar) return;` → 无数据则
  `bar.classList.add("hidden"); return;` → 有数据则填内容 + `bar.classList.remove("hidden")`。
- **入参传整个 session 对象 `s`**（不像两个邻居那样传单字段），
  因为要读 `s.linked_todo_titles`（经 §4.5 的辅助函数）。
- 每次调用都要 `bar.classList.remove("expanded")` **复位展开态**——
  否则从"展开着的 A 会话"切到 B 会话，B 会继承 A 的展开态，是可感知的 bug。
- `bar.onclick` 每次重新赋值（同 `updateSessionIdBar` app.js:2472 的 `bar.onclick =` 写法，
  避免 `addEventListener` 重复叠加）；点击内**不需要** `stopPropagation`——
  `.detail-meta` 及其祖先没有 click 处理器（已核实 `.detail-head` 无 onclick）；
  `#workdir-bar` 虽自带 onclick（app.js:2476）但与新元素是**兄弟不是父子**，互不影响。

**两处调用点（必须都加，这是本节最大的漏做风险）**：

1. **`web/app.js:2413` 之后** —— `switchSession` 的**短路分支**
   （app.js:2408-2422，"重复点当前会话时不重新拉历史"）。
   在现有三行 `$("session-title").textContent` / `updateWorkdirBar(cur.workdir)` /
   `updateSessionIdBar(cur.id)`（app.js:2411-2413）后追加 `updateLinkedTodoBar(cur);`。
   ⚠️ **漏了这处的症状**：从列表首次点进详情正常，但停在 A 的详情页再点列表里同一个 A
   （或任何走到该短路分支的路径）时，待办条不刷新、且展开态不复位。
2. **`web/app.js:2439` 之后** —— `switchSession` 的**完整加载分支**（app.js:2435-2441）。
   同样在 `updateSessionIdBar(cur.id)`（app.js:2439）后追加 `updateLinkedTodoBar(cur);`。

**第三处（建议一并加，成本 1 行）**：`web/app.js:353-355`，`loadSessions()` 里已有
`const cur = state.sessions.find(...)`（app.js:353）与 `if (cur) markSeen(...)`（app.js:355），
在该 `if (cur)` 内补 `updateLinkedTodoBar(cur);`。
理由：`loadSessions` 是**待办数据的唯一来源**（app.js:347 `GET /api/sessions`），
在看板侧改了关联后（§4.6）若不在这里刷新，详情页会一直显示旧数据直到用户切一次会话。
⚠️ `cur` 可能为 undefined，必须写在 `if (cur)` 内。

**WS 推送不需要处理**：`session_update`（app.js:1952-1969）只覆盖
status/activity/elapsed/stuck/summary/title/updated_at（app.js:1956-1962），
**不带 `linked_todo_titles`**，且它是原地改 `state.sessions` 里的同一对象（app.js:1953），
原字段保留。详情页标题的 WS 同步（app.js:1966-1969）只改 title，无需联动待办条。

### 4.5 复用同一份数据 + 同一套截断逻辑（不写两套）

**数据层：完全零成本复用。** §3.2 已在 `list_sessions`（db.py:427-437）里回填
`linked_todo_titles` / `linked_todo_count`，详情页用的 `cur` 就是
`state.sessions.find(...)`（app.js:2409 与 2435）拿到的**同一个对象**——
与列表侧 `renderSessionRow(s)` 收到的是同一份引用。
**详情页不新增任何请求、不新增任何后端字段。** 这是把详情页纳入本轮的最大理由：
§3.2 的后端改动做完后，详情页的增量成本只有前端 ~25 行。

**截断逻辑层：抽一个共用辅助函数。**
新增 `web/app.js` 顶层函数，落在 `sessionSubtitle()` 之后即 **app.js:1778 之后**
（和其它 session 文案派生函数放一起）：

```
// 从 session 对象派生关联待办的展示文案。列表行(.s-todo) 与详情页(.todo-bar) 共用同一口径。
function linkedTodoText(s) {
  const titles = (s && s.linked_todo_titles) || [];
  if (!titles.length) return null;
  return {
    titles,                                  // 原始数组，展开态逐行用
    brief: titles[0] + (titles.length > 1 ? " +" + (titles.length - 1) : ""),  // 收起态单行
    full: titles.join(" / "),                // title 属性 / 搜索匹配用
  };
}
```

三处消费者共用它，**任何一处都不许自己拼字符串**：
- §3.3 的列表行 `.s-todo`（app.js:650-655 之间）→ 用 `brief` 与 `full`；
- §4.4 的 `updateLinkedTodoBar`（app.js:2474 后）→ 收起态用 `brief`、展开态用 `titles`、
  `title` 属性用 `full`；
- §3.4 的 `sessionMatchesQuery`（app.js:581-585）→ 用 `full.toLowerCase()`。

⚠️ **`|| []` 兜底只写在这一个函数里**，其余三处只判 `if (!info) return;`。
这样"字段缺失容忍"（§3.3 讨论的 `patchSessionRow` / WS 场景）**只有一处实现**。

⚠️ **转义仍在各消费点做**，不要把 `escapeHtml` 塞进 `linkedTodoText`——
因为详情页走 `textContent`（无需转义，同 `updateWorkdirBar` app.js:2460 的写法），
而列表行走 `el()` 的第三参即 `innerHTML`（app.js:4758，**必须转义**）。
把转义放进派生函数会让详情页出现 `&amp;` 字面量。**这是本节最容易写错的一点。**
详情页若用 `innerHTML` 拼 toggle span，则标题部分必须 `escapeHtml`；
更省事的做法：bar 内放两个固定子节点——`<span class="todo-bar-text">` 走 `textContent`
+ `<span class="todo-bar-toggle">` 走固定文案——**完全避开 innerHTML**。**推荐后者。**

### 4.6 看板侧改动后的详情页刷新（可选，建议做）

`showEditTodoModal` 保存后只 `await renderKanban()`（app.js:1638），
`archiveTodo` / `unarchiveTodo` / `deleteTodo`（app.js:1143-1169）也只走
`refreshKanbanAfterTodoMutation`（app.js:1128-1141）→ 只重渲染看板。
所以**在看板里改了会话关联后，会话列表与详情页的待办展示都是旧的**，直到下次 `loadSessions()`。

最小修法：在 `refreshKanbanAfterTodoMutation` 成功分支（app.js:1139 之前）追加
`loadSessions().catch(() => {});`，并在 `showEditTodoModal` 保存成功处（app.js:1638 后）也加一次。
⚠️ `loadSessions` 内部会 `renderSessionLists()` + `renderKanban()`（app.js:350-352），
这会造成**看板重复渲染一次**。若嫌重复，改为只在这两处 `state.sessions = await api("/api/sessions")`
后调 `renderSessionLists()`。**developer 二选一。**
本项收益是"看板改完立刻同步"，不做也不算 bug（下次任何 `loadSessions` 都会自愈），
**优先级低于 §4.4**。

### 4.7 CSS 落点

**`web/style.css:242` 之后**（紧跟 `.sid-bar:hover`、`#engine-select`（style.css:243）之前），
与 `.workdir-bar` / `.sid-bar` 放在一起——**必须落在这里**，不要落到 §3.5 的 `.s-todo` 旁边，
因为它属于 detail-meta 家族，后续读者按区块找样式。

```
.todo-bar { display: inline-flex; align-items: center; gap: 4px; cursor: pointer;
            font-size: 11.5px; color: var(--accent); opacity: .85;
            max-width: 42%; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.todo-bar:hover { opacity: 1; }
.todo-bar.expanded { flex: 1 1 100%; max-width: 100%; white-space: pre-wrap;
                     word-break: break-word; overflow: visible; text-overflow: clip;
                     align-items: flex-start; }
.todo-bar-toggle { font-size: 11px; color: var(--accent); opacity: .8; flex: 0 0 auto; }
```

命名依据：
- `.todo-bar` 与同行邻居 `.workdir-bar` / `.sid-bar` **同 `-bar` 后缀**（style.css:239/241）；
- 用 `.expanded` 而非 `.collapsed`：§4.2 复用的 `goal-summary-panel` 是 `.collapsed`
  （默认展开、加 class 收起），**这里默认收起、加 class 展开，语义相反**，
  所以 `.expanded` 更直白。这是对既有范式的**合理镜像，不是新范式**——
  "toggle 一个 class + CSS 控形态 + 同步切 toggle 文案"的机制完全一致。
- 颜色取 `var(--accent)`，与列表侧 `.s-todo`（§3.5）和 `.kanban-row-hint`
  （style.css:1886-1890）同一档强调色，跨三处视觉统一。

**移动端（style.css:1070-1079 那段 `.detail-head` media query 内）追加**：
```
.todo-bar { max-width: 100%; font-size: 12px; }
```
理由：移动端 `.detail-head` 是纵向 flex（style.css:1072），横向空间本就紧张，
`42%` 会截得只剩两三个字；给满宽让它自己占 meta 的一行。字号与移动端
`.s-sub` / `.s-meta` 的放大口径一致（style.css:1058-1059）。
**这条容易漏，明确要求改**（同 §2.6 对 `.s-pin` 热区那行的要求）。

### 4.8 详情页部分的验证（补充 §6）

**手工（用户/ops 在 UI 上走一遍，4 步）**：
1. 点一个**有**关联待办的会话 → 详情页 meta 行出现 `📋 <标题> 展开`，且**单行不换行**、
   `#engine-select` 仍在原位；点一下 → 展开成整行全文，chat 区被往下推但无横向覆盖。
2. **展开态不跨会话残留**：展开后，在列表点另一个**也有**待办的会话 →
   新会话必须是**收起态**（验 §4.4 的 `remove("expanded")`）。
3. **短路分支**（验最易漏的那处）：停在会话 A 的详情页，再点列表里**同一个 A** →
   待办条应仍正确显示并复位为收起态。此步不刷新 = app.js:2413 那处漏加。
4. 点一个**无**关联待办的会话 → meta 行**完全看不到** `📋`（不是空占位、不是残留上一个会话的）。

**静态契约测试（并入 §6.5 的 `tests/test_session_pin_todo_frontend_contract.py`）**，追加断言：
5. `index.html` 含 `id="linked-todo-bar"`，且该串出现在 `class="detail-meta"` 与
   `id="engine-select"` 之间（按出现位置比较下标即可）；
6. `app.js` 中 `updateLinkedTodoBar(` 出现**至少 3 次**（1 处定义 + 2 处 switchSession 调用；
   做了 §4.4 第三处则为 4 次）→ **这条专门防"只加了一处 switchSession 分支"**，
   是本节唯一的自动化护栏，**必须写**；
7. `app.js` 中 `linkedTodoText(` 出现**至少 3 次**（定义 + 列表行 + 详情页）
   → 防 §4.5 的"写了两套截断逻辑"；
8. `style.css` 中 `.todo-bar` 出现**至少 2 次**（基础样式 + 移动端 media query）
   → 防 §4.7 移动端那行漏改；
9. `updateLinkedTodoBar` 函数体内出现 `expanded` 的 `classList.remove`
   → 防展开态跨会话残留。

---

## 5. 影响面与回归风险清单

| 风险 | 位置 | 说明 |
|---|---|---|
| `update_session` 刷 `updated_at` | db.py:497-502 | 19 处调用（main.py 8 处、session_hub.py 11 处）。**禁止改它的通用行为**，pin 走新函数（§2.4） |
| 前端二次排序吃掉后端置顶 | app.js:413 | 唯一的 `items.sort`，**必须改**，否则两个主列表置顶无效（§2.3） |
| `list_sessions` 的另两个调用方 | main.py:54、session_import.py:48/73 | 都只读 `worktree_base`/`claude_session_id`，新字段与排序变化对它们无影响；但 main.py:54 走 `include_archived=True`，确认新 ORDER BY 不报错即可 |
| 秘书日报 | secretary.py:61-68 `_format_sessions` | 只读 `title`/`msgs`，不受影响 |
| 双进程迁移竞态 | db.py:253-258 | 80/8800 两进程同时 ALTER，`_add_col` 已吞 duplicate，照范式写即安全 |
| `linked_todo_count` 语义不能变 | db.py:437 → app.js:669/677/2356 | 它在控制删除按钮的锁定态与删除拦截提示。扩 SQL 时**数值必须与改动前一致**（§6） |
| `.s-actions` 从 2 个按钮变 3 个 | style.css:207-213、1061 | 移动端窄屏下 `.s-main` 是 `flex:1; min-width:0`，会自动压缩，不会溢出；但热区那行必须改（§2.6） |
| 详情页只加了一处 switchSession 调用 | app.js:2413 / 2439 | 短路分支（app.js:2408-2422）与完整加载分支各有一套 meta 同步三连，**漏任一处都是可感知 bug**；§4.8 第 6 条静态断言专防此项（§4.4） |
| 展开态跨会话残留 | app.js `updateLinkedTodoBar` | `.todo-bar.expanded` 是 DOM 上的常驻节点（不像列表行会整行重建），切会话时必须显式 `remove("expanded")`（§4.4） |
| `.detail-meta` 行被挤成两行 | style.css:238 / 239 | `.workdir-bar` 已占 `max-width:70%`，新 `.todo-bar` 占 42% → 超 100%，靠 `flex-wrap: wrap` 自动换行。**这是预期行为不是 bug**；移动端给满宽（§4.7） |

---

## 6. ops 只读验证步骤

全部为只读或幂等，**不重启服务**（服务重启另行由用户决定；`_add_col` 迁移需进程重启
或新进程首次 `init_db` 才生效，这点要在交付说明里写清）。

### 6.1 迁移与字段
```
# 1. pinned 列已加且默认 0
sqlite3 data/console.db "PRAGMA table_info(sessions);" | grep pinned
sqlite3 data/console.db "SELECT COUNT(*) FROM sessions WHERE pinned IS NULL;"   # 期望 0
sqlite3 data/console.db "SELECT pinned, COUNT(*) FROM sessions GROUP BY pinned;" # 期望全部 0（未置顶前）
```

### 6.2 `linked_todo_count` 未被扩 SQL 改坏（关键回归）
改动前基线已采集，ops 直接比对即可：
```
sqlite3 data/console.db "SELECT ts.session_id, COUNT(*) FROM todo_sessions ts
  JOIN todos t ON t.id=ts.todo_id WHERE (t.archived=0 OR t.archived IS NULL)
  GROUP BY ts.session_id;"
```
**期望恰好 4 行、每行计数为 1**（实测：6 条关联中 2 条的 todo 已归档，故活跃口径是 4）。
再用接口对齐：
```
curl -s -H "Authorization: Bearer $AUTH_TOKEN" localhost:8800/api/sessions \
 | python3 -c "import sys,json; d=json.load(sys.stdin);
print('sessions:', len(d));
print('with_count:', sum(1 for s in d if s.get('linked_todo_count')));
print('with_titles:', sum(1 for s in d if s.get('linked_todo_titles')));
print('mismatch:', [s['id'] for s in d if s.get('linked_todo_count',0) != len(s.get('linked_todo_titles') or [])])"
```
**三条硬断言**：`with_count == with_titles == 4`，且 `mismatch` 为空列表
（count 与 titles 长度必须逐行一致——这是 §3.2 两条查询口径是否统一的唯一检验）。

### 6.3 置顶排序生效（后端）
```
# 只读确认排序：pinned 的必须全部排在未 pinned 之前
curl -s -H "Authorization: Bearer $AUTH_TOKEN" localhost:8800/api/sessions \
 | python3 -c "import sys,json; d=json.load(sys.stdin);
p=[i for i,s in enumerate(d) if s.get('pinned')];
print('pinned idx:', p);
assert p == list(range(len(p))), '置顶未排到最前';
ts=[s['updated_at'] for s in d[len(p):]];
assert ts == sorted(ts, reverse=True), '非置顶段未按 updated_at 降序'; print('OK')"
```
需先由用户/ops 在 UI 上 pin 一个会话，或用一次写操作制造样本（见 6.4）。

### 6.4 pin/unpin 接口幂等 + 不刷 updated_at（关键，验 §2.4）
```
SID=<挑一个 updated_at 很老的会话 id>
sqlite3 data/console.db "SELECT id,pinned,updated_at FROM sessions WHERE id='$SID';"  # 记下 updated_at
curl -s -XPOST -H "Authorization: Bearer $AUTH_TOKEN" localhost:8800/api/sessions/$SID/pin
curl -s -XPOST -H "Authorization: Bearer $AUTH_TOKEN" localhost:8800/api/sessions/$SID/pin   # 重复一次验幂等
sqlite3 data/console.db "SELECT id,pinned,updated_at FROM sessions WHERE id='$SID';"
#   期望：pinned=1（两次调用后仍是 1，不是翻回 0）；updated_at 与记下的值【完全不变】
curl -s -XPOST -H "Authorization: Bearer $AUTH_TOKEN" localhost:8800/api/sessions/$SID/unpin
sqlite3 data/console.db "SELECT id,pinned,updated_at FROM sessions WHERE id='$SID';"
#   期望：pinned=0，updated_at 仍然【完全不变】  ← 若变了说明走了 update_session，§2.4 没落实
curl -s -o /dev/null -w '%{http_code}\n' -XPOST -H "Authorization: Bearer $AUTH_TOKEN" \
     localhost:8800/api/sessions/no-such-id/pin    # 期望 404
```

### 6.5 前端静态断言（照 tests/ 既有范式，无需浏览器）
项目已有"前端契约测试"范式：读 app.js 正文做正则断言，
见 `tests/test_history_refresh_frontend_contract.py:1-35`（`APP_JS = Path(...).read_text()`）。
建议 developer 补一个 `tests/test_session_pin_todo_frontend_contract.py`，至少断言：
1. `renderSessionRow` 函数体内出现 `s-pin` 且出现 `linked_todo_titles`；
2. `fillListGrouped` 函数体内的 `items.sort` 比较器**同时**含 `pinned` 与 `ts`
   （防 §2.3 被漏做，这是最容易漏的一处）；
3. `.s-peek, .s-del, .s-pin` 在 style.css 中出现两次（基础样式 + 移动端热区，防 §2.6 漏改）；
4. `renderSessionRow` 的归档分支内**不含** `s-pin`（§2.5 的约定）。

另 DB 侧可补 pytest，用 `temp_db` fixture（tests/conftest.py:14-20，
它把 `config.DB_PATH` 指到 tmp 后 `db.init_db()`，**绝不碰生产库**）：
断言 `list_sessions` 对 pinned 会话的排序，以及 `linked_todo_titles` 的
count/titles 一致性。参考 `tests/test_db_bulk_cleanup.py`。

```
python3 -m pytest tests/ -q      # 期望全绿；现有 17 个测试文件不应因本改动变红
```

---

## 7. 改动清单速查（developer 按序执行）

**阶段 A —— 需求2 置顶（先）**
1. `server/db.py:287` 后 → 加 `_add_col("sessions", "pinned INTEGER DEFAULT 0")`
2. `server/db.py:420` → `ORDER BY COALESCE(pinned,0) DESC, updated_at DESC`
3. `server/db.py`（`update_session` db.py:497 附近）→ 新增 `set_session_pinned(sid, pinned)`，不刷 `updated_at`
4. `server/main.py:286` 后 → 加 `POST /api/sessions/{sid}/pin` 与 `/unpin`
5. `web/app.js:403/408/410/413` → `fillListGrouped` 的 items 带 `pinned` 并改比较器 ⚠️必做
6. `web/app.js:637` 附近 → `if (s.pinned) li.classList.add("pinned")`
7. `web/app.js:663-680` → `.s-actions` 活跃分支插 pin 按钮（归档分支不插）
8. `web/app.js:2376` 附近 → 新增 `togglePinSession(id, pinned)`
9. `web/style.css:208` → 复合选择器加 `.s-pin`；`:213` 后加 `.s-pin` 相关规则；`:1061` 加 `.s-pin` 热区 ⚠️易漏

**阶段 B —— 需求1 关联待办（后）**
10. `server/db.py:427-437` → 扩批量查询，回填 `linked_todo_titles`（推荐明细查询 + Python 聚合 + `ORDER BY ts.created_at`）
10.5 `web/app.js:1778` 后 → 新增共用辅助 `linkedTodoText(s)`（§4.5）——**必须先于第 11/12 步**，两者都消费它
11. `web/app.js:650-655` 之间 → 新建 `.s-todo` 节点并加入 `main.append`
12. `web/app.js:581-585` → `sessionMatchesQuery` 并入待办标题匹配
13. `web/style.css:204` 后 → 加 `.s-todo` 样式

**阶段 B2 —— 需求1 详情页联动（紧接阶段 B，依赖第 10 步的后端字段与第 10.5 步的辅助函数）**
14a. `web/index.html:269` 后 → 加 `<span id="linked-todo-bar" class="todo-bar hidden"></span>`（在 `#engine-select` 之前）
14b. `web/app.js:2474` 后 → 新增 `updateLinkedTodoBar(s)`（含 `remove("expanded")` 复位 + `onclick` toggle）
14c. `web/app.js:2413` 后 → 调 `updateLinkedTodoBar(cur)` ⚠️**短路分支，最易漏**
14d. `web/app.js:2439` 后 → 调 `updateLinkedTodoBar(cur)`（完整加载分支）
14e. `web/app.js:355` 附近 → `loadSessions` 的 `if (cur)` 内补一次调用（建议做，§4.4 第三处）
14f. `web/style.css:242` 后 → 加 `.todo-bar` / `.todo-bar:hover` / `.todo-bar.expanded` / `.todo-bar-toggle`
14g. `web/style.css:1070-1079` media query 内 → 加 `.todo-bar { max-width:100%; font-size:12px; }` ⚠️易漏
14h.（可选）`web/app.js:1139` 前 与 `:1638` 后 → 看板改动后刷新会话数据（§4.6）

**阶段 C**
15. 补 `tests/test_session_pin_todo_frontend_contract.py` + DB 侧 pytest（§6.5 的 4 条 + §4.8 的第 5-9 条，共 9 条断言）

后端改动**不涉及**接口新增以外的路由变更，前端改动集中在
`renderSessionRow` / `fillListGrouped` / `sessionMatchesQuery` / `switchSession`（两处分支）
四个既有函数 + 三个新函数（`togglePinSession` / `linkedTodoText` / `updateLinkedTodoBar`）。
**详情页不新增任何后端字段或接口**，完全复用 §3.2 回填到 `state.sessions` 的同一份数据（§4.5）。
`renderSessionRow` 被阶段 A 第 7 步与阶段 B 第 11 步各改一次，
**但落点不同段**（`actions` 块 vs `main` 块），按本顺序执行不会冲突。

---

## 8. 图谱报告的相关提示（仅作导航，结论均已用 Read/Grep 核实）

`graphify` CLI 本机未安装（`which graphify` 为空，已复核），改读 `graphify-out/GRAPH_REPORT.md`。
- 相关社区：`Frontend Session List Management`（报告 L193-195，38 节点，
  含 `fillList()`/`fillListGrouped()`/`deriveState()`/`deleteSession()`）与
  `Session-Todo Linking`（L41）、`Frontend Session List Management` 邻域。
  与本方案落点一致。
- `Session-Todo Linking` 社区（L143 附近）点出 `set_todo_sessions()` 与 `delete_session()`
  同群——已核实 `delete_session`（db.py:480-494）含"主会话字段晋升"逻辑
  （db.py:487-493），本方案不触碰它。
- **Knowledge Gaps（L313-316）与本需求无关**：47 个孤立节点是脚本/配置常量
  （`agent-tunnel.sh`、`AUTH_TOKEN`、`CLAUDE_BIN` 等），
  22 个 thin community 被省略。报告里**没有**关于 session 渲染的 gap 提示。
- 报告 L331 提到 `Frontend Chat UI` 社区 cohesion 仅 0.067 建议拆分——
  与本改动无关（我们动的是 session list 不是 chat），不在本轮处理。
