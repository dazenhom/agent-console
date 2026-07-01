"""Skill 展开：把网页对话里输入的 `/<skill名> [附加内容]` 展开成 SKILL.md 正文。

claude-code 的 skill（.claude/skills/<name>/SKILL.md）在 headless `-p` 模式下不会
被当作 `/命令` 暴露，直接发 `/devops-team` 会被 tclaude 当未知斜杠命令拒绝。
这里在消息进入 hub 前做一层预处理：识别 `/<name>`，读对应 SKILL.md 正文注入为
prompt，附加内容拼到末尾。单一来源就是 SKILL.md，改流水线只需改它。
"""
import re
import threading
from pathlib import Path

from . import config

_lock = threading.Lock()


def _skills_dir() -> Path:
    return Path(config.SKILLS_DIR)


def _skill_body(name: str) -> str | None:
    """读 .claude/skills/<name>/SKILL.md，返回去掉 frontmatter 和 <command-args> 占位的正文。"""
    from .fs_util import safe_join, parse_frontmatter

    try:
        p = safe_join(_skills_dir(), name) / "SKILL.md"
    except Exception:
        return None  # 非法 name（路径穿越等）
    if not p.exists():
        return None
    try:
        _, body = parse_frontmatter(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    # 去掉 <command-args>...</command-args> 占位段（skill 模板里给 CLI 用的）
    body = re.sub(r"<command-args>.*?</command-args>", "", body, flags=re.S)
    # 去掉末尾的 "## 用法" 整段（skill 模板的使用说明，对终端用户无意义）
    body = re.sub(r"\n##\s*用法.*$", "", body, flags=re.S)
    return body.strip()


def list_skills() -> list[str]:
    d = _skills_dir()
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if (p / "SKILL.md").exists())


# 形如 `/devops-team 给登录加限流` 或单独 `/devops-team`。名字限定字母数字-_。
_SLASH_RE = re.compile(r"^/([A-Za-z0-9_\-]+)\s*(.*)$", re.S)


def expand(user_text: str) -> tuple[str, str | None]:
    """把可能的 `/<skill> [args]` 展开成完整 prompt。

    返回 (展开后的文本, 错误消息)。
    - 非斜杠开头：原样返回 (user_text, None)。
    - 已知 skill：返回 (SKILL正文 + 附加内容, None)。
    - 未知 skill：返回 (user_text, 友好错误提示)，由调用方决定是否拦截。
    """
    with _lock:
        m = _SLASH_RE.match(user_text)
        if not m:
            return user_text, None
        name, rest = m.group(1), (m.group(2) or "").strip()
        body = _skill_body(name)
        if body is None:
            avail = "、".join("/" + s for s in list_skills()) or "（无）"
            return user_text, f"未知的 skill：/{name}。可用的有：{avail}"
        prompt = body if not rest else f"{body}\n\n{rest}"
        return prompt, None
