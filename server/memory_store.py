"""Memory 管理：读写 tclaude 的 project memory 文件。

tclaude 把记忆按 cwd（workdir）分项目存到
  <TCLAUDE_HOME>/projects/<slug>/memory/<name>.md
其中 slug = workdir 路径里的 `/ _ .` 全替换成 `-`（与 tclaude 一致）。
每个 memory 是一个带 YAML frontmatter 的 .md，另有一个 MEMORY.md 索引。

本模块固定作用域到 DEFAULT_WORKDIR 对应的那个 memory 目录（按计划只管默认项目）。
每次 CRUD 后全量重建 MEMORY.md，保证索引与磁盘文件一致、且幂等。
"""
import re
import threading
from pathlib import Path

from . import config

_lock = threading.Lock()


def _slug(path: str) -> str:
    """workdir 路径 → tclaude 项目目录 slug。"""
    return re.sub(r"[/_.]", "-", path)


def _memory_dir() -> Path:
    return Path(config.TCLAUDE_HOME) / "projects" / _slug(config.DEFAULT_WORKDIR) / "memory"


def _path(name: str) -> Path:
    """name（带不带 .md 都行）→ 安全的 .md 路径。"""
    from .fs_util import safe_join

    if not name.endswith(".md"):
        name = name + ".md"
    return safe_join(_memory_dir(), name)


def _read(p: Path) -> tuple[dict, str]:
    from .fs_util import parse_frontmatter

    return parse_frontmatter(p.read_text(encoding="utf-8"))


def _reindex() -> None:
    """扫描目录里所有 memory 文件，全量重建 MEMORY.md。须在锁内调用。"""
    d = _memory_dir()
    d.mkdir(parents=True, exist_ok=True)
    entries = []
    for f in sorted(d.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        try:
            meta, _ = _read(f)
        except Exception:
            continue
        title = meta.get("name") or f.stem
        hook = (meta.get("description") or "").split("\n")[0].strip()[:120]
        suffix = f" — {hook}" if hook else ""
        entries.append(f"- [{title}]({f.name}){suffix}")
    body = "# Memory Index\n\n" + ("\n".join(entries) + "\n" if entries else "")
    (d / "MEMORY.md").write_text(body, encoding="utf-8")


# ---------- 对外 API ----------
def list_memories() -> list[dict]:
    d = _memory_dir()
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.md")):
        if f.name == "MEMORY.md":
            continue
        try:
            meta, body = _read(f)
        except Exception:
            continue
        out.append({
            "name": f.stem,
            "description": meta.get("description", ""),
            "type": (meta.get("metadata") or {}).get("type", ""),
            "preview": body.strip()[:160],
        })
    return out


def get_memory(name: str) -> dict | None:
    p = _path(name)
    if not p.exists():
        return None
    meta, body = _read(p)
    return {
        "name": p.stem,
        "description": meta.get("description", ""),
        "metadata": meta.get("metadata") or {},
        "body": body.strip(),
    }


def create_memory(name: str, description: str, body: str, mtype: str = "project") -> dict:
    from .fs_util import dump_frontmatter

    with _lock:
        p = _path(name)
        if p.exists():
            raise FileExistsError("同名 memory 已存在")
        p.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "name": p.stem,
            "description": description or "",
            "metadata": {"node_type": "memory", "type": mtype or "project"},
        }
        p.write_text(dump_frontmatter(meta, body or ""), encoding="utf-8")
        _reindex()
    return {"ok": True, "name": p.stem}


def update_memory(name: str, description: str | None, body: str | None) -> dict:
    """更新：读旧 meta 合并，不擦 tclaude 写入的字段（originSessionId 等）。"""
    from .fs_util import dump_frontmatter

    with _lock:
        p = _path(name)
        if not p.exists():
            raise FileNotFoundError("memory 不存在")
        meta, old_body = _read(p)
        meta.setdefault("name", p.stem)
        if description is not None:
            meta["description"] = description
        meta.setdefault("metadata", {})
        new_body = old_body if body is None else body
        p.write_text(dump_frontmatter(meta, new_body), encoding="utf-8")
        _reindex()
    return {"ok": True}


def delete_memory(name: str) -> dict:
    with _lock:
        p = _path(name)
        if not p.exists():
            raise FileNotFoundError("memory 不存在")
        p.unlink()
        _reindex()
    return {"ok": True}
