"""编排层公共 codex 一次性子进程助手：便宜档机械任务（看板摘要/分诊/目标循环总览/
行摘要/标题/验收）统一走这里调 tcodex，避免每处重复拼命令 + 解析 JSONL。

命令拼法与 arbiter._run_codex_oneshot 一致（无 resume）；复用者：kanban.summarize_progress /
triage.run_triage / goal_summary.summarize_goals / summarizer._codex_oneshot。
goal_verifier.verify 有自己的 DONE/CONTINUE 首行解析，未复用此helper（保留独立实现）。
"""
import json

from . import config
from .job_store import run_logged_oneshot


async def run_codex_oneshot_text(kind: str, prompt: str, timeout: float, model: str | None = None,
                                  session_id: str | None = None, schedule_id: str | None = None,
                                  input_summary: str | None = None, cwd: str | None = None
                                  ) -> tuple[str, str, str, str]:
    """一次性调用 tcodex exec，返回 (job_id, 拼接后的答案文本, stderr 前200字, status)。

    status ∈ success/timeout/error；答案文本解析自 JSONL 事件流里 item.completed 的
    agent_message.text（与 arbiter._run_codex_oneshot / codex_runner 解析逻辑一致）。
    """
    m = model or config.CODEX_MODEL
    cmd = [config.CODEX_BIN, "--", "exec", "--json"]
    if config.CODEX_SKIP_GIT_CHECK:
        cmd += ["--skip-git-repo-check"]
    if config.CODEX_BYPASS:
        cmd += ["--dangerously-bypass-approvals-and-sandbox"]
    else:
        cmd += ["-s", config.CODEX_SANDBOX]
    if m:
        cmd += ["-m", m]
    cmd += [prompt]

    jid, text, stderr_text, status = await run_logged_oneshot(
        kind, cmd, timeout, session_id=session_id, schedule_id=schedule_id,
        model=m, input_summary=(input_summary or prompt)[:120], cwd=cwd,
    )
    if status in ("timeout", "error"):
        return jid, "", stderr_text[:200], status

    messages: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if evt.get("type") == "item.completed":
            item = evt.get("item") or {}
            if item.get("type") == "agent_message":
                txt = (item.get("text") or "").strip()
                if txt:
                    messages.append(txt)
    result = "\n\n".join(messages).strip()
    return jid, result, stderr_text[:200], "success"
