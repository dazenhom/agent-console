"""封装 Claude Code CLI 的 headless 模式。

通过 `claude -p <msg> --output-format stream-json --verbose [--resume <sid>]`
启动子进程，逐行解析 stream-json 事件，回调给上层（WebSocket 推送 + 持久化）。

事件类型（Claude Code stream-json）：
  system/init   -> 初始化，含 session_id、可用工具
  assistant     -> 助手消息，content 是 block 列表（text / tool_use）
  user          -> 工具结果回填（tool_result）
  result        -> 本回合结束，含耗时、花费、num_turns
"""
import asyncio
import collections
import hashlib
import json
import logging
import os
import signal
import time
from typing import Awaitable, Callable

from . import config

logger = logging.getLogger(__name__)

EventCallback = Callable[[dict], Awaitable[None]]


class LoopDetector:
    """轻量循环检测：喂进原始 stream-json 事件，判断 agent 是否卡在循环里。

    两种循环信号：
      1. 连续 repeat_threshold 次完全相同的工具调用（name+input 一致）；
      2. 连续 error_threshold 次工具报错（tool_result.is_error）。
    """

    def __init__(self, repeat_threshold=8, error_threshold=10):
        self.repeat_threshold = repeat_threshold
        self.error_threshold = error_threshold
        self._recent_calls = collections.deque(maxlen=repeat_threshold + 2)
        self._consec_errors = 0

    def feed(self, evt: dict):
        """返回 (is_loop: bool, reason: str)。"""
        # 真实 stream-json 事件里 content 嵌在 message 下（见 session_hub.translate_event），
        # 少数场景可能直接挂在顶层，两处都兼容。
        content = (evt.get("message") or {}).get("content")
        if content is None:
            content = evt.get("content")
        if not isinstance(content, list):
            content = []
        # 从 assistant 事件提取 tool_use，检测重复调用
        if evt.get("role") == "assistant" or evt.get("type") == "assistant":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    sig = block.get("name", "") + "|" + hashlib.md5(
                        json.dumps(block.get("input", {}), sort_keys=True).encode()
                    ).hexdigest()[:8]
                    self._recent_calls.append(sig)
                    if len(self._recent_calls) >= self.repeat_threshold:
                        tail = list(self._recent_calls)[-self.repeat_threshold:]
                        if len(set(tail)) == 1:
                            return True, f"工具 '{block.get('name')}' 连续相同调用 {self.repeat_threshold} 次"
        # 从 user 事件提取 tool_result，检测连续报错
        if evt.get("role") == "user" or evt.get("type") == "user":
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    if block.get("is_error"):
                        self._consec_errors += 1
                        if self._consec_errors >= self.error_threshold:
                            return True, f"连续工具报错 {self._consec_errors} 次"
                    else:
                        self._consec_errors = 0
        return False, ""

# stream-json 单行可能很大（含命令输出），放大缓冲上限
_STREAM_LIMIT = 16 * 1024 * 1024

# 若本服务本身是被某个 claude-code / tclaude 会话拉起的（常见于在 Agent
# 终端里 `nohup bash run.sh`），进程会继承一批“编排态”环境变量：
#   ANTHROPIC_BASE_URL  -> 指向父会话的临时网关代理（127.0.0.1:<随机端口>）
#   ANTHROPIC_AUTH_TOKEN/ANTHROPIC_API_KEY -> 只对那个代理有效的临时令牌
#   CLAUDE_CODE_CHILD_SESSION / CLAUDE_CODE_SESSION_ID / ANTHROPIC_CUSTOM_HEADERS ...
# 这些值会让我们 spawn 出来的 tclaude 子进程去连父会话的代理，导致
# “403 API key not allowed for this path or method”——每个回合都鉴权失败。
# 解决：spawn 前把这些继承来的编排变量清掉，让 tclaude 用它自己的 IOA 登录凭证。
_STRIP_ENV_KEYS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_TEAMMATE_COMMAND",
    "CLAUDE_CONFIG_DIR",      # 让 tclaude 回落到自己的默认配置目录（/root/.tclaude）
    "CLAUDE_SETTINGS_DIR",
    "CLAUDECODE",
)


