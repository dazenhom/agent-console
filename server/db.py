"""SQLite 持久化层。

表结构：
  sessions  会话：id / title / claude_session_id / workdir / status / 时间
  messages  消息：每一条对话事件（user / assistant_text / tool_use / tool_result / result / error）
  tasks     任务：一次 Agent 回合的汇总（耗时、花费、状态），用于看板展示
"""
import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from . import config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _now() -> float:
    return time.time()


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def init_db() -> None:
    global _conn
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    # WAL：双进程（80/8800）共享同一库时并发读写更稳，写不再阻塞读；busy_timeout 让并发写
    # 排队等锁而非立刻抛 "database is locked"。这些 PRAGMA 幂等，直接设即可。
    _conn.execute("PRAGMA journal_mode=WAL;")
    _conn.execute("PRAGMA busy_timeout=15000;")
    _conn.execute("PRAGMA synchronous=NORMAL;")
    with _lock:
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT,
                claude_session_id TEXT,
                workdir TEXT,
                status TEXT DEFAULT 'idle',
                mode TEXT DEFAULT 'fast',     -- fast / strong
                effort TEXT DEFAULT '',       -- 推理强度：low/medium/high/xhigh/max（空=用全局默认）
                engine TEXT DEFAULT 'claude', -- 底层 Agent 引擎：claude / codex
                summary TEXT DEFAULT '',      -- 行摘要：列表里"刚做了什么"一句话
                title_auto INTEGER DEFAULT 1, -- 标题是否由 AI 自动维护（用户手动改名后置 0）
                created_at REAL,
                updated_at REAL,
                archived INTEGER DEFAULT 0    -- 归档：从活跃列表隐藏，不物理删除
            );
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                role TEXT,          -- user / assistant / tool_use / tool_result / result / error / system
                content TEXT,       -- JSON 字符串
                created_at REAL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                summary TEXT,
                status TEXT,        -- running / success / error
                duration_ms INTEGER,
                cost_usd REAL,
                num_turns INTEGER,
                started_at REAL,
                ended_at REAL,
                remote_session_url TEXT,
                resolved_model TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_msg_session ON messages(session_id);
            CREATE INDEX IF NOT EXISTS idx_task_session ON tasks(session_id);
            CREATE TABLE IF NOT EXISTS snippets (
                id TEXT PRIMARY KEY,
                label TEXT NOT NULL,
                text TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS schedules (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                prompt TEXT NOT NULL,
                kind TEXT,            -- interval / daily / goal
                interval_min INTEGER, -- kind=interval：每多少分钟
                at_hhmm TEXT,         -- kind=daily：每天 HH:MM
                enabled INTEGER DEFAULT 1,
                next_run REAL,
                last_run REAL,
                created_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_sched_session ON schedules(session_id);
            CREATE TABLE IF NOT EXISTS todos (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                priority INTEGER DEFAULT 0,
                session_id TEXT,
                created_at REAL,
                updated_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_todos_status ON todos(status);
            CREATE TABLE IF NOT EXISTS todo_sessions (
                todo_id TEXT,
                session_id TEXT,
                created_at REAL,
                PRIMARY KEY (todo_id, session_id)
            );
            CREATE INDEX IF NOT EXISTS idx_todosess_todo ON todo_sessions(todo_id);
            CREATE INDEX IF NOT EXISTS idx_todosess_sess ON todo_sessions(session_id);
            CREATE TABLE IF NOT EXISTS reports (
                id TEXT PRIMARY KEY,
                report_date TEXT NOT NULL,
                report_type TEXT NOT NULL,
                content TEXT NOT NULL,
                session_id TEXT,
                created_at REAL,
                UNIQUE(report_date, report_type)
            );
            CREATE INDEX IF NOT EXISTS idx_reports_date ON reports(report_date DESC);
            CREATE TABLE IF NOT EXISTS queue_items (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                text TEXT,
                created_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_queue_session ON queue_items(session_id);
            CREATE TABLE IF NOT EXISTS memos (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                status TEXT DEFAULT 'active',
                remind_enabled INTEGER DEFAULT 1,
                reminded_date TEXT DEFAULT '',
                last_reminded_at REAL DEFAULT 0,
                created_at REAL,
                updated_at REAL,
                remind_mode TEXT DEFAULT 'daily',
                remind_at TEXT DEFAULT '',
                remind_days_before INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_memos_status ON memos(status);
            CREATE TABLE IF NOT EXISTS artifacts (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                url TEXT NOT NULL,
                title TEXT DEFAULT '',
                favicon TEXT DEFAULT '',
                description TEXT DEFAULT '',
                label TEXT DEFAULT '',
                published_at REAL,
                created_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_artifacts_session ON artifacts(session_id);
            CREATE TABLE IF NOT EXISTS job_runs (
                id TEXT PRIMARY KEY,
                kind TEXT,           -- triage / goal_verify / kanban_progress
                session_id TEXT,
                schedule_id TEXT,
                status TEXT,         -- running / success / timeout / error
                model TEXT,
                input_summary TEXT,
                output TEXT,         -- 解析后的结论（分诊/验收/摘要文本），由调用方补写
                error TEXT,
                log_path TEXT,       -- job_logs/<id>.log 绝对路径
                started_at REAL,
                ended_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_jobruns_started ON job_runs(started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_jobruns_kind ON job_runs(kind);
            CREATE TABLE IF NOT EXISTS arbitrations (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                question TEXT,
                status TEXT,          -- running / done / error
                engine_a TEXT, model_a TEXT, result_a TEXT, job_a_id TEXT,
                engine_b TEXT, model_b TEXT, result_b TEXT, job_b_id TEXT,
                arbiter_model TEXT, verdict TEXT, job_final_id TEXT,
                stage TEXT DEFAULT '',  -- 仲裁阶段进展：''/pending/running_ab/a_done/b_done/arbitrating/done/error
                error TEXT,
                created_at REAL, updated_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_arb_created ON arbitrations(created_at DESC);
            CREATE TABLE IF NOT EXISTS dispatch_subtasks (
                id TEXT PRIMARY KEY,
                plan_id TEXT,
                parent_session_id TEXT,
                seq INTEGER,
                title TEXT, instruction TEXT,
                category TEXT,          -- plan / deep / dev / codex
                engine TEXT, model TEXT,
                child_session_id TEXT,
                status TEXT,            -- dispatched / verifying / done / failed / error
                verdict TEXT DEFAULT '',   -- 完成判定结果：done / failed
                feedback TEXT DEFAULT '',  -- 验收反馈文本
                base_workdir TEXT DEFAULT '', -- 该子任务派发时的原始工作目录（worktree 取 repo 根，共享取 workdir），重派据此复位
                isolate INTEGER DEFAULT 0, -- 是否 worktree 隔离，重派据此决定新会话隔离方式
                need_arbitration INTEGER DEFAULT 0, -- 困难任务标记：验收走背对背双评委仲裁而非单 judge
                created_at REAL, updated_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_dispatch_plan ON dispatch_subtasks(plan_id);
            CREATE TABLE IF NOT EXISTS goal_iterations (
                id TEXT PRIMARY KEY,
                schedule_id TEXT,
                iter_no INTEGER,
                prompt TEXT,             -- 本轮派发给会话的完整指令（目标+完成标准+上轮反馈）
                task_id TEXT DEFAULT '', -- 关联 tasks.id：本轮 start_turn 产生的回合，用于查耗时/花费/状态
                verify_job_id TEXT DEFAULT '', -- 关联 job_runs.id：本轮验收调用记录
                verdict TEXT DEFAULT '',       -- 收敛判定结果：done / continue / exhausted
                feedback TEXT DEFAULT '',      -- 验收反馈文本
                produced_excerpt TEXT DEFAULT '', -- 喂给 verifier 的产出摘要（含命令结果/代码改动），截断存档
                status TEXT DEFAULT 'producing', -- producing / verifying / done / continue / exhausted / error
                started_at REAL,
                ended_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_goaliter_schedule ON goal_iterations(schedule_id, iter_no);
            CREATE TABLE IF NOT EXISTS goal_subtasks (
                id TEXT PRIMARY KEY,
                schedule_id TEXT,
                seq INTEGER,
                title TEXT, instruction TEXT,
                category TEXT,                 -- plan / deep / dev
                status TEXT DEFAULT 'pending', -- pending / running / done / skipped / error
                attempts INTEGER DEFAULT 0,    -- 该子任务已派发验收的轮次数
                last_feedback TEXT DEFAULT '', -- 最近一轮验收反馈（未过时喂给下轮）
                verify_job_id TEXT DEFAULT '', -- 关联 job_runs.id：最近一次验收记录
                task_id TEXT DEFAULT '',       -- 关联 tasks.id：最近一轮 start_turn 回合
                created_at REAL, updated_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_goalsub_schedule ON goal_subtasks(schedule_id, seq);
            CREATE TABLE IF NOT EXISTS work_items (
                id TEXT PRIMARY KEY,
                origin TEXT,        -- goal / dispatch / arbiter / triage：来源机制
                topology TEXT,      -- iterate / fanout / candidates：执行拓扑
                isolation TEXT,     -- shared / worktree：工作区隔离方式
                verify_mode TEXT,   -- none / nl / command / candidates：验收方式
                status TEXT,        -- pending / running / verifying / done / exhausted / error
                ref_id TEXT,        -- 指向来源表主键（schedule id / dispatch plan_id / arbitration id）
                session_id TEXT,
                summary TEXT,       -- 简短描述（goal/triage 的目标、dispatch 的需求、arbiter 的问题前缀）
                created_at REAL, updated_at REAL
            );
            CREATE INDEX IF NOT EXISTS idx_workitems_origin ON work_items(origin, created_at DESC);
            """
        )
        # 兼容老库：缺列就补。双进程（80/8800）可能同时启动产生竞态——
        # 两个连接都读到没列、都去 ALTER，一个成功另一个报 duplicate。故吞掉 duplicate。
        def _add_col(table, coldef):
            try:
                _conn.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
            except sqlite3.OperationalError as e:
                if "duplicate column" not in str(e).lower():
                    raise
        cols = [r[1] for r in _conn.execute("PRAGMA table_info(sessions)").fetchall()]
        if "mode" not in cols:
            _add_col("sessions", "mode TEXT DEFAULT 'fast'")
        if "effort" not in cols:
            _add_col("sessions", "effort TEXT DEFAULT ''")
        if "engine" not in cols:
            _add_col("sessions", "engine TEXT DEFAULT 'claude'")
        if "summary" not in cols:
            _add_col("sessions", "summary TEXT DEFAULT ''")
        if "is_secretary" not in cols:
            _add_col("sessions", "is_secretary INTEGER DEFAULT 0")
        if "title_auto" not in cols:
            _add_col("sessions", "title_auto INTEGER DEFAULT 1")
        if "archived" not in cols:
            _add_col("sessions", "archived INTEGER DEFAULT 0")
        # Git worktree 会话隔离三列：分支名 / 是否隔离 / 原 repo 目录（删除时据此清理 worktree）
        if "worktree_branch" not in cols:
            _add_col("sessions", "worktree_branch TEXT DEFAULT ''")
        if "is_worktree" not in cols:
            _add_col("sessions", "is_worktree INTEGER DEFAULT 0")
        if "worktree_base" not in cols:
            _add_col("sessions", "worktree_base TEXT DEFAULT ''")
        # /compact 上下文压缩：待注入下一条消息的摘要前缀（拿到新 claude_session_id 后清空）/ 最近一次压缩时间戳
        if "pending_compact_summary" not in cols:
            _add_col("sessions", "pending_compact_summary TEXT DEFAULT ''")
        if "compacted_at" not in cols:
            _add_col("sessions", "compacted_at REAL DEFAULT 0")
        # 看板进展摘要三列：正文 / 生成时间 / 生成时所依据的 jsonl mtime（用于缓存判断）
        _add_col("todos", "progress TEXT DEFAULT ''")
        _add_col("todos", "progress_at REAL DEFAULT 0")
        _add_col("todos", "progress_src_mtime REAL DEFAULT 0")
        _add_col("todos", "archived INTEGER DEFAULT 0")
        # Triage 自动分流（H3）：来源 / 置信度 / 建议动作 / 分诊原始载荷(json) / 自动派单后的 schedule id
        _add_col("todos", "source TEXT DEFAULT 'manual'")
        _add_col("todos", "confidence REAL DEFAULT 0")
        _add_col("todos", "suggested_action TEXT DEFAULT ''")
        _add_col("todos", "triage_payload TEXT DEFAULT ''")
        _add_col("todos", "dispatched_schedule_id TEXT DEFAULT ''")
        _add_col("memos", "remind_mode TEXT DEFAULT 'daily'")
        _add_col("memos", "remind_at TEXT DEFAULT ''")
        _add_col("memos", "remind_days_before INTEGER DEFAULT 0")
        _add_col("tasks", "resolved_model TEXT")
        # 目标循环（kind=goal）：自然语言停止条件 / 迭代上限 / 已迭代次数 /
        # 状态机字段（running/producing/verifying/done/exhausted）/ 上一轮验收反馈
        _add_col("schedules", "stop_condition TEXT DEFAULT ''")
        _add_col("schedules", "max_iterations INTEGER DEFAULT 10")
        _add_col("schedules", "iter_count INTEGER DEFAULT 0")
        _add_col("schedules", "goal_status TEXT DEFAULT ''")
        _add_col("schedules", "last_feedback TEXT DEFAULT ''")
        # 可执行验收（B）：会话 workdir 下跑的验证命令；执行模式（C）：solo 裸 prompt / team 走 /console-dev 流水线
        _add_col("schedules", "verify_command TEXT DEFAULT ''")
        _add_col("schedules", "exec_mode TEXT DEFAULT 'solo'")
        # 目标拆解为子任务（A）：goal_mode=flat 走整体自迭代（旧行为）；planned 先拆成有序子任务，
        # 每轮只推进一个。plan_status 记拆解生命周期（''→planning→planned→finalizing→finalized/plan_failed），
        # active_subtask_id 记当前正在执行/验收的子任务，供跨 tick 的 producing/verifying 关联回本轮子任务。
        _add_col("schedules", "goal_mode TEXT DEFAULT 'flat'")
        _add_col("schedules", "plan_status TEXT DEFAULT ''")
        _add_col("schedules", "active_subtask_id TEXT DEFAULT ''")
        # 续跑重置成本窗口：成本熔断从此时间戳起算（点「继续」时置为当前时间），为空/0 时回退 created_at。
        _add_col("schedules", "cost_base_ts REAL DEFAULT 0")
        # 每目标成本上限（美元），<=0 回退全局 config.GOAL_MAX_COST_USD
        _add_col("schedules", "max_cost_usd REAL DEFAULT 0")
        # 老库 goal_iterations 补 produced_excerpt 列（新库已在 CREATE TABLE 里带上）
        _add_col("goal_iterations", "produced_excerpt TEXT DEFAULT ''")
        # 阶段4：给四张来源子表补 work_item_id 关联列，把它们挂到统一的 work_items 观测视图。
        # 纯附加（历史数据该列自然为空，属已知局限，不回填）；不删任何既有列、不改既有数据。
        _add_col("goal_iterations", "work_item_id TEXT DEFAULT ''")
        _add_col("goal_subtasks", "work_item_id TEXT DEFAULT ''")
        _add_col("dispatch_subtasks", "work_item_id TEXT DEFAULT ''")
        _add_col("arbitrations", "work_item_id TEXT DEFAULT ''")
        # 背对背仲裁阶段进展：running_ab / a_done / b_done / arbitrating / done / error，供前端分卡片显示实时文案
        _add_col("arbitrations", "stage TEXT DEFAULT ''")
        # 第五步：dispatch 完成判定——给子任务补验收结果/反馈两列。status 取值扩展为
        # dispatched → verifying → done/failed（error 保留：仅建子会话本身失败）。
        _add_col("dispatch_subtasks", "verdict TEXT DEFAULT ''")
        _add_col("dispatch_subtasks", "feedback TEXT DEFAULT ''")
        # 重派工作目录锚定：子任务自存派发时的原始工作目录与隔离选择，避免旧子会话被删/字段
        # 为空时退回共享大目录、新会话探索到不相关项目。老库补列，历史数据该列为空（重派回退旧逻辑）。
        _add_col("dispatch_subtasks", "base_workdir TEXT DEFAULT ''")
        _add_col("dispatch_subtasks", "isolate INTEGER DEFAULT 0")
        # 困难任务标记：为真则验收走背对背双评委仲裁（arbiter.verify_back_to_back）而非单 judge。老库补列，历史数据默认 0（走普通验收路径）。
        _add_col("dispatch_subtasks", "need_arbitration INTEGER DEFAULT 0")
        # 旧库的 reports 表无 UNIQUE 约束。SQLite 不支持 ADD CONSTRAINT，
        # 改用唯一索引补上去重保护（重复 report_date+report_type 再插入会被拦）。
        _conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_reports_unique ON reports(report_date, report_type)")
        _conn.commit()
        # 如果 snippets 表是空的，从 config 预置默认值
        if not _conn.execute("SELECT 1 FROM snippets LIMIT 1").fetchone():
            from . import config as _cfg
            for i, s in enumerate(_cfg.SNIPPETS):
                _conn.execute(
                    "INSERT OR IGNORE INTO snippets(id,label,text,sort_order) VALUES(?,?,?,?)",
                    (s["id"], s["label"], s["text"], i),
                )
        _conn.commit()
        # 回填老数据：把 todos.session_id 迁进关联表（INSERT OR IGNORE 幂等，可重复执行）
        _conn.execute(
            """
            INSERT OR IGNORE INTO todo_sessions (todo_id, session_id, created_at)
            SELECT id, session_id, created_at FROM todos
            WHERE session_id IS NOT NULL AND session_id != ''
            """
        )
        _conn.commit()


def _exec(sql: str, params: tuple = ()):  # 写操作
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur


def _query(sql: str, params: tuple = ()):  # 读操作
    with _lock:
        return _conn.execute(sql, params).fetchall()


# ---------- sessions ----------
def create_session(title: str, workdir: str, mode: str | None = None,
                   worktree_branch: str = "", is_worktree: int = 0, worktree_base: str = "",
                   engine: str = "claude", effort: str | None = None) -> dict:
    sid = new_id()
    now = _now()
    m = mode or config.CLAUDE_DEFAULT_MODE
    eff = effort or config.CLAUDE_DEFAULT_EFFORT
    _exec(
        "INSERT INTO sessions(id,title,claude_session_id,workdir,status,mode,effort,engine,"
        "worktree_branch,is_worktree,worktree_base,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, title or "新会话", None, workdir, "idle", m, eff, engine,
         worktree_branch, is_worktree, worktree_base, now, now),
    )
    return get_session(sid)


def get_session(sid: str) -> dict | None:
    rows = _query("SELECT * FROM sessions WHERE id=?", (sid,))
    return dict(rows[0]) if rows else None


def list_sessions(include_archived: bool = False, archived_only: bool = False) -> list[dict]:
    base = "SELECT * FROM sessions WHERE (is_secretary=0 OR is_secretary IS NULL)"
    if archived_only:
        base += " AND archived=1"
    elif not include_archived:
        base += " AND (archived=0 OR archived IS NULL)"
    rows = _query(base + " ORDER BY updated_at DESC", ())
    result = [dict(r) for r in rows]
    # 批量补 linked_todo_count（被多少个看板任务关联），供前端置灰删除按钮，避免 N+1
    sids = [d["id"] for d in result]
    link_map = {}
    if sids:
        placeholders = ",".join("?" * len(sids))
        link_rows = _query(
            f"SELECT ts.session_id, COUNT(*) AS c FROM todo_sessions ts"
            f" JOIN todos t ON t.id = ts.todo_id"
            f" WHERE ts.session_id IN ({placeholders}) AND (t.archived = 0 OR t.archived IS NULL)"
            f" GROUP BY ts.session_id",
            tuple(sids),
        )
        for lr in link_rows:
            link_map[lr[0]] = lr[1]
    for d in result:
        d["linked_todo_count"] = link_map.get(d["id"], 0)
    # 批量补 dispatch_plan_id / dispatch_plan_title（该会话若是某调度批次的子会话），
    # 供前端把同批次子会话折叠成一组，避免 N+1
    plan_of_sid = {}   # session_id -> plan_id
    title_of_plan = {}  # plan_id -> 批次标题（取 seq 最小的子任务标题）
    if sids:
        placeholders = ",".join("?" * len(sids))
        sub_rows = _query(
            f"SELECT child_session_id, plan_id FROM dispatch_subtasks"
            f" WHERE child_session_id IN ({placeholders}) AND child_session_id != ''",
            tuple(sids),
        )
        for sr in sub_rows:
            plan_of_sid[sr[0]] = sr[1]
        plan_ids = list(set(plan_of_sid.values()))
        if plan_ids:
            pph = ",".join("?" * len(plan_ids))
            title_rows = _query(
                f"SELECT plan_id, title, seq FROM dispatch_subtasks"
                f" WHERE plan_id IN ({pph}) ORDER BY plan_id, seq ASC",
                tuple(plan_ids),
            )
            for tr in title_rows:
                # ORDER BY seq ASC，同 plan_id 的第一条即 seq 最小者
                if tr[0] not in title_of_plan:
                    title_of_plan[tr[0]] = tr[1]
    for d in result:
        pid = plan_of_sid.get(d["id"])
        d["dispatch_plan_id"] = pid
        d["dispatch_plan_title"] = title_of_plan.get(pid) if pid else None
    return result


def todos_linked_to_session(session_id: str) -> list[str]:
    """返回关联了该会话的【未归档】看板任务标题列表（归档任务不锁定会话，避免死锁）。"""
    rows = _query(
        "SELECT t.title FROM todo_sessions ts JOIN todos t ON t.id = ts.todo_id"
        " WHERE ts.session_id = ? AND (t.archived = 0 OR t.archived IS NULL)",
        (session_id,),
    )
    return [r[0] for r in rows]


def delete_session(sid: str) -> None:
    _exec("DELETE FROM messages WHERE session_id=?", (sid,))
    _exec("DELETE FROM tasks WHERE session_id=?", (sid,))
    _exec("DELETE FROM queue_items WHERE session_id=?", (sid,))
    _exec("DELETE FROM todo_sessions WHERE session_id=?", (sid,))
    # 主会话字段晋升：关联表已清完该会话，把仍以它为主会话的任务改指向剩余关联的下一个
    # （无剩余则置 NULL）。否则 todos.session_id 会留悬空引用，导致进展刷新永久失效、徽章虚高。
    _exec(
        "UPDATE todos SET session_id = ("
        "  SELECT session_id FROM todo_sessions WHERE todo_id = todos.id"
        "  ORDER BY created_at LIMIT 1"
        ") WHERE session_id = ?",
        (sid,),
    )
    _exec("DELETE FROM sessions WHERE id=?", (sid,))


def update_session(sid: str, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ",".join(f"{k}=?" for k in fields)
    _exec(f"UPDATE sessions SET {cols} WHERE id=?", (*fields.values(), sid))


def reconcile_stale_running() -> None:
    """进程重启后对齐"僵尸 running"状态：上次进程被杀时，DB 里可能残留 status='running'
    的会话与 tasks。它们的执行早已不在，重启后不会自愈，导致前端永久卡在"运行中"。
    启动时把它们归位：会话置 idle，未结束的任务置 error。"""
    _exec("UPDATE sessions SET status='idle' WHERE status='running'")
    _exec("UPDATE tasks SET status='error', ended_at=? WHERE status='running'", (_now(),))
    _exec("UPDATE job_runs SET status='error', error='进程重启中断', ended_at=? WHERE status='running'", (_now(),))
    # 背对背仲裁：跑到一半随进程消失的置为 error，避免前端永久轮询"running"
    _exec("UPDATE arbitrations SET status='error', error='服务重启中断', updated_at=? WHERE status='running'", (_now(),))


# ---------- messages ----------
def add_message(session_id: str, role: str, content: dict) -> dict:
    mid = new_id()
    now = _now()
    _exec(
        "INSERT INTO messages(id,session_id,role,content,created_at) VALUES(?,?,?,?,?)",
        (mid, session_id, role, json.dumps(content, ensure_ascii=False), now),
    )
    update_session(session_id)
    return {"id": mid, "session_id": session_id, "role": role, "content": content, "created_at": now}


def count_messages(session_id: str) -> int:
    rows = _query("SELECT COUNT(*) FROM messages WHERE session_id=?", (session_id,))
    return rows[0][0] if rows else 0


def count_user_messages(session_id: str) -> int:
    rows = _query("SELECT COUNT(*) FROM messages WHERE session_id=? AND role='user'", (session_id,))
    return rows[0][0] if rows else 0


def list_messages(session_id: str) -> list[dict]:
    rows = _query("SELECT * FROM messages WHERE session_id=? ORDER BY created_at ASC", (session_id,))
    out = []
    for r in rows:
        d = dict(r)
        d["content"] = json.loads(d["content"])
        out.append(d)
    return out


def search_message_sessions(q: str, limit: int = 200) -> list:
    """按内容子串匹配 messages.content，返回去重后的 session_id 列表。"""
    like = f"%{q}%"
    rows = _query(
        "SELECT DISTINCT session_id FROM messages "
        "WHERE content LIKE ? ESCAPE '\\' "
        "ORDER BY created_at DESC LIMIT ?",
        (like, limit),
    )
    return [r[0] for r in rows if r[0]]


# ---------- tasks ----------
def start_task(session_id: str, summary: str) -> str:
    tid = new_id()
    _exec(
        "INSERT INTO tasks(id,session_id,summary,status,started_at) VALUES(?,?,?,?,?)",
        (tid, session_id, summary, "running", _now()),
    )
    return tid


def finish_task(tid: str, status: str, duration_ms=None, cost_usd=None, num_turns=None) -> None:
    _exec(
        "UPDATE tasks SET status=?,duration_ms=?,cost_usd=?,num_turns=?,ended_at=? WHERE id=?",
        (status, duration_ms, cost_usd, num_turns, _now(), tid),
    )


def list_tasks(limit: int = 30) -> list[dict]:
    rows = _query("SELECT * FROM tasks ORDER BY started_at DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def get_latest_task(session_id: str) -> dict | None:
    """该会话最近一条 tasks 记录（含 status），用于判断回合是否真正结束。
    fire-and-forget 的 start_turn 从写 status='running' 到 task 注册进 hub._turns 有窗口，
    调用方据此避开"还没起跑就被误判成已跑完"的时序竞态。"""
    rows = _query("SELECT * FROM tasks WHERE session_id=? ORDER BY started_at DESC LIMIT 1", (session_id,))
    return dict(rows[0]) if rows else None


def update_task(tid: str, **fields) -> bool:
    allowed = {"resolved_model", "summary", "status"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    cols = ", ".join(f"{k}=?" for k in updates)
    cur = _exec(f"UPDATE tasks SET {cols} WHERE id=?", (*updates.values(), tid))
    return cur.rowcount > 0


# ---------- job_runs（一次性子进程运行记录：triage / goal_verify / kanban_progress）----------
def start_job(kind: str, session_id: str | None = None, schedule_id: str | None = None,
              model: str = "", input_summary: str = "") -> str:
    jid = new_id()
    _exec(
        "INSERT INTO job_runs(id,kind,session_id,schedule_id,status,model,input_summary,started_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (jid, kind, session_id, schedule_id, "running", model, input_summary, _now()),
    )
    return jid


def finish_job(jid: str, status: str, output: str = "", error: str = "", log_path: str = "") -> None:
    _exec(
        "UPDATE job_runs SET status=?,output=?,error=?,log_path=?,ended_at=? WHERE id=?",
        (status, output, error, log_path, _now(), jid),
    )


def set_job_output(jid: str, output: str) -> None:
    _exec("UPDATE job_runs SET output=? WHERE id=?", (output, jid))


def list_jobs(kind: str | None = None, limit: int = 50) -> list[dict]:
    if kind:
        rows = _query(
            "SELECT * FROM job_runs WHERE kind=? ORDER BY started_at DESC LIMIT ?",
            (kind, limit),
        )
    else:
        rows = _query("SELECT * FROM job_runs ORDER BY started_at DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def get_job(jid: str) -> dict | None:
    rows = _query("SELECT * FROM job_runs WHERE id=?", (jid,))
    return dict(rows[0]) if rows else None


# ---------- snippets ----------
def list_snippets() -> list[dict]:
    rows = _query("SELECT * FROM snippets ORDER BY sort_order ASC, id ASC")
    return [dict(r) for r in rows]


def create_snippet(label: str, text: str) -> dict:
    sid = new_id()
    max_order = _query("SELECT COALESCE(MAX(sort_order),0) FROM snippets")[0][0]
    _exec("INSERT INTO snippets(id,label,text,sort_order) VALUES(?,?,?,?)",
          (sid, label, text, max_order + 1))
    return {"id": sid, "label": label, "text": text, "sort_order": max_order + 1}


def update_snippet(sid: str, label: str | None, text: str | None) -> bool:
    row = _query("SELECT id FROM snippets WHERE id=?", (sid,))
    if not row:
        return False
    if label is not None:
        _exec("UPDATE snippets SET label=? WHERE id=?", (label, sid))
    if text is not None:
        _exec("UPDATE snippets SET text=? WHERE id=?", (text, sid))
    return True


def delete_snippet(sid: str) -> bool:
    row = _query("SELECT id FROM snippets WHERE id=?", (sid,))
    if not row:
        return False
    _exec("DELETE FROM snippets WHERE id=?", (sid,))
    return True


# ---------- schedules（定时/周期任务）----------
def list_schedules() -> list[dict]:
    rows = _query("SELECT * FROM schedules ORDER BY created_at DESC")
    return [dict(r) for r in rows]


def get_schedule(sid: str) -> dict | None:
    rows = _query("SELECT * FROM schedules WHERE id=?", (sid,))
    return dict(rows[0]) if rows else None


def create_schedule(session_id: str, prompt: str, kind: str, interval_min, at_hhmm, next_run: float,
                    stop_condition: str = "", max_iterations: int = 10, goal_status: str = "",
                    verify_command: str = "", exec_mode: str = "solo", goal_mode: str = "flat",
                    max_cost_usd: float = 0) -> dict:
    sid = new_id()
    _exec(
        "INSERT INTO schedules(id,session_id,prompt,kind,interval_min,at_hhmm,enabled,next_run,last_run,created_at,"
        "stop_condition,max_iterations,iter_count,goal_status,last_feedback,verify_command,exec_mode,goal_mode,max_cost_usd)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, session_id, prompt, kind, interval_min, at_hhmm, 1, next_run, None, _now(),
         stop_condition, max_iterations, 0, goal_status, "", verify_command, exec_mode, goal_mode, max_cost_usd),
    )
    return get_schedule(sid)


def update_schedule(sid: str, **fields) -> bool:
    if not get_schedule(sid):
        return False
    if not fields:
        return True
    cols = ",".join(f"{k}=?" for k in fields)
    _exec(f"UPDATE schedules SET {cols} WHERE id=?", (*fields.values(), sid))
    return True


def delete_schedule(sid: str) -> bool:
    if not get_schedule(sid):
        return False
    _exec("DELETE FROM schedules WHERE id=?", (sid,))
    return True


def due_schedules(now_ts: float) -> list[dict]:
    rows = _query("SELECT * FROM schedules WHERE enabled=1 AND next_run IS NOT NULL AND next_run<=?", (now_ts,))
    return [dict(r) for r in rows]


def sum_session_cost(session_id: str, since_ts: float) -> float:
    """某会话自 since_ts 起累计的回合花费（美元），用于目标循环的成本熔断。"""
    rows = _query(
        "SELECT COALESCE(SUM(cost_usd),0) FROM tasks WHERE session_id=? AND started_at>=?",
        (session_id, since_ts),
    )
    return float(rows[0][0]) if rows else 0.0


def reconcile_goal_schedules() -> None:
    """进程重启后把卡在中间态的目标循环复位：producing/verifying 的 fire-and-forget
    任务已随进程消失，不会自愈；重启时归位到 running，下个 tick 重新推进。"""
    _exec(
        "UPDATE schedules SET goal_status='running' WHERE kind='goal' AND goal_status IN ('producing','verifying')"
    )
    # planned 模式：拆解(planning)/收尾验收(finalizing)的后台任务同样随进程消失，复位到可重跑的态
    #   planning → ''（下个 tick 重新拆解）；finalizing → planned（回到子任务收尾判定，重跑最终验收）
    _exec("UPDATE schedules SET plan_status='' WHERE kind='goal' AND plan_status='planning'")
    _exec("UPDATE schedules SET plan_status='planned' WHERE kind='goal' AND plan_status='finalizing'")
    # 子任务卡在 running（其 start_turn 回合随进程消失）→ 复位 pending，下轮重新派发
    _exec("UPDATE goal_subtasks SET status='pending' WHERE status='running'")


def reconcile_dispatch_subtasks() -> None:
    """进程重启后复位卡在 verifying 的 dispatch 子任务：其后台判定任务（_run_dispatch_verify）
    随进程消失，不会自愈，而 _tick_fanout 只拾取 status='dispatched' 的记录，导致这条子任务永久
    卡死、所属 plan 也无法触发聚合收尾。启动时复位到 dispatched：下个 tick 会在子会话已空闲时
    重新起判定，子会话仍在运行则跳过，不会重复触发。一次性、幂等的兜底。"""
    _exec("UPDATE dispatch_subtasks SET status='dispatched', updated_at=? WHERE status='verifying'", (_now(),))


def has_active_goal(session_id: str) -> bool:
    """该会话是否挂着一条启用中、且未进入终态的目标循环。用于抑制 per-turn 企微通知。"""
    rows = _query(
        "SELECT 1 FROM schedules WHERE session_id=? AND kind='goal' AND enabled=1"
        " AND goal_status NOT IN ('done','exhausted') LIMIT 1",
        (session_id,),
    )
    return bool(rows)


# ---------- todos（待办事项）----------
def list_todos(status: str | None = None, include_archived: bool = False, archived_only: bool = False) -> list[dict]:
    where = []
    params: list = []
    if status:
        where.append("status=?")
        params.append(status)
    if archived_only:
        where.append("archived=1")
    elif not include_archived:
        where.append("(archived=0 OR archived IS NULL)")
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    rows = _query(f"SELECT * FROM todos{clause} ORDER BY priority DESC, created_at ASC", tuple(params))
    result = [dict(r) for r in rows]
    # 批量补 session_ids（关联多个会话）：一次查关联表再映射回每行，避免 N+1
    todo_ids = [d["id"] for d in result]
    mapping: dict[str, list] = {}
    if todo_ids:
        placeholders = ",".join("?" * len(todo_ids))
        sess_rows = _query(
            f"SELECT todo_id, session_id FROM todo_sessions WHERE todo_id IN ({placeholders}) ORDER BY created_at",
            tuple(todo_ids),
        )
        for sr in sess_rows:
            mapping.setdefault(sr[0], []).append(sr[1])
    for d in result:
        d["session_ids"] = mapping.get(d["id"], [])
    return result


def list_todo_session_ids(todo_id: str) -> list:
    rows = _query(
        "SELECT session_id FROM todo_sessions WHERE todo_id=? ORDER BY created_at",
        (todo_id,),
    )
    return [r[0] for r in rows]


def set_todo_sessions(todo_id: str, session_ids: list) -> None:
    """覆盖式设置任务关联会话；同步把主会话（列表第一个）写回 todos.session_id。"""
    now = _now()
    with _lock:
        # 先取该 todo 当前已关联的所有 session_id（作为白名单，保留孤儿关联）
        existing = set(
            r[0] for r in _conn.execute(
                "SELECT session_id FROM todo_sessions WHERE todo_id=?", (todo_id,)
            ).fetchall()
        )
        _conn.execute("DELETE FROM todo_sessions WHERE todo_id=?", (todo_id,))
        # 过滤非法 session_id，避免把不存在的会话写进关联表产生悬空引用
        if session_ids:
            placeholders = ','.join('?' * len(session_ids))
            valid = set(
                r[0] for r in _conn.execute(
                    f"SELECT id FROM sessions WHERE id IN ({placeholders})", session_ids
                ).fetchall()
            )
            # 合法 = 在 sessions 表里 OR 本来就已关联（保留孤儿关联，不静默删）
            session_ids = [s for s in session_ids if s in valid or s in existing]
        for sid in session_ids:
            _conn.execute(
                "INSERT OR IGNORE INTO todo_sessions (todo_id, session_id, created_at) VALUES (?,?,?)",
                (todo_id, sid, now),
            )
        primary = session_ids[0] if session_ids else None
        _conn.execute("UPDATE todos SET session_id=? WHERE id=?", (primary, todo_id))
        _conn.commit()


def list_todos_by_session(session_id: str) -> list[dict]:
    rows = _query(
        "SELECT id FROM todos WHERE session_id=? AND status='in_progress'",
        (session_id,)
    )
    return [dict(r) for r in rows]


def create_todo(title: str, description: str = "", priority: int = 0, session_id: str | None = None, status: str = "pending") -> dict:
    tid = new_id()
    now = _now()
    _exec(
        "INSERT INTO todos(id,title,description,status,priority,session_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (tid, title, description, status, priority, session_id, now, now),
    )
    return dict(_query("SELECT * FROM todos WHERE id=?", (tid,))[0])


def create_triage_todo(title: str, description: str, confidence: float,
                       suggested_action: str, triage_payload: dict) -> dict:
    """建一条待分诊收件箱条目：status/source 固定 'triage'，原始载荷存 json。"""
    tid = new_id()
    now = _now()
    _exec(
        "INSERT INTO todos(id,title,description,status,priority,session_id,created_at,updated_at,"
        "source,confidence,suggested_action,triage_payload)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, title, description, "triage", 0, None, now, now,
         "triage", confidence, suggested_action, json.dumps(triage_payload, ensure_ascii=False)),
    )
    return dict(_query("SELECT * FROM todos WHERE id=?", (tid,))[0])


def count_triage_dispatched_today(since_ts: float) -> int:
    """自 since_ts 起分诊来源已自动派单的条数，用于每日自动派单上限。"""
    rows = _query(
        "SELECT COUNT(*) FROM todos WHERE source='triage' AND dispatched_schedule_id != '' AND updated_at>=?",
        (since_ts,),
    )
    return rows[0][0] if rows else 0


def update_todo(tid: str, **fields) -> bool:
    allowed = {"title", "description", "status", "priority", "session_id",
               "progress", "progress_at", "progress_src_mtime", "archived",
               "source", "confidence", "suggested_action", "triage_payload",
               "dispatched_schedule_id"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    updates["updated_at"] = _now()
    sets = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values()) + [tid]
    cur = _exec(f"UPDATE todos SET {sets} WHERE id=?", vals)
    return cur.rowcount > 0


def set_todo_progress(tid: str, progress: str, src_mtime: float) -> None:
    now = _now()
    _exec(
        "UPDATE todos SET progress=?, progress_at=?, progress_src_mtime=?, updated_at=? WHERE id=?",
        (progress, now, src_mtime, now, tid),
    )


def delete_todo(tid: str) -> bool:
    # 级联删除放进单个事务：两条语句要么都提交，要么都不提交，避免进程崩溃留下孤儿关联行
    with _lock:
        _conn.execute("DELETE FROM todo_sessions WHERE todo_id=?", (tid,))
        cur = _conn.execute("DELETE FROM todos WHERE id=?", (tid,))
        _conn.commit()
    return cur.rowcount > 0


# ---------- reports（日报）----------
def create_report(report_date: str, report_type: str, content: str, session_id: str | None = None) -> dict | None:
    rid = new_id()
    now = _now()
    cur = _exec(
        "INSERT OR IGNORE INTO reports(id,report_date,report_type,content,session_id,created_at) VALUES(?,?,?,?,?,?)",
        (rid, report_date, report_type, content, session_id, now),
    )
    if cur.rowcount == 0:
        return get_report_by_date_type(report_date, report_type)
    return dict(_query("SELECT * FROM reports WHERE id=?", (rid,))[0])


def list_reports(limit: int = 30) -> list[dict]:
    rows = _query("SELECT * FROM reports ORDER BY report_date DESC, report_type DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def get_report(rid: str) -> dict | None:
    rows = _query("SELECT * FROM reports WHERE id=?", (rid,))
    return dict(rows[0]) if rows else None


def get_report_by_date_type(report_date: str, report_type: str) -> dict | None:
    rows = _query("SELECT * FROM reports WHERE report_date=? AND report_type=?", (report_date, report_type))
    return dict(rows[0]) if rows else None


# ---------- secretary session（秘书会话）----------
def get_secretary_session() -> dict | None:
    rows = _query("SELECT * FROM sessions WHERE is_secretary=1 LIMIT 1")
    return dict(rows[0]) if rows else None


def ensure_secretary_session(workdir: str) -> dict:
    # 用 _lock 包裹「查→无则建」整段，避免双进程/并发下 TOCTOU 竞态建出两个秘书会话。
    # 注意 _lock 不可重入，故内部直接用 _conn 而非 _exec/_query。
    with _lock:
        row = _conn.execute("SELECT * FROM sessions WHERE is_secretary=1 LIMIT 1").fetchone()
        if row:
            return dict(row)
        sid = new_id()
        now = _now()
        _conn.execute(
            "INSERT INTO sessions(id,title,workdir,status,mode,is_secretary,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            (sid, "秘书 Agent", workdir, "idle", "strong", 1, now, now),
        )
        _conn.commit()
        row = _conn.execute("SELECT * FROM sessions WHERE is_secretary=1 LIMIT 1").fetchone()
        return dict(row)


# ---------- queue_items（会话内待执行的排队指令）----------
def enqueue_item(session_id: str, text: str) -> dict:
    qid = new_id()
    now = _now()
    _exec(
        "INSERT INTO queue_items(id,session_id,text,created_at) VALUES(?,?,?,?)",
        (qid, session_id, text, now),
    )
    return {"id": qid, "session_id": session_id, "text": text, "created_at": now}


def list_queue(session_id: str) -> list:
    rows = _query(
        "SELECT * FROM queue_items WHERE session_id=? ORDER BY created_at ASC, id ASC",
        (session_id,),
    )
    return [dict(r) for r in rows]


def get_queue_item(item_id: str):
    rows = _query("SELECT * FROM queue_items WHERE id=?", (item_id,))
    return dict(rows[0]) if rows else None


def update_queue_item(item_id: str, text: str) -> bool:
    if not get_queue_item(item_id):
        return False
    _exec("UPDATE queue_items SET text=? WHERE id=?", (text, item_id))
    return True


def delete_queue_item(item_id: str) -> bool:
    if not get_queue_item(item_id):
        return False
    _exec("DELETE FROM queue_items WHERE id=?", (item_id,))
    return True


def pop_next_queue_item(session_id: str):
    rows = _query(
        "SELECT * FROM queue_items WHERE session_id=? ORDER BY created_at ASC, id ASC LIMIT 1",
        (session_id,),
    )
    if not rows:
        return None
    item = dict(rows[0])
    _exec("DELETE FROM queue_items WHERE id=?", (item["id"],))
    return item


# ---------- memos（备忘录）----------
def list_memos(status: str | None = None) -> list[dict]:
    if status:
        rows = _query(
            "SELECT * FROM memos WHERE status=? ORDER BY status ASC, updated_at DESC",
            (status,),
        )
    else:
        rows = _query("SELECT * FROM memos ORDER BY status ASC, updated_at DESC")
    return [dict(r) for r in rows]


def create_memo(content: str, remind_enabled: int = 1, remind_mode: str = "daily",
                remind_at: str = "", remind_days_before: int = 0) -> str:
    mid = new_id()
    now = _now()
    _exec(
        "INSERT INTO memos(id,content,status,remind_enabled,reminded_date,last_reminded_at,"
        "remind_mode,remind_at,remind_days_before,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (mid, content, "active", remind_enabled, "", 0,
         remind_mode, remind_at, remind_days_before, now, now),
    )
    return mid


def update_memo(mid: str, **fields) -> bool:
    allowed = {"content", "status", "remind_enabled", "reminded_date", "last_reminded_at",
               "remind_mode", "remind_at", "remind_days_before"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    updates["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in updates)
    vals = list(updates.values()) + [mid]
    cur = _exec(f"UPDATE memos SET {cols} WHERE id=?", vals)
    return cur.rowcount > 0


def delete_memo(mid: str) -> bool:
    cur = _exec("DELETE FROM memos WHERE id=?", (mid,))
    return cur.rowcount > 0


def list_memos_to_remind() -> list[dict]:
    rows = _query("SELECT * FROM memos WHERE status='active'")
    return [dict(r) for r in rows]


# ---------- artifacts（产出物）----------
def create_artifact(session_id, url, title="", favicon="", description="", label="", published_at=None) -> dict:
    aid = new_id()
    now = _now()
    _exec(
        "INSERT INTO artifacts(id,session_id,url,title,favicon,description,label,published_at,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?)",
        (aid, session_id, url, title, favicon, description, label, published_at, now),
    )
    rows = _query("SELECT * FROM artifacts WHERE id=?", (aid,))
    return dict(rows[0])


def list_artifacts(session_id=None, limit=100) -> list:
    if session_id:
        rows = _query(
            "SELECT * FROM artifacts WHERE session_id=? ORDER BY created_at DESC LIMIT ?",
            (session_id, limit),
        )
    else:
        rows = _query(
            "SELECT * FROM artifacts ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    return [dict(r) for r in rows]


# ---------- arbitrations（背对背双执行 + 综合仲裁）----------
def create_arbitration(**fields) -> dict:
    aid = new_id()
    now = _now()
    fields.setdefault("status", "running")
    fields["id"] = aid
    fields["created_at"] = now
    fields["updated_at"] = now
    cols = ",".join(fields.keys())
    ph = ",".join("?" * len(fields))
    _exec(f"INSERT INTO arbitrations({cols}) VALUES({ph})", tuple(fields.values()))
    return get_arbitration(aid)


def get_arbitration(aid: str) -> dict | None:
    rows = _query("SELECT * FROM arbitrations WHERE id=?", (aid,))
    return dict(rows[0]) if rows else None


def list_arbitrations(limit: int = 50) -> list[dict]:
    rows = _query("SELECT * FROM arbitrations ORDER BY created_at DESC LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def update_arbitration(aid: str, **fields) -> bool:
    allowed = {"status", "result_a", "job_a_id", "result_b", "job_b_id",
               "verdict", "job_final_id", "error", "work_item_id", "stage", "updated_at"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    updates["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in updates)
    cur = _exec(f"UPDATE arbitrations SET {cols} WHERE id=?", (*updates.values(), aid))
    return cur.rowcount > 0


# ---------- dispatch_subtasks（角色化动态调度）----------
def create_dispatch_subtask(**fields) -> dict:
    sid = new_id()
    now = _now()
    fields["id"] = sid
    fields["created_at"] = now
    fields["updated_at"] = now
    cols = ",".join(fields.keys())
    ph = ",".join("?" * len(fields))
    _exec(f"INSERT INTO dispatch_subtasks({cols}) VALUES({ph})", tuple(fields.values()))
    return dict(_query("SELECT * FROM dispatch_subtasks WHERE id=?", (sid,))[0])


def list_dispatch_subtasks(plan_id: str) -> list[dict]:
    rows = _query(
        "SELECT * FROM dispatch_subtasks WHERE plan_id=? ORDER BY seq ASC, created_at ASC",
        (plan_id,),
    )
    return [dict(r) for r in rows]


def get_dispatch_subtask(sid: str) -> dict | None:
    rows = _query("SELECT * FROM dispatch_subtasks WHERE id=?", (sid,))
    return dict(rows[0]) if rows else None


def list_active_dispatch_plans() -> list[str]:
    """还有未终结子任务（status in dispatched/verifying）的 plan_id 列表，
    供调度 tick 发现待推进的 dispatch 扇出。仅只读，不做任何决策依据用途之外的读写。"""
    rows = _query(
        "SELECT DISTINCT plan_id FROM dispatch_subtasks"
        " WHERE status IN ('dispatched','verifying') ORDER BY plan_id"
    )
    return [r[0] for r in rows]


def list_dispatch_plans(limit: int = 20) -> list[dict]:
    """按 plan_id 聚合：每个 plan 取最早 created_at、子任务数、首个子任务标题作为汇总。"""
    rows = _query(
        "SELECT plan_id, MIN(created_at) AS created_at, COUNT(*) AS subtask_count,"
        " MIN(seq) AS min_seq"
        " FROM dispatch_subtasks GROUP BY plan_id ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    result = []
    for r in rows:
        d = dict(r)
        # 取该 plan 里 seq 最小那条的标题当汇总标题
        head = _query(
            "SELECT title FROM dispatch_subtasks WHERE plan_id=? ORDER BY seq ASC, created_at ASC LIMIT 1",
            (d["plan_id"],),
        )
        d["title"] = head[0][0] if head else ""
        result.append(d)
    return result


def update_dispatch_subtask(sid: str, **fields) -> bool:
    allowed = {"child_session_id", "status", "engine", "model", "category",
               "verdict", "feedback", "updated_at"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    updates["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in updates)
    cur = _exec(f"UPDATE dispatch_subtasks SET {cols} WHERE id=?", (*updates.values(), sid))
    return cur.rowcount > 0


# ---------- goal_iterations（目标循环每轮迭代历史：派发指令/子任务/验收判定）----------
def latest_task_id(session_id: str) -> str | None:
    """该会话最近一条 tasks 记录的 id，用于把刚发起的迭代回合关联进本轮历史。"""
    rows = _query("SELECT id FROM tasks WHERE session_id=? ORDER BY started_at DESC LIMIT 1", (session_id,))
    return rows[0][0] if rows else None


def get_latest_job(schedule_id: str, kind: str) -> dict | None:
    """该 schedule 最近一条指定 kind 的一次性子进程记录，用于把验收调用关联进本轮历史。"""
    rows = _query(
        "SELECT * FROM job_runs WHERE schedule_id=? AND kind=? ORDER BY started_at DESC LIMIT 1",
        (schedule_id, kind),
    )
    return dict(rows[0]) if rows else None


def create_goal_iteration(schedule_id: str, iter_no: int, prompt: str, task_id: str = "",
                          work_item_id: str = "") -> dict:
    gid = new_id()
    now = _now()
    _exec(
        "INSERT INTO goal_iterations(id,schedule_id,iter_no,prompt,task_id,work_item_id,status,started_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (gid, schedule_id, iter_no, prompt, task_id, work_item_id, "producing", now),
    )
    return get_goal_iteration(gid)


def get_goal_iteration(gid: str) -> dict | None:
    rows = _query("SELECT * FROM goal_iterations WHERE id=?", (gid,))
    return dict(rows[0]) if rows else None


def get_goal_iteration_by_no(schedule_id: str, iter_no: int) -> dict | None:
    rows = _query(
        "SELECT * FROM goal_iterations WHERE schedule_id=? AND iter_no=? ORDER BY started_at DESC LIMIT 1",
        (schedule_id, iter_no),
    )
    return dict(rows[0]) if rows else None


def update_goal_iteration(gid: str, **fields) -> bool:
    allowed = {"task_id", "verify_job_id", "verdict", "feedback", "produced_excerpt", "status", "ended_at"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    cols = ", ".join(f"{k}=?" for k in updates)
    cur = _exec(f"UPDATE goal_iterations SET {cols} WHERE id=?", (*updates.values(), gid))
    return cur.rowcount > 0


def list_goal_iterations(schedule_id: str) -> list[dict]:
    """某个目标循环的完整轮次历史（详情态用），按轮次升序，附带该轮回合的耗时/花费/状态。"""
    rows = _query(
        "SELECT * FROM goal_iterations WHERE schedule_id=? ORDER BY iter_no ASC, started_at ASC",
        (schedule_id,),
    )
    result = [dict(r) for r in rows]
    for d in result:
        if d.get("task_id"):
            trows = _query(
                "SELECT status,duration_ms,cost_usd,num_turns,started_at,ended_at FROM tasks WHERE id=?",
                (d["task_id"],),
            )
            d["task"] = dict(trows[0]) if trows else None
        else:
            d["task"] = None
    return result


def list_goal_loops(limit: int = 50) -> list[dict]:
    """目标循环历史列表态：所有 kind=goal 的 schedule，附带已记录的轮次数。"""
    rows = _query(
        "SELECT * FROM schedules WHERE kind='goal' ORDER BY created_at DESC LIMIT ?",
        (limit,),
    )
    result = [dict(r) for r in rows]
    for d in result:
        c = _query("SELECT COUNT(*) FROM goal_iterations WHERE schedule_id=?", (d["id"],))
        d["iteration_count"] = c[0][0] if c else 0
    return result


def get_goal_loop_detail(schedule_id: str) -> dict | None:
    """目标循环详情态：schedule 本体 + 完整轮次历史 + (planned 模式) 子任务清单。"""
    sch = get_schedule(schedule_id)
    if not sch:
        return None
    sch["iterations"] = list_goal_iterations(schedule_id)
    sch["subtasks"] = list_goal_subtasks(schedule_id)
    return sch


# ---------- goal_subtasks（planned 目标循环：目标拆解出的有序子任务）----------
def replace_goal_subtasks(schedule_id: str, subtasks: list[dict], work_item_id: str = "") -> None:
    """整体重置某目标循环的子任务清单（拆解成功后写入，或重启循环时清空传 []）。"""
    _exec("DELETE FROM goal_subtasks WHERE schedule_id=?", (schedule_id,))
    now = _now()
    for seq, st in enumerate(subtasks):
        _exec(
            "INSERT INTO goal_subtasks(id,schedule_id,seq,title,instruction,category,status,"
            "attempts,last_feedback,verify_job_id,task_id,work_item_id,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (new_id(), schedule_id, seq, (st.get("title") or "")[:200], st.get("instruction") or "",
             st.get("category") or "", "pending", 0, "", "", "", work_item_id, now, now),
        )


def list_goal_subtasks(schedule_id: str) -> list[dict]:
    rows = _query(
        "SELECT * FROM goal_subtasks WHERE schedule_id=? ORDER BY seq ASC, created_at ASC",
        (schedule_id,),
    )
    return [dict(r) for r in rows]


def get_goal_subtask(sid: str) -> dict | None:
    rows = _query("SELECT * FROM goal_subtasks WHERE id=?", (sid,))
    return dict(rows[0]) if rows else None


def next_pending_goal_subtask(schedule_id: str) -> dict | None:
    """下一个待执行子任务（按 seq 升序取第一个 pending），没有则 None。"""
    rows = _query(
        "SELECT * FROM goal_subtasks WHERE schedule_id=? AND status='pending'"
        " ORDER BY seq ASC, created_at ASC LIMIT 1",
        (schedule_id,),
    )
    return dict(rows[0]) if rows else None


def update_goal_subtask(sid: str, **fields) -> bool:
    allowed = {"status", "attempts", "last_feedback", "verify_job_id", "task_id"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    updates["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in updates)
    cur = _exec(f"UPDATE goal_subtasks SET {cols} WHERE id=?", (*updates.values(), sid))
    return cur.rowcount > 0


# ---------- work_items（阶段2 影子表→阶段4 统一"任务运行"聚合视图：观测四套机制的"发起"，
# 仍只作观测+展示用途，绝不作为任何状态机的决策依据；可整表 drop 回退）----------
def create_work_item(origin: str, topology: str, isolation: str, verify_mode: str,
                     status: str, ref_id: str = "", session_id: str = "", summary: str = "") -> str:
    wid = new_id()
    now = _now()
    _exec(
        "INSERT INTO work_items(id,origin,topology,isolation,verify_mode,status,"
        "ref_id,session_id,summary,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (wid, origin, topology, isolation, verify_mode, status,
         ref_id, session_id or "", (summary or "")[:200], now, now),
    )
    return wid


def create_work_item_safe(origin: str, topology: str, isolation: str, verify_mode: str,
                          status: str, ref_id: str = "", session_id: str = "", summary: str = "") -> str:
    """create_work_item 的容错包装：work_items 纯观测，写失败绝不影响主流程——
    异常只记日志并返回空串。四处发起点统一走这里，免得各自重复 try/except。"""
    try:
        return create_work_item(origin, topology, isolation, verify_mode, status,
                                ref_id=ref_id, session_id=session_id, summary=summary)
    except Exception as e:
        print(f"[work_items] {origin} insert failed: {type(e).__name__}: {e}")
        return ""


def update_work_item_status_by_ref(ref_id: str, status: str) -> None:
    """按来源表主键收尾对应影子记录状态（找不到静默跳过）。
    ref_id 全局唯一（schedule/plan/arbitration id），无需再按 origin 过滤。"""
    _exec("UPDATE work_items SET status=?,updated_at=? WHERE ref_id=?", (status, _now(), ref_id))


def work_item_id_for_ref(ref_id: str) -> str | None:
    """按来源表主键反查对应影子记录 id，供四处发起点把 work_item_id 回填进各自子表。
    ref_id 全局唯一；找不到（如历史数据、边缘情况）返回 None，调用方跳过即可。"""
    if not ref_id:
        return None
    rows = _query("SELECT id FROM work_items WHERE ref_id=? ORDER BY created_at DESC LIMIT 1", (ref_id,))
    return rows[0][0] if rows else None


def list_work_items(origin: str | None = None, status: str | None = None, limit: int = 50) -> list[dict]:
    """阶段4：统一"任务运行"聚合视图的列表态。按 origin/status 过滤（None/空则不过滤），
    纯只读参数化查询，不做任何决策依据用途。"""
    where = []
    params: list = []
    if origin:
        where.append("origin=?")
        params.append(origin)
    if status:
        where.append("status=?")
        params.append(status)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    rows = _query(f"SELECT * FROM work_items{clause} ORDER BY created_at DESC LIMIT ?", tuple(params))
    return [dict(r) for r in rows]


def get_work_item(wid: str) -> dict | None:
    rows = _query("SELECT * FROM work_items WHERE id=?", (wid,))
    return dict(rows[0]) if rows else None
