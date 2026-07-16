"""一次性子进程运行记录：起子进程 + 落库 + 落盘日志。

triage.run_triage / goal_verifier.verify / kanban.summarize_progress 三处
一次性调用共用的执行外壳，把原始 stdout/stderr 写进 job_logs/<jid>.log、
往 job_runs 表记一行，避免产出随进程重启或会话结束而丢失。

只负责"跑 + 记 + 落盘"，不解析结果——解析与失败降级逻辑留在各调用方，
调用方拿到 stdout 后按需 db.set_job_output(jid, 结论) 补写结论。
"""
import asyncio
from pathlib import Path

from . import db
from .claude_runner import _child_env


def _log_dir() -> Path:
    d = Path(db.config.DB_PATH).parent / "job_logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def run_logged_oneshot(kind: str, cmd: list, timeout: float, *,
                             session_id: str | None = None, schedule_id: str | None = None,
                             model: str = "", input_summary: str = "") -> tuple[str, str, str, str]:
    """跑一次性子进程并落库落盘，返回 (jid, stdout_text, stderr_text, status)。

    status 取 success/timeout/error 之一；正常结束默认 success，若调用方解析后
    认定失败可通过 db.finish_job 覆盖。stdout+stderr 全文写进 job_logs/<jid>.log。
    """
    jid = db.start_job(kind, session_id=session_id, schedule_id=schedule_id,
                       model=model, input_summary=input_summary)
    log_path = _log_dir() / f"{jid}.log"

    stdout_text = ""
    stderr_text = ""
    status = "success"
    error = ""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=_child_env(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            stdout_text = out.decode("utf-8", errors="replace") if out else ""
            stderr_text = err.decode("utf-8", errors="replace") if err else ""
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            status = "timeout"
            error = "子进程超时"
    except Exception as e:
        status = "error"
        error = f"{type(e).__name__}: {e}"

    # 落盘：stdout 全文 + stderr 全文，方便事后定位模型/CLI 报错
    try:
        log_path.write_text(
            f"=== STDOUT ===\n{stdout_text}\n=== STDERR ===\n{stderr_text}\n",
            encoding="utf-8",
        )
    except Exception:
        pass

    db.finish_job(jid, status, error=error, log_path=str(log_path))
    return jid, stdout_text, stderr_text, status
