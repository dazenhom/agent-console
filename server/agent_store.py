"""Subagent 管理：读写 claude-code 标准的 .claude/agents/<name>.md。

每个 subagent 是一个带 frontmatter（name / description / tools / model）的 .md，
正文是该 agent 的 system prompt。tools 在文件里是逗号分隔单行，model 可省略
（省略表示继承主模型）。无索引文件，比 memory 简单。
"""
import threading
from pathlib import Path

from . import config

_lock = threading.Lock()


def _agents_dir() -> Path:
    return Path(config.AGENTS_DIR)


def _path(name: str) -> Path:
    from .fs_util import safe_join

    if not name.endswith(".md"):
        name = name + ".md"
    return safe_join(_agents_dir(), name)


def _read(p: Path) -> tuple[dict, str]:
    from .fs_util import parse_frontmatter

    return parse_frontmatter(p.read_text(encoding="utf-8"))


def _norm_tools(tools) -> str:
    """tools 接受逗号串或数组，归一成 claude-code 的逗号分隔单行字符串。"""
    if not tools:
        return ""
    if isinstance(tools, str):
        items = [t.strip() for t in tools.split(",")]
    else:
        items = [str(t).strip() for t in tools]
    return ", ".join([t for t in items if t])


def _tools_list(tools_str: str) -> list[str]:
    return [t.strip() for t in (tools_str or "").split(",") if t.strip()]


# ---------- 对外 API ----------
def list_agents() -> list[dict]:
    d = _agents_dir()
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.md")):
        try:
            meta, _ = _read(f)
        except Exception:
            continue
        out.append({
            "name": f.stem,
            "description": meta.get("description", ""),
            "tools": _tools_list(meta.get("tools", "")),
            "model": meta.get("model", ""),
        })
    return out


def get_agent(name: str) -> dict | None:
    p = _path(name)
    if not p.exists():
        return None
    meta, body = _read(p)
    return {
        "name": p.stem,
        "description": meta.get("description", ""),
        "tools": _tools_list(meta.get("tools", "")),
        "model": meta.get("model", ""),
        "prompt": body.strip(),
    }


def _build_meta(name: str, description: str, tools, model: str | None) -> dict:
    meta = {"name": name, "description": description or ""}
    t = _norm_tools(tools)
    if t:
        meta["tools"] = t
    if model:
        meta["model"] = model
    return meta


def create_agent(name: str, description: str, tools, model: str | None, prompt: str) -> dict:
    from .fs_util import dump_frontmatter

    with _lock:
        p = _path(name)
        if p.exists():
            raise FileExistsError("同名 subagent 已存在")
        p.parent.mkdir(parents=True, exist_ok=True)
        meta = _build_meta(p.stem, description, tools, model)
        p.write_text(dump_frontmatter(meta, prompt or ""), encoding="utf-8")
    return {"ok": True, "name": p.stem}


def update_agent(name: str, description, tools, model, prompt) -> dict:
    from .fs_util import dump_frontmatter

    with _lock:
        p = _path(name)
        if not p.exists():
            raise FileNotFoundError("subagent 不存在")
        meta, old_body = _read(p)
        meta["name"] = p.stem
        if description is not None:
            meta["description"] = description
        if tools is not None:
            t = _norm_tools(tools)
            if t:
                meta["tools"] = t
            else:
                meta.pop("tools", None)
        if model is not None:
            if model:
                meta["model"] = model
            else:
                meta.pop("model", None)
        new_body = old_body if prompt is None else prompt
        p.write_text(dump_frontmatter(meta, new_body), encoding="utf-8")
    return {"ok": True}


def delete_agent(name: str) -> dict:
    with _lock:
        p = _path(name)
        if not p.exists():
            raise FileNotFoundError("subagent 不存在")
        p.unlink()
    return {"ok": True}


def agents_json() -> str:
    """把所有 subagent 拼成 tclaude `--agents <json>` 需要的 JSON 字符串。

    headless `-p` 模式下，tclaude 不会自动从 .claude/agents/ 目录加载 subagent，
    必须用 --agents 显式传入（已实测：传入后 init 事件的 agents 列表会出现它们）。
    schema: { "<name>": { "description"(必填), "prompt", "tools":[...], "model" } }
    description 为空的 agent 会被 tclaude 忽略，这里也跳过。
    """
    import json

    d = _agents_dir()
    if not d.exists():
        return ""
    out: dict = {}
    for f in sorted(d.glob("*.md")):
        try:
            meta, body = _read(f)
        except Exception:
            continue
        desc = (meta.get("description") or "").strip()
        if not desc:
            continue  # description 必填，否则 tclaude 不加载
        entry: dict = {"description": desc, "prompt": body.strip()}
        tools = _tools_list(meta.get("tools", ""))
        if tools:
            entry["tools"] = tools
        model = (meta.get("model") or "").strip()
        if model:
            entry["model"] = model
        out[f.stem] = entry
    return json.dumps(out, ensure_ascii=False) if out else ""
