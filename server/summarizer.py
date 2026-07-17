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
        "--effort", config.CLAUDE_ONESHOT_EFFORT,
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


async def _gen_title_haiku(convo: str, current_title: str = "") -> str:
    """一次性 Haiku 起标题：给一段对话内容取一个不超过 10 字的中文标题。任何异常/超时抛出由上层兜底。

    current_title 非空时改判「是否需要换标题」：话题仍延续则回固定标记 KEEP（上层转成空串保留原标题），
    仅当话题明显漂移或原标题不准时才输出新标题。"""
    clean = re.sub(r"\s+", " ", convo).strip()[:800]
    if not current_title:
        prompt = (
            "给下面这段对话内容起一个不超过 40 个字的中文标题，要具体说明做什么事、遇到什么问题，"
            "让人一眼看出对话在干什么，只输出标题本身，不要引号、标点或任何前后缀。"
            f"对话：『{clean}』"
        )
    else:
        prompt = (
            "判断是否需要给一段对话换标题。"
            f"当前标题：『{current_title}』；最近对话：『{clean}』。"
            "若最近对话仍是当前标题所描述任务的延续、且当前标题已能准确概括，"
            "只输出标记 KEEP（不要复述当前标题，不要加任何其他字）。"
            "仅当话题已明显偏离到别的事情，或当前标题明显不准确、过于笼统时，"
            "才输出一个不超过 40 个字、反映最近在做什么的新标题。不要仅因为想换个说法就改标题。"
            "只输出标记或标题本身，不要引号、标点或任何前后缀。"
        )
    cmd = [
        config.CLAUDE_BIN, "--", "-p", prompt,
        "--model", config.CLAUDE_MODEL_KANBAN, "--output-format", "json",
        "--effort", config.CLAUDE_ONESHOT_EFFORT,
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
    # 模型判定话题仍延续时回固定标记 KEEP，转成空串让上层保留原标题。
    # 先剥掉常见引号/书名号/结尾标点噪音再比对，兼容『KEEP』、KEEP。等变体（仅用于判断，不污染返回值）。
    if re.sub(r"[\"'『』「」。.,!！]", "", result).strip().upper() == "KEEP":
        return ""
    return result


async def gen_title(convo: str, current_title: str = "") -> str:
    """给一段对话内容生成语义标题。关闭/失败返回空串（上层保留截取标题）。永不抛异常。

    current_title 非空时交给模型判断是否需要换标题，无需换则返回空串。"""
    if not config.TITLE_REFRESH_ENABLED:
        return ""
    try:
        return await _gen_title_haiku(convo, current_title) or ""
    except Exception:
        return ""
