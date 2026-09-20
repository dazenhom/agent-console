"""目标循环验收器（checker）：用一次性子进程判断本轮产出是否达成目标。

与 kanban.summarize_progress 一脉相承：独立的一次性子进程，绝不碰会话常驻上下文。

关键设计：verifier 走 codex 引擎（GOAL_VERIFY_MODEL），与生产会话（claude）引擎分离，
保证 maker/checker 独立，避免"自己判自己完成"的乐观偏差。之前用 claude 侧便宜档
CLAUDE_MODEL_KANBAN，在没有 verify_command 时验收员要自己去 workdir 里 Glob/Read
核实产出，复杂真实项目（大型数据管线等）常在旧的 300s 超时内探不完、频繁"验收超时按
未完成继续"（多个目标循环的系统性未完成出口）；换成更强的 gpt-5.6-terra + 600s 缓解。
任何不确定（超时/异常/无输出/首行非 DONE）一律当 CONTINUE——绝不误判完成，宁可多迭代
一轮也不提前收工。命令拼法与 arbiter._run_codex_oneshot 一致（无 resume，JSONL 事件流）。
"""
import json
import re

from . import config, db, spill
from .job_store import run_logged_oneshot


def _build_prompt(goal: str, stop_condition: str, produced: str,
                  cmd_result: str = "", git_diff: str = "", workdir: str | None = None,
                  session_id: str | None = None) -> str:
    # 目标/完成标准是人写的短文本，盲截断即可（超长本身说明目标没写清）。
    # 三段证据走 spill：全文落盘 + 首尾预览 + 文件路径，避免被砍掉的尾部无声消失
    # 而导致假 CONTINUE（见 server/spill.py 开头）。
    goal = (goal or "").strip()[:1500]
    stop_condition = (stop_condition or "").strip()[:800]
    produced = spill.spill_text(
        (produced or "").strip(), config.GOAL_SPILL_PRODUCED_BYTES,
        label="produced", session_id=session_id,
    ) or "（本轮无可读产出）"
    cmd_result = spill.spill_text(
        (cmd_result or "").strip(), config.GOAL_SPILL_CMD_RESULT_BYTES,
        label="cmd_result", session_id=session_id,
    )
    git_diff = spill.spill_text(
        (git_diff or "").strip(), config.GOAL_SPILL_GIT_DIFF_BYTES,
        label="git_diff", session_id=session_id,
    )
    workdir = (workdir or "").strip()
    parts = [
        "你是一个严格的验收员。下面是一个 AI 开发任务的【目标】【完成标准】和【本轮产出片段】。"
        "请判断当前是否已经真正达成完成标准。\n"
        "输出格式：\n"
        "- 第一行只能是 DONE 或 CONTINUE 之一，不带任何其它字符。\n"
        "- 只有在你有充分把握确认完成标准已全部满足时才输出 DONE；"
        "任何不确定、部分完成、或无法从产出中确认的情况，一律输出 CONTINUE。\n"
        "- 从第二行起，简述判断理由；若为 CONTINUE，请给出下一步应该做什么的具体指示。\n"
        "- 下方证据若出现「此处省略 N 字节，完整内容已存于文件：<路径>」，说明该段证据过长已落盘："
        "请直接读取该文件或用 grep 检索，据完整内容判断，不要因为预览被省略就判 CONTINUE。\n\n"
    ]
    if workdir:
        # 隔离 worktree 等场景：明确告知评委去哪个目录核实产出，避免在错误的 cwd 下
        # 找不到文件而产生假阴性
        parts.append(f"【工作目录】任务在目录 {workdir} 下执行，请在该目录下核实产出。\n\n")
    parts.append(
        f"【目标】\n{goal}\n\n"
        f"【完成标准】\n{stop_condition}\n\n"
        f"【本轮产出片段】\n{produced}\n"
    )
    if cmd_result:
        # 客观信号：验收命令的退出码与输出尾部，比自然语言产出更可信，优先据此判定
        parts.append(f"\n【验收命令执行结果（退出码 0 通常表示通过）】\n{cmd_result}\n")
    if git_diff:
        parts.append(f"\n【本轮代码改动（git diff --stat）】\n{git_diff}\n")
    return "".join(parts)


async def verify(goal: str, stop_condition: str, produced: str,
                 session_id: str | None = None, schedule_id: str | None = None,
                 cmd_result: str = "", git_diff: str = "",
                 workdir: str | None = None) -> tuple[bool, str]:
    """返回 (done, reason)。done=True 表示判定达成；任何异常/超时/无输出都返回 (False, 说明)。

    cmd_result / git_diff 为可选的客观上下文（验收命令输出、代码改动 stat），有则拼进 prompt。
    workdir 为任务实际执行目录（如隔离 worktree）：非空时既写进 prompt 告知评委去哪核实，
    也作为子进程 cwd，避免评委在错误目录下核实产出而假阴性；None 时保持原行为。"""
    prompt = _build_prompt(goal, stop_condition, produced, cmd_result=cmd_result,
                           git_diff=git_diff, workdir=workdir, session_id=session_id)
    cmd = [config.CODEX_BIN, "--", "exec", "--json"]
    if config.CODEX_SKIP_GIT_CHECK:
        cmd += ["--skip-git-repo-check"]
    if config.CODEX_BYPASS:
        cmd += ["--dangerously-bypass-approvals-and-sandbox"]
    else:
        cmd += ["-s", config.CODEX_SANDBOX]
    cmd += ["-m", config.GOAL_VERIFY_MODEL]
    cmd += [prompt]

    jid, text, stderr_text, status = await run_logged_oneshot(
        "goal_verify", cmd, config.GOAL_VERIFY_TIMEOUT,
        session_id=session_id, schedule_id=schedule_id,
        model=config.GOAL_VERIFY_MODEL, input_summary=(goal or "")[:120],
        cwd=workdir or None,
    )
    if status == "timeout":
        return False, "验收超时，按未完成继续"
    if status == "error":
        return False, "验收进程异常，按未完成继续"
    # 解析 codex exec --json 的 JSONL 事件流：最终答案来自 item.completed 里
    # item.type == 'agent_message' 的 text（与 arbiter._run_codex_oneshot 一致）
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
    if not result:
        err_text = stderr_text[:200]
        print(f"[goal_verifier] no result, stderr={err_text!r}")
        return False, "验收无输出，按未完成继续"
    lines = result.splitlines()
    # 取首个非空行精确匹配 == "DONE" 才算完成（容忍前后空白）：绝不误判完成，
    # 像 "DONE, but I'm not sure..." 这类带尾巴的一律当 CONTINUE 继续迭代。
    first = next((ln.strip() for ln in lines if ln.strip()), "")
    done = first.upper() == "DONE"
    reason = re.sub(r"\s+", " ", " ".join(lines[1:])).strip()[:200]
    if not reason:
        reason = "已达成完成标准" if done else "尚未达成，继续迭代"
    db.set_job_output(jid, f"done={done}, reason={reason}")
    return done, reason
