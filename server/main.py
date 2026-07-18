"""FastAPI 应用：REST（会话/历史/任务）+ WebSocket（流式对话）。"""
import asyncio
import base64
import json
import re
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config, db, memory_store, agent_store, skill_store, asr_client, session_import, wecom_notify, scheduler, worktree, arbiter, dispatcher, goal_summary
from . import logging_util
from .llms_doc import build_llms_txt, build_llms_full_txt
from .claude_runner import runner
from .session_hub import hub, Subscriber, _runner_for


_uploads_cleanup_task = None

# 合法 mode：完整模型列表 + 兼容存量的旧档位值。
_VALID_MODES = set(config.CLAUDE_MODELS) | set(config.CODEX_MODELS) | {"fast", "strong", "super"}
# 合法 effort（推理强度）：low/medium/high/xhigh/max。
_VALID_EFFORTS = set(config.CLAUDE_EFFORTS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 日志装配点：安装 LoggerProvider（默认透传 stdlib logging，行为等价）。各业务模块
    # 经 logging_util.get_logger 获取 logger，此处是唯一的初始化/注入入口——将来要切换
    # 日志后端或测试注入假 logger，只改这一行传入自定义 Provider 即可，无需动业务模块。
    logging_util.configure()
    # 启动：初始化 SQLite。比 @app.on_event("startup") 更可靠，TestClient / 多种部署方式都能触发。
    db.init_db()
    # 目标循环卡在 producing/verifying 的复位到 running：无条件跑（幂等 UPDATE，双进程各跑一次
    # 无害）。不能藏在 RECONCILE_ON_START 守卫下——默认 run.sh 不设该变量，否则重启后目标循环永久卡死。
    db.reconcile_goal_schedules()
    # 同理复位卡在 verifying 的 dispatch 扇出子任务：后台判定随进程消失、_tick_fanout 只拾 dispatched，
    # 不复位则永久卡死、plan 无法聚合收尾。幂等 UPDATE，同样不藏在 RECONCILE_ON_START 守卫下。
    db.reconcile_dispatch_subtasks()
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
    # 预热 SwanLab sid，避免第一批请求并发登录竞争
    asyncio.ensure_future(_swanlab_sid_warmup())
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
    if workdir.strip() == "@self":
        workdir = config.SELF_REPO_DIR
    mode = payload.get("mode") if payload.get("mode") in _VALID_MODES else None
    effort = payload.get("effort") if payload.get("effort") in _VALID_EFFORTS else None
    engine = payload.get("engine")
    engine = engine if engine in config.VALID_ENGINES else config.DEFAULT_ENGINE
    workdir, branch, is_wt, wt_base, notice = await asyncio.to_thread(
        worktree.provision_workdir, workdir, title, bool(payload.get("isolate")))
    s = db.create_session(title, workdir, mode,
                          worktree_branch=branch, is_worktree=is_wt, worktree_base=wt_base,
                          engine=engine, effort=effort)
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


@app.patch("/api/sessions/{sid}/effort", dependencies=[Depends(require_auth)])
async def set_session_effort(sid: str, payload: dict):
    effort = payload.get("effort")
    if effort not in _VALID_EFFORTS:
        raise HTTPException(status_code=400, detail="effort 需为：" + ", ".join(config.CLAUDE_EFFORTS))
    sess = db.get_session(sid)
    if not sess:
        raise HTTPException(status_code=404, detail="会话不存在")
    if sess.get("engine") == "codex":
        raise HTTPException(status_code=400, detail="codex 会话不支持调节推理强度")
    db.update_session(sid, effort=effort)
    # 会话正在跑：只更库，下一回合自然生效，不动进程；空闲则回收常驻进程使其立即重建生效。
    if not hub.is_running(sid):
        await runner.forget_session(sid)
    return {"ok": True, "effort": effort}


@app.patch("/api/sessions/{sid}/engine", dependencies=[Depends(require_auth)])
async def set_session_engine(sid: str, payload: dict):
    engine = payload.get("engine")
    if engine not in config.VALID_ENGINES:
        raise HTTPException(status_code=400, detail="engine 需为：" + ", ".join(sorted(config.VALID_ENGINES)))
    if not db.get_session(sid):
        raise HTTPException(status_code=404, detail="会话不存在")
    if db.list_messages(sid):
        raise HTTPException(status_code=409, detail="会话已开始，无法切换底层 Agent")
    db.update_session(sid, engine=engine)
    return {"ok": True, "engine": engine}


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
        await _runner_for(old).forget_session(sid)
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


def _claimed_worktree_branches() -> set[str]:
    """当前 sessions 表里被占用的 worktree 分支集合（含归档/秘书会话——只要记录在就算认领）。"""
    rows = db._query(
        "SELECT DISTINCT worktree_branch FROM sessions"
        " WHERE worktree_branch IS NOT NULL AND worktree_branch != ''"
    )
    return {r["worktree_branch"] for r in rows}


@app.get("/api/worktrees/orphans", dependencies=[Depends(require_auth)])
async def list_worktree_orphans():
    """列出孤儿 agent/* 分支：仓库里存在但没有任何会话记录认领的分支（会话已删、分支残留）。
    只读维护接口，供 ops/管理员手动清理用，无前端 UI。"""
    branches = await asyncio.to_thread(worktree.list_agent_branches, config.SELF_REPO_DIR)
    claimed = _claimed_worktree_branches()
    orphans = [b for b in branches if b["branch"] not in claimed]
    return {"orphans": orphans}


@app.delete("/api/worktrees/orphans/{branch_name:path}", dependencies=[Depends(require_auth)])
async def delete_worktree_orphan(branch_name: str):
    """删除一个孤儿 agent/* 分支（git branch -D）。删除前再次核实未被会话占用，防 race。"""
    if not worktree.is_agent_branch(branch_name):
        raise HTTPException(status_code=400, detail="分支名不合法（仅允许 agent/<name> 形态）")
    if branch_name in _claimed_worktree_branches():
        raise HTTPException(status_code=409, detail="该分支已被会话占用，拒绝删除")
    ok, err = await asyncio.to_thread(worktree.delete_branch, config.SELF_REPO_DIR, branch_name)
    if not ok:
        raise HTTPException(status_code=400, detail=f"删除分支失败：{err}")
    return {"ok": True, "branch": branch_name}


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
        # codex 无 effort 概念，其 ensure_warm 也不接受该 kwarg；仅 claude 引擎透传。
        warm_extra = {} if sess.get("engine") == "codex" else {
            "effort": sess.get("effort") or config.CLAUDE_EFFORT
        }
        status = await _runner_for(sess).ensure_warm(sid, workdir, resume=claude_session_id, **warm_extra)
        return {"ok": True, "status": status, "claude_session_id": claude_session_id}
    except (FileNotFoundError, PermissionError, OSError) as e:
        raise HTTPException(status_code=500, detail=f"启动 Claude CLI 失败：{e}")


@app.post("/api/sessions/{sid}/compact", dependencies=[Depends(require_auth)])
async def compact_session(sid: str):
    """真实上下文压缩：把整段对话概括成摘要并重置会话，摘要作前缀注入下一条消息续接。"""
    if not db.get_session(sid):
        raise HTTPException(status_code=404, detail="会话不存在")
    res = await hub.compact(sid)
    # 回合进行中属资源冲突，与「会话已开始，无法切换底层 Agent」统一用 409 语义
    if not res.get("ok"):
        raise HTTPException(status_code=409, detail=res.get("error", "压缩失败"))
    return res


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


@app.get("/api/jobs", dependencies=[Depends(require_auth)])
async def get_jobs(kind: str = Query(default=None), limit: int = Query(default=50)):
    return db.list_jobs(kind, limit)


@app.get("/api/jobs/{jid}", dependencies=[Depends(require_auth)])
async def get_job(jid: str):
    from pathlib import Path as _P
    job = db.get_job(jid)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    # 读取 log 全文（限最后 200KB），拼进 log_content 字段。安全校验：log_path 必须
    # 解析后落在 job_logs 目录内，不允许 .. 穿越；文件不存在则 log_content 留空。
    log_content = ""
    log_path = job.get("log_path") or ""
    if log_path:
        try:
            base = (_P(config.DB_PATH).parent / "job_logs").resolve()
            target = _P(log_path).resolve()
            if (target == base or target.is_relative_to(base)) and target.is_file():
                data = target.read_bytes()[-200 * 1024:]
                log_content = data.decode("utf-8", errors="replace")
        except (OSError, ValueError):
            log_content = ""
    job["log_content"] = log_content
    return job


# ---------------- 背对背双执行 + 综合仲裁 ----------------
@app.post("/api/arbitrate", dependencies=[Depends(require_auth)])
async def post_arbitrate(payload: dict):
    question = (payload.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="question 不能为空")
    # 与普通会话输入一致：支持 /<skill> 展开（延迟 import 打破循环依赖）
    from . import skill_store
    question, err = skill_store.expand(question)
    if err:
        raise HTTPException(status_code=400, detail=err)
    model_a = config.CLAUDE_MODEL_SUPER
    model_b = config.CODEX_MODEL or (config.CODEX_MODELS[0] if config.CODEX_MODELS else "")
    arb = db.create_arbitration(
        session_id=payload.get("session_id"), question=question, status="running",
        engine_a="claude", model_a=model_a,
        engine_b="codex", model_b=model_b,
        arbiter_model=config.ARBITER_MODEL,
    )
    asyncio.ensure_future(arbiter.run_arbitration(arb["id"]))
    return {"id": arb["id"]}


@app.get("/api/arbitrations", dependencies=[Depends(require_auth)])
async def get_arbitrations():
    return db.list_arbitrations()


@app.get("/api/arbitrations/{arb_id}", dependencies=[Depends(require_auth)])
async def get_arbitration(arb_id: str):
    arb = db.get_arbitration(arb_id)
    if not arb:
        raise HTTPException(status_code=404, detail="仲裁记录不存在")
    return arb


# ---------------- 角色化动态调度 ----------------
@app.post("/api/dispatch", dependencies=[Depends(require_auth)])
async def post_dispatch(payload: dict):
    request = (payload.get("request") or "").strip()
    if not request:
        raise HTTPException(status_code=400, detail="request 不能为空")
    # 与普通会话输入一致：支持 /<skill> 展开（延迟 import 打破循环依赖）
    from . import skill_store
    request, err = skill_store.expand(request)
    if err:
        raise HTTPException(status_code=400, detail=err)
    workdir = payload.get("workdir") or config.DEFAULT_WORKDIR
    if workdir.strip() == "@self":
        workdir = config.SELF_REPO_DIR
    plan_id = await dispatcher.dispatch(request, payload.get("session_id"), workdir,
                                        isolate=bool(payload.get("isolate")),
                                        need_arbitration=bool(payload.get("need_arbitration")))
    return {"plan_id": plan_id, "subtasks": db.list_dispatch_subtasks(plan_id)}


@app.get("/api/dispatch/plans", dependencies=[Depends(require_auth)])
async def get_dispatch_plans():
    return db.list_dispatch_plans()


@app.get("/api/dispatch/{plan_id}", dependencies=[Depends(require_auth)])
async def get_dispatch_plan(plan_id: str):
    subtasks = db.list_dispatch_subtasks(plan_id)
    # 附带每个子会话的当前 status，供前端展示进度
    for st in subtasks:
        csid = st.get("child_session_id")
        sess = db.get_session(csid) if csid else None
        st["session_status"] = sess.get("status") if sess else None
    return subtasks


@app.post("/api/dispatch/{plan_id}/subtasks/{subtask_id}/retry",
          dependencies=[Depends(require_auth)])
async def post_dispatch_subtask_retry(plan_id: str, subtask_id: str):
    """重派一个失败的 dispatch 子任务：新起子会话跑同样内容，状态重置回 dispatched，
    交回 _tick_fanout 自动接管判定。仅对终态 failed/error 生效，其余状态拒绝避免误重派。"""
    sub = db.get_dispatch_subtask(subtask_id)
    if not sub or sub.get("plan_id") != plan_id:
        raise HTTPException(status_code=404, detail="子任务不存在")
    if sub.get("status") not in ("failed", "error"):
        raise HTTPException(status_code=409, detail="仅失败的子任务可重派")
    updated = await dispatcher.retry_subtask(subtask_id)
    if not updated:
        raise HTTPException(status_code=409, detail="子任务当前状态不可重派")
    return updated
@app.get("/api/work_items", dependencies=[Depends(require_auth)])
async def get_work_items(origin: str = Query(default=None), status: str = Query(default=None),
                         limit: int = Query(default=50)):
    return db.list_work_items(origin=origin, status=status, limit=limit)


@app.get("/api/work_items/{wid}", dependencies=[Depends(require_auth)])
async def get_work_item(wid: str):
    item = db.get_work_item(wid)
    if not item:
        raise HTTPException(status_code=404, detail="work_item not found")
    # 关联详情（尽力而为）：按 origin 顺手反查来源表摘要，复用现有查询函数，
    # 任一查询失败都不影响返回 work_item 本体。related 为 None 表示无关联或反查失败。
    related = None
    try:
        origin = item.get("origin")
        ref_id = item.get("ref_id") or ""
        if origin in ("goal", "triage") and ref_id:
            sch = db.get_schedule(ref_id)
            if sch:
                related = {"kind": "schedule", "goal_status": sch.get("goal_status"),
                           "enabled": sch.get("enabled"), "iter_count": sch.get("iter_count"),
                           "max_iterations": sch.get("max_iterations")}
        elif origin == "dispatch" and ref_id:
            related = {"kind": "dispatch_subtasks", "subtasks": db.list_dispatch_subtasks(ref_id)}
        elif origin == "arbiter" and ref_id:
            arb = db.get_arbitration(ref_id)
            if arb:
                related = {"kind": "arbitration", "status": arb.get("status"),
                           "verdict": arb.get("verdict")}
    except Exception as e:
        print(f"[work_items] related lookup failed: {type(e).__name__}: {e}")
        related = None
    item["related"] = related
    return item


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


# ---------------- Skill 管理（.claude/skills/<name>/SKILL.md）----------------
@app.get("/api/skills", dependencies=[Depends(require_auth)])
async def skills_list():
    return skill_store.list_skills_meta()


@app.post("/api/skills", dependencies=[Depends(require_auth)])
async def skills_create(payload: dict):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name 不能为空")
    try:
        return skill_store.create_skill(name, payload.get("description", ""), payload.get("body", ""))
    except FileExistsError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/skills/{name}", dependencies=[Depends(require_auth)])
async def skills_get(name: str):
    try:
        s = skill_store.get_skill(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not s:
        raise HTTPException(status_code=404, detail="skill 不存在")
    return s


@app.put("/api/skills/{name}", dependencies=[Depends(require_auth)])
async def skills_update(name: str, payload: dict):
    try:
        return skill_store.update_skill(name, payload.get("description"), payload.get("body"))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/api/skills/{name}", dependencies=[Depends(require_auth)])
async def skills_delete(name: str):
    try:
        return skill_store.delete_skill(name)
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


# ---------------- llms.txt / llms-full.txt（供外部 agent 摄取，不鉴权）----------------
@app.get("/llms.txt")
async def llms_txt():
    """项目文本索引（llms.txt 约定）；无鉴权供外部 agent 直接摄取。"""
    return PlainTextResponse(build_llms_txt(app), media_type="text/plain; charset=utf-8")


@app.get("/llms-full.txt")
async def llms_full_txt():
    """项目全文：索引 + 所有 skill / subagent 定义全文内联。"""
    return PlainTextResponse(build_llms_full_txt(app), media_type="text/plain; charset=utf-8")


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
        verify_command = (payload.get("verify_command") or "").strip()
        exec_mode = payload.get("exec_mode") or "solo"
        if exec_mode not in ("solo", "team"):
            raise HTTPException(status_code=400, detail="exec_mode 仅支持 solo / team")
        goal_mode = payload.get("goal_mode") or "flat"
        if goal_mode not in ("flat", "planned"):
            raise HTTPException(status_code=400, detail="goal_mode 仅支持 flat / planned")
        return {"kind": kind, "interval_min": None, "at_hhmm": None,
                "stop_condition": stop_condition, "max_iterations": max_iterations,
                "verify_command": verify_command, "exec_mode": exec_mode, "goal_mode": goal_mode}
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
        sch = db.create_schedule(session_id, prompt, "goal", None, None, nxt,
                                 stop_condition=norm["stop_condition"],
                                 max_iterations=norm["max_iterations"], goal_status="running",
                                 verify_command=norm["verify_command"], exec_mode=norm["exec_mode"],
                                 goal_mode=norm["goal_mode"])
        # 阶段2 影子表：纯附加观测，写失败只记日志绝不影响建循环主流程
        sess = db.get_session(session_id)
        db.create_work_item_safe(
            origin="goal", topology="iterate",
            isolation="worktree" if (sess and sess.get("is_worktree")) else "shared",
            verify_mode="command" if norm["verify_command"] else "nl",
            status="pending", ref_id=sch["id"], session_id=session_id, summary=prompt,
        )
        return sch
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
                "verify_command": norm["verify_command"], "exec_mode": norm["exec_mode"],
                "goal_mode": norm["goal_mode"],
                "goal_status": "running", "iter_count": 0, "last_feedback": "",
                # planned 相关状态全部复位，旧子任务清空——下个 tick 会按新目标重新拆解
                "plan_status": "", "active_subtask_id": "",
            })
            db.replace_goal_subtasks(sid, [])
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
            # planned 循环重启也要复位拆解态并清子任务，才能按当前目标重新拆
            fields["plan_status"] = ""
            fields["active_subtask_id"] = ""
            db.replace_goal_subtasks(sid, [])
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


