"""FastAPI 应用：REST（会话/历史/任务）+ WebSocket（流式对话）。"""
import asyncio
import base64
import json
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, memory_store, agent_store, asr_client, session_import, wecom_notify, scheduler
from .claude_runner import runner
from .session_hub import hub, Subscriber


_uploads_cleanup_task = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动：初始化 SQLite。比 @app.on_event("startup") 更可靠，TestClient / 多种部署方式都能触发。
    db.init_db()
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


# ---------------- REST ----------------
@app.post("/api/login")
async def login(payload: dict):
    if check_token(payload.get("token")):
        return {"ok": True}
    raise HTTPException(status_code=401, detail="口令错误")


@app.get("/api/sessions", dependencies=[Depends(require_auth)])
async def get_sessions(archived: int = Query(default=0)):
    return db.list_sessions(archived_only=bool(archived))


@app.post("/api/sessions", dependencies=[Depends(require_auth)])
async def create_session(payload: dict):
    title = payload.get("title", "新会话")
    workdir = payload.get("workdir") or config.DEFAULT_WORKDIR
    mode = payload.get("mode") if payload.get("mode") in ("fast", "strong", "super") else None
    return db.create_session(title, workdir, mode)


@app.patch("/api/sessions/{sid}/mode", dependencies=[Depends(require_auth)])
async def set_session_mode(sid: str, payload: dict):
    mode = payload.get("mode")
    if mode not in ("fast", "strong", "super"):
        raise HTTPException(status_code=400, detail="mode 仅支持 fast / strong / super")
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
    """校验并归一化定时任务参数。返回 {kind, interval_min, at_hhmm}。"""
    kind = payload.get("kind")
    if kind not in ("interval", "daily"):
        raise HTTPException(status_code=400, detail="kind 仅支持 interval / daily")
    interval_min = None
    at_hhmm = None
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
        fields.update(norm)
        fields["next_run"] = scheduler.compute_next_run(norm["kind"], norm["interval_min"], norm["at_hhmm"])
    elif fields.get("enabled") == 1 and not sch.get("next_run"):
        # 重新启用且没有 next_run → 按现有规格补算
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
async def todos_list():
    return db.list_todos()


@app.post("/api/todos", dependencies=[Depends(require_auth)])
async def todos_create(payload: dict):
    title = (payload.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title 不能为空")
    status = payload.get("status") if payload.get("status") in ("pending", "in_progress") else "pending"
    return db.create_todo(
        title,
        payload.get("description", ""),
        int(payload.get("priority", 0)),
        payload.get("session_id"),
        status,
    )


@app.put("/api/todos/{tid}", dependencies=[Depends(require_auth)])
async def todos_update(tid: str, payload: dict):
    fields = {k: payload[k] for k in ("title", "description", "status", "priority", "session_id") if k in payload}
    if not db.update_todo(tid, **fields):
        raise HTTPException(status_code=404, detail="待办不存在")
    return {"ok": True}


@app.delete("/api/todos/{tid}", dependencies=[Depends(require_auth)])
async def todos_delete(tid: str):
    db.delete_todo(tid)
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
    )
    sem = asyncio.Semaphore(3)

    async def bounded(tid):
        async with sem:
            return await refresh_todo_progress(tid)

    results = await asyncio.gather(*[bounded(r["id"]) for r in rows], return_exceptions=True)
    updated = sum(1 for r in results if isinstance(r, dict) and r.get("ok"))
    return {"ok": True, "updated": updated, "total": len(rows)}


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


@app.get("/api/file", dependencies=[Depends(require_auth)])
async def get_file(session_id: str = Query(...), path: str = Query(...), download: int = Query(default=0)):
    """读取会话 workdir 内的文件。图片返回二进制，文本返回内容（JSON）。严格防越界。"""
    from pathlib import Path as _P
    from .fs_util import safe_path_under

    sess = db.get_session(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="会话不存在")
    base = _P(sess.get("workdir") or config.DEFAULT_WORKDIR)
    try:
        target = safe_path_under(base, path)
    except ValueError as e:
        raise HTTPException(status_code=403, detail=str(e))
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    size = target.stat().st_size
    if size > _FILE_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"文件过大（{size // 1024 // 1024}MB，上限 20MB）")

    ext = target.suffix.lower()
    if download:
        return FileResponse(str(target), filename=target.name)
    if ext in _IMG_EXTS:
        return FileResponse(str(target), media_type=_IMG_MIME.get(ext, "application/octet-stream"))
    # 文本：尝试 utf-8 解码返回内容
    if size > _TEXT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="文本过大，请下载查看")
    try:
        content = target.read_text(encoding="utf-8", errors="replace")
    except Exception:
        raise HTTPException(status_code=415, detail="无法以文本读取，请下载查看")
    return {"name": target.name, "content": content, "size": size}


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
    import time as _t
    from pathlib import Path as _P
    from .fs_util import safe_path_under

    session_id = payload.get("session_id") or ""
    sess = db.get_session(session_id)
    if not sess:
        raise HTTPException(status_code=404, detail="会话不存在")

    b64 = payload.get("image", "")
    if not b64:
        raise HTTPException(status_code=400, detail="缺少 image 字段")
    mime = payload.get("mime", "")
    if b64.startswith("data:"):
        head, _, b64 = b64.partition(",")
        if not mime and ";" in head:
            mime = head[5:].split(";")[0]
    try:
        raw = base64.b64decode(b64)
    except Exception:
        raise HTTPException(status_code=400, detail="image 不是合法 base64")
    if not raw:
        raise HTTPException(status_code=400, detail="空图片")
    if len(raw) > config.UPLOAD_MAX_BYTES:
        raise HTTPException(status_code=413, detail=f"图片过大（上限 {config.UPLOAD_MAX_BYTES // 1024 // 1024}MB）")

    ext = _IMG_MIME_TO_EXT.get(mime.lower(), "")
    if not ext:
        # 从原名兜底取扩展名
        orig = (payload.get("name") or "").lower()
        for e in _IMG_UPLOAD_EXTS:
            if orig.endswith(e):
                ext = e
                break
    if not ext:
        ext = ".png"

    base = config.UPLOAD_DIR
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
    return {"path": str(target), "abs": str(target), "bytes": len(raw)}


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
        mode_hint = data.get("mode") if data.get("mode") in ("fast", "strong", "super") else None
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


app.mount("/", NoCacheStatic(directory=str(config.WEB_DIR)), name="web")
