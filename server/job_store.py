"""一次性子进程运行记录：起子进程 + 落库 + 落盘日志。

triage.run_triage / goal_verifier.verify / kanban.summarize_progress 三处
一次性调用共用的执行外壳，把原始 stdout/stderr 写进 job_logs/<jid>.log、
往 job_runs 表记一行，避免产出随进程重启或会话结束而丢失。

只负责"跑 + 记 + 落盘"，不解析结果——解析与失败降级逻辑留在各调用方，
调用方拿到 stdout 后按需 db.set_job_output(jid, 结论) 补写结论。

原先超时的便宜档被归因于冷启动/连接建立卡顿，真实根因是 proc.communicate() 等的是管道
EOF 而非进程退出：codex 会 fork git 孙进程（git ls-remote/fetch），孙进程继承 stdout
管道写端，父进程退出后孙进程 reparent 到 init 继续持有写端 → 管道永不 EOF → wait_for
必然吃满 timeout，已生成的结果被全部丢弃。现以"进程退出"为完成判据，退出后补发 killpg
清掉孤儿孙进程；stdin=DEVNULL 的历史修复保留（codex exec 在 stdin 为管道时会追加读取并
等 EOF）。超时仍保留小退避重试，每次尝试独立落一行 job_runs；
所有 oneshot 子进程受模块级信号量封顶，避免堆叠打满并发。

跑子进程外壳之外，还提供调用方共用的两个纯文本解析器：parse_claude_result_line（从
claude --output-format json 的 result 行取文本）与 parse_done_verdict（DONE/CONTINUE
verdict 解析），都不依赖子进程，便于单测与复用。
"""
import asyncio
import json
import os
import re
import signal
from pathlib import Path

from . import config, db
from .agent_provider import _kill_process_group
from .claude_runner import _child_env

# 所有 oneshot 子进程的全局并发上限
_sem = asyncio.Semaphore(config.ONESHOT_MAX_CONCURRENCY)


def _log_dir() -> Path:
    d = Path(db.config.DB_PATH).parent / "job_logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def _wait_process_exit(proc, interval: float = 0.02) -> None:
    """等"进程真实退出"（轮询 returncode），不搭管道：asyncio 的 proc.wait() 的
    exit waiter 要等 _call_connection_lost（全部管道断开）才 resolve，孙进程持
    stdout 写端时会跟着管道一起挂住；子进程 watcher 在进程退出时即写入
    returncode，与管道是否 EOF 无关。"""
    while proc.returncode is None:
        await asyncio.sleep(interval)