@app.get("/api/schedules/{sid}/iterations", dependencies=[Depends(require_auth)])
async def schedules_iterations(sid: str):
    """某个目标循环的每轮迭代历史（含验收判定/反馈/产出摘要），只读。"""
    if not db.get_schedule(sid):
        raise HTTPException(status_code=404, detail="定时任务不存在")
    return db.list_goal_iterations(sid)


@app.get("/api/schedules/{sid}/subtasks", dependencies=[Depends(require_auth)])
async def schedules_subtasks(sid: str):
    """planned 目标循环拆解出的子任务清单及各自执行/验收状态，只读。"""
    if not db.get_schedule(sid):
        raise HTTPException(status_code=404, detail="定时任务不存在")
    return db.list_goal_subtasks(sid)


@app.post("/api/schedules/{sid}/continue", dependencies=[Depends(require_auth)])
async def schedules_continue(sid: str, payload: dict):
    """目标循环「续跑」：在保留历史（iter_count/反馈/子任务）的前提下追加轮数继续迭代。

    与 PUT 的「重新启用」语义不同——PUT 对 goal 传 enabled=true 会清零 iter_count/清空反馈子任务
    （从头重跑），本接口只抬 max_iterations + 复位 running/enabled + 重算 next_run，绝不碰历史。
    payload 全可选：prompt（改目标）、stop_condition（改完成标准）、add_iterations（追加轮数，默认 3）。"""
    sch = db.get_schedule(sid)
    if not sch:
        raise HTTPException(status_code=404, detail="定时任务不存在")
    if sch.get("kind") != "goal":
        raise HTTPException(status_code=400, detail="仅目标循环支持续跑")
    # 纵深防御：仅未完成（已耗尽或人工暂停）才允许续跑，拒掉进行中/已完成，避免误把 max_iterations
    # 缩成 iter_count+add 而提前掐断正在跑的循环。与前端「继续」按钮的显示条件一致。
    status = sch.get("goal_status") or ""
    enabled = sch.get("enabled")
    is_exhausted = status == "exhausted"
    is_paused = (not enabled) and status not in ("done", "exhausted")  # 人工暂停：enabled=0 且非终态
    if not (is_exhausted or is_paused):
        raise HTTPException(status_code=400, detail="仅未完成（已耗尽或已暂停）的目标循环支持续跑")
    iter_count = int(sch.get("iter_count") or 0)
    add = int(payload.get("add_iterations") or 3)
    if add < 1 or add > 50:
        raise HTTPException(status_code=400, detail="追加轮数需在 1-50 之间")
    new_max = iter_count + add
    if new_max > 100:
        raise HTTPException(status_code=400, detail="累计迭代上限不能超过 100 轮")
    # 只复位可继续的调度字段，绝不碰 iter_count/last_feedback/goal_iterations/子任务
    fields = {
        "goal_status": "running", "enabled": 1, "max_iterations": new_max,
        "next_run": scheduler.compute_next_run("goal", None, None),
    }
    p = (payload.get("prompt") or "").strip()
    if p:
        fields["prompt"] = p
    sc = payload.get("stop_condition")
    if sc is not None and sc.strip():
        fields["stop_condition"] = sc.strip()
    db.update_schedule(sid, **fields)
    return db.get_schedule(sid)


