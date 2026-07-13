"""封装 Codex CLI（tcodex wrapper）的无状态 exec 模式。

通过 `tcodex -- exec [resume <conv_id>] --json [--skip-git-repo-check]
[--dangerously-bypass-approvals-and-sandbox | -s <sandbox>] [-m <model>] <prompt>`
启动子进程，逐行解析 stdout 上的 JSONL 事件，翻译成 Claude 归一化格式后回调上层
（与 claude_runner / knot_runner 输出兼容）。

Codex 每回合一个新进程（无常驻），thread_id（conversation id）等同于原先
`claude_session_id`，对外字段名保持不变；下一回合用 `exec resume <thread_id>` 续接。

JSONL 事件 → 上层消息 翻译：
  thread.started                          → 记 conv_id=thread_id，回调落库
  item.completed (agent_message)          → assistant text
  item.started   (command_execution)      → tool_use（name=shell）
  item.completed (command_execution)      → tool_result
  error / turn.failed                     → error
  turn.completed                          → 回合结束，发 result
"""
import asyncio
import json
import logging
import signal
from typing import Awaitable, Callable

from . import config
from .claude_runner import _child_env, _kill_process_group, _STREAM_LIMIT

logger = logging.getLogger(__name__)

EventCallback = Callable[[dict], Awaitable[None]]


