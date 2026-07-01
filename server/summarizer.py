"""行摘要：给会话列表生成一句话「这会话刚做了什么」。

主路径用 Haiku 一次性概括（漂亮、像 agent view 的行摘要）；失败/超时回退到启发式截断。
关键：这是**独立的一次性子进程**（不是会话的常驻进程），绝不碰会话上下文。
复用 claude_runner._child_env() 剔除编排态环境变量（否则 403，见同模块注释）。
"""
import asyncio
import json
import re

from . import config
from .claude_runner import _child_env


def _heuristic(user_text: str, reply_text: str, limit: int = 40) -> str:
    """启发式兜底：优先取 Agent 回复首句/前 N 字，没有则取用户指令。"""
    src = (reply_text or "").strip() or (user_text or "").strip()
    if not src:
        return ""
    s = re.sub(r"\s+", " ", src)
    return s[:limit] + ("…" if len(s) > limit else "")


def _build_prompt(user_text: str, reply_text: str) -> str:
    user_text = re.sub(r"\s+", " ", (user_text or "").strip())[:300]
    reply_text = re.sub(r"\s+", " ", (reply_text or "").strip())[:800]
    # 把内容塞进一句话（避免"用户：/助手："被当成对话角色让模型去接话）。
    return (
        "下面是一轮对话的记录，请用不超过 18 个字的中文概括「助手做了什么」，"
        "只输出概括本身，不要引号或任何前后缀。"
        f"记录：用户说『{user_text}』，助手回复『{reply_text}』"
    )


async def _haiku(user_text: str, reply_text: str) -> str:
    """一次性 Haiku 概括。返回干净摘要字符串；任何异常/超时抛出由上层兜底。"""
    cmd = [
        config.CLAUDE_BIN, "--", "-p", _build_prompt(user_text, reply_text),
        "--model", config.CLAUDE_MODEL_FAST, "--output-format", "json",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, env=_child_env(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=config.SUMMARY_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise
    # 输出里可能混有 "Update available..." 之类噪音行，挑出 JSON 那行解析
    text = out.decode("utf-8", errors="replace")
    summary = ""
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("type") == "result" and not data.get("is_error"):
            summary = (data.get("result") or "").strip()
            break
    # 清掉可能的引号/换行，限长
    summary = re.sub(r"\s+", " ", summary).strip().strip('"""')
    return summary[:40]


async def summarize(user_text: str, reply_text: str) -> str:
    """生成一句话摘要：Haiku 优先，失败回退启发式。永不抛异常。"""
    if config.SUMMARY_ENABLED:
        try:
            s = await _haiku(user_text, reply_text)
            if s:
                return s
        except Exception:
            pass
    return _heuristic(user_text, reply_text)


async def _gen_title_haiku(convo: str) -> str:
    """一次性 Haiku 起标题：给一段对话内容取一个不超过 10 字的中文标题。任何异常/超时抛出由上层兜底。"""
    clean = re.sub(r"\s+", " ", convo).strip()[:800]
    prompt = (
        "给下面这段对话内容起一个不超过 40 个字的中文标题，要具体说明做什么事、遇到什么问题，"
        "让人一眼看出对话在干什么，只输出标题本身，不要引号、标点或任何前后缀。"
        f"对话：『{clean}』"
    )
    cmd = [
        config.CLAUDE_BIN, "--", "-p", prompt,
        "--model", config.CLAUDE_MODEL_KANBAN, "--output-format", "json",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, env=_child_env(),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=config.SUMMARY_TIMEOUT)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise
    # 输出里可能混有 "Update available..." 之类噪音行，挑出 JSON 那行解析
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
    # 清掉可能的引号/换行，限长
    result = re.sub(r"\s+", " ", result).strip().strip('"""')[:45]
    return result


async def gen_title(convo: str) -> str:
    """给一段对话内容生成语义标题。关闭/失败返回空串（上层保留截取标题）。永不抛异常。"""
    if not config.TITLE_REFRESH_ENABLED:
        return ""
    try:
        return await _gen_title_haiku(convo) or ""
    except Exception:
        return ""
