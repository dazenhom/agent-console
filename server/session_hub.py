"""SessionHub：连接无关的会话中枢。

把「跑一个回合」从 WebSocket 连接里彻底解耦出来，是「多 Agent 调度台」的地基：
  - 回合执行不再绑某条 WS。WS 断开（切会话/锁屏/弱网）不再杀任务——任务在 hub 里
    后台跑完、落库、发企业微信通知。
  - 一个会话可被 0..N 条 WS 同时订阅（多端同看）；事件广播给所有订阅者。
  - 跨会话天然并行：runner 的常驻进程本就按 session_id 隔离，hub 只是按 sid 各起一个
    后台回合 task，互不阻塞。
  - 额外一条「监控」订阅（/ws/monitor）拿到所有会话的状态/活动变化，驱动前端会话列表
    实时刷新（working / 完成 / 需要输入）。

设计要点：
  - 单例 `hub`（模块底部），main.py 直接用。
  - Subscriber 是对一条 WS 的轻封装，带 send() 和 hidden（前台/后台可见性）。
  - 企业微信门控升级：回合结束时，只要该会话「没有任何处于前台的订阅者」就发通知
    （含 0 订阅者——后台并行跑完也能收到，比旧的单连接 client_hidden 更对）。
"""
import asyncio
import json
import re
import time
from typing import Awaitable, Callable

from . import agent_store, config, db, wecom_notify
from .claude_runner import runner as claude_runner
from .codex_runner import runner as codex_runner
from .logging_util import get_logger

logger = get_logger(__name__)

_PROVIDERS = {"claude": claude_runner, "codex": codex_runner}

# agent_store.list_agents() 会读磁盘上的 agent 定义文件（运行时可增删改），
# 这里做 30s 短 TTL 缓存：既不每条事件都读盘，又能及时感知定义变更。
_AGENT_MODEL_TTL = 30.0
_agent_model_cache: dict[str, str] = {}
_agent_model_cache_at = 0.0


def _agent_model_map() -> dict[str, str]:
    """subagent_type -> model 映射（带短 TTL 缓存，空 model 归一为空串）。"""
    global _agent_model_cache, _agent_model_cache_at
    now = time.monotonic()
    if now - _agent_model_cache_at > _AGENT_MODEL_TTL:
        try:
            _agent_model_cache = {a["name"]: a.get("model") or "" for a in agent_store.list_agents()}
        except Exception:
            _agent_model_cache = {}
        _agent_model_cache_at = now
    return _agent_model_cache


def _runner_for(sess):
    """按会话 engine 字段选 runner，未知或缺省 engine 默认落 ClaudeRunner。"""
    return _PROVIDERS.get((sess or {}).get("engine"), claude_runner)


# ---------------- stream-json 事件翻译（从 main.py 搬入） ----------------
def translate_event(evt: dict) -> list[dict]:
    """把 Claude stream-json 事件翻译成前端消息（可能 0~N 条）。"""
    out: list[dict] = []
    etype = evt.get("type")

    if etype == "system":
        # 子智能体实时事件只放行 task_progress / task_notification 这两个 subtype，
        # 其余（init/task_started/task_updated 等）仍丢弃，避免 messages 膨胀。
        subtype = evt.get("subtype")
        usage = evt.get("usage") or {}
        if subtype == "task_progress":
            out.append({
                "role": "subagent_progress",
                "content": {
                    "parent": evt.get("tool_use_id"),
                    "tokens": usage.get("total_tokens"),
                    "tool_uses": usage.get("tool_uses"),
                    "duration_ms": usage.get("duration_ms"),
                    "last_tool": evt.get("last_tool_name"),
                },
            })
        elif subtype == "task_notification":
            out.append({
                "role": "subagent_done",
                "content": {
                    "parent": evt.get("tool_use_id"),
                    "status": evt.get("status"),
                    "tokens": usage.get("total_tokens"),
                    "tool_uses": usage.get("tool_uses"),
                    "duration_ms": usage.get("duration_ms"),
                },
            })

    elif etype == "stream_event":
        # --include-partial-messages 的实时增量，只取文本增量做打字机。
        # sub-agent 的增量同样带 parent_tool_use_id，透传给前端按 parent 路由到子卡片，
        # 避免子智能体的流式文本绕过归拢冒到顶层。
        ev = evt.get("event", {})
        if ev.get("type") == "content_block_delta":
            delta = ev.get("delta", {})
            if delta.get("type") == "text_delta" and delta.get("text"):
                c = {"text": delta["text"]}
                pid = evt.get("parent_tool_use_id")
                if pid:
                    c["parent"] = pid
                out.append({"role": "assistant_delta", "content": c})

    elif etype == "assistant":
        # sub-agent（Agent 工具）内部产生的事件带 parent_tool_use_id，指向发起它的
        # Agent tool_use id；顶层事件该字段为空。透传给前端做子智能体折叠归拢。
        pid = evt.get("parent_tool_use_id")
        for block in evt.get("message", {}).get("content", []):
            btype = block.get("type")
            if btype == "text" and block.get("text", "").strip():
                c = {"text": block["text"]}
                if pid:
                    c["parent"] = pid
                out.append({"role": "assistant", "content": c})
            elif btype == "tool_use":
                c = {"id": block.get("id"), "name": block.get("name"), "input": block.get("input", {})}
                if pid:
                    c["parent"] = pid
                if block.get("input", {}).get("run_in_background"):
                    c["bg"] = True
                if block.get("name") == "Agent":
                    # 附带 subagent_type 及 agent_store 里该类型的配置模型，供卡片头部汇总展示；
                    # Explore/Task 这类内置 subagent_type 不在 agent_store 里，查不到就不塞 model。
                    inp = block.get("input", {})
                    c["subagent_type"] = inp.get("subagent_type")
                    model = _agent_model_map().get(inp.get("subagent_type") or "")
                    if model:
                        c["model"] = model
                out.append({"role": "tool_use", "content": c})

    elif etype == "user":
        pid = evt.get("parent_tool_use_id")
        content = evt.get("message", {}).get("content", [])
        # tool_use_result 在事件级而非 block 级；只把它并入本事件的第一个 tool_result。
        tur = evt.get("tool_use_result")
        if isinstance(content, list):
            seen_tool_result = False
            for block in content:
                if block.get("type") == "tool_result":
                    raw = block.get("content")
                    if isinstance(raw, list):
                        raw = "\n".join(b.get("text", "") for b in raw if isinstance(b, dict))
                    c = {
                        "tool_use_id": block.get("tool_use_id"),
                        "output": raw,
                        "is_error": block.get("is_error", False),
                    }
                    if pid:
                        c["parent"] = pid
                    if not seen_tool_result and isinstance(tur, dict) and tur.get("agentId"):
                        c["agent_meta"] = {
                            "agent_type": tur.get("agentType"),
                            "model": tur.get("resolvedModel"),
                            "duration_ms": tur.get("totalDurationMs"),
                            "tokens": tur.get("totalTokens"),
                            "tool_uses": tur.get("totalToolUseCount"),
                            "status": tur.get("status"),
                        }
                    seen_tool_result = True
                    out.append({"role": "tool_result", "content": c})

    elif etype == "result":
        out.append({
            "role": "result",
            "content": {
                "subtype": evt.get("subtype"),
                "duration_ms": evt.get("duration_ms"),
                "cost_usd": evt.get("total_cost_usd"),
                "num_turns": evt.get("num_turns"),
                "result": evt.get("result"),
                "is_error": evt.get("is_error", False),
            },
        })

    elif etype == "status":
        # 纯瞬时状态提示（如 codex 续跑心跳），只广播给前端展示，不落库、不进 reply_text、
        # 不计入 _activity_label —— 避免被当成真实 assistant 回复污染摘要/标题/通知/进度。
        txt = evt.get("text", "")
        if txt:
            out.append({"role": "status", "content": {"text": txt}})

    return out


