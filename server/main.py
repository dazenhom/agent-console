"""FastAPI 应用：REST（会话/历史/任务）+ WebSocket（流式对话）。"""
import asyncio
import base64
import json
import re
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, memory_store, agent_store, asr_client, session_import, wecom_notify, scheduler, worktree
from .claude_runner import runner
from .session_hub import hub, Subscriber


_uploads_cleanup_task = None

# 合法 mode：完整模型列表 + 兼容存量的旧档位值。
_VALID_MODES = set(config.CLAUDE_MODELS) | {"fast", "strong", "super"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：初始化 SQLite。比 @app.on_event("startup") 更可靠，TestClient / 多种部署方式都能触发。
    db.init_db()
    # 目标循环卡在 producing/verifying 的复位到 running：无条件跑（幂等 UPDATE，双进程各跑一次
    # 无害）。不能藏在 RECONCILE_ON_START 守卫下——默认 run.sh 不设该变量，否则重启后目标循环永久卡死。
    db.reconcile_goal_schedules()
    # 仅在被显式要求的进程里对齐僵尸 running 状态（双端口下只让一个进程做，避免重复）。
    import os
    if os.environ.get("RECONCILE_ON_START"):
        db.reconcile_stale_running()
        # 清理隔离会话遗留的失效 worktree 记录（目录被删但 git 元数据残留），各 repo 去重后 prune 一次
        try:
            bases = {
                s.get("worktree_base")
                for s in db.list_sessions(include_archived=True)
                if s.get("is_worktree") and s.get("worktree_base")
            }
            for b in bases:
                worktree.prune(b)
        except Exception:
            pass
    scheduler.start()  # 挂起定时任务后台循环
    global _uploads_cleanup_task
    _uploads_cleanup_task = asyncio.ensure_future(_uploads_cleanup_loop())
    yield
    # 关闭：当前没有需要清理的资源（子进程在每回合结束时会自行清理）。
    if _uploads_cleanup_task and not _uploads_cleanup_task.done():
        _uploads_cleanup_task.cancel()


app = FastAPI(title="Agent Console", lifespan=lifespan)


# ---------------- 鉴权 ----------------
def check_token(token: str | None) -> bool:
    return token == config.AUTH_TOKEN


def require_auth(authorization: str | None = Header(default=None)):
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization[7:]
    if not check_token(token):
        raise HTTPException(status_code=401, detail="未授权")
    return True


def require_auth_query(
    authorization: str | None = Header(default=None),
    token: str | None = Query(default=None),
):
    """鉴权：优先 Authorization header，其次 query 参数 token。
    用于 <img src> 等浏览器直接发起、无法携带 header 的 GET 请求。"""
    tok = None
    if authorization and authorization.startswith("Bearer "):
        tok = authorization[7:]
    if tok is None:
        tok = token
    if not check_token(tok):
        raise HTTPException(status_code=401, detail="未授权")
    return True


# ---------------- REST ----------------
@app.post("/api/login")
async def login(payload: dict):
    if check_token(payload.get("token")):
        return {"ok": True}
    raise HTTPException(status_code=401, detail="口令错误")


@app.get("/api/sessions", dependencies=[Depends(require_auth)])
async def get_sessions(archived: int = Query(default=0)):
    return db.list_sessions(archived_only=bool(archived))


@app.get("/api/sessions/search", dependencies=[Depends(require_auth)])
async def search_sessions(q: str = Query(...), scope: str = Query(default="content")):
    q = (q or "").strip()
    if len(q) < 2:
        return {"session_ids": []}
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return {"session_ids": db.search_message_sessions(esc)}


@app.post("/api/sessions", dependencies=[Depends(require_auth)])
async def create_session(payload: dict):
    title = payload.get("title", "新会话")
    workdir = payload.get("workdir") or config.DEFAULT_WORKDIR
    mode = payload.get("mode") if payload.get("mode") in _VALID_MODES else None
    branch, is_wt, wt_base, notice = "", 0, "", ""
    if payload.get("isolate"):
        base = workdir
        path, branch = await asyncio.to_thread(worktree.create, base, title)
        if branch:
            workdir, is_wt, wt_base = path, 1, base
        else:
            branch = ""
            notice = "该目录不是 Git 仓库（或创建 worktree 失败），已使用共享工作区，未隔离。"
    s = db.create_session(title, workdir, mode,
                          worktree_branch=branch, is_worktree=is_wt, worktree_base=wt_base)
    if notice:
        s["isolate_notice"] = notice  # 仅本次响应提示，不入库
    return s


@app.patch("/api/sessions/{sid}/mode", dependencies=[Depends(require_auth)])
async def set_session_mode(sid: str, payload: dict):
    mode = payload.get("mode")
    if mode not in _VALID_MODES:
        raise HTTPException(status_code=400, detail="mode 需为合法模型 ID，支持：" + ", ".join(config.CLAUDE_MODELS))
    if not db.get_session(sid):
        raise HTTPException(status_code=404, detail="会话不存在")
    db.update_session(sid, mode=mode)
    return {"ok": True, "mode": mode}


@app.patch("/api/sessions/{sid}/title", dependencies=[Depends(require_auth)])
async def set_session_title(sid: str, payload: dict):
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title 不能为空")
    if not db.get_session(sid):
        raise HTTPException(status_code=404, detail="会话不存在")
    db.update_session(sid, title=title, title_auto=0)
    return {"ok": True, "title": title}


@app.patch("/api/sessions/{sid}/workdir", dependencies=[Depends(require_auth)])
async def set_session_workdir(sid: str, payload: dict):
    workdir = (payload.get("workdir") or "").strip()
    if not workdir:
        raise HTTPException(status_code=400, detail="workdir 不能为空")
    old = db.get_session(sid)
    if not old:
        raise HTTPException(status_code=404, detail="会话不存在")
    if (old.get("workdir") or "").strip() != workdir:
        db.update_session(sid, workdir=workdir, claude_session_id=None)
        await runner.forget_session(sid)
    else:
        db.update_session(sid, workdir=workdir)
    return {"ok": True, "workdir": workdir}


@app.delete("/api/sessions/{sid}", dependencies=[Depends(require_auth)])
async def remove_session(sid: str):
    sess = db.get_session(sid)
    if not sess:
        raise HTTPException(status_code=404, detail="会话不存在")
    linked = db.todos_linked_to_session(sid)
    if linked:
        raise HTTPException(
            status_code=409,
            detail="该会话已被智能看板任务关联，无法删除。请先在看板中解除关联：" + "、".join(linked[:5]),
        )
    # 隔离会话：物理删除时清理 worktree 目录（不删分支，合并/保留由人工决定）
    if sess.get("is_worktree") and sess.get("worktree_base"):
        await asyncio.to_thread(worktree.remove, sess.get("workdir"), sess.get("worktree_base"))
    db.delete_session(sid)
    return {"ok": True}


@app.post("/api/sessions/{sid}/archive", dependencies=[Depends(require_auth)])
async def archive_session(sid: str):
    if not db.get_session(sid):
        raise HTTPException(status_code=404, detail="会话不存在")
    db.update_session(sid, archived=1)
    return {"ok": True, "archived": True}


@app.post("/api/sessions/{sid}/unarchive", dependencies=[Depends(require_auth)])
async def unarchive_session(sid: str):
    if not db.get_session(sid):
        raise HTTPException(status_code=404, detail="会话不存在")
    db.update_session(sid, archived=0)
    return {"ok": True, "archived": False}


@app.post("/api/sessions/{sid}/resume", dependencies=[Depends(require_auth)])
async def resume_session(sid: str):
    sess = db.get_session(sid)
    if not sess:
        raise HTTPException(status_code=404, detail="会话不存在")

    claude_session_id = sess.get("claude_session_id")

    # 非常驻模式：无进程可预热，直接返回
    if not config.CLAUDE_PERSISTENT:
        return {"ok": True, "status": "idle", "claude_session_id": claude_session_id}

    try:
        workdir = sess.get("workdir") or config.DEFAULT_WORKDIR
        status = await runner.ensure_warm(sid, workdir, resume=claude_session_id)
        return {"ok": True, "status": status, "claude_session_id": claude_session_id}
    except (FileNotFoundError, PermissionError, OSError) as e:
        raise HTTPException(status_code=500, detail=f"启动 Claude CLI 失败：{e}")


@app.get("/api/sessions/{sid}/messages", dependencies=[Depends(require_auth)])
async def get_messages(sid: str):
    if not db.get_session(sid):
        raise HTTPException(status_code=404, detail="会话不存在")
    return db.list_messages(sid)


@app.get("/api/sessions/{sid}/queue", dependencies=[Depends(require_auth)])
async def queue_list(sid: str):
    if not db.get_session(sid):
        raise HTTPException(404, "会话不存在")
    return db.list_queue(sid)


@app.patch("/api/sessions/{sid}/queue/{item_id}", dependencies=[Depends(require_auth)])
async def queue_edit(sid: str, item_id: str, payload: dict):
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(400, "text 不能为空")
    item = db.get_queue_item(item_id)
    if not item or item["session_id"] != sid:
        raise HTTPException(404, "队列项不存在")
    db.update_queue_item(item_id, text)
    await hub.emit_queue_update(sid)
    return {"ok": True}


@app.delete("/api/sessions/{sid}/queue/{item_id}", dependencies=[Depends(require_auth)])
async def queue_delete(sid: str, item_id: str):
    item = db.get_queue_item(item_id)
    if not item or item["session_id"] != sid:
        raise HTTPException(404, "队列项不存在")
    db.delete_queue_item(item_id)
    await hub.emit_queue_update(sid)
    return {"ok": True}


@app.get("/api/tasks", dependencies=[Depends(require_auth)])
async def get_tasks():
    return db.list_tasks()


@app.get("/api/artifacts", dependencies=[Depends(require_auth)])
async def get_artifacts(session_id: str = Query(default=None)):
    return db.list_artifacts(session_id)


@app.post("/api/artifacts", dependencies=[Depends(require_auth)])
async def post_artifact(payload: dict):
    url = (payload.get("url") or "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="url 不能为空")
    if not re.match(r'^https?://', url, re.IGNORECASE):
        raise HTTPException(status_code=400, detail="url 须以 http/https 开头")
    pub = payload.get("published_at")
    try:
        pub = float(pub) if pub is not None else None
    except (TypeError, ValueError):
        pub = None
    return db.create_artifact(
        payload.get("session_id"),
        url,
        payload.get("title", ""),
        payload.get("favicon", ""),
        payload.get("description", ""),
        payload.get("label", ""),
        pub,
    )


# ---------------- Memory 管理 ----------------
@app.get("/api/memory", dependencies=[Depends(require_auth)])
async def memory_list():
    return memory_store.list_memories()


@app.post("/api/memory", dependencies=[Depends(require_auth)])
async def memory_create(payload: dict):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name 不能为空")
    try:
        return memory_store.create_memory(name, payload.get("description", ""), payload.get("body", ""))
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/memory/{name}", dependencies=[Depends(require_auth)])
async def memory_get(name: str):
    try:
        m = memory_store.get_memory(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not m:
        raise HTTPException(status_code=404, detail="memory 不存在")
    return m


@app.put("/api/memory/{name}", dependencies=[Depends(require_auth)])
async def memory_update(name: str, payload: dict):
    try:
        return memory_store.update_memory(name, payload.get("description"), payload.get("body"))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/memory/{name}", dependencies=[Depends(require_auth)])
async def memory_delete(name: str):
    try:
        return memory_store.delete_memory(name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------- Subagent 管理 ----------------
@app.get("/api/agents", dependencies=[Depends(require_auth)])
async def agents_list():
    return agent_store.list_agents()


@app.post("/api/agents", dependencies=[Depends(require_auth)])
async def agents_create(payload: dict):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name 不能为空")
    try:
        return agent_store.create_agent(
            name, payload.get("description", ""), payload.get("tools"),
            payload.get("model"), payload.get("prompt", ""),
        )
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/agents/{name}", dependencies=[Depends(require_auth)])
async def agents_get(name: str):
    try:
        a = agent_store.get_agent(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not a:
        raise HTTPException(status_code=404, detail="subagent 不存在")
    return a


@app.put("/api/agents/{name}", dependencies=[Depends(require_auth)])
async def agents_update(name: str, payload: dict):
    try:
        return agent_store.update_agent(
            name, payload.get("description"), payload.get("tools"),
            payload.get("model"), payload.get("prompt"),
        )
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/agents/{name}", dependencies=[Depends(require_auth)])
async def agents_delete(name: str):
    try:
        return agent_store.delete_agent(name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------- 快捷指令（可自定义，存 DB）----------------
@app.get("/api/snippets", dependencies=[Depends(require_auth)])
async def snippets_list():
    return db.list_snippets()


@app.post("/api/snippets", dependencies=[Depends(require_auth)])
async def snippets_create(payload: dict):
    label = (payload.get("label") or "").strip()
    text = payload.get("text", "")
    if not label:
        raise HTTPException(status_code=400, detail="label 不能为空")
    return db.create_snippet(label, text)


@app.get("/api/snippets/{sid}", dependencies=[Depends(require_auth)])
async def snippets_get(sid: str):
    rows = db._query("SELECT * FROM snippets WHERE id=?", (sid,))
    if not rows:
        raise HTTPException(status_code=404, detail="快捷指令不存在")
    return dict(rows[0])


@app.put("/api/snippets/{sid}", dependencies=[Depends(require_auth)])
async def snippets_update(sid: str, payload: dict):
    ok = db.update_snippet(sid, payload.get("label"), payload.get("text"))
    if not ok:
        raise HTTPException(status_code=404, detail="快捷指令不存在")
    return {"ok": True}


@app.delete("/api/snippets/{sid}", dependencies=[Depends(require_auth)])
async def snippets_delete(sid: str):
    ok = db.delete_snippet(sid)
    if not ok:
        raise HTTPException(status_code=404, detail="快捷指令不存在")
    return {"ok": True}


# ---------------- 接续电脑终端会话 ----------------
@app.get("/api/import/sessions", dependencies=[Depends(require_auth)])
async def import_list():
    return session_import.list_importable()


@app.post("/api/import/sessions", dependencies=[Depends(require_auth)])
async def import_do(payload: dict):
    ids = payload.get("ids")
    if ids is None:
        ids = [s["claude_session_id"] for s in session_import.list_importable() if not s["imported"]]
    if not isinstance(ids, list):
        raise HTTPException(status_code=400, detail="ids 必须是数组")
    return session_import.import_sessions(ids)


# ---------------- 定时/周期任务 ----------------
def _validate_schedule(payload: dict) -> dict:
    """校验并归一化定时任务参数。返回 {kind, interval_min, at_hhmm[, stop_condition, max_iterations]}。"""
    kind = payload.get("kind")
    if kind not in ("interval", "daily", "goal"):
        raise HTTPException(status_code=400, detail="kind 仅支持 interval / daily / goal")
    interval_min = None
    at_hhmm = None
    if kind == "goal":
        stop_condition = (payload.get("stop_condition") or "").strip()
        if not stop_condition:
            raise HTTPException(status_code=400, detail="目标循环必须填写完成标准（stop_condition）")
        raw_max = payload.get("max_iterations")
        # 用 is None 判空（而非 `or`），否则传入 0 会被 falsy 静默替换为默认值，绕过下面的范围校验
        try:
            max_iterations = config.GOAL_MAX_ITERATIONS if raw_max is None else int(raw_max)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="max_iterations 需为整数")
        if not (1 <= max_iterations <= 100):
            raise HTTPException(status_code=400, detail="max_iterations 需在 1..100 之间")
        return {"kind": kind, "interval_min": None, "at_hhmm": None,
                "stop_condition": stop_condition, "max_iterations": max_iterations}
    if kind == "interval":
        try:
            interval_min = int(payload.get("interval_min"))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="interval_min 需为整数（分钟）")
        if interval_min < 1:
            raise HTTPException(status_code=400, detail="interval_min 至少 1 分钟")
    else:
        at_hhmm = str(payload.get("at_hhmm") or "")
        try:
            hh, mm = at_hhmm.split(":")
            hh, mm = int(hh), int(mm)
            assert 0 <= hh < 24 and 0 <= mm < 60
        except Exception:
            raise HTTPException(status_code=400, detail="at_hhmm 格式应为 HH:MM（24小时制）")
        at_hhmm = f"{hh:02d}:{mm:02d}"
    return {"kind": kind, "interval_min": interval_min, "at_hhmm": at_hhmm}


@app.get("/api/schedules", dependencies=[Depends(require_auth)])
async def schedules_list():
    return db.list_schedules()


@app.post("/api/schedules", dependencies=[Depends(require_auth)])
async def schedules_create(payload: dict):
    session_id = payload.get("session_id") or ""
    if not db.get_session(session_id):
        raise HTTPException(status_code=404, detail="会话不存在")
    prompt = (payload.get("prompt") or "").strip()
    if not prompt:
        raise HTTPException(status_code=400, detail="prompt 不能为空")
    norm = _validate_schedule(payload)
    if norm["kind"] == "goal":
        nxt = scheduler.compute_next_run("goal", None, None)
        return db.create_schedule(session_id, prompt, "goal", None, None, nxt,
                                  stop_condition=norm["stop_condition"],
                                  max_iterations=norm["max_iterations"], goal_status="running")
    nxt = scheduler.compute_next_run(norm["kind"], norm["interval_min"], norm["at_hhmm"])
    return db.create_schedule(session_id, prompt, norm["kind"], norm["interval_min"], norm["at_hhmm"], nxt)


@app.put("/api/schedules/{sid}", dependencies=[Depends(require_auth)])
async def schedules_update(sid: str, payload: dict):
    sch = db.get_schedule(sid)
    if not sch:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    fields = {}
    if "enabled" in payload:
        fields["enabled"] = 1 if payload.get("enabled") else 0
    if "prompt" in payload:
        p = (payload.get("prompt") or "").strip()
        if not p:
            raise HTTPException(status_code=400, detail="prompt 不能为空")
        fields["prompt"] = p
    # 改了调度规格 → 重算 next_run
    if "kind" in payload:
        norm = _validate_schedule(payload)
        if norm["kind"] == "goal":
            fields.update({
                "kind": "goal", "interval_min": None, "at_hhmm": None,
                "stop_condition": norm["stop_condition"], "max_iterations": norm["max_iterations"],
                "goal_status": "running", "iter_count": 0, "last_feedback": "",
            })
            fields["next_run"] = scheduler.compute_next_run("goal", None, None)
        else:
            fields.update({"kind": norm["kind"], "interval_min": norm["interval_min"],
                           "at_hhmm": norm["at_hhmm"]})
            fields["next_run"] = scheduler.compute_next_run(norm["kind"], norm["interval_min"], norm["at_hhmm"])
    elif fields.get("enabled") == 1:
        # 重新启用：goal 干净重启循环（复位 running + iter_count 归零 + 清 last_feedback），
        # 否则 done/exhausted 态重新启用会立刻因 iter_count>=max 再次 exhausted 空转；interval/daily 缺 next_run 才补算
        if sch.get("kind") == "goal":
            fields["goal_status"] = "running"
            fields["iter_count"] = 0
            fields["last_feedback"] = ""
            fields["next_run"] = scheduler.compute_next_run("goal", None, None)
        elif not sch.get("next_run"):
            fields["next_run"] = scheduler.compute_next_run(sch["kind"], sch.get("interval_min"), sch.get("at_hhmm"))
    db.update_schedule(sid, **fields)
    return db.get_schedule(sid)


@app.delete("/api/schedules/{sid}", dependencies=[Depends(require_auth)])
async def schedules_delete(sid: str):
    if not db.delete_schedule(sid):
        raise HTTPException(status_code=404, detail="定时任务不存在")
    return {"ok": True}


# ---------------- 待办事项 ----------------
@app.get("/api/todos", dependencies=[Depends(require_auth)])
async def todos_list(archived: int = Query(default=0)):
    return db.list_todos(archived_only=bool(archived))


@app.post("/api/todos", dependencies=[Depends(require_auth)])
async def todos_create(payload: dict):
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title 不能为空")
    status = payload.get("status") if payload.get("status") in ("pending", "in_progress") else "pending"
    # 关联多个会话：显式传 session_ids 优先；否则用旧字段 session_id 兜底
    session_ids = payload.get("session_ids") or []
    if not session_ids and payload.get("session_id"):
        session_ids = [payload["session_id"]]
    # 有 session_ids 时主会话由 set_todo_sessions 统一写回，避免此处重复写 session_id
    todo = db.create_todo(
        title,
        payload.get("description", ""),
        int(payload.get("priority", 0)),
        None if session_ids else payload.get("session_id"),
        status,
    )
    if session_ids:
        db.set_todo_sessions(todo["id"], session_ids)
        todo["session_ids"] = session_ids
    else:
        todo["session_ids"] = []
    return todo


@app.put("/api/todos/{tid}", dependencies=[Depends(require_auth)])
async def todos_update(tid: str, payload: dict):
    if not db._query("SELECT 1 FROM todos WHERE id=?", (tid,)):
        raise HTTPException(status_code=404, detail="待办不存在")
    fields = {k: payload[k] for k in ("title", "description", "status", "priority", "session_id") if k in payload}
    if fields:
        db.update_todo(tid, **fields)
    # 关联多个会话：显式传 session_ids（非 None）则覆盖式更新
    if payload.get("session_ids") is not None:
        db.set_todo_sessions(tid, payload["session_ids"])
    return {"ok": True}


@app.put("/api/todos/{tid}/sessions", dependencies=[Depends(require_auth)])
async def todos_set_sessions(tid: str, payload: dict):
    session_ids = payload.get("session_ids", [])
    if not db._query("SELECT 1 FROM todos WHERE id=?", (tid,)):
        raise HTTPException(status_code=404, detail="待办不存在")
    db.set_todo_sessions(tid, session_ids)
    return {"ok": True, "session_ids": session_ids}


@app.delete("/api/todos/{tid}", dependencies=[Depends(require_auth)])
async def todos_delete(tid: str):
    if not db.delete_todo(tid):
        raise HTTPException(status_code=404, detail="待办不存在")
    return {"ok": True}


@app.post("/api/todos/{tid}/archive", dependencies=[Depends(require_auth)])
async def todos_archive(tid: str):
    if not db._query("SELECT 1 FROM todos WHERE id=?", (tid,)):
        raise HTTPException(status_code=404, detail="待办不存在")
    db.update_todo(tid, archived=1)
    return {"ok": True, "archived": True}


@app.post("/api/todos/{tid}/unarchive", dependencies=[Depends(require_auth)])
async def todos_unarchive(tid: str):
    if not db._query("SELECT 1 FROM todos WHERE id=?", (tid,)):
        raise HTTPException(status_code=404, detail="待办不存在")
    db.update_todo(tid, archived=0)
    return {"ok": True, "archived": False}


# ---------------- Triage 待分诊收件箱（H3）----------------
@app.get("/api/triage", dependencies=[Depends(require_auth)])
async def triage_list():
    return db.list_todos(status="triage")


@app.post("/api/triage/{tid}/dispatch", dependencies=[Depends(require_auth)])
async def triage_dispatch(tid: str):
    rows = db._query("SELECT * FROM todos WHERE id=?", (tid,))
    if not rows:
        raise HTTPException(status_code=404, detail="待分诊事项不存在")
    todo = dict(rows[0])
    if todo.get("status") != "triage":
        raise HTTPException(status_code=404, detail="该事项已不在待分诊状态")
    # 读回分诊原始载荷，缺字段用 todo 兜底（人工明示派单，不受自动派单开关和每日上限约束）
    try:
        payload = json.loads(todo.get("triage_payload") or "{}")
    except json.JSONDecodeError:
        payload = {}
    payload.setdefault("title", todo["title"])
    if not payload.get("goal_prompt"):
        payload["goal_prompt"] = todo["title"]
    if not payload.get("stop_condition"):
        payload["stop_condition"] = todo.get("description") or payload.get("reason") or "完成该任务"
    from . import triage
    ok = await triage._dispatch_existing_todo(tid, payload)
    if not ok:
        raise HTTPException(status_code=500, detail="派单失败")
    return {"ok": True}


@app.post("/api/triage/{tid}/ignore", dependencies=[Depends(require_auth)])
async def triage_ignore(tid: str):
    if not db._query("SELECT 1 FROM todos WHERE id=?", (tid,)):
        raise HTTPException(status_code=404, detail="待分诊事项不存在")
    db.update_todo(tid, status="cancelled")
    return {"ok": True}


@app.post("/api/triage/{tid}/to_todo", dependencies=[Depends(require_auth)])
async def triage_to_todo(tid: str):
    if not db._query("SELECT 1 FROM todos WHERE id=?", (tid,)):
        raise HTTPException(status_code=404, detail="待分诊事项不存在")
    db.update_todo(tid, status="pending", source="manual")
    return {"ok": True}


# ---------------- 智能任务看板：进展摘要 ----------------
@app.post("/api/todos/{tid}/refresh_progress", dependencies=[Depends(require_auth)])
async def todo_refresh_progress(tid: str, force: bool = False):
    from .kanban import refresh_todo_progress
    return await refresh_todo_progress(tid, force=force)


@app.post("/api/kanban/refresh", dependencies=[Depends(require_auth)])
async def kanban_refresh_all():
    from .kanban import refresh_todo_progress
    rows = db._query(
        "SELECT id FROM todos WHERE status='in_progress' AND session_id IS NOT NULL AND session_id != ''"
        " AND (archived=0 OR archived IS NULL)"
    )
    sem = asyncio.Semaphore(3)

    async def bounded(tid):
        async with sem:
            return await refresh_todo_progress(tid)

    results = await asyncio.gather(*[bounded(r["id"]) for r in rows], return_exceptions=True)
    updated = sum(1 for r in results if isinstance(r, dict) and r.get("ok"))
    return {"ok": True, "updated": updated, "total": len(rows)}


# ---------------- 备忘录 ----------------
@app.get("/api/memos", dependencies=[Depends(require_auth)])
async def memos_list(status: str = None):
    return db.list_memos(status)


@app.post("/api/memos", dependencies=[Depends(require_auth)])
async def memos_create(payload: dict):
    content = (payload.get("content") or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="content 不能为空")
    try:
        remind_enabled = int(payload.get("remind_enabled", 1))
    except (TypeError, ValueError):
        remind_enabled = 1
    remind_mode = str(payload.get("remind_mode") or "daily")
    remind_at = str(payload.get("remind_at") or "")
    try:
        remind_days_before = int(payload.get("remind_days_before", 0))
    except (TypeError, ValueError):
        remind_days_before = 0
    mid = db.create_memo(content, remind_enabled, remind_mode, remind_at, remind_days_before)
    memos = db.list_memos()
    return next((m for m in memos if m["id"] == mid), {"id": mid})


# 注意：/remind 必须放在 /{mid} 之前，否则 "remind" 会被当成 mid 匹配
@app.post("/api/memos/remind", dependencies=[Depends(require_auth)])
async def memos_remind(payload: dict = None):
    from . import memo_reminder
    count = await memo_reminder.run_reminder(force=(payload or {}).get("force", False))
    return {"ok": True, "count": count}


@app.put("/api/memos/{mid}", dependencies=[Depends(require_auth)])
async def memos_update(mid: str, payload: dict):
    fields = {k: payload[k] for k in
              ("content", "status", "remind_enabled", "remind_mode", "remind_at", "remind_days_before")
              if k in payload}
    if not db.update_memo(mid, **fields):
        raise HTTPException(status_code=404, detail="备忘不存在")
    return {"ok": True}


@app.delete("/api/memos/{mid}", dependencies=[Depends(require_auth)])
async def memos_delete(mid: str):
    if not db.delete_memo(mid):
        raise HTTPException(status_code=404, detail="备忘不存在")
    return {"ok": True}


# ---------------- 日报 ----------------
@app.get("/api/reports", dependencies=[Depends(require_auth)])
async def reports_list():
    return db.list_reports()


@app.get("/api/reports/{rid}", dependencies=[Depends(require_auth)])
async def reports_get(rid: str):
    r = db.get_report(rid)
    if not r:
        raise HTTPException(status_code=404, detail="报告不存在")
    return r


# ---------------- 秘书触发 ----------------
@app.post("/api/secretary/trigger", dependencies=[Depends(require_auth)])
async def secretary_trigger(payload: dict):
    report_type = payload.get("type", "evening")
    if report_type not in ("evening", "morning"):
        raise HTTPException(status_code=400, detail="type 须为 evening 或 morning")
    from . import secretary
    asyncio.ensure_future(secretary.run_report(report_type))
    return {"ok": True, "message": f"已触发 {report_type} 报告生成（异步执行）"}


@app.get("/api/secretary/session", dependencies=[Depends(require_auth)])
async def secretary_session():
    sess = db.get_secretary_session()
    return sess or {}


# ---------------- 产物文件预览（只读会话 workdir 内）----------------
_FILE_MAX_BYTES = 20 * 1024 * 1024   # 文件大小上限 20MB
_TEXT_MAX_BYTES = 512 * 1024         # 文本内容返回上限 512KB
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".bmp"}
_IMG_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp", ".bmp": "image/bmp",
}
_AUDIO_EXTS = {".wav", ".mp3", ".flac", ".ogg", ".m4a", ".aac"}
_AUDIO_MIME = {
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".flac": "audio/flac",
    ".ogg": "audio/ogg",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
}


@app.get("/api/file", dependencies=[Depends(require_auth_query)])
async def get_file(session_id: str = Query(...), path: str = Query(...), download: int = Query(default=0)):
    """读取文件。图片允许任意绝对路径（模型可输出工作目录外的图片路径）；文本仍限 workdir 内。"""
    from pathlib import Path as _P
    from .fs_util import safe_path_under

    sess = db.get_session(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="会话不存在")

    p = _P(path)
    ext = p.suffix.lower()
    is_img = ext in _IMG_EXTS
    is_audio = ext in _AUDIO_EXTS
    is_media = is_img or is_audio

    if is_media and p.is_absolute():
        # 图片/音频：允许任意绝对路径，不限于 workdir
        target = p.resolve()
    else:
        base = _P(sess.get("workdir") or config.DEFAULT_WORKDIR)
        try:
            target = safe_path_under(base, path)
        except ValueError as e:
            raise HTTPException(status_code=403, detail=str(e))
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    size = target.stat().st_size
    max_bytes = 100 * 1024 * 1024 if is_audio else _FILE_MAX_BYTES
    if size > max_bytes:
        raise HTTPException(status_code=413, detail=f"文件过大（{size // 1024 // 1024}MB，上限 {max_bytes // 1024 // 1024}MB）")

    ext = target.suffix.lower()
    if download:
        return FileResponse(str(target), filename=target.name)
    if ext in _IMG_EXTS:
        return FileResponse(str(target), media_type=_IMG_MIME.get(ext, "application/octet-stream"))
    if is_audio:
        return FileResponse(str(target), media_type=_AUDIO_MIME.get(ext, "application/octet-stream"))
    # 文本：尝试 utf-8 解码返回内容
    if size > _TEXT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="文本过大，请下载查看")
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception:
        raise HTTPException(status_code=415, detail="无法以文本读取，请下载查看")
    return {"name": target.name, "content": content, "size": size}


_PREVIEW_EXTS = {".html", ".htm"}
_PREVIEW_REPORTER = (
    "<script>(function(){function p(){try{var h=Math.max("
    "document.documentElement.scrollHeight,(document.body||{}).scrollHeight||0);"
    "parent.postMessage({__embedHeight:h},'*');}catch(e){}}"
    "window.addEventListener('load',p);window.addEventListener('resize',p);"
    "if(window.ResizeObserver){try{new ResizeObserver(p).observe(document.documentElement);}catch(e){}}"
    "p();setTimeout(p,300);setTimeout(p,1200);})();</script>"
)


@app.get("/api/preview", dependencies=[Depends(require_auth_query)])
async def preview_html(path: str = Query(...), request: Request = None):
    """把任意绝对路径的 HTML 文件重定向到 /fs/<path>，由静态文件服务提供，
    这样页面内的相对路径（fetch('viz/data.json') 等）都能正常解析。"""
    from pathlib import Path as _P
    from fastapi.responses import RedirectResponse

    p = _P(path).resolve()
    if p.suffix.lower() not in _PREVIEW_EXTS:
        raise HTTPException(status_code=415, detail="仅支持 HTML 文件预览")
    # 防路径穿越：resolve() 后必须是绝对路径（正常情况下总是）
    if not p.is_absolute():
        raise HTTPException(status_code=400, detail="非法路径")
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    # 重定向到 /fs/<绝对路径>，让静态文件服务处理，相对路径因此能正常解析
    fs_url = f"/fs{p}"
    return RedirectResponse(url=fs_url, status_code=302)


# ---------------- 图片上传（手机拍照/截图发给 Agent）----------------
_IMG_UPLOAD_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_IMG_MIME_TO_EXT = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/gif": ".gif", "image/webp": ".webp", "image/bmp": ".bmp",
}


