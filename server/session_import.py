"""接续电脑终端交互模式聊过的会话。

用户在服务器终端跑交互式 tclaude 的会话存在
  <TCLAUDE_HOME>/projects/<slug>/<claude_session_id>.jsonl
文件名就是 claude session_id。Console 后端用 --resume <id> 续接，所以"接续"只需：
  建一个 Console 会话 + 绑定 claude_session_id，首次发消息就 resume 接上完整上下文。

只扫 DEFAULT_WORKDIR 对应的项目（用户主工作目录），不混入 /tmp、agent-console 等。
"""
import json
import re
from pathlib import Path

from . import config, db


def _slug(path: str) -> str:
    """workdir → tclaude 项目目录 slug（与 memory_store 一致：/ _ . → -）。"""
    return re.sub(r"[/_.]", "-", path)


def _project_dir() -> Path:
    return Path(config.TCLAUDE_HOME) / "projects" / _slug(config.DEFAULT_WORKDIR)


def _first_user_text(lines: list[str]) -> str:
    """找第一条用户文本消息做标题。"""
    for ln in lines[:40]:
        try:
            d = json.loads(ln)
        except Exception:
            continue
        if d.get("type") != "user":
            continue
        c = (d.get("message") or {}).get("content")
        if isinstance(c, list):
            c = " ".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")
        if isinstance(c, str) and c.strip():
            return c.strip()
    return ""


def list_importable() -> list[dict]:
    """列出可接续的终端会话，按最近修改排序，标注已接续的。"""
    d = _project_dir()
    if not d.exists():
        return []
    imported = {s.get("claude_session_id") for s in db.list_sessions(include_archived=True) if s.get("claude_session_id")}
    out = []
    for f in sorted(d.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        sid = f.stem
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        if len(lines) < 2:
            continue
        title = _first_user_text(lines) or "（无标题会话）"
        out.append({
            "claude_session_id": sid,
            "title": title[:60],
            "events": len(lines),
            "mtime": f.stat().st_mtime,
            "imported": sid in imported,
        })
    return out


def import_sessions(claude_session_ids: list[str]) -> dict:
    """把指定终端会话接续进 Console（建会话 + 绑 claude_session_id）。返回导入数 + 首个新会话 id。"""
    d = _project_dir()
    avail = {f.stem for f in d.glob("*.jsonl")} if d.exists() else set()
    imported = {s.get("claude_session_id") for s in db.list_sessions(include_archived=True) if s.get("claude_session_id")}
    n = 0
    first_new = None
    for sid in claude_session_ids:
        if sid not in avail or sid in imported:
            continue
        f = d / (sid + ".jsonl")
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            continue
        title = _first_user_text(lines) or "接续的会话"
        sess = db.create_session("💻 " + title[:50], config.DEFAULT_WORKDIR, None)
        db.update_session(sess["id"], claude_session_id=sid)
        if first_new is None:
            first_new = sess["id"]
        n += 1
    return {"ok": True, "imported": n, "session_id": first_new}


# 模块级缓存：claude_session_id -> (jsonl_mtime, ai_title)
_ai_title_cache: dict = {}


def latest_ai_title(claude_session_id, workdir=None):
    """读会话 JSONL，返回最后一条 ai-title 的 aiTitle；无则返回空字符串。按文件 mtime 缓存。"""
    if not claude_session_id:
        return ""
    slug = _slug(workdir or config.DEFAULT_WORKDIR)
    f = Path(config.TCLAUDE_HOME) / "projects" / slug / (claude_session_id + ".jsonl")
    try:
        mtime = f.stat().st_mtime
    except OSError:
        return ""
    cached = _ai_title_cache.get(claude_session_id)
    if cached and cached[0] == mtime:
        return cached[1]
    title = ""
    try:
        with f.open(encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                if '"ai-title"' not in ln:
                    continue
                try:
                    d = json.loads(ln)
                except Exception:
                    continue
                if d.get("type") == "ai-title" and d.get("aiTitle"):
                    title = str(d["aiTitle"]).strip()
    except OSError:
        return cached[1] if cached else ""
    _ai_title_cache[claude_session_id] = (mtime, title)
    return title
