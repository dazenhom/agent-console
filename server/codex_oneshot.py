"""编排层公共 codex 一次性子进程助手：便宜档机械任务（看板摘要/分诊/目标循环总览/
行摘要/标题/验收）统一走这里调 tcodex，避免每处重复拼命令 + 解析 JSONL。

命令拼法与 arbiter._run_codex_oneshot 一致（无 resume）；复用者：kanban.summarize_progress /
triage.run_triage / goal_summary.summarize_goals / summarizer._codex_oneshot。
goal_verifier.verify 也复用本模块的 run_codex_oneshot_text，其 DONE/CONTINUE 首行解析统一走
job_store.parse_done_verdict（共用实现）。
"""
import json

from . import config
from .job_store import run_logged_oneshot


# codex 推理深度：该值目前不会被传给 codex CLI 命令行。已确认命令行显式传
# `-c model_reasoning_effort=<level>` 会触发 codex CLI (v0.144.6) 的 prewarm-mismatch bug——
# 即使数值与 `~/.tcodex/config.toml` 里的默认值相同，显式传参也会导致本轮 turn 无法复用
# 启动时预热好的 websocket 连接，卡 ~15 秒后现开新连接，而新连接在当前腾讯内网网关环境下
# 必现失败超时。四处调用点（goal_verifier.py / arbiter.py / codex_oneshot.py /
# codex_runner.py）已移除该拼接；实际生效的 effort 档位由 `~/.tcodex/config.toml` 里的
# `model_reasoning_effort` 默认值决定。若未来要复用这个环境变量，必须先确认 codex CLI
# 有不触发该 bug 的传参方式，不要简单恢复 `-c` 拼接。
def build_codex_cmd(model: str, prompt: str) -> list[str]:
    """拼装一次性 tcodex exec 命令（--json 事件流）。"""
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
    return cmd


def parse_codex_jsonl(text: str) -> str:
    """从 codex exec --json 的 JSONL 事件流里拼出最终答案文本。

    收集 item.completed 事件里 item.type == 'agent_message' 的 text 再 join。
    """
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
    return "\n\n".join(messages).strip()


async def run_codex_oneshot_text(kind: str, prompt: str, timeout: float, model: str | None = None,
                                  session_id: str | None = None, schedule_id: str | None = None,
                                  input_summary: str | None = None, cwd: str | None = None
                                  ) -> tuple[str, str, str, str]:
    """一次性调用 tcodex exec，返回 (job_id, 拼接后的答案文本, stderr 前200字, status)。

    status ∈ success/timeout/error；答案文本解析自 JSONL 事件流里 item.completed 的
    agent_message.text（与 arbiter._run_codex_oneshot / codex_runner 解析逻辑一致）。
    """
    m = model or config.CODEX_MODEL
    cmd = build_codex_cmd(model, prompt)

    jid, text, stderr_text, status = await run_logged_oneshot(
        kind, cmd, timeout, session_id=session_id, schedule_id=schedule_id,
        model=m, input_summary=(input_summary or prompt)[:120], cwd=cwd,
    )
    if status in ("timeout", "error"):
        return jid, "", stderr_text[:200], status

    result = parse_codex_jsonl(text)
    return jid, result, stderr_text[:200], "success"