def _cleanup_uploads(directory, cutoff):
    try:
        for f in directory.iterdir():
            try:
                if f.is_file() and f.stat().st_mtime < cutoff:
                    f.unlink()
            except Exception:
                pass
    except Exception:
        pass


async def _uploads_cleanup_loop():
    import time as _t2
    ttl_days = config.UPLOAD_TTL_DAYS
    interval = config.UPLOAD_CLEAN_INTERVAL_HOURS
    if ttl_days <= 0 or interval <= 0:
        return
    while True:
        try:
            cutoff = _t2.time() - ttl_days * 86400
            await asyncio.to_thread(_cleanup_uploads, config.UPLOAD_DIR, cutoff)
        except Exception:
            pass
        await asyncio.sleep(interval * 3600)


@app.post("/api/upload", dependencies=[Depends(require_auth)])
async def upload_image(payload: dict):
    """收 base64 图片存到项目固定目录 UPLOAD_DIR 下，返回绝对路径供注入消息。

    body: {"session_id": "...", "image": "<dataURL或纯base64>", "mime": "image/jpeg", "name": "可选原名"}
    用 JSON+base64 而非 multipart，与 /api/asr 一致，绕过反向代理的 body 限制。
    tclaude 之后用 Read 工具读这个路径的图。
    """
    import os
    import time as _t
    from pathlib import Path as _P
    from .fs_util import safe_path_under

    session_id = payload.get("session_id") or ""
    sess = db.get_session(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="会话不存在")

    # 兼容新旧字段：file（任意文件）优先，image（旧图片上传）兜底
    b64 = payload.get("file") or payload.get("image", "")
    if not b64:
        raise HTTPException(status_code=400, detail="缺少 file 字段")
    mime = payload.get("mime", "")
    if b64.startswith("data:"):
        head, _, b64 = b64.partition(",")
        if not mime and ";" in head:
            mime = head[5:].split(";")[0]
    try:
        raw = base64.b64decode(b64)
    except Exception:
        raise HTTPException(status_code=400, detail="file 不是合法 base64")
    if not raw:
        raise HTTPException(status_code=400, detail="空文件")
    if len(raw) > config.UPLOAD_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"文件过大（上限 {config.UPLOAD_MAX_BYTES // 1024 // 1024}MB）")

    # 扩展名：优先取原名后缀并做安全清洗，其次按 mime 映射，最后兜底
    orig_name = payload.get("name") or "upload"
    _, ext = os.path.splitext(orig_name)
    ext = "".join(c for c in ext if c.isalnum() or c == ".")[:10]
    if not ext or ext == ".":
        ext = _IMG_MIME_TO_EXT.get(mime.lower(), "")
    if not ext:
        ext = ".bin"

    base = config.UPLOAD_DIR
    # 落盘丢弃原名（防路径穿越/冲突），仅在返回值里带原名供前端显示
    fname = f"{int(_t.time())}_{db.new_id()}{ext}"
    try:
        target = safe_path_under(base, fname)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_bytes(raw)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"写入失败：{e}")
    return {"path": str(target), "abs": str(target), "bytes": len(raw), "name": orig_name}