class CodexRunner:
    def __init__(self):
        # session_id -> {"proc": Process, "cancelled": bool}（每回合一个进程）
        self._procs: dict[str, dict] = {}

    def _build_cmd(self, *, message: str, resume: str | None) -> list[str]:
        """组 tcodex exec 命令。prompt 作为最后一个位置参数。"""
        cmd = [config.CODEX_BIN, "--", "exec"]
        if resume:
            cmd += ["resume", resume]
        cmd += ["--json"]
        if config.CODEX_SKIP_GIT_CHECK:
            cmd += ["--skip-git-repo-check"]
        if config.CODEX_BYPASS:
            cmd += ["--dangerously-bypass-approvals-and-sandbox"]
        else:
            cmd += ["-s", config.CODEX_SANDBOX]
        if config.CODEX_MODEL:
            cmd += ["-m", config.CODEX_MODEL]
        cmd += [message]
        return cmd

    def _get_proc(self, session_id: str) -> asyncio.subprocess.Process | None:
        rec = self._procs.get(session_id)
        return rec["proc"] if rec else None

    def is_running(self, session_id: str) -> bool:
        proc = self._get_proc(session_id)
        return proc is not None and proc.returncode is None

    def was_cancelled(self, session_id: str) -> bool:
        rec = self._procs.get(session_id)
        return bool(rec and rec.get("cancelled"))

    async def cancel(self, session_id: str) -> None:
        """中断当前回合：杀整个进程组（下回合自动 resume 续上）。"""
        rec = self._procs.get(session_id)
        if not rec:
            return
        proc = rec["proc"]
        if proc.returncode is not None:
            return
        rec["cancelled"] = True
        _kill_process_group(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except asyncio.TimeoutError:
            _kill_process_group(proc, signal.SIGKILL)

    async def run_turn(
        self,
        session_id: str,
        message: str,
        workdir: str,
        resume_claude_session: str | None,
        on_event: EventCallback,
        model: str | None = None,
        on_permission=None,
        on_session_id=None,
    ) -> dict:
        """跑一个回合。返回 {claude_session_id, returncode, error, cancelled}。

        on_permission: codex 走沙箱/免审批，无交互授权，忽略此参数（仅保持接口一致）。
        model: codex 模型由 CODEX_MODEL 控制，忽略传入 model（保持接口一致）。
        """
        cmd = self._build_cmd(message=message, resume=resume_claude_session)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=workdir,
                env=_child_env(),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_STREAM_LIMIT,
                # 独立 process group，cancel/超时时一次性杀掉（含子进程）
                start_new_session=True,
            )
        except FileNotFoundError:
            return {
                "claude_session_id": resume_claude_session,
                "returncode": -1,
                "error": f"找不到 Codex CLI：{config.CODEX_BIN}，请设置环境变量 CODEX_BIN 指向真实路径。",
                "cancelled": False,
            }

        self._procs[session_id] = {"proc": proc, "cancelled": False}
        conv_id = resume_claude_session
        error_msg = ""
        # 是否已发过 result（turn.completed / turn.failed），避免 finally 重复兜底
        result_emitted = False

        async def _emit_result(is_error: bool):
            nonlocal result_emitted
            if result_emitted:
                return
            result_emitted = True
            await on_event({
                "type": "result",
                "subtype": "error" if is_error else "success",
                "duration_ms": None,
                "total_cost_usd": None,
                "num_turns": 1,
                "result": None,
                "is_error": is_error,
            })

        async def _read_stdout():
            nonlocal conv_id, error_msg
            while True:
                try:
                    line = await proc.stdout.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    # 单行超长，跳过该行避免崩溃
                    continue
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    evt = json.loads(text)
                except json.JSONDecodeError:
                    continue

                etype = evt.get("type")

                if etype == "thread.started":
                    tid = evt.get("thread_id")
                    if tid:
                        first = conv_id is None
                        conv_id = tid
                        # 首次拿到 thread_id 即回调落库，崩溃/超时也能保住 resume 目标
                        if on_session_id and first:
                            try:
                                on_session_id(tid)
                            except Exception:
                                pass
                    continue

                if etype == "item.started":
                    item = evt.get("item") or {}
                    if item.get("type") == "command_execution":
                        await on_event({
                            "type": "assistant",
                            "message": {"content": [{
                                "type": "tool_use",
                                "id": item.get("id"),
                                "name": "shell",
                                "input": {"command": item.get("command", "")},
                            }]},
                        })
                    continue

                if etype == "item.completed":
                    item = evt.get("item") or {}
                    itype = item.get("type")
                    if itype == "agent_message":
                        txt = item.get("text", "")
                        if txt:
                            await on_event({
                                "type": "assistant",
                                "message": {"content": [{"type": "text", "text": txt}]},
                            })
                    elif itype == "command_execution":
                        exit_code = item.get("exit_code")
                        await on_event({
                            "type": "user",
                            "message": {"content": [{
                                "type": "tool_result",
                                "tool_use_id": item.get("id"),
                                "content": item.get("aggregated_output", ""),
                                "is_error": exit_code not in (0, None),
                            }]},
                        })
                    continue

                if etype in ("error", "turn.failed"):
                    err = evt.get("error") or {}
                    if isinstance(err, dict):
                        error_msg = err.get("message") or evt.get("message") or "Codex 运行错误"
                    else:
                        error_msg = str(err) or evt.get("message") or "Codex 运行错误"
                    continue

                if etype == "turn.completed":
                    await _emit_result(is_error=bool(error_msg))
                    continue

        cancelled = False
        try:
            await asyncio.wait_for(_read_stdout(), timeout=config.CODEX_TURN_TIMEOUT)
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                _kill_process_group(proc, signal.SIGKILL)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    pass
        except asyncio.TimeoutError:
            error_msg = error_msg or f"Agent 回合超过 {config.CODEX_TURN_TIMEOUT}s 超时，已终止。"
            _kill_process_group(proc, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                _kill_process_group(proc, signal.SIGKILL)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    pass
        finally:
            stderr_bytes = b""
            try:
                stderr_bytes = await asyncio.wait_for(proc.stderr.read(), timeout=3)
            except Exception:
                pass
            if stderr_bytes and proc.returncode not in (0, None):
                error_msg = error_msg or stderr_bytes.decode("utf-8", errors="replace").strip()

        cancelled = bool(self._procs.get(session_id, {}).get("cancelled"))
        if cancelled and not error_msg:
            error_msg = "已取消"
        elif cancelled:
            error_msg = f"已取消（{error_msg}）"
        self._procs.pop(session_id, None)

        # 兜底 result：进程没吐 turn.completed（崩溃/超时/取消）也补一条，保证上层收尾
        try:
            await _emit_result(is_error=bool(error_msg))
        except Exception:
            pass

        return {
            "claude_session_id": conv_id,
            "returncode": proc.returncode,
            "error": error_msg,
            "cancelled": cancelled,
        }

    # ---- 空操作方法：保持与 ClaudeRunner 接口一致（codex 无常驻进程/授权/预热）----
    async def forget_session(self, session_id: str) -> None:
        return None

    async def respond_permission(self, session_id: str, request_id: str, behavior: str,
                                 updated_input: dict = None) -> None:
        return None

    async def ensure_warm(self, session_id: str, workdir: str, resume: str | None = None) -> str:
        return "warmed"

    async def cleanup_idle(self) -> None:
        return None


runner = CodexRunner()
