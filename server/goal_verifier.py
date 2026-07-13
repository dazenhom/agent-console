"""目标循环验收器（checker）：用一次性子进程判断本轮产出是否达成目标。

与 kanban.summarize_progress 一脉相承：独立的一次性子进程，绝不碰会话常驻上下文；
复用 claude_runner._child_env() 剔除编排态环境变量（否则 403）。

关键设计：verifier 用 CLAUDE_MODEL_KANBAN，与生产会话模型分离（maker/checker 独立），
避免"自己判自己完成"的乐观偏差。任何不确定（超时/异常/无输出/首行非 DONE）一律当
CONTINUE——绝不误判完成，宁可多迭代一轮也不提前收工。
"""
import asyncio
import json
import re

from . import config
from .claude_runner import _child_env


def _build_prompt(goal: str, stop_condition: str, produced: str) -> str:
    goal = (goal or "").strip()[:1500]
    stop_condition = (stop_condition or "").strip()[:800]
    produced = (produced or "").strip()[:3000] or "（本轮无可读产出）"
    return (
        "你是一个严格的验收员。下面是一个 AI 开发任务的【目标】【完成标准】和【本轮产出片段】。"
        "请判断当前是否已经真正达成完成标准。\n"
        "输出格式：\n"
        "- 第一行只能是 DONE 或 CONTINUE 之一，不带任何其它字符。\n"
        "- 只有在你有充分把握确认完成标准已全部满足时才输出 DONE；"
        "任何不确定、部分完成、或无法从产出中确认的情况，一律输出 CONTINUE。\n"
        "- 从第二行起，简述判断理由；若为 CONTINUE，请给出下一步应该做什么的具体指示。\n\n"
        f"【目标】\n{goal}\n\n"
        f"【完成标准】\n{stop_condition}\n\n"
        f"【本轮产出片段】\n{produced}\n"
    )


async def verify(goal: str, stop_condition: str, produced: str) -> tuple[bool, str]:
    """返回 (done, reason)。done=True 表示判定达成；任何异常/超时/无输出都返回 (False, 说明)。"""
    prompt = _build_prompt(goal, stop_condition, produced)
    cmd = [
        config.CLAUDE_BIN, "--", "-p", prompt,
        "--model", config.CLAUDE_MODEL_KANBAN, "--output-format", "json",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, env=_child_env(),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=config.GOAL_VERIFY_TIMEOUT)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            return False, "验收超时，按未完成继续"
    except Exception as e:
        return False, f"验收进程异常（{type(e).__name__}），按未完成继续"
    # 挑出 JSON 那行解析（与 kanban.summarize_progress 一致）
    text = out.decode("utf-8", errors="replace")
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
    if not result:
        err_text = err.decode("utf-8", errors="replace")[:200] if err else ""
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
    return done, reason
