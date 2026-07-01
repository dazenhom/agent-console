"""封装 Knot HTTPS API（AG-UI 协议）作为底层 Agent。

通过 POST `{KNOT_API_BASE}/apigw/api/v1/agents/agui/{KNOT_AGENT_ID}` 启动一次
对话，服务端以 SSE（`data: <json>\n\n`，结尾 `data: [DONE]`）返回 AG-UI 事件流。

事件 → 上层消息 翻译（保持与原 claude_runner 输出兼容）：
  TextMessageStart/Content/End          → assistant text（聚合后发一条）
  ThinkingTextMessageStart/Content/End  → assistant thinking 段
  ToolCallStart/Args/End                → tool_use
  ToolCallResult                        → tool_result
  RunError                              → error
  StepFinished(call_llm, token_usage)   → 累计到本回合 usage
  RunFinished / [DONE]                  → 回合结束，发 result

Knot 的 `conversation_id` 等同于原先 `claude_session_id`，对外字段名保持不变。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, Callable

import httpx

from . import config

EventCallback = Callable[[dict], Awaitable[None]]


def _strip_sse_prefix(line: str) -> str:
    """SSE 行: 'data: {...}' / 'data:{...}'。心跳冒号行返回空串。"""
    s = line.strip()
    if not s or s.startswith(":"):
        return ""
    while s.startswith("data:"):
        s = s[5:].lstrip()
    return s


class KnotRunner:
    def __init__(self) -> None:
        # session_id -> {"cancel_event": asyncio.Event, "cancelled": bool}
        self._sessions: dict[str, dict[str, Any]] = {}

    def is_running(self, session_id: str) -> bool:
        return session_id in self._sessions

    def was_cancelled(self, session_id: str) -> bool:
        rec = self._sessions.get(session_id)
        return bool(rec and rec.get("cancelled"))

    async def cancel(self, session_id: str) -> None:
        rec = self._sessions.get(session_id)
        if not rec:
            return
        rec["cancelled"] = True
        ev: asyncio.Event = rec["cancel_event"]
        ev.set()

    async def run_turn(
        self,
        session_id: str,
        message: str,
        workdir: str,
        resume_claude_session: str | None,
        on_event: EventCallback,
        model: str | None = None,
    ) -> dict:
        if not config.KNOT_TOKEN:
            return {
                "claude_session_id": resume_claude_session,
                "returncode": -1,
                "error": "未配置 KNOT_TOKEN（在 https://knot.woa.com/settings/token 申请）",
                "cancelled": False,
            }
        if not config.KNOT_AGENT_ID:
            return {
                "claude_session_id": resume_claude_session,
                "returncode": -1,
                "error": "未配置 KNOT_AGENT_ID",
                "cancelled": False,
            }

        url = f"{config.KNOT_API_BASE.rstrip('/')}/apigw/api/v1/agents/agui/{config.KNOT_AGENT_ID}"
        headers = {
            "x-knot-api-token": config.KNOT_TOKEN,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if config.KNOT_USER:
            headers["x-knot-api-user"] = config.KNOT_USER

        chat_extra: dict[str, Any] = {}
        if workdir:
            chat_extra["workspace"] = [workdir]

        body = {
            "input": {
                "message": message,
                "conversation_id": resume_claude_session or "",
                "model": model or "",
                "stream": True,
                "enable_web_search": False,
                "chat_extra": chat_extra,
            }
        }

        cancel_event = asyncio.Event()
        self._sessions[session_id] = {"cancel_event": cancel_event, "cancelled": False}

        conv_id: str | None = resume_claude_session
        text_buffer: list[str] = []
        thinking_buffer: list[str] = []
        tool_calls: dict[str, dict[str, Any]] = {}
        usage_total: dict[str, Any] = {}
        error_msg: str = ""

        async def _flush_text():
            nonlocal text_buffer
            if text_buffer:
                joined = "".join(text_buffer)
                if joined.strip():
                    await on_event({
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": joined}]},
                    })
                text_buffer = []

        async def _flush_thinking():
            nonlocal thinking_buffer
            if thinking_buffer:
                joined = "".join(thinking_buffer)
                if joined.strip():
                    await on_event({
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": f"💭 {joined}"}]},
                    })
                thinking_buffer = []

        timeout = httpx.Timeout(connect=15.0, read=config.KNOT_READ_TIMEOUT, write=15.0, pool=15.0)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                async with client.stream("POST", url, headers=headers, json=body) as resp:
                    if resp.status_code != 200:
                        try:
                            err_bytes = await resp.aread()
                            err_text = err_bytes.decode("utf-8", errors="replace")
                        except Exception:
                            err_text = ""
                        return {
                            "claude_session_id": conv_id,
                            "returncode": resp.status_code,
                            "error": f"Knot API HTTP {resp.status_code}: {err_text[:500]}",
                            "cancelled": False,
                        }

                    async for raw_line in resp.aiter_lines():
                        if cancel_event.is_set():
                            error_msg = "已取消"
                            break

                        payload = _strip_sse_prefix(raw_line or "")
                        if not payload:
                            continue
                        if payload == "[DONE]":
                            break

                        try:
                            evt = json.loads(payload)
                        except json.JSONDecodeError:
                            continue

                        etype = (evt.get("type") or "").upper()
                        raw_evt = evt.get("rawEvent") or {}

                        new_conv = raw_evt.get("conversation_id") or evt.get("conversation_id")
                        if new_conv:
                            conv_id = new_conv

                        if etype == "TEXT_MESSAGE_START":
                            await _flush_thinking()
                            text_buffer = []
                        elif etype == "TEXT_MESSAGE_CONTENT":
                            text_buffer.append(raw_evt.get("content", ""))
                        elif etype == "TEXT_MESSAGE_END":
                            await _flush_text()

                        elif etype == "THINKING_TEXT_MESSAGE_START":
                            await _flush_text()
                            thinking_buffer = []
                        elif etype == "THINKING_TEXT_MESSAGE_CONTENT":
                            thinking_buffer.append(raw_evt.get("content", ""))
                        elif etype == "THINKING_TEXT_MESSAGE_END":
                            await _flush_thinking()

                        elif etype == "TOOL_CALL_START":
                            await _flush_text()
                            await _flush_thinking()
                            tcid = (
                                raw_evt.get("tool_call_id")
                                or evt.get("toolCallId")
                                or raw_evt.get("id")
                            )
                            name = (
                                raw_evt.get("tool_call_name")
                                or evt.get("toolCallName")
                                or raw_evt.get("name")
                                or "tool"
                            )
                            if tcid:
                                tool_calls[tcid] = {"name": name, "args": []}
                        elif etype == "TOOL_CALL_ARGS":
                            tcid = raw_evt.get("tool_call_id") or evt.get("toolCallId")
                            delta = (
                                raw_evt.get("delta")
                                or evt.get("delta")
                                or raw_evt.get("args")
                                or ""
                            )
                            if tcid and tcid in tool_calls:
                                tool_calls[tcid]["args"].append(
                                    delta if isinstance(delta, str)
                                    else json.dumps(delta, ensure_ascii=False)
                                )
                        elif etype == "TOOL_CALL_END":
                            tcid = raw_evt.get("tool_call_id") or evt.get("toolCallId")
                            if tcid and tcid in tool_calls:
                                tc = tool_calls[tcid]
                                args_str = "".join(tc["args"])
                                try:
                                    parsed = json.loads(args_str) if args_str else {}
                                except json.JSONDecodeError:
                                    parsed = {"_raw": args_str}
                                await on_event({
                                    "type": "assistant",
                                    "message": {"content": [{
                                        "type": "tool_use",
                                        "id": tcid,
                                        "name": tc["name"],
                                        "input": parsed,
                                    }]},
                                })
                        elif etype == "TOOL_CALL_RESULT":
                            tcid = (
                                raw_evt.get("tool_call_id")
                                or evt.get("toolCallId")
                                or raw_evt.get("toolCallId")
                            )
                            result_content = (
                                raw_evt.get("content")
                                or raw_evt.get("result")
                                or evt.get("content")
                                or ""
                            )
                            is_error = bool(raw_evt.get("is_error") or raw_evt.get("isError"))
                            if isinstance(result_content, (dict, list)):
                                result_content = json.dumps(result_content, ensure_ascii=False)
                            await on_event({
                                "type": "user",
                                "message": {"content": [{
                                    "type": "tool_result",
                                    "tool_use_id": tcid,
                                    "content": result_content,
                                    "is_error": is_error,
                                }]},
                            })

                        elif etype == "STEP_FINISHED":
                            tu = raw_evt.get("token_usage") or {}
                            if tu:
                                for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                                    if tu.get(k) is not None:
                                        usage_total[k] = usage_total.get(k, 0) + tu[k]

                        elif etype == "RUN_ERROR":
                            tip = raw_evt.get("tip_option") or {}
                            error_msg = (
                                tip.get("content")
                                or raw_evt.get("error")
                                or "Knot 运行错误"
                            )
                            break

                    await _flush_text()
                    await _flush_thinking()

        except httpx.HTTPError as e:
            error_msg = error_msg or f"网络/HTTP 错误：{e}"
        except Exception as e:  # noqa
            error_msg = error_msg or f"Knot runner 异常：{e}"
        finally:
            try:
                await on_event({
                    "type": "result",
                    "subtype": "success" if not error_msg else "error",
                    "duration_ms": None,
                    "total_cost_usd": None,
                    "num_turns": 1,
                    "result": None,
                    "is_error": bool(error_msg),
                    "usage": usage_total or None,
                })
            except Exception:
                pass

        cancelled = bool(self._sessions.get(session_id, {}).get("cancelled"))
        self._sessions.pop(session_id, None)

        if cancelled and not error_msg:
            error_msg = "已取消"

        return {
            "claude_session_id": conv_id,
            "returncode": 0 if not error_msg else -1,
            "error": error_msg,
            "cancelled": cancelled,
        }


runner = KnotRunner()
