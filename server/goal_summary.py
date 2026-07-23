"""目标循环总览总结：把所有 goal 循环的完成情况汇总，用一次性子进程生成一段中文小结。

与 kanban.summarize_progress / goal_verifier.verify 一脉相承：独立的一次性子进程，
绝不碰会话常驻上下文；走 codex 引擎的便宜档 CHEAP_MODEL（见 codex_oneshot），点按钮才触发、
不做缓存（KISS）。
"""
import re

from . import config, db
from .codex_oneshot import run_codex_oneshot_text


def _classify(g: dict) -> str:
    """按 enabled×goal_status 归类：进行中 / 已完成 / 未完成（耗尽）/ 未完成（暂停）。"""
    status = g.get("goal_status") or "running"
    enabled = bool(g.get("enabled"))
    if status == "done":
        return "已完成"
    if status == "exhausted":
        return "未完成（耗尽）"
    if enabled and status in ("running", "producing", "verifying"):
        return "进行中"
    return "未完成（暂停）"


def _title(g: dict) -> str:
    """会话标题优先，prompt 兜底；压平换行截 50 字。"""
    sess = db.get_session(g.get("session_id") or "")
    raw = (sess.get("title") if sess else "") or g.get("prompt") or ""
    return re.sub(r"\s+", " ", raw).strip()[:50]


def _last_reason(schedule_id: str) -> str:
    """末轮验收反馈：过滤 verdict 非空的轮（去掉进程重启残留的纯 producing 僵尸行），
    取 iter_no 最大那条的 feedback，截 200 字。"""
    its = db.list_goal_iterations(schedule_id)  # 升序
    scored = [it for it in its if (it.get("verdict") or "").strip()]
    if not scored:
        return ""
    last = max(scored, key=lambda it: it.get("iter_no") or 0)
    return re.sub(r"\s+", " ", (last.get("feedback") or "").strip())[:200]


def _build_prompt(goals: list[dict]) -> str:
    total = len(goals)
    done = sum(1 for g in goals if _classify(g) == "已完成")
    lines = []
    for g in goals:
        group = _classify(g)
        iter_count = int(g.get("iter_count") or 0)
        max_iter = int(g.get("max_iterations") or config.GOAL_MAX_ITERATIONS)
        reason = _last_reason(g["id"]) or "无反馈"
        lines.append(f"- [{group}] {_title(g)}（{iter_count}/{max_iter} 轮）：{reason}")
    body = "\n".join(lines)[:6000]
    return (
        f"下面是所有 AI 目标循环任务的完成情况（共 {total} 个，已完成 {done} 个）。"
        "请生成一段中文小结，只输出三段，段与段之间空一行，不要 markdown 标题、不要序号列表、不要客套话：\n"
        "第一段：一句话概括整体完成情况，包含完成率（已完成/总数）。\n"
        "第二段：归类未完成任务的主要原因（把相似原因合并成几类，别逐条罗列）。\n"
        "第三段：建议优先继续推进哪 1-3 个任务，并简述理由。\n\n"
        f"{body}"
    )


async def summarize_goals() -> tuple[bool, str]:
    """汇总所有目标循环并生成小结。返回 (ok, summary)。任何超时/异常都返回 (False, 原因)。"""
    goals = db.list_goal_loops(limit=100)
    if not goals:
        return True, "当前还没有任何目标循环。"
    jid, result, stderr_text, status = await run_codex_oneshot_text(
        "goal_summary", _build_prompt(goals), config.SUMMARY_TIMEOUT, model=config.CHEAP_MODEL,
        input_summary=f"目标循环总览（{len(goals)} 个）",
    )
    if status == "timeout":
        return False, "总结生成超时"
    if status == "error":
        return False, "总结生成进程异常"
    if not result:
        print(f"[goal_summary] no result, stderr={stderr_text!r}")
        return False, "总结无输出"
    db.set_job_output(jid, result[:200])
    return True, result