def _child_env() -> dict:
    """构造给 tclaude 子进程的环境：复制当前环境，剔除继承来的编排态变量。"""
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV_KEYS}
    return env


def _kill_process_group(proc: asyncio.subprocess.Process, sig: int) -> bool:
    """优雅地把 proc 整个进程组发信号。返回 True 表示信号已发出。"""
    if proc.returncode is not None:
        return False
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        # 兜底：至少把直接子进程杀掉
        try:
            if sig == signal.SIGKILL:
                proc.kill()
            else:
                proc.terminate()
            return True
        except Exception:
            return False


class ClaudeRunner:
    def __init__(self):
        # session_id -> {"proc": Process, "cancelled": bool}  （老模式：每回合一个进程）
        self._procs: dict[str, dict] = {}
        # session_id -> 常驻进程记录（新模式）：
        #   {proc, stdin, reader_task, claude_sid, last_active, on_event, result_evt,
        #    cancelled, lock, error}
        self._sessions: dict[str, dict] = {}
        # session_id -> 预热锁：避免并发 ensure_warm 重复 spawn
        self._session_locks: dict[str, asyncio.Lock] = {}

    def _build_cmd(self, *, message: str | None, model: str | None,
                   resume: str | None, stream_input: bool) -> list[str]:
        """组 tclaude 命令。stream_input=True 时走常驻 stream-json 输入（message 走 stdin）。"""
        cmd = [config.CLAUDE_BIN, "--", "-p"]
        if not stream_input:
            cmd.append(message)
        cmd += ["--output-format", "stream-json", "--verbose"]
        if stream_input:
            cmd += ["--input-format", "stream-json"]
        if model:
            cmd += ["--model", model]
        if config.CLAUDE_EFFORT:
            cmd += ["--effort", config.CLAUDE_EFFORT]
        if config.CLAUDE_STREAM_PARTIAL:
            cmd += ["--include-partial-messages"]
        try:
            from . import agent_store
            aj = agent_store.agents_json()
            if aj:
                cmd += ["--agents", aj]
        except Exception:
            pass
        if resume:
            cmd += ["--resume", resume]
        if config.CLAUDE_SKIP_PERMISSIONS:
            cmd += ["--dangerously-skip-permissions"]
        elif config.CLAUDE_ALLOWED_TOOLS.strip():
            # root 下 skip-permissions 被拒，改用 allowedTools 放行工具免权限提示。
            # 空格分隔的工具名直接作为多个参数传给 --allowedTools。
            cmd += ["--allowedTools", *config.CLAUDE_ALLOWED_TOOLS.split()]
        disallowed = config.CLAUDE_DISALLOWED_TOOLS.split()
        if stream_input:
            # 常驻 stream-json 模式有 stdin 可回 control_response：AskUserQuestion 能经
            # control_request → 前端选项卡片 → updatedInput.answers 正常交互，不禁用。
            # 仅老的 headless -p 模式（无 stdin、无法回授权）才禁掉它，防回合卡死。
            disallowed = [t for t in disallowed if t != "AskUserQuestion"]
        if disallowed:
            cmd += ["--disallowedTools", *disallowed]
        return cmd

    def _get_proc(self, session_id: str) -> asyncio.subprocess.Process | None:
        rec = self._procs.get(session_id)
        return rec["proc"] if rec else None

    def is_running(self, session_id: str) -> bool:
        # 常驻模式：以"本回合是否进行中"为准（进程常活着）
        sess = self._sessions.get(session_id)
        if sess is not None:
            return bool(sess.get("turn_active"))
        proc = self._get_proc(session_id)
        return proc is not None and proc.returncode is None

    async def ensure_warm(self, session_id: str, workdir: str, resume: str | None = None) -> str:
        """预热常驻进程：进程已存活则返回 'running'，否则 spawn 并返回 'warmed'。"""
        lock = self._session_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            sess = self._sessions.get(session_id)
            if sess and sess["proc"].returncode is None:
                return "running"
            if sess and sess.get("turn_active"):
                return "running"  # 回合进行中，不干预
            # 进程不存在或已退出，重新 spawn（model=None 沿用会话默认 model）
            await self._spawn_session(session_id, workdir, model=None, resume=resume)
            return "warmed"

    def was_cancelled(self, session_id: str) -> bool:
        rec = self._procs.get(session_id)
        return bool(rec and rec.get("cancelled"))

    async def cancel(self, session_id: str) -> None:
        """中断当前回合。常驻模式：杀进程（下回合自动 resume 续上）；老模式：杀进程组。"""
        sess = self._sessions.get(session_id)
        if sess is not None:
            sess["cancelled"] = True
            await self._kill_session(session_id)
            # 唤醒在等本回合结束的 send_turn
            evt = sess.get("result_evt")
            if evt:
                evt.set()
            return
        rec = self._procs.get(session_id)
        if not rec:
            return
        proc = rec["proc"]
        if proc.returncode is not None:
            return
        rec["cancelled"] = True
        _kill_process_group(proc, signal.SIGTERM)
        # 异步等 2 秒，看是否需要 SIGKILL
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
    ) -> dict:
        """跑一个回合。返回 {claude_session_id, returncode, error}。

        model: 传给 `tclaude -- -p ... --model <name>`，None/空字符串则不传由 CLI 决定。
        on_permission: 老模式无 stdin 回写权限，忽略此参数（仅常驻模式支持）。
        """
        cmd = self._build_cmd(message=message, model=model,
                              resume=resume_claude_session, stream_input=False)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=workdir,
                env=_child_env(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_STREAM_LIMIT,
                # 把子进程放到独立的 process group，cancel 时一次性杀掉
                # （否则 node + bash 子进程会变孤儿继续跑）
                start_new_session=True,
            )
        except FileNotFoundError:
            return {
                "claude_session_id": resume_claude_session,
                "returncode": -1,
                "error": f"找不到 Claude CLI：{config.CLAUDE_BIN}，请设置环境变量 CLAUDE_BIN 指向真实路径。",
            }

        self._procs[session_id] = {"proc": proc, "cancelled": False}
        claude_session_id = resume_claude_session

        async def _read_stdout():
            nonlocal claude_session_id
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
                sid = evt.get("session_id")
                if sid:
                    claude_session_id = sid
                await on_event(evt)

        error = ""
        try:
            await asyncio.wait_for(_read_stdout(), timeout=config.CLAUDE_TURN_TIMEOUT)
            # 读完 stdout 后再等子进程退出，但同样给个上限，防 wait 永远卡住
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    pass
        except asyncio.TimeoutError:
            proc.terminate()
            error = f"Agent 回合超过 {config.CLAUDE_TURN_TIMEOUT}s 超时，已终止。"
            # 超时后给一点时间让子进程响应 SIGTERM，否则强杀
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    pass
        finally:
            stderr_bytes = b""
            try:
                # 读 stderr 也加超时，避免 fd 异常时永远阻塞
                stderr_bytes = await asyncio.wait_for(proc.stderr.read(), timeout=3)
            except Exception:
                pass
            if stderr_bytes and proc.returncode not in (0, None):
                error = error or stderr_bytes.decode("utf-8", errors="replace").strip()
            # 注意：此处不 pop，留到 return 前再 pop，这样上面能读到 cancelled 标记

        # 若是用户主动 cancel，覆盖原始 error，给前端一个清晰提示
        cancelled = bool(self._procs.get(session_id, {}).get("cancelled"))
        if cancelled and not error:
            error = "已取消"
        elif cancelled:
            error = f"已取消（{error}）"
        # 注意：此处先读 cancelled 再 pop，避免 finally 已经 pop 后丢状态
        self._procs.pop(session_id, None)

        return {
            "claude_session_id": claude_session_id,
            "returncode": proc.returncode,
            "error": error,
            "cancelled": cancelled,
        }

    # ============== 常驻进程模式（--input-format stream-json）==============
    async def respond_permission(self, session_id: str, request_id: str, behavior: str,
                                 updated_input: dict = None) -> None:
        """回一行 control_response 给 CLI，放行/拒绝一个待授权的工具调用。

        updated_input 用于 AskUserQuestion 这类工具：答案必须经 updatedInput 回填，
        CLI 才能拿到用户的选择（如 {"answers": {...}}），否则模型只能自答。"""
        sess = self._sessions.get(session_id)
        if not sess or not sess.get("proc"):
            return
        inner = {"behavior": behavior}
        if updated_input:
            inner["updatedInput"] = updated_input
        resp = {
            "type": "control_response",
            "response": {
                "request_id": request_id,
                "subtype": "success",
                "response": inner,
            },
        }
        line = json.dumps(resp, ensure_ascii=False) + "\n"
        try:
            sess["proc"].stdin.write(line.encode())
            await sess["proc"].stdin.drain()
        except Exception:
            pass

    async def _kill_session(self, session_id: str) -> None:
        """杀掉某会话的常驻进程（不删记录，spawn 时会覆盖）。"""
        sess = self._sessions.get(session_id)
        if not sess:
            return
        proc = sess.get("proc")
        if proc and proc.returncode is None:
            _kill_process_group(proc, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=2)
            except asyncio.TimeoutError:
                _kill_process_group(proc, signal.SIGKILL)
        rt = sess.get("reader_task")
        if rt and not rt.done():
            rt.cancel()

    async def forget_session(self, session_id: str) -> None:
        """彻底丢弃缓存的常驻进程与 claude_sid：杀进程并删记录。

        用于 resume 失败后的自动重启——下次 send_turn 会以全新会话 spawn，
        不再带上那个已失效的 claude_sid。"""
        await self._kill_session(session_id)
        self._sessions.pop(session_id, None)

    async def _spawn_session(self, session_id: str, workdir: str, model: str | None,
                             resume: str | None) -> dict:
        """为会话拉起一个常驻 stream-json 进程，启动后台 reader。返回 sess 记录。"""
        cmd = self._build_cmd(message=None, model=model, resume=resume, stream_input=True)
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=workdir, env=_child_env(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LIMIT, start_new_session=True,
        )
        sess = {
            "proc": proc, "stdin": proc.stdin, "claude_sid": resume,
            "last_active": time.monotonic(), "turn_active": False,
            "cancelled": False, "on_event": None, "on_permission": None,
            "result_evt": None, "workdir": workdir,
            "model": model,
            # 看门狗：循环检测器 + 循环命中标记（reader 里喂事件，send_turn 里轮询判定）
            "loop_detector": LoopDetector(config.CLAUDE_LOOP_REPEAT, config.CLAUDE_LOOP_ERRORS),
            "loop_detected": False,
            "loop_reason": "",
        }
        self._sessions[session_id] = sess
        sess["reader_task"] = asyncio.ensure_future(self._reader(session_id))
        return sess

    async def _reader(self, session_id: str):
        """后台持续读常驻进程 stdout，把事件转给当前回合的 on_event；result 标志回合结束。"""
        sess = self._sessions.get(session_id)
        if not sess:
            return
        proc = sess["proc"]
        while True:
            try:
                line = await proc.stdout.readline()
            except (asyncio.LimitOverrunError, ValueError):
                continue
            if not line:
                break  # 进程结束/ stdout 关闭
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                evt = json.loads(text)
            except json.JSONDecodeError:
                continue
            sid = evt.get("session_id")
            if sid:
                sess["claude_sid"] = sid
            # 看门狗：任何一条成功解析的事件都算"仍在推进"，刷新活跃时间并喂给循环检测器
            sess["last_active"] = time.monotonic()
            is_loop, reason = sess["loop_detector"].feed(evt)
            if is_loop:
                sess["loop_detected"] = True
                sess["loop_reason"] = reason
            # control_request：CLI 请求授权某个未放行的工具（--input-format stream-json 下），
            # 等我们回一行 control_response。转给 on_permission 回调走前端弹窗，不当作普通事件，
            # 更不触发 result（本回合还没结束，仍在等授权）。
            if evt.get("type") == "control_request":
                req = evt.get("request", {}) or {}
                request_id = evt.get("request_id") or req.get("request_id")
                tool_name = req.get("tool_name") or evt.get("tool_name")
                tool_input = req.get("input") or evt.get("input") or {}
                cb = sess.get("on_permission")
                if cb and request_id:
                    try:
                        await cb(request_id, tool_name, tool_input)
                    except Exception:
                        pass
                elif request_id:
                    # 权限弹窗已禁用或回调未注册，自动拒绝避免 CLI 卡死等授权
                    try:
                        await self.respond_permission(session_id, request_id, "deny")
                    except Exception:
                        pass
                continue
            # resume 失败：`tclaude --resume <sid>` 在 cwd 与该 sid 的 jsonl 归属目录
            # 不一致时，会立即吐一条 error result（num_turns=0，errors 含 "No conversation
            # found with session ID"），随后进程退出。这条坏 result 不该落库/广播（否则
            # 前端出现空的“$0.0000 完成”行），只打标记让 session_hub 侧自动重启续接。
            # 判定必须严格：num_turns==0 且命中 resume 失败关键字，避免误伤 agent 正常
            # 执行中的报错（is_error=true 但 num_turns>0）。
            if evt.get("type") == "result" and evt.get("subtype") == "error_during_execution":
                errors_text = json.dumps(evt.get("errors", ""), ensure_ascii=False)
                if evt.get("num_turns", 0) == 0 and "No conversation found" in errors_text:
                    sess["resume_failed"] = True
                    ev = sess.get("result_evt")
                    if ev:
                        ev.set()
                    continue  # 不转发这条坏 result
            cb = sess.get("on_event")
            if cb:
                try:
                    await cb(evt)
                except Exception:
                    pass
            # result 事件 = 本回合结束，唤醒 send_turn
            if evt.get("type") == "result":
                ev = sess.get("result_evt")
                if ev:
                    ev.set()
        # 进程退出：若有人在等回合结束，也唤醒（让其按崩溃处理）
        ev = sess.get("result_evt")
        if ev:
            ev.set()

    async def send_turn(self, session_id: str, message: str, workdir: str,
                        resume_claude_session: str | None, on_event: EventCallback,
                        model: str | None = None, on_permission=None) -> dict:
        """常驻进程模式跑一回合。进程不存在/已死则拉起（带 resume），写 stdin，等本回合 result。"""
        sess = self._sessions.get(session_id)
        proc_dead = (not sess) or (sess["proc"].returncode is not None)
        if proc_dead:
            # 崩溃/超时/首次：重新拉起，用上次 claude_sid 或传入的 resume 续上下文
            resume = (sess or {}).get("claude_sid") or resume_claude_session
            try:
                sess = await self._spawn_session(session_id, workdir, model, resume)
            except FileNotFoundError:
                return {"claude_session_id": resume_claude_session, "returncode": -1,
                        "error": f"找不到 Claude CLI：{config.CLAUDE_BIN}"}

        sess["on_event"] = on_event
        sess["on_permission"] = on_permission
        sess["cancelled"] = False
        sess["resume_failed"] = False
        sess["turn_active"] = True
        sess["last_active"] = time.monotonic()
        # 每回合开始清掉上一回合的循环命中标记，并重建检测器让内部计数从新回合基线开始
        sess["loop_detected"] = False
        sess["loop_reason"] = ""
        sess["loop_detector"] = LoopDetector(config.CLAUDE_LOOP_REPEAT, config.CLAUDE_LOOP_ERRORS)
        result_evt = asyncio.Event()
        sess["result_evt"] = result_evt

        # 写一行 stream-json 用户消息
        payload = json.dumps({
            "type": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": message}]},
        }, ensure_ascii=False) + "\n"
        try:
            sess["stdin"].write(payload.encode("utf-8"))
            await sess["stdin"].drain()
        except Exception as e:
            sess["turn_active"] = False
            return {"claude_session_id": sess.get("claude_sid"), "returncode": -1,
                    "error": f"写入失败，进程可能已退出：{e}"}

        # 等本回合 result（或被 cancel/崩溃唤醒）。看门狗轮询：只要 agent 还在推进
        # （有新事件刷新 last_active）就继续等；仅在检测到循环、长时间空闲、或超过绝对
        # 安全上限时才主动终止。
        error = ""
        start = time.monotonic()
        while True:
            try:
                await asyncio.wait_for(result_evt.wait(), timeout=config.CLAUDE_WATCHDOG_INTERVAL)
                break  # 回合正常结束（result 事件或进程退出唤醒）
            except asyncio.TimeoutError:
                now = time.monotonic()
                # 会话记录理论上不会被 _kill_session 移除（它只杀进程不删记录），
                # 但仍防御性判空，避免竞态下解引用崩溃。
                cur = self._sessions.get(session_id)
                if cur is None:
                    break
                # 循环检测
                if cur.get("loop_detected"):
                    reason = cur.get("loop_reason", "未知循环")
                    error = f"检测到 Agent 循环（{reason}），已终止。"
                    logger.warning("session %s: loop detected: %s", session_id, reason)
                    await self._kill_session(session_id)
                    break
                # 空闲超时
                idle = now - cur.get("last_active", start)
                if idle > config.CLAUDE_IDLE_TIMEOUT:
                    error = f"Agent {int(idle)}s 内无任何输出，判定卡死，已终止。"
                    logger.warning("session %s: idle timeout after %ds", session_id, int(idle))
                    await self._kill_session(session_id)
                    break
                # 绝对上限
                elapsed = now - start
                if elapsed > config.CLAUDE_TURN_MAX:
                    error = f"Agent 回合超过安全上限 {config.CLAUDE_TURN_MAX}s，已终止。"
                    logger.warning("session %s: hard ceiling %ds reached", session_id, int(elapsed))
                    await self._kill_session(session_id)
                    break
                # 仍在推进，继续等
                logger.debug("session %s: watchdog tick, idle=%.1fs, elapsed=%.1fs",
                             session_id, idle, elapsed)

        cancelled = bool(sess.get("cancelled"))
        sess["turn_active"] = False
        sess["on_event"] = None
        sess["on_permission"] = None
        sess["result_evt"] = None
        sess["last_active"] = time.monotonic()

        # 进程是否在本回合中死掉（崩溃或被 cancel 杀）
        if sess["proc"].returncode is not None and not error:
            if cancelled:
                error = "已取消"
            else:
                error = "Agent 进程已退出（下次发送会自动重启续上下文）"

        return {
            "claude_session_id": sess.get("claude_sid"),
            "returncode": sess["proc"].returncode,
            "error": error,
            "cancelled": cancelled,
            "resume_failed": bool(sess.get("resume_failed")),
        }

    async def cleanup_idle(self) -> None:
        """回收空闲超时的常驻进程（下次消息会自动重起 + resume）。"""
        now = time.monotonic()
        for sid, sess in list(self._sessions.items()):
            if sess.get("turn_active"):
                continue
            if now - sess.get("last_active", now) > config.CLAUDE_SESSION_IDLE_SEC:
                await self._kill_session(sid)


runner = ClaudeRunner()