# ---------------- 语音识别 ----------------
_ASR_MAX_BYTES = 10 * 1024 * 1024  # 录音上限 10MB，防滥用


@app.post("/api/asr", dependencies=[Depends(require_auth)])
async def asr(request: dict):
    """接受 JSON body: {"audio": "<base64>", "mime": "audio/webm"} 或 {"audio": "<base64>"}。
    前端用 FileReader.readAsDataURL 或 readAsArrayBuffer + btoa 编码后发来。
    改用 JSON 而非 multipart，绕过反向代理的 body 大小/格式限制。
    """
    if not config.ASR2_BASE_URLS:
        raise HTTPException(status_code=503, detail="ASR 服务未配置")
    audio_b64 = request.get("audio", "")
    if not audio_b64:
        raise HTTPException(status_code=400, detail="缺少 audio 字段")
    # 支持 data URL 格式（data:audio/webm;base64,XXXX）或纯 base64
    if audio_b64.startswith("data:"):
        _, _, audio_b64 = audio_b64.partition(",")
    try:
        raw = base64.b64decode(audio_b64)
    except Exception:
        raise HTTPException(status_code=400, detail="audio 字段不是合法的 base64")
    if len(raw) > _ASR_MAX_BYTES:
        raise HTTPException(status_code=413, detail="录音过大（超过 10MB）")
    hint = request.get("mime", "audio/webm")
    try:
        wav = await asyncio.to_thread(asr_client.decode_to_wav16k, raw, hint)
        b64 = base64.b64encode(wav).decode("ascii")
        text = await asyncio.to_thread(asr_client.transcribe_wav_bytes, b64)
    except asr_client.ASRError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"text": text}