# ---------------- 目标循环历史（H2 goal loop：列表态 + 详情态）----------------
@app.get("/api/goals", dependencies=[Depends(require_auth)])
async def goals_list():
    return db.list_goal_loops()


@app.get("/api/goals/{sid}", dependencies=[Depends(require_auth)])
async def goals_detail(sid: str):
    detail = db.get_goal_loop_detail(sid)
    if not detail:
        raise HTTPException(status_code=404, detail="目标循环不存在")
    return detail


@app.post("/api/goals/summary", dependencies=[Depends(require_auth)])
async def goals_summary():
    """汇总所有目标循环的完成情况，用一次性子进程生成一段中文小结（点按钮触发，不缓存）。"""
    try:
        ok, summary = await goal_summary.summarize_goals()
        return {"ok": ok, "summary": summary}
    except Exception as e:
        return {"ok": False, "summary": "总结生成失败", "error": f"{type(e).__name__}: {e}"}


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
    rows = db._query("SELECT * FROM todos WHERE id=?", (tid,))
    if not rows or dict(rows[0]).get("status") != "triage":
        raise HTTPException(status_code=404, detail="该事项已不在待分诊状态")
    db.update_todo(tid, status="cancelled")
    return {"ok": True}


@app.post("/api/triage/{tid}/to_todo", dependencies=[Depends(require_auth)])
async def triage_to_todo(tid: str):
    rows = db._query("SELECT * FROM todos WHERE id=?", (tid,))
    if not rows or dict(rows[0]).get("status") != "triage":
        raise HTTPException(status_code=404, detail="该事项已不在待分诊状态")
    db.update_todo(tid, status="pending", source="manual")
    return {"ok": True}


