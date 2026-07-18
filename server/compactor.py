"""/compact 上下文压缩：把整段对话概括成一份摘要。

会话上下文越滚越长会推高每回合 token 与成本，tclaude/tcodex 又没有原生的"截断历史再续"
机制。这里用一次性子进程（复用 job_store.run_logged_oneshot）把整段对话概括成中文摘要，
交给 session_hub.compact 重置会话、把摘要作前缀注入下一条消息续接——绝不碰会话常驻进程，
与 arbiter / summarizer 一脉相承。

必须有兜底：概括子进程失败/超时/无输出时，直接用转录文本尾部截断作摘要，绝不让 compact 失败。
"""
import json
import re

from . import config, db
from .job_store import run_logged_oneshot


def _build_transcript(sid: str) -> str:
    """拼整段对话转录（仅 user/assistant 文本）。超过上限时保留首尾两段、中间省略，
    兼顾"概括整段"（不只取尾部）与 prompt 体积。"""
    parts: list[str] = []
    for m in db.list_messages(sid):
        if m["role"] not in ("user", "assistant"):
            continue
        txt = str((m.get("content") or {}).get("text", "")).strip()
        if not txt:
            continue
        who = "用户" if m["role"] == "user" else "助手"
        parts.append(f"{who}：{txt}")
    transcript = "\n".join(parts)
    limit = config.COMPACT_TRANSCRIPT_CHARS
    if len(transcript) > limit:
        head = transcript[: limit // 2]
        tail = transcript[-(limit // 2):]
        transcript = head + "\n\n…（中间省略）…\n\n" + tail
    return transcript


def _build_prompt(transcript: str) -> str:
    return (
        "下面是一段人与 AI 助手的完整对话记录。请把它压缩成一份简洁但信息完整的中文摘要，"
        "供助手在后续对话中续接上下文。按以下结构组织，缺项可略：\n"
        "1. 背景：这次对话在做什么；\n"
        "2. 已完成事项：已经做了哪些操作、改了哪些东西；\n"
        "3. 关键结论：达成的重要判断与事实；\n"
        "4. 待办：尚未完成、需要继续推进的事；\n"
        "5. 重要文件路径与约定：涉及的路径、命名、配置等需要记住的细节。\n"
        "只输出摘要正文，不要任何前后缀或解释。\n\n"
        f"【对话记录】\n{transcript}"
    )


def _fallback(transcript: str) -> str:
    """兜底摘要：概括失败时直接用转录文本尾部截断。"""
    tail = transcript[-2000:].strip()
    return ("（自动概括不可用，以下为近期对话摘录）\n" + tail) if tail else ""


async def summarize_session(sid: str) -> str:
    """把会话整段对话概括成摘要文本。永不抛异常：子进程失败/超时/无输出时回退转录尾部截断。"""
    transcript = _build_transcript(sid)
    if not transcript:
        return ""
    try:
        cmd = [
            config.CLAUDE_BIN, "--", "-p", _build_prompt(transcript),
            "--model", config.COMPACT_MODEL, "--output-format", "json",
            "--effort", config.CLAUDE_ONESHOT_EFFORT,
        ]
        jid, text, _stderr, status = await run_logged_oneshot(
            "compact", cmd, config.COMPACT_TIMEOUT,
            session_id=sid, model=config.COMPACT_MODEL, input_summary=transcript[:120],
        )
        if status not in ("timeout", "error"):
            # 逐行挑出 type=result 的 JSON 取 result 文本（与 arbiter._run_claude_oneshot 同源）
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
            result = re.sub(r"\n{3,}", "\n\n", result).strip()
            if result:
                db.set_job_output(jid, result[:500])
                return result
    except Exception:
        pass
    return _fallback(transcript)
