"""一次性子进程运行记录：起子进程 + 落库 + 落盘日志。

triage.run_triage / goal_verifier.verify / kanban.summarize_progress 三处
一次性调用共用的执行外壳，把原始 stdout/stderr 写进 job_logs/<jid>.log、
往 job_runs 表记一行，避免产出随进程重启或会话结束而丢失。

只负责"跑 + 记 + 落盘"，不解析结果——解析与失败降级逻辑留在各调用方，
调用方拿到 stdout 后按需 db.set_job_output(jid, 结论) 补写结论。

原先超时的便宜档被归因于冷启动/连接建立卡顿，真实根因是子进程继承 stdin：codex exec
在 stdin 为管道时会追加读取并等 EOF，最终被 wait_for 判超时；现以 stdin=DEVNULL 显式
断开。超时仍保留小退避重试，每次尝试独立落一行 job_runs；
所有 oneshot 子进程受模块级信号量封顶，避免堆叠打满并发。

跑子进程外壳之外，还提供调用方共用的两个纯文本解析器：parse_claude_result_line（从
claude --output-format json 的 result 行取文本）与 parse_done_verdict（DONE/CONTINUE
verdict 解析），都不依赖子进程，便于单测与复用。
"""
import asyncio
import json
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


async def _attempt(cmd: list, timeout: float, cwd: str | None) -> tuple[str, str, str, str]:
    """单次跑一次性子进程，返回 (stdout_text, stderr_text, status, error)。"""
    stdout_text = ""
    stderr_text = ""
    status = "success"
    error = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=_child_env(), cwd=cwd,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            # stdin 必须显式断开：继承会让 codex exec 在 stdin 为管道时追加读取并等 EOF，
            # 挂到被判超时（历史 83% 超时的根因）；prompt 一律经 argv 传入。
            stdin=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            stdout_text = out.decode("utf-8", errors="replace") if out else ""
            stderr_text = err.decode("utf-8", errors="replace") if err else ""
        except asyncio.TimeoutError:
            try:
                _kill_process_group(proc, signal.SIGKILL)
                await proc.wait()
            except Exception:
                pass
            status = "timeout"
            error = "子进程超时"
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

        async with _sem:
            stdout_text, stderr_text, status, error = await _attempt(cmd, timeout, cwd)

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