# ---------------- Backlog 导入：mandatory-backlog.md → triage 收件箱 ----------------
@app.post("/api/backlog/scan", dependencies=[Depends(require_auth)])
async def backlog_scan(auto: bool = False):
    from . import backlog_import
    # scan_backlog 是同步函数（auto_dispatch 时内部用 asyncio.run 派单），放线程里跑避免阻塞事件循环
    return await asyncio.to_thread(backlog_import.scan_backlog, auto_dispatch=auto)


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


@app.get("/api/audio")
async def get_audio(path: str = Query(default=None)):
    """音频代理：给 viz HTML 里的 <audio src> 用。本路由不挂 require_auth——因为
    app.mount("/fs", StaticFiles(directory="/")) 已无鉴权全盘暴露文件系统，本路由
    不算新增风险面；安全边界靠 AUDIO_ROOTS 白名单（只允许读白名单 root 下的绝对
    路径）+ 文件类型过滤。Range/206 由 Starlette FileResponse 原生支持。"""
    from pathlib import Path as _P

    if not path:
        raise HTTPException(status_code=400, detail="缺少 path 参数")
    p = _P(path).resolve()
    if not any(p == _P(root).resolve() or p.is_relative_to(_P(root).resolve()) for root in config.AUDIO_ROOTS):
        raise HTTPException(status_code=403, detail="路径不在允许的音频根目录内")
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="文件不存在")
    ext = p.suffix.lower()
    if ext not in _AUDIO_EXTS:
        raise HTTPException(status_code=415, detail="不支持的文件类型")
    return FileResponse(str(p), media_type=_AUDIO_MIME.get(ext, "application/octet-stream"))


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

    # 文件落全局 UPLOAD_DIR，与会话无关：payload.session_id 仅作参考，
    # 为空或查不到也照常上传（仲裁/分派/目标循环等入口发起时可能尚无当前会话）。

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
SWANLAB_ALLOWED_PREFIXES = ("_next/", "api/", "@", "login/", "static/", "favicon", "assets/", "")
_swanlab_sid_cache: dict = {}


