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
                summary TEXT DEFAULT '',      -- 行摘要：列表里"刚做了什么"一句话
                title_auto INTEGER DEFAULT 1, -- 标题是否由 AI 自动维护（用户手动改名后置 0）
                created_at REAL,
                updated_at REAL
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
                ended_at REAL
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
                kind TEXT,            -- interval / daily
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
        if "summary" not in cols:
            _add_col("sessions", "summary TEXT DEFAULT ''")
        if "is_secretary" not in cols:
            _add_col("sessions", "is_secretary INTEGER DEFAULT 0")
        if "title_auto" not in cols:
            _add_col("sessions", "title_auto INTEGER DEFAULT 1")
        # 看板进展摘要三列：正文 / 生成时间 / 生成时所依据的 jsonl mtime（用于缓存判断）
        _add_col("todos", "progress TEXT DEFAULT ''")
        _add_col("todos", "progress_at REAL DEFAULT 0")
        _add_col("todos", "progress_src_mtime REAL DEFAULT 0")
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


def _exec(sql: str, params: tuple = ()):  # 写操作
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur


def _query(sql: str, params: tuple = ()):  # 读操作
    with _lock:
        return _conn.execute(sql, params).fetchall()


# ---------- sessions ----------
def create_session(title: str, workdir: str, mode: str | None = None) -> dict:
    sid = new_id()
    now = _now()
    m = mode or config.CLAUDE_DEFAULT_MODE
    _exec(
        "INSERT INTO sessions(id,title,claude_session_id,workdir,status,mode,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?)",
        (sid, title or "新会话", None, workdir, "idle", m, now, now),
    )
    return get_session(sid)


def get_session(sid: str) -> dict | None:
    rows = _query("SELECT * FROM sessions WHERE id=?", (sid,))
    return dict(rows[0]) if rows else None


def list_sessions() -> list[dict]:
    rows = _query(
        "SELECT * FROM sessions WHERE (is_secretary=0 OR is_secretary IS NULL) ORDER BY updated_at DESC"
    )
    return [dict(r) for r in rows]


def delete_session(sid: str) -> None:
    _exec("DELETE FROM messages WHERE session_id=?", (sid,))
    _exec("DELETE FROM tasks WHERE session_id=?", (sid,))
    _exec("DELETE FROM queue_items WHERE session_id=?", (sid,))
    _exec("DELETE FROM sessions WHERE id=?", (sid,))


def update_session(sid: str, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ",".join(f"{k}=?" for k in fields)
    _exec(f"UPDATE sessions SET {cols} WHERE id=?", (*fields.values(), sid))


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


def create_schedule(session_id: str, prompt: str, kind: str, interval_min, at_hhmm, next_run: float) -> dict:
    sid = new_id()
    _exec(
        "INSERT INTO schedules(id,session_id,prompt,kind,interval_min,at_hhmm,enabled,next_run,last_run,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (sid, session_id, prompt, kind, interval_min, at_hhmm, 1, next_run, None, _now()),
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


# ---------- todos（待办事项）----------
def list_todos(status: str | None = None) -> list[dict]:
    if status:
        rows = _query("SELECT * FROM todos WHERE status=? ORDER BY priority DESC, created_at ASC", (status,))
    else:
        rows = _query("SELECT * FROM todos ORDER BY priority DESC, created_at ASC")
    return [dict(r) for r in rows]


def create_todo(title: str, description: str = "", priority: int = 0, session_id: str | None = None, status: str = "pending") -> dict:
    tid = new_id()
    now = _now()
    _exec(
        "INSERT INTO todos(id,title,description,status,priority,session_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (tid, title, description, status, priority, session_id, now, now),
    )
    return dict(_query("SELECT * FROM todos WHERE id=?", (tid,))[0])


def update_todo(tid: str, **fields) -> bool:
    allowed = {"title", "description", "status", "priority", "session_id",
               "progress", "progress_at", "progress_src_mtime"}
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
    _exec("DELETE FROM todos WHERE id=?", (tid,))
    return True


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