def _activity_label(msg: dict) -> str | None:
    """从一条翻译后的消息提炼「当前在干嘛」一行活动，给会话列表实时显示。"""
    role = msg.get("role")
    c = msg.get("content") or {}
    if role == "tool_use":
        name = c.get("name") or "工具"
        inp = c.get("input") or {}
        brief = inp.get("command") or inp.get("file_path") or inp.get("path") or inp.get("pattern") or ""
        brief = str(brief)[:48]
        return f"{name}：{brief}" if brief else f"调用 {name}"
    if role == "assistant":
        t = (c.get("text") or "").strip().replace("\n", " ")
        if t:
            return "回复：" + t[:48]
    return None


class Subscriber:
    """一条 WS 连接的轻封装。send 为发送回调；hidden 表示该客户端是否在后台（锁屏/切走）。"""
    __slots__ = ("send", "hidden")

    def __init__(self, send: Callable[[dict], Awaitable[None]], hidden: bool = False):
        self.send = send
        self.hidden = hidden


class SessionHub:
    def __init__(self):
        self._subs: dict[str, set[Subscriber]] = {}      # session_id -> 订阅者
        self._monitor: set[Subscriber] = set()           # 全局监控订阅者
        self._turns: dict[str, asyncio.Task] = {}        # session_id -> 当前回合 task
        self._activity: dict[str, str] = {}              # session_id -> 当前活动一行
        self._turn_started: dict[str, float] = {}        # session_id -> 本回合起始 time.monotonic()
        self._progress: dict[str, dict] = {}              # session_id -> 最近一次进度快照
        self._stuck_notified: set[str] = set()            # 本回合已发过 stuck 提醒的 session_id
        # session_id -> {request_id: {request_id, tool_name, input}} 待用户确认的权限请求。
        # 用户回应/回合取消后清空；WS 重连时重发，避免弹窗因断线丢失。
        self._pending_perms: dict[str, dict] = {}
        # session_id -> 在途的行摘要/标题合并生成任务（含去抖等待）。新回合开始/会话删除
        # 时取消旧的：前者让摘要改由新回合接手，后者防止对已删 sid 写孤儿数据
        # （_bind_out_of_turn 注释里踩过同款坑）。
        self._summary_tasks: dict[str, asyncio.Task] = {}

    # ---------- 订阅管理 ----------
    def subscribe(self, sid: str, sub: Subscriber) -> None:
        self._subs.setdefault(sid, set()).add(sub)

    def unsubscribe(self, sid: str, sub: Subscriber) -> None:
        subs = self._subs.get(sid)
        if subs:
            subs.discard(sub)
            if not subs:
                self._subs.pop(sid, None)

    def subscribe_monitor(self, sub: Subscriber) -> None:
        self._monitor.add(sub)

    def unsubscribe_monitor(self, sub: Subscriber) -> None:
        self._monitor.discard(sub)

    def is_running(self, sid: str) -> bool:
        t = self._turns.get(sid)
        return bool(t and not t.done())

    def running_sids(self) -> list[str]:
        return [sid for sid, t in self._turns.items() if t and not t.done()]

    def activity(self, sid: str) -> str:
        return self._activity.get(sid, "")

    # ---------- 广播 ----------
    async def broadcast(self, sid: str, obj: dict) -> None:
        for sub in list(self._subs.get(sid, ())):
            try:
                await sub.send(obj)
            except Exception:
                pass

    async def broadcast_monitor(self, obj: dict) -> None:
        for sub in list(self._monitor):
            try:
                await sub.send(obj)
            except Exception:
                pass

    async def emit_queue_update(self, sid: str) -> None:
        await self.broadcast(sid, {"type": "queue_update", "queue": db.list_queue(sid)})

    async def _emit_session_update(self, sid: str, **extra) -> None:
        """给监控订阅者推一条会话状态变化，驱动前端列表实时刷新。"""
        sess = db.get_session(sid)
        payload = {
            "type": "session_update",
            "session_id": sid,
            "status": "running" if self.is_running(sid) else (sess or {}).get("status", "idle"),
            "activity": self._activity.get(sid, ""),
            "summary": (sess or {}).get("summary", ""),
            "title": (sess or {}).get("title", ""),
            "updated_at": (sess or {}).get("updated_at", 0),
        }
        prog = self._progress.get(sid) or {}
        # elapsed 按 _turn_started 现算而非读 _progress 缓存：watchdog 两次进度推送之间
        # 隔了 PROGRESS_EVERY_SEC(30分钟)，读缓存会让列表里的耗时冻在上次推送的数字上。
        # 同 /api/progress 的处理。stuck 只有 watchdog 判得出，仍取缓存。
        started = self._turn_started.get(sid)
        payload["elapsed"] = (
            time.monotonic() - started if started is not None else prog.get("elapsed")
        )
        payload["stuck"] = bool(prog.get("stuck"))
        payload.update(extra)
        await self.broadcast_monitor(payload)

    def _has_foreground_sub(self, sid: str) -> bool:
        """该会话是否有「处于前台」的订阅者。无 → 回合结束发企业微信。"""
        return any(not s.hidden for s in self._subs.get(sid, ()))

    async def _notify_turn_done(self, sid: str, user_text: str, reply_text: str,
                                final_result: dict, *, kind: str = "turn",
                                elapsed: float | None = None) -> None:
        """统一的回合完成提醒出口：广播前端事件，并视条件发送企业微信。

        elapsed: 本回合实际耗时（秒），由调用方在清掉 self._turn_started[sid] 之前算好显式
        传入——这里不读 self._turn_started，因为 _run_turn 的 finally 块里
        self._turn_started.pop(sid, ...) 早于本方法调用，到这里读已经拿不到起始时间。
        长回合（elapsed 达到 config.NOTIFY_LONG_TURN_SEC）即使有前台订阅者也要发企微：
        用户开着页面切去干别的事、或手机锁屏但 WS 仍连着，都属于"人不在看"的场景，
        不能被 _has_foreground_sub 一刀切挡掉。"""
        status = final_result.get("status", "success")
        await self.broadcast(sid, {"type": "turn_done", "kind": kind, "status": status})
        await self.broadcast_monitor({
            "type": "turn_done", "session_id": sid, "kind": kind, "status": status,
        })
        # kind == "resume" 是续跑汇报：tclaude 后台任务跑完后 CLI 自发起的回合，
        # 用户此时必然不在前台等待（否则不会走后台续跑），无条件视为"长回合"，
        # 不能被 elapsed is None 挡掉，也不能被 _has_foreground_sub 挡掉。
        is_long_turn = kind == "resume" or (
            elapsed is not None and elapsed >= config.NOTIFY_LONG_TURN_SEC
        )
        if config.WECOM_ENABLED and not db.has_active_goal(sid) and (
            not self._has_foreground_sub(sid) or is_long_turn
        ):
            sess = db.get_session(sid)
            ok, detail = await wecom_notify.notify(
                title=(sess or {}).get("title") or "会话",
                user_text=user_text,
                reply_text=reply_text,
                status=status,
                duration_ms=final_result.get("duration_ms"),
                num_turns=final_result.get("num_turns"),
            )
            if not ok:
                logger.warning("wecom notify failed sid=%s reason=%s", sid, detail)

    def _bind_out_of_turn(self, sid: str) -> None:
        """给常驻 Claude 进程绑定回合外续跑事件回调，避免后台 Agent 汇报被吞。"""
        sess = db.get_session(sid)
        if sess is None:
            # 会话已被删除（如 remove_session 并发执行）：不能静默 fallback 到
            # _runner_for(None)（会落到 claude_runner，把"会话不存在"这个信号吞掉），
            # 直接放弃绑定，避免继续对已删 sid 写孤儿数据。
            return
        r = _runner_for(sess)
        sessions = getattr(r, "_sessions", None)
        if not sessions:
            return
        sess_rec = sessions.get(sid)
        if not sess_rec or sess_rec.get("out_of_turn_cb"):
            return
        reply_parts: list[str] = []
        resume_running = False

        async def _on_out_of_turn(evt: dict):
            nonlocal resume_running
            translated = translate_event(evt)
            for msg in translated:
                role = msg["role"]
                # assistant_delta/status 之外，子智能体进度/完成事件也纯实时刷新用，不进聊天记录。
                if role not in ("assistant_delta", "status", "subagent_progress", "subagent_done"):
                    db.add_message(sid, role, msg["content"])
                if role == "assistant":
                    text = (msg["content"] or {}).get("text", "")
                    if text:
                        reply_parts.append(text)
                await self.broadcast(sid, {"type": "message", **msg})
                label = _activity_label(msg)
                if label and not resume_running:
                    resume_running = True
                    db.update_session(sid, status="running")
                    self._activity[sid] = label
                    await self._emit_session_update(sid)
                elif label:
                    self._activity[sid] = label
                    await self._emit_session_update(sid)
                if role == "result":
                    content = msg["content"]
                    final_result = {
                        "status": "error" if content.get("is_error") else "success",
                        "duration_ms": content.get("duration_ms"),
                        "cost_usd": content.get("cost_usd"),
                        "num_turns": content.get("num_turns"),
                    }
                    task_id = db.start_task(sid, "续跑汇报")
                    db.finish_task(
                        task_id,
                        final_result["status"],
                        duration_ms=final_result.get("duration_ms"),
                        cost_usd=final_result.get("cost_usd"),
                        num_turns=final_result.get("num_turns"),
                    )
                    db.update_session(sid, status="idle")
                    self._activity[sid] = ""
                    await self._emit_session_update(sid)
                    reply_text = "\n".join(reply_parts)
                    await self._notify_turn_done(
                        sid, "", reply_text, final_result, kind="resume",
                    )
                    reply_parts.clear()
                    resume_running = False

        # 常驻进程的 reader 在普通回合结束后仍继续读，只能直接挂这条跨回合回调。
        sess_rec["out_of_turn_cb"] = _on_out_of_turn

    async def _make_on_progress(self, sid: str):
        """构造给 runner 回合看门狗使用的进度回调。"""
        async def _on_progress(info: dict) -> None:
            self._progress[sid] = info
            await self.broadcast(sid, {"type": "turn_progress", "session_id": sid, **info})
            await self.broadcast_monitor({"type": "turn_progress", "session_id": sid, **info})
            if info.get("stuck") and sid not in self._stuck_notified:
                self._stuck_notified.add(sid)
                await self._notify_turn_done(
                    sid,
                    "",
                    f"该回合已 {int(info.get('elapsed', 0))}s 无新输出",
                    {"status": "stuck"},
                    kind="stuck",
                    elapsed=info.get("elapsed"),
                )
        return _on_progress

    async def _emit_todo_progress(self, tid: str, progress: str, progress_at: float) -> None:
        await self.broadcast_monitor({
            "type": "todo_progress_update",
            "todo_id": tid,
            "progress": progress,
            "progress_at": progress_at,
        })

    async def _auto_progress_by_ai(self, sid: str) -> None:
        """回合结束后自动刷该会话关联的 in_progress 看板进展，并推 monitor。"""
        try:
            from . import kanban
            todos = db.list_todos_by_session(sid)
            for t in todos:
                res = await kanban.refresh_todo_progress(t["id"])
                if res.get("ok") and not res.get("cached") and res.get("progress"):
                    await self._emit_todo_progress(
                        t["id"], res["progress"], res.get("progress_at") or 0
                    )
        except Exception:
            pass

    # ---------- 回合执行 ----------
    async def start_turn(self, sid: str, user_text: str, model: str | None = None) -> bool:
        """发起一个回合。已在跑则拒绝（同会话内串行）。返回是否成功发起。"""
        if self.is_running(sid):
            return False
        # 新回合开始：取消还在去抖等待/生成中的上一轮摘要任务，摘要改由本回合结束后
        # 重新生成（连续追问只在末尾生成一次）。
        self.cancel_summary_task(sid)
        # 自动命名：首条消息时将内容截取为标题
        sess = db.get_session(sid)
        if sess and db.count_messages(sid) == 0:
            cur_title = sess.get("title", "") or ""
            if not cur_title or cur_title.startswith("新会话"):
                # skill 前言（编排指令）很长，直接截会得到乱码标题；剥掉前言只取附加需求。
                # 附加需求由 skill_store.expand() 拼在最后一个 \n\n 之后，故从末尾切；
                # 且前半段自身要够长才算 skill body，否则整条都是 body（无附加需求）。
                seed = user_text
                if len(user_text) >= config.TITLE_SKIP_PREFIX_CHARS:
                    parts = user_text.rsplit("\n\n", 1)
                    if len(parts) == 2 and len(parts[0]) >= config.TITLE_SKIP_PREFIX_CHARS:
                        seed = parts[1].strip()
                    else:
                        seed = ""
                clean = re.sub(r"\s+", " ", seed).strip()
                # 超长首条不再裸截断当标题（此前 29.6% 会话标题以 … 结尾、读不出在干
                # 什么）：改占位符，让前 TITLE_EARLY_TURNS 回合的 AI 标题去填；
                # ≤20 字的短首条仍直接用。
                new_title = clean if clean and len(clean) <= 20 else "新任务"
                if new_title:
                    db.update_session(sid, title=new_title, title_auto=1)
        db.add_message(sid, "user", {"text": user_text})
        db.update_session(sid, status="running")
        task_id = db.start_task(sid, user_text[:80])
        await self.broadcast(sid, {"type": "status", "status": "running", "task_id": task_id})
        self._activity[sid] = "思考中…"
        await self._emit_session_update(sid)
        self._turns[sid] = asyncio.ensure_future(self._run_turn(sid, user_text, task_id, model))
        return True

    async def _start_expanded(self, sid: str, user_text: str, mode=None) -> str:
        """展开 skill 后开始回合。返回 'started' 或 'error:...'"""
        from . import skill_store
        # /compact：真实上下文压缩，不走 skill 展开、不开回合
        if user_text.strip() == "/compact":
            res = await self.compact(sid)
            return "started" if res.get("ok") else f"error:{res.get('error', '压缩失败')}"
        # 始终走 skill 展开：带附件时前端把附件行前置到文本最前，展开后不再以 `/` 开头，
        # 用 startswith("/") 预判会漏掉"附件+/skill"组合。expand() 内部自会剥离附件行、
        # 对非 skill 文本原样返回 (user_text, None)，故无条件调用与旧逻辑等价。
        expanded, err = skill_store.expand(user_text)
        if err:
            await self.broadcast(sid, {"type": "error", "message": err})
            return f"error:{err}"
        user_text = expanded
        if mode in config.CLAUDE_MODELS or mode in config.CODEX_MODELS or mode in ("fast", "strong", "super"):
            db.update_session(sid, mode=mode)
        await self.start_turn(sid, user_text)
        return "started"

    async def submit_user_message(self, sid: str, user_text: str, mode=None) -> str:
        """在跑则入队，空闲则直接开跑。返回 'queued'/'started'/'error:...'"""
        if self.is_running(sid):
            db.enqueue_item(sid, user_text)
            await self.emit_queue_update(sid)
            return "queued"
        return await self._start_expanded(sid, user_text, mode)

    async def compact(self, sid: str) -> dict:
        """真实上下文压缩：对当前会话发一条 `/compact` 消息，触发 CLI 原生压缩
        （同一个 claude_session_id 不变，历史被真实压缩，不再是"外部摘要+丢弃会话重开"）。

        回合进行中直接拒绝（不入队，语义会混乱）。从没跑过真实回合（无 claude_session_id）
        时没有可压缩的历史，直接 noop。

        压缩本身是一次真实回合，耗时与普通回合相当。期间若不占位，is_running 会一直是
        False，用户此刻发消息就会起一个真实回合，与压缩这次回合抢占同一个常驻进程。故仿照
        start_turn 往 self._turns[sid] 注册占位 task，让整个压缩窗口内 is_running(sid)
        保持为真，用户发的消息会被 submit_user_message 入队。"""
        if self.is_running(sid):
            return {"ok": False, "error": "回合进行中，稍后再压缩"}
        sess = db.get_session(sid)
        if not sess:
            return {"ok": False, "error": "会话不存在"}
        if not sess.get("claude_session_id"):
            return {"ok": True, "noop": True, "reason": "empty"}
        task = asyncio.ensure_future(self._run_compact(sid, sess))
        self._turns[sid] = task
        try:
            return await task
        finally:
            self._turns.pop(sid, None)
            # 压缩窗口期间攒进队列的消息（如另一标签页发的）在此接着出队执行，
            # 与 _run_turn 收尾口径一致，避免消息一直躺到无关回合结束才被带出。
            asyncio.ensure_future(self._drain_queue(sid))

    async def _run_compact(self, sid: str, sess: dict) -> dict:
        """发一条 `/compact` 消息触发 CLI 原生压缩，用专属 on_event 抓压缩边界事件与摘要。

        stdout 事件序列（常驻 stream-json 模式实测）：
          system/compact_boundary（compact_metadata 含 pre_tokens/post_tokens）
          -> user 事件（message.content 是字符串，"This session is being continued..." 开头，
             含摘要正文）-> result。历史太短时没有 compact_boundary，result 事件的 result
             字段是 "Not enough messages to compact."（同时会带一条 assistant 文本），
             用这条 result 文本精确识别 noop，不能靠"是否出现过 assistant 文本"判断——
             该信号在历史太短场景下同样为真，会跟真正的 DISABLE_COMPACT 禁用混淆。
             命令被 DISABLE_COMPACT 禁用时 CLI 也会把 /compact 当成普通问题回答
             （出现 assistant 文本、无 boundary、result 文本不含 "Not enough messages"）。
        """
        if sess.get("engine") == "codex":
            return {"ok": False, "error": "该会话使用 codex 引擎，暂不支持原生 /compact"}
        try:
            await self.broadcast(sid, {"type": "compacting"})
            model_name, effort = self._resolve_model_effort(sess)
            captured = {"boundary": None, "summary": "", "saw_assistant_text": False, "result_text": "", "local_err": ""}

            async def on_event(evt: dict):
                etype = evt.get("type")
                if etype == "system" and evt.get("subtype") == "compact_boundary":
                    captured["boundary"] = evt.get("compact_metadata") or {}
                elif etype == "system" and evt.get("subtype") == "local_command":
                    # CLI 本地命令失败（如 /compact 被网关 400 拒绝）不走普通事件流，
                    # 而是以 system/local_command 事件携带 <local-command-stderr> 文本；
                    # 多次输出时累积拼接，末尾统一判定。
                    raw = evt.get("content")
                    if isinstance(raw, list):
                        raw = " ".join(str(b.get("text", "")) for b in raw if isinstance(b, dict))
                    elif not isinstance(raw, str):
                        raw = evt.get("text") or ""
                    if raw:
                        captured["local_err"] += str(raw)
                elif etype == "user":
                    content = evt.get("message", {}).get("content")
                    if isinstance(content, str) and content.startswith("This session is being continued"):
                        captured["summary"] = content.split("Summary:", 1)[-1].strip()
                elif etype == "assistant":
                    for block in evt.get("message", {}).get("content", []):
                        if isinstance(block, dict) and block.get("type") == "text" and block.get("text", "").strip():
                            captured["saw_assistant_text"] = True
                elif etype == "result":
                    captured["result_text"] = evt.get("result") or ""

            def _on_session_id(cid: str):
                db.update_session(sid, claude_session_id=cid)

            r = _runner_for(sess)
            _turn_fn = r.send_turn if config.CLAUDE_PERSISTENT else r.run_turn
            turn_task = asyncio.ensure_future(_turn_fn(
                session_id=sid, message="/compact",
                workdir=(sess or {}).get("workdir") or config.DEFAULT_WORKDIR,
                resume_claude_session=sess.get("claude_session_id"),
                on_event=on_event, model=model_name,
                on_permission=None, on_session_id=_on_session_id, effort=effort,
            ))
            try:
                ret = await asyncio.wait_for(asyncio.shield(turn_task), timeout=config.COMPACT_TIMEOUT)
            except asyncio.TimeoutError:
                # 超时：CLI 子进程可能还在跑压缩。先 cancel（杀进程并唤醒等待中的
                # 回合，让其内部正常复位 turn_active；常驻模式下回合自动 resume 续
                # 上下文），再给收尾宽限；确保 _turns 释放后用户能立刻再发消息。
                try:
                    await r.cancel(sid)
                except Exception as e:  # noqa
                    logger.warning("session %s: compact timeout cleanup failed: %s", sid, e)
                try:
                    await asyncio.wait_for(turn_task, timeout=30)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    turn_task.cancel()
                return {"ok": False, "error": f"压缩超时（>{config.COMPACT_TIMEOUT}s），可能上下文过大导致网关拒绝"}
            if ret.get("error"):
                return {"ok": False, "error": ret["error"]}
            if ret.get("resume_failed"):
                # resume 失效（缓存 sid 与 cwd 归属目录不一致）：自动重启丢弃坏 sid，
                # 与 _run_turn 的自愈口径一致；此时历史仍在，不是"对话为空"。
                await r.forget_session(sid)
                db.update_session(sid, claude_session_id=None, codex_usage_baseline="")
                return {"ok": True, "noop": True, "reason": "resume_failed"}
            if "error during compaction" in captured["local_err"].lower():
                # 展示层剥离 CLI 本地命令错误标签，captured["local_err"] 原始数据保持完整。
                err = captured["local_err"].replace("<local-command-stderr>", "").replace("</local-command-stderr>", "")
                return {"ok": False, "error": f"CLI 压缩失败：{err[:300]}"}
            if captured["boundary"]:
                meta = captured["boundary"]
                content = {
                    "summary": captured["summary"],
                    "pre_tokens": meta.get("pre_tokens"),
                    "post_tokens": meta.get("post_tokens"),
                }
                db.update_session(sid, compacted_at=time.time())
                db.add_message(sid, "compact", content)
                await self.broadcast(sid, {"type": "message", "role": "compact", "content": content})
                return {"ok": True}
            if "not enough messages" in captured["result_text"].lower():
                return {"ok": True, "noop": True, "reason": "not_enough"}
            if captured["saw_assistant_text"]:
                return {"ok": False, "error": "原生 /compact 不可用（可能被 DISABLE_COMPACT 禁用）"}
            return {"ok": True, "noop": True, "reason": "not_enough"}
        except Exception as e:  # noqa
            # 异常兜住走统一错误响应路径，避免一路冒到 main.py 变成裸 500。
            # _turns 的清理在 compact() 的 finally。
            return {"ok": False, "error": f"压缩失败：{e}"}

    async def _drain_queue(self, sid: str) -> None:
        """出队并自动开始下一条（循环处理 skill 报错跳过）"""
        while True:
            item = db.pop_next_queue_item(sid)
            if not item:
                return
            await self.emit_queue_update(sid)  # 先广播移除
            # 广播用户气泡（出队项需让所有端看到）
            await self.broadcast(sid, {
                "type": "message",
                "role": "user",
                "content": {"text": item["text"]},
            })
            res = await self._start_expanded(sid, item["text"], None)
            if not res.startswith("error"):
                return  # 成功开跑，退出循环
            # skill 报错则跳过该项，继续循环取下一条

    def _resolve_model_effort(self, sess: dict | None, model: str | None = None) -> tuple[str, str]:
        """模型档位 → 模型名 + effort（按 engine 分流：codex 用 CODEX_MODELS，claude 走旧档位映射）。"""
        engine = (sess or {}).get("engine")
        sess_mode = (sess or {}).get("mode")
        if engine == "codex":
            # codex 会话的 mode 直接存模型 ID；脏值/空值回退到 CODEX_MODEL 或默认模型。
            cand = model or sess_mode
            model_name = cand if cand in config.CODEX_MODELS else (config.CODEX_MODEL or config.CODEX_DEFAULT_MODEL)
        else:
            sess_mode = sess_mode or config.CLAUDE_DEFAULT_MODE
            # 旧档位值映射到具体模型；新会话的 mode 本身就是模型 ID，直接用。
            _LEGACY_MAP = {
                "fast": config.CLAUDE_MODEL_FAST,
                "strong": config.CLAUDE_MODEL_STRONG,
                "super": config.CLAUDE_MODEL_SUPER,
            }
            model_name = model or _LEGACY_MAP.get(sess_mode, sess_mode)
        # 会话级 effort（推理强度）：空则回落全局默认；不支持的 provider 接收后忽略。
        effort = (sess or {}).get("effort") or config.CLAUDE_EFFORT
        return model_name, effort

    async def _run_turn(self, sid: str, user_text: str, task_id: str, model: str | None) -> None:
        final_result: dict = {"status": "success"}
        reply_parts: list[str] = []
        self._turn_started[sid] = time.monotonic()
        self._stuck_notified.discard(sid)
        self._bind_out_of_turn(sid)
        ctx_warned = False  # 本回合是否已推过上下文用量预警（每回合只报一次，避免刷屏）

        async def on_event(evt: dict):
            nonlocal ctx_warned
            self._bind_out_of_turn(sid)
            # 上下文用量预警：满上下文时 input_tokens 极小，cache_creation/cache_read
            # 才是大头，三项相加才是真实占用。按模型上下文上限的比例判阈值，超限推一条
            # status 提示（不落库、不进摘要，符合 translate_event 对 status 的既有约定）；
            # 同一回合只报一次。
            if evt.get("type") == "assistant":
                usage = (evt.get("message") or {}).get("usage") or {}
                total = sum(
                    int(usage.get(k) or 0)
                    for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
                )
                limit = 1_000_000 if "[1m]" in str((sess or {}).get("mode") or "") else 200_000
                if total > limit * config.CONTEXT_WARN_RATIO and not ctx_warned:
                    ctx_warned = True
                    pct = min(round(total * 100 / limit), 999)
                    try:
                        await self.broadcast(sid, {
                            "type": "message", "role": "status",
                            "content": {"text": f"上下文已用 {pct}%（约 {total:,} tokens），建议压缩上下文或另起会话"},
                        })
                    except Exception:
                        pass
            for msg in translate_event(evt):
                # assistant_delta（打字机增量）、status（瞬时状态提示）不落库：前者靠后续权威
                # assistant 全文入库，后者纯前端提示、不该进聊天记录/摘要/通知。
                # subagent_progress/subagent_done 纯实时刷新用，同样不进聊天记录。
                if msg["role"] not in ("assistant_delta", "status", "subagent_progress", "subagent_done"):
                    db.add_message(sid, msg["role"], msg["content"])
                if msg["role"] == "assistant":
                    t = (msg["content"] or {}).get("text", "")
                    if t:
                        reply_parts.append(t)
                await self.broadcast(sid, {"type": "message", **msg})
                # 更新「当前活动」→ 推监控
                label = _activity_label(msg)
                if label:
                    self._activity[sid] = label
                    await self._emit_session_update(sid)
                if msg["role"] == "result":
                    c = msg["content"]
                    final_result.update({
                        "status": "error" if c.get("is_error") else "success",
                        "duration_ms": c.get("duration_ms"),
                        "cost_usd": c.get("cost_usd"),
                        "num_turns": c.get("num_turns"),
                    })

        async def on_permission(request_id: str, tool_name: str, tool_input: dict):
            """CLI 请求授权某工具：记入 pending 并广播弹窗给所有订阅者。"""
            info = {"request_id": request_id, "tool_name": tool_name, "input": tool_input}
            self._pending_perms.setdefault(sid, {})[request_id] = info
            await self.broadcast(sid, {
                "type": "permission_request",
                "session_id": sid,
                "request_id": request_id,
                "tool_name": tool_name,
                "input": tool_input,
            })

        def _on_session_id(cid: str):
            """claude_sid 首次落定时即刻落库：即便本回合中途崩溃、走不到末尾兜底，
            下次也能凭它 resume 续接，避免每次都从空会话重开。"""
            db.update_session(sid, claude_session_id=cid)
            self._bind_out_of_turn(sid)

        try:
            sess = db.get_session(sid)
            model_name, effort = self._resolve_model_effort(sess, model)
            db.update_task(task_id, resolved_model=model_name)
            turn_extra = {"effort": effort}
            if config.PROGRESS_PUSH_ENABLED:
                turn_extra["on_progress"] = await self._make_on_progress(sid)
            r = _runner_for(sess)
            send_message = user_text
            # provider 统一按配置走 send_turn / run_turn；无常驻实现时基类会转发到 run_turn。
            _turn_fn = r.send_turn if config.CLAUDE_PERSISTENT else r.run_turn
            ret = await _turn_fn(
                session_id=sid,
                message=send_message,
                workdir=(sess or {}).get("workdir") or config.DEFAULT_WORKDIR,
                resume_claude_session=(sess or {}).get("claude_session_id"),
                on_event=on_event,
                model=model_name,
                on_permission=on_permission if config.CLAUDE_PERMISSION_PROMPT else None,
                on_session_id=_on_session_id,
                **turn_extra,
            )
            self._bind_out_of_turn(sid)
            # resume 失败：缓存的 claude_sid 与当前 cwd 归属目录不一致，tclaude 立即报
            # "No conversation found"。此时自动重启（丢弃坏 sid，全新 spawn），并把近期
            # 对话摘录拼到消息前面注入，让 agent 续接上下文。本回合最多重试一次，避免递归。
            if ret.get("resume_failed"):
                await self.broadcast(sid, {
                    "type": "message", "role": "error",
                    "content": {"message": "Agent 上下文已失效，正在自动重启并续接近期对话…"},
                })
                await r.forget_session(sid)
                db.update_session(sid, claude_session_id=None, codex_usage_baseline="")
                retry_message = self._build_resume_recovery_prompt(sid, user_text)
                ret = await _turn_fn(
                    session_id=sid,
                    message=retry_message,
                    workdir=(sess or {}).get("workdir") or config.DEFAULT_WORKDIR,
                    resume_claude_session=None,
                    on_event=on_event,
                    model=model_name,
                    on_permission=on_permission if config.CLAUDE_PERMISSION_PROMPT else None,
                    on_session_id=_on_session_id,
                    **turn_extra,
                )
                self._bind_out_of_turn(sid)
            if ret.get("claude_session_id"):
                db.update_session(sid, claude_session_id=ret["claude_session_id"])
            if ret.get("error"):
                final_result["status"] = "error"
                db.add_message(sid, "error", {"message": ret["error"]})
                await self.broadcast(sid, {"type": "message", "role": "error", "content": {"message": ret["error"]}})
            if ret.get("cancelled"):
                final_result["status"] = "cancelled"
        except Exception as e:  # noqa
            final_result["status"] = "error"
            try:
                db.add_message(sid, "error", {"message": f"服务端异常：{e}"})
                await self.broadcast(sid, {"type": "message", "role": "error", "content": {"message": f"服务端异常：{e}"}})
            except Exception:
                pass
        finally:
            db.finish_task(
                task_id,
                final_result.get("status", "error"),
                duration_ms=final_result.get("duration_ms"),
                cost_usd=final_result.get("cost_usd"),
                num_turns=final_result.get("num_turns"),
            )
            db.update_session(sid, status="idle")
            self._activity[sid] = ""
            self._pending_perms.pop(sid, None)  # 回合结束：清掉本会话所有待确认权限
            self._turns.pop(sid, None)
            # 必须在 pop self._turn_started 之前算好 elapsed 再传给 _notify_turn_done——
            # pop 之后就读不到起始时间了，_notify_turn_done 本身也不读这个字典。
            _started_at = self._turn_started.get(sid)
            turn_elapsed = (time.monotonic() - _started_at) if _started_at is not None else None
            self._turn_started.pop(sid, None)
            self._progress.pop(sid, None)
            self._stuck_notified.discard(sid)
            await self.broadcast(sid, {"type": "status", "status": "idle", "result": final_result})

            # 行摘要 + 标题合并生成（SUMMARY_ENABLED 且命中节流点才发）：early 前每回合、
            # 之后每 every 回合一次；先等去抖窗口，期间同会话又起新回合则本任务被取消、
            # 由新回合接手（连续追问只在末尾生成一次）。
            reply_text = "\n".join(reply_parts)
            n_user = db.count_user_messages(sid)
            due = n_user <= config.TITLE_EARLY_TURNS or (
                n_user - config.TITLE_EARLY_TURNS) % config.TITLE_EVERY_N == 0
            if config.SUMMARY_ENABLED and due:
                old = self._summary_tasks.pop(sid, None)
                if old and not old.done():
                    old.cancel()
                self._summary_tasks[sid] = asyncio.ensure_future(
                    self._summarize_and_emit(sid, user_text, reply_text))
            asyncio.ensure_future(self._auto_progress_by_ai(sid))  # 看板进展不动，自带 mtime 缓存
            await self._emit_session_update(sid)
            await self._notify_turn_done(sid, user_text, reply_text, final_result, kind="turn", elapsed=turn_elapsed)

            # 本回合结束后自动出队执行下一条（_turns.pop 已在上方执行，is_running 为假）
            asyncio.ensure_future(self._drain_queue(sid))

    def cancel_summary_task(self, sid: str) -> None:
        """取消该会话在途的行摘要/标题生成任务（含去抖等待中的）。新回合开始与删除会话时
        调用：前者让摘要改由新回合接手，后者防止对已删 sid 写孤儿数据。"""
        task = self._summary_tasks.pop(sid, None)
        if task and not task.done():
            task.cancel()

    async def _summarize_and_emit(self, sid: str, user_text: str, reply_text: str) -> None:
        """合并生成行摘要 + 标题写库 + 推监控。开头先等去抖窗口：期间同会话又起新回合
        则本任务被取消（由新回合的任务接手）。失败静默（summarizer 自带启发式兜底）。"""
        try:
            try:
                await asyncio.sleep(config.SUMMARY_DEBOUNCE_SEC)
            except asyncio.CancelledError:
                return
            from . import summarizer
            # 发起前先查一次：会话已删，或用户已手动改名（title_auto=0 标题锁定）时不带
            # 原标题进 prompt（手动标题也会在写回时被 title_auto 复查拦掉）。
            sess = db.get_session(sid)
            if not sess:
                return
            current_title = (sess.get("title") or "") if sess.get("title_auto", 1) else ""
            convo = self._build_title_convo(sid)
            title, summary = await summarizer.summarize_and_title(
                convo, current_title, user_text, reply_text)
            if not title and not summary:
                return
            # await 期间会话可能已被删：别再往已删 sid 写数据/推幽灵 session_update。
            if db.get_session(sid) is None:
                return
            fields: dict = {}
            if summary:
                fields["summary"] = summary
            if title:
                # await 期间用户可能手动改名：复查 title_auto，防止覆盖手动标题
                # （双重检查，发起前查过一次、写回前再查一次）。
                sess = db.get_session(sid)
                if sess and sess.get("title_auto", 1) != 0 and title != (sess.get("title") or ""):
                    fields["title"] = title
                    fields["title_auto"] = 1
            if fields:
                db.update_session(sid, **fields)
                await self._emit_session_update(sid)
        except asyncio.CancelledError:
            return
        except Exception:
            pass

    def _build_resume_recovery_prompt(self, sid: str, user_text: str) -> str:
        """resume 失败自动重启后，把近期对话摘录拼到原始消息前面，帮 agent 续接上下文。

        取最后若干条 user/assistant 消息（不含本回合已入库的当前 user 气泡），
        拼成约 4000 字以内的摘要作前缀。"""
        msgs = [m for m in db.list_messages(sid) if m["role"] in ("user", "assistant")]
        # 本回合的用户消息在 start_turn 已入库，是列表末尾那条 user，摘要里剔除避免重复
        if msgs and msgs[-1]["role"] == "user":
            msgs = msgs[:-1]
        recent = msgs[-10:]
        parts = []
        for m in recent:
            who = "用户" if m["role"] == "user" else "助手"
            txt = str((m.get("content") or {}).get("text", "")).strip()
            if txt:
                parts.append(f"{who}：{txt}")
        excerpt = "\n".join(parts)[:4000]
        if not excerpt:
            return user_text
        return "以下是之前对话的近期摘录，请据此继续：\n\n" + excerpt + "\n\n" + user_text

    def _build_title_convo(self, sid: str) -> str:
        """拼一段用于起标题的对话摘录：首条用户消息 + 最近若干轮，去重限长。

        始终锚定首条摘要（含 skill 前言剥离）+ 最近 6 轮；是否需要换标题交由模型判断，
        故这里不再区分长短会话，也不拼入当前标题（当前标题通过独立参数传给 summarize_and_title）。"""
        msgs = db.list_messages(sid)
        users = [m for m in msgs if m["role"] == "user"]
        parts = []
        seed = ""
        raw_first = ""
        if users:
            raw_first = str((users[0].get("content") or {}).get("text", ""))
            # skill 前言（编排指令）很长且固定，会污染标题；超阈值则剥掉前言只取附加需求。
            # 附加需求拼在最后一个 \n\n 之后，故从末尾切；前半段够长才算 skill body。
            if len(raw_first) >= config.TITLE_SKIP_PREFIX_CHARS:
                parts_split = raw_first.rsplit("\n\n", 1)
                if len(parts_split) == 2 and len(parts_split[0]) >= config.TITLE_SKIP_PREFIX_CHARS:
                    seed = parts_split[1].strip()[:120]
                # 纯 skill 无附加需求则 seed 为空，跳过首条，交给后续 recent 覆盖
            else:
                seed = raw_first[:120]
            if seed.strip():
                parts.append("用户：" + seed)
        recent = [m for m in msgs if m["role"] in ("user", "assistant")][-6:]
        seen = set()
        if users:
            if seed:
                seen.add(seed)
            seen.add(raw_first[:120])  # 始终阻止原始首条经 recent 重新混入
        for m in recent:
            who = "用户" if m["role"] == "user" else "助手"
            txt = str((m.get("content") or {}).get("text", ""))[:120 if who == "用户" else 200]
            if txt.strip() and txt not in seen:
                seen.add(txt)
                parts.append(f"{who}：{txt}")
        return re.sub(r"\s+", " ", " ".join(parts)).strip()

    async def respond_permission(self, sid: str, request_id: str, behavior: str,
                                 updated_input: dict = None) -> None:
        """用户点了允许/拒绝：清掉这条 pending，回 control_response 给 CLI。

        updated_input 透传给 runner，供 AskUserQuestion 回填用户选择。"""
        pend = self._pending_perms.get(sid)
        if pend:
            pend.pop(request_id, None)
            if not pend:
                self._pending_perms.pop(sid, None)
        await _runner_for(db.get_session(sid)).respond_permission(sid, request_id, behavior, updated_input)

    async def resend_pending_perms(self, sid: str, sub: "Subscriber") -> None:
        """WS 连接建立时，把该会话未决的权限请求重发给这条订阅者（补断线期间漏掉的弹窗）。"""
        for info in list(self._pending_perms.get(sid, {}).values()):
            try:
                await sub.send({
                    "type": "permission_request",
                    "session_id": sid,
                    "request_id": info["request_id"],
                    "tool_name": info.get("tool_name"),
                    "input": info.get("input"),
                })
            except Exception:
                pass

    async def cancel(self, sid: str) -> None:
        # 取消回合前，对该会话所有待确认权限自动回 deny，避免 CLI 卡在等授权
        r = _runner_for(db.get_session(sid))
        pend = self._pending_perms.pop(sid, None)
        if pend:
            for request_id in list(pend.keys()):
                await r.respond_permission(sid, request_id, "deny")
        await r.cancel(sid)


hub = SessionHub()