async def _swanlab_sid_warmup():
    """启动时预热 SwanLab sid，避免第一批并发请求各自登录。"""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{SWANLAB_HOST}/api/login/api_key",
                headers={"authorization": config.SWANLAB_API_KEY},
            )
            if r.status_code == 200:
                _swanlab_sid_cache["sid"] = r.json().get("sid", "")
    except Exception:
        pass


@app.api_route("/proxy/swanlab/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"])
async def swanlab_proxy(request: Request, path: str):
    # 不加 require_auth：iframe 里的静态资源请求带不上鉴权 header，否则会 401
    import httpx
    # 路径白名单：只放行 SwanLab 已知前缀，其余一律拒绝
    if path and not any(path.startswith(p) for p in SWANLAB_ALLOWED_PREFIXES):
        raise HTTPException(status_code=403, detail="不允许访问该路径")

    async def _login_swanlab() -> str:
        try:
            async with httpx.AsyncClient(timeout=10) as lc:
                r = await lc.post(
                    f"{SWANLAB_HOST}/api/login/api_key",
                    headers={"authorization": config.SWANLAB_API_KEY},
                )
                if r.status_code == 200:
                    sid = r.json().get("sid", "")
                    _swanlab_sid_cache["sid"] = sid
                    return sid
        except Exception:
            pass
        return ""

    # sid：服务端缓存，避免每次重新登录
    browser_sid = _swanlab_sid_cache.get("sid", "") or request.cookies.get("swanlab_sid", "")
    if not browser_sid:
        browser_sid = await _login_swanlab()

    url = f"{SWANLAB_HOST}/{path}"
    params = dict(request.query_params)
    FORWARD_HEADERS = {
        "user-agent", "accept", "accept-language", "content-type",
        "rsc", "next-router-state-tree", "next-router-prefetch",
        "next-router-segment-prefetch", "next-url",
    }
    forward_headers = {k: v for k, v in request.headers.items() if k.lower() in FORWARD_HEADERS}
    if browser_sid:
        forward_headers["cookie"] = f"sid={browser_sid}"
    body = await request.body()

    async with httpx.AsyncClient(follow_redirects=False, timeout=30) as client:
        resp = await client.request(
            method=request.method, url=url, params=params,
            headers=forward_headers, content=body,
        )

    # sid 过期（401）→ 重新登录并重试一次
    if resp.status_code == 401 and path.startswith("api/"):
        _swanlab_sid_cache.clear()
        browser_sid = await _login_swanlab()
        if browser_sid:
            forward_headers["cookie"] = f"sid={browser_sid}"
            async with httpx.AsyncClient(follow_redirects=False, timeout=30) as client:
                resp = await client.request(
                    method=request.method, url=url, params=params,
                    headers=forward_headers, content=body,
                )
    # set-cookie 也剔除：sid 由下面统一用 swanlab_sid 名字重新种，避免 SwanLab 原始 cookie 干扰
    excluded = {"transfer-encoding", "content-encoding", "content-length", "connection", "set-cookie"}
    resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded}

    # 重定向：把 Location 头里指向 SwanLab 的绝对/相对 URL 改写回代理路径
    if resp.status_code in (301, 302, 303, 307, 308):
        location = resp.headers.get("location", "")
        if location.startswith(SWANLAB_HOST):
            location = "/proxy/swanlab/" + location[len(SWANLAB_HOST):].lstrip("/")
        elif location.startswith("/") and not location.startswith("/proxy/swanlab"):
            location = "/proxy/swanlab" + location
        resp_headers["location"] = location

    # HTML 路径重写：SwanLab 是 Vite SPA，资源路径和 JS 内 fetch('/api/...') 都是绝对路径，
    # 在 iframe 里会打到 agent-console 自身导致 404/错误。三步处理：
    # 1) 注入 <base> 让相对路径资源走代理
    # 2) 正则重写 src/href/action 里的绝对路径
    # 3) 注入 fetch/XHR 拦截器，把 JS 运行时的 /api/ 请求重定向到 /proxy/swanlab/api/
    content_type = resp.headers.get("content-type", "")
    content = resp.content
    if "javascript" in content_type or (path.startswith("assets/") and "text" not in content_type and "image" not in content_type):
        try:
            text = content.decode("utf-8", errors="replace")
            # Vite preload URL builder: function(e){return"/"+e} → patch to include proxy prefix
            if 'return"/"+e' in text:
                text = text.replace('return"/"+e', 'return"/proxy/swanlab/"+e')
                content = text.encode("utf-8")
        except Exception:
            pass
    if "text/html" in content_type:
        try:
            text = content.decode("utf-8", errors="replace")
            # 第一步：正则重写 src/href/action 里的绝对路径（必须在注入 <base> 之前，否则会把注入的 base href 也重写一遍）
            text = re.sub(r'((?:src|href|action)=["\'])(/(?!/))', r'\1/proxy/swanlab\2', text)
            # 第二步：注入 fetch/XHR 拦截器 + Vue Router pathname 修正 + <base>，统一拼在 <head> 开头
            base_tag = '<base href="/proxy/swanlab/">'
            interceptor = """<script>
(function(){
  var _PROXY = '/proxy/swanlab';
  var _ORIGIN = location.origin; // e.g. https://29.191.211.218.devcloud.woa.com
  function rewrite(url) {
    if (typeof url !== 'string') return url;
    // absolute URL pointing to same origin: strip origin then add proxy prefix
    if (url.startsWith(_ORIGIN + '/') && !url.startsWith(_ORIGIN + _PROXY)) {
      return _ORIGIN + _PROXY + url.slice(_ORIGIN.length);
    }
    // root-relative path
    if (url.startsWith('/') && !url.startsWith(_PROXY)) {
      return _PROXY + url;
    }
    return url;
  }
  // 1. fetch interceptor — covers Vite preload polyfill's fetch(link.href) and API calls
  var _fetch = window.fetch;
  window.fetch = function(input, init) {
    if (typeof input === 'string') input = rewrite(input);
    else if (input && typeof input === 'object' && input.url) input = new Request(rewrite(input.url), input);
    return _fetch.call(this, input, init);
  };
  // 2. XHR interceptor
  var _open = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function(method, url) {
    arguments[1] = rewrite(url);
    return _open.apply(this, arguments);
  };
  // 3. Intercept createElement so any <link href="/assets/..."> or <script src="/assets/...">
  //    gets the proxy prefix BEFORE it is appended to the DOM.
  var _createElement = document.createElement.bind(document);
  document.createElement = function(tag) {
    var el = _createElement(tag);
    var t = tag.toLowerCase();
    if (t === 'link') {
      Object.defineProperty(el, 'href', {
        set: function(v) { el.setAttribute('href', rewrite(v)); },
        get: function() { return el.getAttribute('href') || ''; },
        configurable: true,
      });
    } else if (t === 'script') {
      Object.defineProperty(el, 'src', {
        set: function(v) { el.setAttribute('src', rewrite(v)); },
        get: function() { return el.getAttribute('src') || ''; },
        configurable: true,
      });
    }
    return el;
  };
  // 4. dynamic import() resolves relative to the current page URL which already has /proxy/swanlab/
  //    so "./dynamic/App.js" resolves correctly. Nothing needed here.
  // NOTE: do NOT override history.pushState/replaceState or location.pathname.
  // Vue Router reads <base href="/proxy/swanlab/"> and sets its own base correctly.
  // Overriding pushState causes an infinite navigation loop.
})();
</script>"""
            inject = base_tag + interceptor
            if "<head>" in text:
                text = text.replace("<head>", f"<head>{inject}", 1)
            else:
                text = inject + text
            content = text.encode("utf-8")
            resp_headers["content-type"] = "text/html; charset=utf-8"
        except Exception:
            pass  # 解码/重写失败就原样透传

    response = Response(content=content, status_code=resp.status_code, headers=resp_headers)
    if "text/html" in resp_headers.get("content-type", ""):
        response.headers["cache-control"] = "no-store"
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