async def _attempt(cmd: list, timeout: float, cwd: str | None) -> tuple[str, str, str, str]:
    """单次跑一次性子进程，返回 (stdout_text, stderr_text, status, error)。

    完成判据是"进程退出"而非"管道 EOF"：codex 退出后 git 孙进程仍持有 stdout 写端，
    管道迟迟不 EOF，故直接等进程退出本身；退出后先给 reader 0.5s 宽限读完已 flush 的
    余量，再取消 reader、killpg 清掉孤儿孙进程。stdout/stderr 用 read(65536) 分块累积，
    不用 readline()：StreamReader 默认 64KB 行长上限会把超长的 codex JSONL 行截断。
    """
    stdout_text = ""
    stderr_text = ""
    status = "success"
    error = ""
    proc = None
    readers: list[asyncio.Task] = []
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=_child_env(), cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            # stdin 必须显式断开：继承会让 codex exec 在 stdin 为管道时追加读取并等 EOF，
            # 挂到被判超时（历史 83% 超时的根因）；prompt 一律经 argv 传入。
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        out_chunks: list[bytes] = []
        err_chunks: list[bytes] = []

        async def _drain(stream, sink):
            if stream is None:
                return
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                sink.append(chunk)

        readers = [asyncio.create_task(_drain(proc.stdout, out_chunks)),
                   asyncio.create_task(_drain(proc.stderr, err_chunks))]

        try:
            # timeout 只包"进程退出"这一件事，不包 reader
            await asyncio.wait_for(_wait_process_exit(proc), timeout=timeout)
        except asyncio.TimeoutError:
            # 超时兜底（判据改对后正常不会走到）：此时进程本身还活着（wait 没返回），
            # _kill_process_group 能正常发信号；已读到的部分输出保留返回（旧实现全丢弃，不利排查）
            try:
                _kill_process_group(proc, signal.SIGKILL)
                # killpg/SIGKILL 后进程必死，returncode 毫秒级就位，5s 是百倍裕量；
                # 万一异常超时也继续走统一收尾，绝不无限挂死占住 _sem
                await asyncio.wait_for(_wait_process_exit(proc), timeout=5.0)
            except Exception:
                pass
            status = "timeout"
            error = "子进程超时"
        else:
            # 进程已退出：给 reader 0.5s 短收尾宽限，读完管道里已 flush 的余量
            # （codex 退出前 flush 完 JSONL，量级几百字节足够；孙进程持写端时 reader 永远
            # 不会 EOF，宽限一到就走统一收尾）
            await asyncio.wait(readers, timeout=0.5)

        # 统一收尾（正常退出/超时两条路径都经过这里）：停 reader → 清孤儿孙进程 → 关管道
        for t in readers:
            t.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        # P1：无条件对已退出子进程的原进程组补发 SIGKILL——codex 退出前 fork 的 git 孙进程
        # 仍在原进程组里持有 stdout 写端（实测每次 oneshot 留 3 个 PPID=1 的孤儿），不杀会
        # 常驻 init 名下。注意不能用 _kill_process_group：它在 returncode 已定时提前返回
        # 不发信号（旧 timeout 路径实测连孤儿都没杀掉）；start_new_session=True 保证
        # pgid == proc.pid，直接 killpg 即可；组已空时抛 ProcessLookupError，忽略。
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        # 主动关管道 transport（asyncio 无公开 API，只能用私有属性 _transport；进程已退出后
        # 它只关两个管道不会误杀进程；防 fd 泄漏与 "Event loop is closed" warning——旧实现
        # 靠 GC 才关，loop 已关时就抛 RuntimeError）
        try:
            proc._transport.close()
        except Exception:
            pass
        stdout_text = b"".join(out_chunks).decode("utf-8", errors="replace")
        stderr_text = b"".join(err_chunks).decode("utf-8", errors="replace")
    except asyncio.CancelledError:
        # 外部取消（去抖取消/删会话）：完整清理后 re-raise。CancelledError 是 BaseException，
        # 绝不能被 except Exception 吞掉；job_runs 收尾由上层 run_logged_oneshot 的 except 分支负责。
        if proc is not None:
            try:
                # 与 P1 同理直接 killpg：取消可能发生在进程已退出后的收尾宽限期，
                # 此时 _kill_process_group 会提前返回、清不到孤儿
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                # killpg/SIGKILL 后进程必死，returncode 毫秒级就位，5s 是百倍裕量；
                # 万一异常超时也继续走统一收尾，绝不无限挂死占住 _sem
                await asyncio.wait_for(_wait_process_exit(proc), timeout=5.0)
            except Exception:
                pass
        for t in readers:
            t.cancel()
        try:
            await asyncio.gather(*readers, return_exceptions=True)
        except Exception:
            pass
        if proc is not None:
            try:
                proc._transport.close()
            except Exception:
                pass
        raise
    except Exception as e:
        status = "error"
        error = f"{type(e).__name__}: {e}"
    return stdout_text, stderr_text, status, error


