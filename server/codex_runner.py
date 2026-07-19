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
import signal
from typing import Awaitable, Callable

from . import config
from .claude_runner import _child_env, _kill_process_group, _STREAM_LIMIT, is_sleep_command
from .logging_util import get_logger

logger = get_logger(__name__)

EventCallback = Callable[[dict], Awaitable[None]]


class CodexRunner:
    def __init__(self):
        # session_id -> {"proc": Process, "cancelled": bool}（每回合一个进程）
        self._procs: dict[str, dict] = {}

    def _build_cmd(self, *, message: str, resume: str | None, model: str | None = None) -> list[str]:
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
        # 优先用会话选定的模型，回退到 CODEX_MODEL（空则不传，让 CLI 用默认）。
        m = model or config.CODEX_MODEL
        if m:
            cmd += ["-m", m]
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
        model: 会话选定的 codex 模型（CODEX_MODELS 之一）；空则回退到 CODEX_MODEL / CLI 默认。
        """
        cmd = self._build_cmd(message=message, resume=resume_claude_session, model=model)

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
        # resume 失败检测 / 续跑心跳用的状态：
        # item_seen —— 本回合是否吐过任意 item 事件（有则说明 resume 正常起来了）；
        # thread_started_at —— 收到 thread.started 的时刻（心跳监视器据此计时）。
        loop = asyncio.get_event_loop()
        item_seen = False
        thread_started_at: float | None = None
        heartbeat_sent = False
        # 等待型（sleep 轮询）调用豁免：把这段"主动等待长任务"（如等 GPU 训练完成）的时长
        # 从有效工作时间倒计时里扣除，避免正常轮询被 CODEX_TURN_TIMEOUT 误杀。
        start_time = loop.time()
        committed_waived = 0.0    # 已完成等待型调用的累计豁免秒数
        pending_wait: dict = {}    # item_id -> 开始时刻，记录在飞的等待型调用
        wait_notice_sent = False   # 是否已推过一次"检测到等待型调用"提示，避免刷屏

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
            nonlocal conv_id, error_msg, item_seen, thread_started_at
            nonlocal committed_waived, wait_notice_sent
            while True:
                try:
                    line = await proc.stdout.readline()
                except asyncio.LimitOverrunError:
                    # 排空超长行，避免 readline 死循环
                    try:
                        while True:
                            chunk = await proc.stdout.read(65536)
                            if not chunk or b"\n" in chunk:
                                break
                    except Exception:
                        pass
                    continue
                except ValueError:
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
                    # 记下 thread 起始时刻，供续跑心跳监视器计时
                    if thread_started_at is None:
                        thread_started_at = loop.time()
                    continue

                if etype == "item.started":
                    item = evt.get("item") or {}
                    item_seen = True
                    if item.get("type") == "command_execution":
                        item_id = item.get("id")
                        # 等待型调用（sleep 轮询）：记下开始时刻，其时长将从超时倒计时豁免。
                        if item_id is not None and is_sleep_command(item.get("command")):
                            pending_wait[item_id] = loop.time()
                            if not wait_notice_sent:
                                wait_notice_sent = True
                                # 用 type=status 纯状态事件（同心跳提示）：不落库、不拼进
                                # reply_text、不污染摘要/标题/企业微信通知，只做前端瞬时提示。
                                try:
                                    await on_event({
                                        "type": "status",
                                        "text": "检测到等待型调用（sleep），超时计时已相应延长",
                                    })
                                except Exception:
                                    pass
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
                    item_seen = True
                    itype = item.get("type")
                    if itype == "agent_message":
                        txt = item.get("text", "")
                        if txt:
                            await on_event({
                                "type": "assistant",
                                "message": {"content": [{"type": "text", "text": txt}]},
                            })
                    elif itype == "command_execution":
                        # 等待型调用收尾：把这次实际等待时长（单次封顶）累加进已完成豁免。
                        item_id = item.get("id")
                        if item_id in pending_wait:
                            dur = loop.time() - pending_wait.pop(item_id)
                            committed_waived = min(
                                config.CODEX_WAIT_WAIVE_TOTAL_MAX,
                                committed_waived + min(dur, config.CODEX_WAIT_WAIVE_PER_CALL_MAX),
                            )
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

        async def _heartbeat_monitor():
            """续跑心跳：thread.started 之后长时间零 item 事件时，推一条状态提示，
            让前端不再干等"思考中"（用户以为卡死点停止是本次要修的核心体验问题）。
            只推一次；一旦收到任意 item 事件即退出。"""
            nonlocal heartbeat_sent
            while True:
                await asyncio.sleep(2)
                if item_seen or heartbeat_sent:
                    return
                if thread_started_at is None:
                    continue
                if loop.time() - thread_started_at < config.CODEX_RESUME_HEARTBEAT_SECONDS:
                    continue
                heartbeat_sent = True
                try:
                    # 用 type=status 的纯状态事件，而非 assistant：后者会被 session_hub 当成真实
                    # 回复落库、拼进 reply_text（污染摘要/标题/企业微信通知）、被 _activity_label
                    # 统计成"已有回复"。status 只广播给前端做瞬时提示，不产生任何回复副作用。
                    await on_event({
                        "type": "status",
                        "text": "正在续接上文，可能需要较长时间，请耐心等待…",
                    })
                except Exception:
                    pass
                return

        # 仅续跑（resume 非空）回合才挂心跳监视器：全新会话首回合冷启动慢是正常的，不提示。
        hb_task = asyncio.create_task(_heartbeat_monitor()) if resume_claude_session else None

        cancelled = False

        async def _terminate_after_timeout():
            """有效工作/墙钟超时后的终止序列（与原硬超时分支同一套）：先 SIGTERM，给 codex
            落稳 rollout 文件的宽限（下回合 resume 靠它），再兜底 SIGKILL。"""
            _kill_process_group(proc, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=config.CODEX_TIMEOUT_GRACE_SECONDS)
            except asyncio.TimeoutError:
                _kill_process_group(proc, signal.SIGKILL)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    pass

        read_task = asyncio.create_task(_read_stdout())
        try:
            # 看门狗：每 CODEX_WATCHDOG_INTERVAL 秒醒一次核对超时预算。stdout 读完（进程正常
            # 收尾）即退出；否则按"有效工作时间"（扣除等待豁免）和"墙钟绝对上限"两条线判杀。
            while True:
                done, _ = await asyncio.wait({read_task}, timeout=config.CODEX_WATCHDOG_INTERVAL)
                if read_task in done:
                    read_task.result()  # 传播 _read_stdout 内的异常（若有），与原 wait_for 行为一致
                    # 读完 stdout 后再等子进程退出，给个上限防 wait 永远卡住
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=10)
                    except asyncio.TimeoutError:
                        _kill_process_group(proc, signal.SIGKILL)
                        try:
                            await asyncio.wait_for(proc.wait(), timeout=3)
                        except asyncio.TimeoutError:
                            pass
                    break
                # 还在跑：算"当前豁免总量"。把在飞的 pending_wait 也实时计入（单次封顶），
                # 避免一个正在进行的长 sleep 期间豁免没被计入、导致提前被杀。
                now = loop.time()
                waived_now = min(
                    config.CODEX_WAIT_WAIVE_TOTAL_MAX,
                    committed_waived + sum(
                        min(now - t, config.CODEX_WAIT_WAIVE_PER_CALL_MAX)
                        for t in pending_wait.values()
                    ),
                )
                if now - start_time - waived_now > config.CODEX_TURN_TIMEOUT:
                    error_msg = error_msg or (
                        f"Agent 回合有效工作时间超过 {config.CODEX_TURN_TIMEOUT}s"
                        "（等待时间已豁免），已终止。"
                    )
                    await _terminate_after_timeout()
                    break
                if now - start_time > config.CODEX_TURN_MAX_WALL:
                    error_msg = error_msg or (
                        f"Agent 回合总耗时超过绝对上限 {config.CODEX_TURN_MAX_WALL}s，已强制终止。"
                    )
                    await _terminate_after_timeout()
                    break
        finally:
            if not read_task.done():
                read_task.cancel()
                try:
                    await read_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            if hb_task and not hb_task.done():
                hb_task.cancel()
            stderr_bytes = b""
            try:
                stderr_bytes = await asyncio.wait_for(proc.stderr.read(), timeout=3)
            except Exception:
                pass
            stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip() if stderr_bytes else ""
            if stderr_text and proc.returncode not in (0, None):
                error_msg = error_msg or stderr_text

        cancelled = bool(self._procs.get(session_id, {}).get("cancelled"))
        if cancelled and not error_msg:
            error_msg = "已由用户手动停止"
        elif cancelled:
            error_msg = f"已由用户手动停止（{error_msg}）"
        self._procs.pop(session_id, None)

        # 无论后续是否触发 resume 自愈，都先把原始错误/stderr 记进服务端日志：session_hub 的
        # 自愈分支会用通用文案（"上下文已失效…"）覆盖 error_msg，不落日志的话原始信息（如超时
        # 分支写好的"回合超过 XXs 超时"、OOM/被杀的 stderr）会彻底消失、无从排查。
        if error_msg or stderr_text:
            logger.warning(
                "codex turn 收尾 sid=%s returncode=%s cancelled=%s item_seen=%s error=%r stderr=%r",
                session_id, proc.returncode, cancelled, item_seen, error_msg, stderr_text[:2000],
            )

        # resume 失败自愈：对齐 claude_runner 的精确判定思路——claude 侧靠结构化强信号
        # （type=result / subtype=error_during_execution / num_turns==0 / errors 含
        # "No conversation found"），绝不靠宽泛关键字或"进程非零退出"兜底。codex 无等价结构化
        # 事件，只能读 stderr：仅当 resume 非空、全程零 item 事件、非用户取消，且 stderr 命中
        # "会话/线程确实找不到"这类强信号时才标记。
        # 刻意不再因"进程非零退出"就触发——超时/被杀已各有专门错误路径，在此兜底只会把无关崩溃
        # （OOM、信号）误判成 resume 失败，还让准确错误信息被通用文案覆盖、误导排查方向。
        # 关键字也去掉了过宽的 "resume"/"not found"/"no such"（正常日志/无关文件缺失都可能命中）。
        resume_failed = False
        if resume_claude_session and not item_seen and not cancelled and stderr_text:
            stderr_low = stderr_text.lower()
            resume_failed = any(k in stderr_low for k in (
                "no conversation", "conversation not found",
                "thread not found", "no such conversation",
            ))

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
            "resume_failed": resume_failed,
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
