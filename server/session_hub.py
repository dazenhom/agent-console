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

from . import config, db, wecom_notify
from .claude_runner import runner


# ---------------- stream-json 事件翻译（从 main.py 搬入） ----------------
def translate_event(evt: dict) -> list[dict]:
    """把 Claude stream-json 事件翻译成前端消息（可能 0~N 条）。"""
    out: list[dict] = []
    etype = evt.get("type")

    if etype == "system":
        pass  # init 含全套工具列表，前端不展示，丢弃避免 messages 膨胀

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
                out.append({"role": "tool_use", "content": c})

    elif etype == "user":
        pid = evt.get("parent_tool_use_id")
        content = evt.get("message", {}).get("content", [])
        if isinstance(content, list):
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
        # session_id -> {request_id: {request_id, tool_name, input}} 待用户确认的权限请求。
        # 用户回应/回合取消后清空；WS 重连时重发，避免弹窗因断线丢失。
        self._pending_perms: dict[str, dict] = {}

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
        payload.update(extra)
        await self.broadcast_monitor(payload)

    def _has_foreground_sub(self, sid: str) -> bool:
        """该会话是否有「处于前台」的订阅者。无 → 回合结束发企业微信。"""
        return any(not s.hidden for s in self._subs.get(sid, ()))

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
        # 自动命名：首条消息时将内容截取为标题
        sess = db.get_session(sid)
        if sess and db.count_messages(sid) == 0:
            cur_title = sess.get("title", "") or ""
            if not cur_title or cur_title.startswith("新会话"):
                # skill 前言（编排指令）很长，直接截会得到乱码标题；剥掉前言只取附加需求。
                seed = user_text
                if len(user_text) >= config.TITLE_SKIP_PREFIX_CHARS:
                    rest = user_text.split("\n\n", 1)[1] if "\n\n" in user_text else ""
                    seed = rest.strip() if rest.strip() else ""
                clean = re.sub(r"\s+", " ", seed).strip()
                new_title = (clean[:24] + ("…" if len(clean) > 24 else "")) if clean else "新任务"
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
        if user_text.startswith("/"):
            expanded, err = skill_store.expand(user_text)
            if err:
                await self.broadcast(sid, {"type": "error", "message": err})
                return f"error:{err}"
            user_text = expanded
        if mode in ("fast", "strong", "super"):
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

    async def _run_turn(self, sid: str, user_text: str, task_id: str, model: str | None) -> None:
        final_result: dict = {"status": "success"}
        reply_parts: list[str] = []

        async def on_event(evt: dict):
            for msg in translate_event(evt):
                if msg["role"] != "assistant_delta":
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

        try:
            sess = db.get_session(sid)
            # 模型档位 → 模型名
            sess_mode = (sess or {}).get("mode") or config.CLAUDE_DEFAULT_MODE
            if sess_mode == "super":
                model_name = model or config.CLAUDE_MODEL_SUPER
            elif sess_mode == "strong":
                model_name = model or config.CLAUDE_MODEL_STRONG
            else:
                model_name = model or config.CLAUDE_MODEL_FAST
            _turn_fn = runner.send_turn if config.CLAUDE_PERSISTENT else runner.run_turn
            ret = await _turn_fn(
                session_id=sid,
                message=user_text,
                workdir=(sess or {}).get("workdir") or config.DEFAULT_WORKDIR,
                resume_claude_session=(sess or {}).get("claude_session_id"),
                on_event=on_event,
                model=model_name,
                on_permission=on_permission if config.CLAUDE_PERMISSION_PROMPT else None,
                on_session_id=_on_session_id,
            )
            # resume 失败：缓存的 claude_sid 与当前 cwd 归属目录不一致，tclaude 立即报
            # "No conversation found"。此时自动重启（丢弃坏 sid，全新 spawn），并把近期
            # 对话摘录拼到消息前面注入，让 agent 续接上下文。本回合最多重试一次，避免递归。
            if ret.get("resume_failed"):
                await self.broadcast(sid, {
                    "type": "message", "role": "error",
                    "content": {"message": "Agent 上下文已失效，正在自动重启并续接近期对话…"},
                })
                await runner.forget_session(sid)
                db.update_session(sid, claude_session_id=None)
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
                )
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
            await self.broadcast(sid, {"type": "status", "status": "idle", "result": final_result})

            # 行摘要（Haiku + 兜底）：后台跑，不阻塞。写 sessions.summary 后再推一次监控。
            reply_text = "\n".join(reply_parts)
            asyncio.ensure_future(self._summarize_and_emit(sid, user_text, reply_text))
            # 标题异步刷新：前 TITLE_EARLY_TURNS 回合每回合刷，之后每 TITLE_EVERY_N 回合刷一次。
            n_user = db.count_user_messages(sid)
            if config.TITLE_REFRESH_ENABLED and (
                n_user <= config.TITLE_EARLY_TURNS
                or (n_user - config.TITLE_EARLY_TURNS) % config.TITLE_EVERY_N == 0
            ):
                asyncio.ensure_future(self._auto_title_by_ai(sid))
            asyncio.ensure_future(self._auto_progress_by_ai(sid))
            await self._emit_session_update(sid)

            # 企业微信：该会话没有任何前台订阅者就发（含 0 订阅者）。
            if config.WECOM_ENABLED and not self._has_foreground_sub(sid):
                sess2 = db.get_session(sid)
                asyncio.ensure_future(wecom_notify.notify(
                    title=(sess2 or {}).get("title") or "会话",
                    user_text=user_text,
                    reply_text=reply_text,
                    status=final_result.get("status", "success"),
                    duration_ms=final_result.get("duration_ms"),
                    num_turns=final_result.get("num_turns"),
                ))

            # 本回合结束后自动出队执行下一条（_turns.pop 已在上方执行，is_running 为假）
            asyncio.ensure_future(self._drain_queue(sid))

    async def _summarize_and_emit(self, sid: str, user_text: str, reply_text: str) -> None:
        """生成行摘要写库 + 推监控。失败静默（summarizer 自带兜底）。"""
        try:
            from . import summarizer
            summary = await summarizer.summarize(user_text, reply_text)
            if summary:
                db.update_session(sid, summary=summary)
                await self._emit_session_update(sid)
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
        """拼一段用于起标题的对话摘录：首条用户消息 + 最近若干轮，去重限长。"""
        msgs = db.list_messages(sid)
        users = [m for m in msgs if m["role"] == "user"]
        parts = []
        seed = ""
        if users:
            raw_first = str((users[0].get("content") or {}).get("text", ""))
            # skill 前言（编排指令）很长且固定，会污染标题；超阈值则剥掉前言只取附加需求。
            if len(raw_first) >= config.TITLE_SKIP_PREFIX_CHARS:
                rest = raw_first.split("\n\n", 1)[1] if "\n\n" in raw_first else ""
                if rest.strip():
                    seed = rest.strip()[:120]
                # 纯 skill 无附加需求则跳过首条，交给后续 recent 覆盖
            else:
                seed = raw_first[:120]
            if seed.strip():
                parts.append("用户：" + seed)
        recent = [m for m in msgs if m["role"] in ("user", "assistant")][-6:]
        seen = {seed} if seed else set()
        for m in recent:
            who = "用户" if m["role"] == "user" else "助手"
            txt = str((m.get("content") or {}).get("text", ""))[:120 if who == "用户" else 200]
            if txt.strip() and txt not in seen:
                seen.add(txt)
                parts.append(f"{who}：{txt}")
        return re.sub(r"\s+", " ", " ".join(parts)).strip()

    async def _auto_title_by_ai(self, sid: str) -> None:
        """异步 AI 语义标题：用整段对话摘录重起标题并推监控。用户手动改名或失败则静默保留原标题。"""
        try:
            from . import summarizer
            sess = db.get_session(sid)
            if not sess or sess.get("title_auto", 1) == 0:
                return
            convo = self._build_title_convo(sid)
            if not convo:
                return
            title = await summarizer.gen_title(convo)
            if not title:
                return
            sess = db.get_session(sid)
            if not sess or sess.get("title_auto", 1) == 0:
                return
            if title == (sess.get("title") or ""):
                return
            db.update_session(sid, title=title, title_auto=1)
            await self._emit_session_update(sid)
        except Exception:
            pass

    async def respond_permission(self, sid: str, request_id: str, behavior: str,
                                 updated_input: dict = None) -> None:
        """用户点了允许/拒绝：清掉这条 pending，回 control_response 给 CLI。

        updated_input 透传给 runner，供 AskUserQuestion 回填用户选择。"""
        pend = self._pending_perms.get(sid)
        if pend:
            pend.pop(request_id, None)
            if not pend:
                self._pending_perms.pop(sid, None)
        await runner.respond_permission(sid, request_id, behavior, updated_input)

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
        pend = self._pending_perms.pop(sid, None)
        if pend:
            for request_id in list(pend.keys()):
                await runner.respond_permission(sid, request_id, "deny")
        await runner.cancel(sid)


hub = SessionHub()