# ---------------- WebSocket ----------------
# 回合执行与 stream-json 事件翻译已搬进 session_hub（连接无关，支持后台并行 + 多端同看）。


@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, token: str = Query(default=""), session_id: str = Query(default="")):
    if not check_token(token):
        await websocket.close(code=4401)
        return
    session = db.get_session(session_id)
    if not session:
        await websocket.close(code=4404)
        return

    await websocket.accept()

    async def send(obj: dict):
        try:
            await websocket.send_text(json.dumps(obj, ensure_ascii=False))
        except Exception:
            pass

    # 这条 WS 只是该会话的一个「订阅者」。回合在 hub 里跑，事件广播到所有订阅者。
    # 断开只 unsubscribe，绝不 cancel——任务继续后台跑（多 Agent 调度台的地基）。
    sub = Subscriber(send, hidden=False)
    hub.subscribe(session_id, sub)

    # 连上时把该会话当前真实状态告诉前端，强制对齐。
    # 关键：不仅 running 要发，idle 也要发——否则超长回合期间客户端断线、
    # 回合在后台跑完广播的 status:idle 被错过，重连后客户端会永久卡在 running
    # 态（输入框灰、停止键无效）。带 sync=True 标记，让前端只校正按钮态，
    # 不触发"完成通知"等真实回合结束的副作用。
    await send({
        "type": "status",
        "status": "running" if hub.is_running(session_id) else "idle",
        "sync": True,
    })
    # 连上时把当前队列同步给前端，驱动队列托盘。
    await send({"type": "queue_update", "queue": db.list_queue(session_id)})
    # 重发断线期间可能漏掉的权限请求弹窗（回合仍在等授权时）。
    await hub.resend_pending_perms(session_id, sub)

    async def _handle_message(raw: str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return
        mtype = data.get("type")

        if mtype == "visibility":
            # 前端 visibilitychange 上报：hidden=True 表示页面切到后台/锁屏
            sub.hidden = bool(data.get("hidden"))
            return

        if mtype == "ping":
            # 应用层心跳：前端每 20s 发，回 pong。经 Tailscale Funnel + SSH 隧道多层代理时，
            # 让中间设备认为连接活跃，避免因"空闲"被掐断（WS 频繁断开重连的主因）。
            await send({"type": "pong"})
            return

        if mtype == "cancel":
            await hub.cancel(session_id)
            return

        if mtype == "permission_response":
            # 用户在弹窗点了允许/拒绝：回 control_response 给 CLI
            # updated_input 供 AskUserQuestion 回填用户选择（answers）
            request_id = data.get("request_id")
            behavior = data.get("behavior", "deny")
            updated_input = data.get("updated_input")
            if request_id:
                await hub.respond_permission(session_id, request_id, behavior, updated_input)
            return

        if mtype != "user_message":
            return

        user_text = (data.get("content") or "").strip()
        if not user_text:
            return

        # 在跑则入队、空闲则直接开跑；skill 展开与档位持久化都下沉到 hub。
        mode_hint = data.get("mode") if data.get("mode") in _VALID_MODES else None
        await hub.submit_user_message(session_id, user_text, mode_hint)

    try:
        while True:
            raw = await websocket.receive_text()
            await _handle_message(raw)
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa
        await send({"type": "error", "message": f"服务端异常：{e}"})
    finally:
        hub.unsubscribe(session_id, sub)


@app.websocket("/ws/monitor")
async def ws_monitor(websocket: WebSocket, token: str = Query(default="")):
    """监控通道：推送所有会话的状态/活动变化，驱动前端会话列表实时刷新。"""
    if not check_token(token):
        await websocket.close(code=4401)
        return
    await websocket.accept()

    async def send(obj: dict):
        try:
            await websocket.send_text(json.dumps(obj, ensure_ascii=False))
        except Exception:
            pass

    sub = Subscriber(send)
    hub.subscribe_monitor(sub)
    try:
        # 监控通道是只读的；这里只需保持连接，收到任何消息忽略（前端可发心跳）
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        hub.unsubscribe_monitor(sub)


# ---------- SwanLab 上传 ----------
SWANLAB_SCRIPT = "/apdcephfs_gy2/share_302533218/zhihangxu/code/asr-code-release-manager/scripts/infer_scripts/vllm0.14/upload_wer_swanlab.py"

SWANLAB_HOST = "https://train-exp.taiji.woa.com"
# 只允许反代 SwanLab 的已知路径前缀，防止代理被滥用去打其他内网接口。
# 空串 "" 放行根路径（首页）。
SWANLAB_ALLOWED_PREFIXES = ("_next/", "api/", "@", "login/", "static/", "favicon", "")


@app.api_route("/proxy/swanlab/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"])
async def swanlab_proxy(request: Request, path: str):
    # 不加 require_auth：iframe 里的静态资源请求带不上鉴权 header，否则会 401
    import httpx
    # 路径白名单：只放行 SwanLab 已知前缀，其余一律拒绝
    if path and not any(path.startswith(p) for p in SWANLAB_ALLOWED_PREFIXES):
        raise HTTPException(status_code=403, detail="不允许访问该路径")

    # sid：优先用浏览器带回来的 cookie，没有则服务端用 api_key 登录换取一个
    # （SwanLab API 只认 sid cookie，仅带 authorization header 会 403）
    browser_sid = request.cookies.get("swanlab_sid")
    if not browser_sid:
        try:
            async with httpx.AsyncClient(timeout=10) as login_client:
                login_resp = await login_client.post(
                    f"{SWANLAB_HOST}/api/login/api_key",
                    headers={"authorization": config.SWANLAB_API_KEY},
                )
                if login_resp.status_code == 200:
                    browser_sid = login_resp.json().get("sid", "")
        except Exception:
            browser_sid = ""

    url = f"{SWANLAB_HOST}/{path}"
    params = dict(request.query_params)
    # 转发给 SwanLab 的 header：sid cookie + user-agent
    forward_headers = {
        "user-agent": request.headers.get("user-agent", ""),
    }
    if browser_sid:
        forward_headers["cookie"] = f"sid={browser_sid}"
    body = await request.body()
    # follow_redirects=False：防止重定向链跳出到其他内网地址，3xx 由下面手动改写 Location
    async with httpx.AsyncClient(follow_redirects=False, timeout=30) as client:
        resp = await client.request(
            method=request.method,
            url=url,
            params=params,
            headers=forward_headers,
            content=body,
        )
    # set-cookie 也剔除：sid 由下面统一用 swanlab_sid 名字重新种，避免 SwanLab 原始 cookie 干扰
    excluded = {"transfer-encoding", "content-encoding", "content-length", "connection", "set-cookie"}
    resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded}

    # 重定向：把 Location 头里指向 SwanLab 的绝对 URL 改写回代理路径
    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get("location", "")
        if location.startswith(SWANLAB_HOST):
            location = "/proxy/swanlab/" + location[len(SWANLAB_HOST):].lstrip("/")
            resp_headers["location"] = location

    # HTML 路径重写：SwanLab 是 Next.js 应用，HTML 里资源是绝对路径（/_next、/api…），
    # 在 iframe 里会打到 agent-console 自身导致 404 白屏。注入 <base> + 正则重写绝对路径。
    content_type = resp.headers.get("content-type", "")
    content = resp.content
    if "text/html" in content_type:
        try:
            text = content.decode("utf-8", errors="replace")
            base_tag = '<base href="/proxy/swanlab/">'
            # 注入 <base>（防重复：SwanLab HTML 本身带 <head>，只在没有 <base> 时注入一次）
            if '<base href=' not in text:
                if "<head>" in text:
                    text = text.replace("<head>", f"<head>{base_tag}", 1)
                else:
                    text = base_tag + text
            # 重写 src/href/action 里以单个 / 开头（排除 //）的绝对路径
            text = re.sub(r'((?:src|href|action)=["\'])(/(?!/))', r'\1/proxy/swanlab\2', text)
            content = text.encode("utf-8")
            resp_headers["content-type"] = "text/html; charset=utf-8"
        except Exception:
            pass  # 解码/重写失败就原样透传

    response = Response(content=content, status_code=resp.status_code, headers=resp_headers)
    # 把 sid 种到浏览器 cookie，后续 JS 的 /api 请求会自动带上（转发时再取出塞进 sid cookie）
    if browser_sid:
        response.set_cookie(
            "swanlab_sid", browser_sid,
            path="/proxy/swanlab",
            samesite="lax",
            httponly=False,
        )
    return response

