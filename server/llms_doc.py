"""llms.txt / llms-full.txt：给外部 agent 摄取的项目文本索引。

遵循 llms.txt 约定（https://llmstxt.org）——一份精选的 markdown 索引，让 LLM 快速
了解本项目有哪些能力、接口、资源。llms-full.txt 在索引基础上把每个 skill / subagent
的定义全文内联，供一次性拉全。两者都不鉴权（供外部 agent 直接摄取），也刻意不输出
任何密钥（AUTH_TOKEN 等只提"见部署配置"，不打印明文）。

请求量不大，但仍做一个模块级 60 秒 TTL 缓存，避免每次请求都重新扫盘。
"""
import threading
import time
from pathlib import Path

from . import config, skill_store, agent_store

# SQLite 表清单（见 db.py 的 CREATE TABLE）。硬编码即可，加表是低频事件；
# 本项目约定不新建表，这里保持与 db.py 同步。
_DB_TABLES = [
    "sessions", "messages", "tasks", "snippets", "schedules", "todos",
    "todo_sessions", "reports", "queue_items", "memos", "artifacts",
    "job_runs", "arbitrations", "dispatch_subtasks", "goal_iterations",
]

_lock = threading.Lock()
_cache: dict = {}   # key("index"/"full") -> (生成时刻 ts, 文本)
_TTL = 60.0


def _route_lines(app) -> list[str]:
    """内省 app.routes，列出 REST 接口（method + path + docstring 首行）。"""
    from fastapi.routing import APIRoute

    lines: list[str] = []
    for r in app.routes:
        if not isinstance(r, APIRoute):  # 跳过 WebSocket / 静态挂载
            continue
        methods = sorted(m for m in (r.methods or set()) if m not in ("HEAD", "OPTIONS"))
        if not methods:
            continue
        doc = (r.endpoint.__doc__ or "").strip().splitlines()
        summary = doc[0].strip() if doc else ""
        for m in methods:
            lines.append(f"- `{m} {r.path}`" + (f" — {summary}" if summary else ""))
    return lines


def _build_index(app) -> str:
    """精选索引正文（llms.txt 与 llms-full.txt 共用的头部）。"""
    parts: list[str] = []
    parts.append("# Agent Console")
    parts.append("")
    parts.append(
        "真实跑 LLM 推理的多智能体编排台（FastAPI + SQLite + 移动端 SPA）："
        "会话/任务调度、目标循环、子智能体与技能编排。"
    )
    parts.append("")

    parts.append("## 基本信息")
    parts.append(f"- 服务地址：http://{config.HOST}:{config.PORT}")
    parts.append(f"- 项目根目录：{config.BASE_DIR}")
    parts.append(f"- Web 前端目录：{config.WEB_DIR}")
    parts.append(f"- 数据库：{config.DB_PATH}（SQLite）")
    parts.append(
        "- 鉴权：Bearer token（Authorization header）或 query 参数 token；"
        "口令见部署配置，不在此明文列出。"
    )
    parts.append("")

    parts.append("## HTTP 接口")
    parts.extend(_route_lines(app))
    parts.append("")

    parts.append("## Skills（斜杠命令 / 编排流水线）")
    skills = skill_store.list_skills()
    parts.extend([f"- /{s}" for s in skills] or ["（无）"])
    parts.append("")

    parts.append("## Subagents（子智能体）")
    agents = agent_store.list_agents()
    if agents:
        for a in agents:
            desc = a.get("description") or ""
            parts.append(f"- {a['name']}" + (f" — {desc}" if desc else ""))
    else:
        parts.append("（无）")
    parts.append("")

    parts.append("## 数据库表")
    parts.append("、".join(_DB_TABLES))
    parts.append("")

    return "\n".join(parts)


def _skill_files() -> list[tuple[str, str]]:
    """[(skill名, SKILL.md 全文)]，按名排序。"""
    d = Path(config.SKILLS_DIR)
    out: list[tuple[str, str]] = []
    if not d.exists():
        return out
    for name in skill_store.list_skills():
        try:
            out.append((name, (d / name / "SKILL.md").read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def _agent_files() -> list[tuple[str, str]]:
    """[(subagent名, .md 全文)]，按名排序。"""
    d = Path(config.AGENTS_DIR)
    out: list[tuple[str, str]] = []
    if not d.exists():
        return out
    for f in sorted(d.glob("*.md")):
        try:
            out.append((f.stem, f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return out


def build_llms_txt(app) -> str:
    """精选文本索引，末尾指向 /llms-full.txt。"""
    with _lock:
        now = time.time()
        hit = _cache.get("index")
        if hit and now - hit[0] < _TTL:
            return hit[1]
        text = _build_index(app) + "\n完整文档见 /llms-full.txt\n"
        _cache["index"] = (now, text)
        return text


def build_llms_full_txt(app) -> str:
    """索引 + 每个 skill / subagent 定义全文，拼成单一 markdown。"""
    with _lock:
        now = time.time()
        hit = _cache.get("full")
        if hit and now - hit[0] < _TTL:
            return hit[1]
        parts = [_build_index(app)]
        parts.append("\n---\n\n# Skills 全文\n")
        for name, text in _skill_files():
            parts.append(f"\n## skill: {name}\n\n```markdown\n{text}\n```\n")
        parts.append("\n---\n\n# Subagents 全文\n")
        for name, text in _agent_files():
            parts.append(f"\n## subagent: {name}\n\n```markdown\n{text}\n```\n")
        text = "".join(parts)
        _cache["full"] = (now, text)
        return text
