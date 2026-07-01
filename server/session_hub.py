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
        # --include-partial-messages 的实时增量，只取文本增量做打字机
        ev = evt.get("event", {})
        if ev.get("type") == "content_block_delta":
            delta = ev.get("delta", {})
            if delta.get("type") == "text_delta" and delta.get("text"):
                out.append({"role": "assistant_delta", "content": {"text": delta["text"]}})

    elif etype == "assistant":
        for block in evt.get("message", {}).get("content", []):
            btype = block.get("type")
            if btype == "text" and block.get("text", "").strip():
                out.append({"role": "assistant", "content": {"text": block["text"]}})
            elif btype == "tool_use":
                out.append({
                    "role": "tool_use",
                    "content": {"id": block.get("id"), "name": block.get("name"), "input": block.get("input", {})},
                })

    elif etype == "user":
        content = evt.get("message", {}).get("content", [])
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_result":
                    raw = block.get("content")
                    if isinstance(raw, list):
                        raw = "\n".join(b.get("text", "") for b in raw if isinstance(b, dict))
                    out.append({
                        "role": "tool_result",
                        "content": {
                            "tool_use_id": block.get("tool_use_id"),
                            "output": raw,
                            "is_error": block.get("is_error", False),
                        },
                    })

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
                clean = re.sub(r"\s+", " ", user_text).strip()
                new_title = clean[:24] + ("…" if len(clean) > 24 else "")
                if new_title:
                    db.update_session(sid, title=new_title)
        db.add_message(sid, "user", {"text": user_text})
        db.update_session(sid, status="running")
        task_id = db.start_task(sid, user_text[:80])
        await self.broadcast(sid, {"type": "status", "status": "running", "task_id": task_id})
        self._activity[sid] = "思考中…"
        await self._emit_session_update(sid)
        self._turns[sid] = asyncio.ensure_future(self._run_turn(sid, user_text, task_id, model))
        return True

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
            self._turns.pop(sid, None)
            await self.broadcast(sid, {"type": "status", "status": "idle", "result": final_result})

            # 行摘要（Haiku + 兜底）：后台跑，不阻塞。写 sessions.summary 后再推一次监控。
            reply_text = "\n".join(reply_parts)
            asyncio.ensure_future(self._summarize_and_emit(sid, user_text, reply_text))
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

    async def cancel(self, sid: str) -> None:
        await runner.cancel(sid)


hub = SessionHub()