@app.post("/api/swanlab/upload", dependencies=[Depends(require_auth)])
async def swanlab_upload(payload: dict):
    import sys, os, signal
    from .claude_runner import _child_env
    ckpt_dir = (payload.get("ckpt_dir") or "").strip()
    if not ckpt_dir:
        raise HTTPException(status_code=400, detail="缺少 ckpt_dir")
    cmd = [sys.executable, "-u", SWANLAB_SCRIPT, ckpt_dir]
    if payload.get("project"):
        cmd += ["--project", payload["project"].strip()]
    if payload.get("name"):
        cmd += ["--name", payload["name"].strip()]
    if payload.get("workspace"):
        cmd += ["--workspace", payload["workspace"].strip()]
    if payload.get("result_dir"):
        cmd += ["--result-dir", payload["result_dir"].strip()]
    for row in (payload.get("extra_result_dirs") or []):
        d = (row.get("dir") or "").strip()
        p = (row.get("prefix") or "").strip()
        if d and p:
            cmd += ["--extra-result-dir", f"{d}:{p}"]

    env = _child_env()
    env["https_proxy"] = "http://star-proxy.oa.com:3128"
    env["HTTPS_PROXY"] = "http://star-proxy.oa.com:3128"
    env["PYTHONUNBUFFERED"] = "1"

    async def gen():
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            async for raw in proc.stdout:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                yield f"data: {line}\n\n"
            rc = await proc.wait()
            if rc == 0:
                yield "event: done\ndata: [DONE] exit=0\n\n"
            else:
                yield f"event: error\ndata: [ERROR] exit={rc}\n\n"
        except asyncio.CancelledError:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:
                pass
            raise

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------- 静态前端 ----------------
# 缓存策略：让浏览器每次都向服务器核对（no-cache=必须 revalidate）。
# StaticFiles 本身带 ETag/Last-Modified，配合 no-cache：文件没变返回 304（省流量），
# 变了立刻拿新版。彻底治好"改了前端、手机还看旧 UI"的缓存问题。
class NoCacheStatic(StaticFiles):
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        return resp


@app.get("/")
async def index():
    # index.html 绝不缓存——它是入口，必须每次拿最新，否则连里面的资源版本号都更新不了
    return FileResponse(
        str(config.WEB_DIR / "index.html"),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


app.mount("/fs", StaticFiles(directory="/"), name="fs")
app.mount("/", NoCacheStatic(directory=str(config.WEB_DIR)), name="web")
