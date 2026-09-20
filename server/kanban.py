"""看板进展总结：读取会话 jsonl，用一次性子进程概括开发进展。

与 summarizer 一脉相承：独立的一次性子进程，绝不碰会话的常驻上下文；
走 codex 引擎的便宜档 CHEAP_MODEL（见 codex_oneshot.run_codex_oneshot_text）。
每个 todo 卡片带 mtime 缓存：jsonl 没变就复用上次的摘要，避免重复烧模型。
"""
import json
import re
import time
from pathlib import Path

from . import config, db
from .codex_oneshot import run_codex_oneshot_text


def _slug(path: str) -> str:
    """workdir → tclaude 项目目录 slug（与 session_import._slug 一致：/ _ . → -）。"""
    return re.sub(r"[/_.]", "-", path)


def _session_jsonl_path(claude_session_id: str, workdir: str) -> Path | None:
    slug = _slug(workdir or config.DEFAULT_WORKDIR)
    p = Path(config.TCLAUDE_HOME) / "projects" / slug / (claude_session_id + ".jsonl")
    return p if p.exists() else None


def _extract_recent_text(jsonl_path: Path, max_chars: int = 3000, *,
                         tail_lines: int = 40, assistant_chars: int = 300,
                         user_chars: int = 200) -> str:
    """读 jsonl 最后若干条 user/assistant 文本消息，拼成上下文。

    四个上限的默认值面向"看板进展摘要"这类只需概括最近在干什么的场景，保持原行为。
    验收取证要的是**完整证据**而非概括：盲截断掉的尾部（跑通的测试结论、报错原因）
    恰恰是判定达成与否的依据，砍掉就只能判 CONTINUE。所以验收路径传大得多的上限、
    把预算交给 spill 落盘管（见 _extract_verify_text / server/spill.py）。
    """
    lines = []
    try:
        with jsonl_path.open(encoding="utf-8", errors="replace") as fh:
            raw = fh.readlines()
    except OSError:
        return ""
    for line in raw[-tail_lines:]:
        try:
            obj = json.loads(line.strip())
        except Exception:
            continue
        role = obj.get("type") or obj.get("role", "")
        content = (obj.get("message") or {}).get("content", "")
        if role == "assistant":
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        lines.append(f"[Assistant]: {block['text'][:assistant_chars]}")
            elif isinstance(content, str):
                lines.append(f"[Assistant]: {content[:assistant_chars]}")
        elif role == "user":
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        lines.append(f"[User]: {block['text'][:user_chars]}")
            elif isinstance(content, str):
                lines.append(f"[User]: {content[:user_chars]}")
    text = "\n".join(lines)
    return text[-max_chars:] if len(text) > max_chars else text


async def summarize_progress(context_text: str, session_id: str | None = None) -> str:
    """用一次性子进程概括进展。返回干净摘要；任何异常兜底成友好提示。"""
    prompt = (
        "以下是一个 AI 开发任务会话的最新对话片段。"
        "请用 80-120 字的中文，给出一段结构化的进展摘要，依次覆盖以下要点："
        "① 当前进展阶段（如「正在实现前端组件」）；② 已经完成了什么；"
        "③ 下一步计划做什么；④ 是否有阻塞或问题（没有则说明进展顺利）。"
        "语言要具体、贴合上下文，不要空泛套话。"
        "只输出摘要正文本身，不要引号、标题或任何前后缀。\n\n" + context_text
    )
    jid, text, stderr_text, status = await run_codex_oneshot_text(
        "kanban_progress", prompt, config.SUMMARY_TIMEOUT, model=config.CHEAP_MODEL,
        session_id=session_id, input_summary="看板进展摘要",
    )
    if status == "timeout":
        return "进展获取超时"
    if status == "error":
        return "进展获取失败"
    result = re.sub(r"\s+", " ", text).strip().strip('"“”')
    if not result:
        # 解析不到结果：把子进程 stderr 前 200 字打出来，方便定位模型/CLI 报错
        print(f"[kanban] summarize_progress: no result, stderr={stderr_text!r}")
        return "暂无进展信息"
    result = result[:160]
    db.set_job_output(jid, result)
    return result


async def refresh_todo_progress(tid: str, force: bool = False) -> dict:
    """刷新单个 todo 卡片的进展摘要，带 jsonl mtime 缓存。"""
    rows = db._query(
        "SELECT id, title, session_id, progress, progress_at, progress_src_mtime FROM todos WHERE id=?",
        (tid,),
    )
    if not rows:
        return {"ok": False, "reason": "todo not found"}
    todo = dict(rows[0])
    session_id = todo.get("session_id")
    if not session_id:
        return {"ok": False, "reason": "该任务未关联 Agent 会话"}

    sess = db.get_session(session_id)
    if not sess or not sess.get("claude_session_id"):
        return {"ok": False, "reason": "关联会话无 claude_session_id"}

    jsonl_path = _session_jsonl_path(sess["claude_session_id"], sess.get("workdir"))
    if not jsonl_path:
        return {"ok": False, "reason": "会话记录文件不存在"}

    # mtime 缓存：文件没变（且已有摘要）就直接复用，不再烧模型
    current_mtime = jsonl_path.stat().st_mtime
    prev_mtime = todo.get("progress_src_mtime") or 0
    if not force and prev_mtime and abs(current_mtime - prev_mtime) < 1.0 and todo.get("progress"):
        return {"ok": True, "progress": todo["progress"], "progress_at": todo.get("progress_at"), "cached": True}

    context = _extract_recent_text(jsonl_path)
    if not context.strip():
        return {"ok": False, "reason": "会话内容为空"}

    new_progress = await summarize_progress(context, session_id=session_id)
    db.set_todo_progress(tid, new_progress, current_mtime)
    return {"ok": True, "progress": new_progress, "progress_at": time.time(), "cached": False}