async def run_logged_oneshot(kind: str, cmd: list, timeout: float, *,
                             session_id: str | None = None, schedule_id: str | None = None,
                             model: str = "", input_summary: str = "",
                             cwd: str | None = None,
                             retries: int | None = None) -> tuple[str, str, str, str]:
    """跑一次性子进程并落库落盘，返回 (jid, stdout_text, stderr_text, status)。

    status 取 success/timeout/error 之一；正常结束默认 success，若调用方解析后
    认定失败可通过 db.finish_job 覆盖。stdout+stderr 全文写进 job_logs/<jid>.log。

    cwd 为子进程工作目录：None 时沿用服务器进程默认 cwd（向后兼容），传入隔离
    worktree 目录时子进程在该目录下运行，验收类调用才能核实到正确的产出位置。

    超时按 retries（None 时取 config.ONESHOT_RETRIES）做小退避重试，最多
    1 + retries 次尝试；每次尝试都有独立 jid，start/finish 各记一行 job_runs，
    历史超时率信号保留在库里。返回最后一次尝试的 (jid, stdout, stderr, status)。
    所有尝试受 config.ONESHOT_MAX_CONCURRENCY 全局封顶，退避 sleep 在信号量外。
    """
    if retries is None:
        retries = config.ONESHOT_RETRIES

    jid = ""
    stdout_text = ""
    stderr_text = ""
    status = "success"
    for i in range(1 + retries):
        jid = db.start_job(kind, session_id=session_id, schedule_id=schedule_id,
                           model=model, input_summary=input_summary)
        log_path = _log_dir() / f"{jid}.log"

        try:
            async with _sem:
                stdout_text, stderr_text, status, error = await _attempt(cmd, timeout, cwd)
        except asyncio.CancelledError:
            # 取消可能发生在两处：排队等信号量时（在 __aenter__ 处抛出，此时尚无输出，
            # 落骨架日志）和 _attempt 执行期间（_attempt 已杀进程组）。两种情况都把本次
            # 尝试落盘 + job_runs 记为 cancelled，再 re-raise 让取消语义继续向上传播
            # （去抖调用方靠它静默退出，不能吞）。
            try:
                log_path.write_text(
                    f"=== STDOUT ===\n{stdout_text}\n=== STDERR ===\n{stderr_text}\n",
                    encoding="utf-8",
                )
            except Exception:
                pass
            db.finish_job(jid, "cancelled", error="任务被取消", log_path=str(log_path))
            raise

        # 落盘：stdout 全文 + stderr 全文，方便事后定位模型/CLI 报错
        try:
            log_path.write_text(
                f"=== STDOUT ===\n{stdout_text}\n=== STDERR ===\n{stderr_text}\n",
                encoding="utf-8",
            )
        except Exception:
            pass

        db.finish_job(jid, status, error=error, log_path=str(log_path))
        if status == "timeout" and i < retries:
            await asyncio.sleep(1 + 2 * i)
            continue
        break

    return jid, stdout_text, stderr_text, status


def parse_claude_result_line(text: str) -> str:
    """从 claude --output-format json 的输出里挑出 type=result 那行取 result 文本。

    逐行只认以 "{" 开头的行，解析失败跳过；命中 type == "result" 且非 is_error
    时取其 result 并停止。没解析到返回空串。
    """
    result = ""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("type") == "result" and not data.get("is_error"):
            result = (data.get("result") or "").strip()
            break
    return result


def parse_done_verdict(text: str) -> tuple[bool, str]:
    """把评委的原始答复解析成 (done, reason)。

    取首个非空行精确匹配 == "DONE" 才算完成（容忍前后空白、大小写），像 "DONE, but…"
    这类带尾巴的一律当 CONTINUE；从第二行起为判断理由，压平空白后截断 200 字。
    空 reason 时按 done 给默认文案。
    """
    lines = (text or "").splitlines()
    first = next((ln.strip() for ln in lines if ln.strip()), "")
    done = first.upper() == "DONE"
    reason = re.sub(r"\s+", " ", " ".join(lines[1:])).strip()[:200]
    if not reason:
        reason = "已达成完成标准" if done else "尚未达成，继续迭代"
    return done, reason
