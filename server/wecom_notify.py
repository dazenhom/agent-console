"""企业微信「消息推送」（原群机器人）通知。

回合完成后给指定群/单聊发一条 markdown 摘要卡片。

关键约束：本服务器在内网，**直连出不去公网**，而 webhook 是公网地址
`qyapi.weixin.qq.com`。实测经公司代理 `star-proxy.oa.com:3128` 可达（返回 200）。
所以这里走 **per-request 代理**——只给这一个请求挂代理，绝不设成全局环境变量，
否则会污染 tclaude 子进程（让它去连代理，重蹈 403 覆辙，见 claude_runner._STRIP_ENV_KEYS）。

文档：企业微信「消息推送接口说明」。text/markdown content 最长 4096 字节，必须 utf8。
"""
import asyncio
import json
import time

import httpx

from . import config, db
from .logging_util import get_logger

logger = get_logger(__name__)

_WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send"


def _clip(s: str, max_bytes: int) -> str:
    """按 utf-8 字节截断，超出加省略号。企业微信 content 上限是字节而非字符数。"""
    s = s or ""
    b = s.encode("utf-8")
    if len(b) <= max_bytes:
        return s
    # 留出省略号的空间，截断后从字节回退到合法 utf-8 边界
    cut = b[: max_bytes - 3]
    return cut.decode("utf-8", errors="ignore") + "…"


def build_markdown(*, title: str, user_text: str, reply_text: str,
                   status: str, duration_ms: int | None, num_turns: int | None) -> str:
    """拼一张 markdown 摘要卡片。status: success / error / cancelled。"""
    if status == "error":
        head = '<font color="warning">⚠️ Agent 回合出错</font>'
    elif status == "cancelled":
        head = '<font color="comment">⏹️ Agent 回合已取消</font>'
    elif status == "stuck":
        # 长时间无新输出的进度提醒：回合还没结束，不能标"已完成"，否则用户会误判已经跑完。
        head = '<font color="warning">⏳ Agent 长时间无新输出</font>'
    else:
        head = '<font color="info">✅ Agent 已完成</font>'

    lines = [f"**{head}**"]
    if title:
        lines.append(f"> 会话：{_clip(title, 120)}")

    user_text = (user_text or "").strip()
    if user_text:
        lines.append(f"**指令**：{_clip(user_text, 400)}")

    reply_text = (reply_text or "").strip()
    if reply_text:
        lines.append(f"**回复**：{_clip(reply_text, 1200)}")

    meta = []
    if duration_ms:
        meta.append(f"耗时 {round(duration_ms / 1000, 1)}s")
    if num_turns:
        meta.append(f"{num_turns} 轮")
    if meta:
        lines.append('<font color="comment">' + " · ".join(meta) + "</font>")

    return _clip("\n".join(lines), 4096)


def _post_sync(content: str) -> tuple[bool, str]:
    """同步发送（放到线程里跑，避免阻塞事件循环）。返回 (ok, detail)。"""
    key = config.WECOM_WEBHOOK_KEY
    if not key:
        return False, "未配置 WECOM_WEBHOOK_KEY"
    payload = {"msgtype": "markdown", "markdown": {"content": content}}
    if config.WECOM_CHATID:
        payload["chatid"] = config.WECOM_CHATID
    proxy = config.WECOM_PROXY or None
    retries = max(1, config.WECOM_RETRY)
    detail = ""
    for attempt in range(retries):
        try:
            with httpx.Client(proxy=proxy, timeout=config.WECOM_TIMEOUT) as client:
                r = client.post(_WEBHOOK, params={"key": key}, json=payload)
            data = r.json()
            if data.get("errcode") == 0:
                return True, "ok"
            detail = f"errcode={data.get('errcode')} {data.get('errmsg')}"
            if data.get("errcode") == 44004:
                return False, detail
        except Exception as e:  # 网络/代理/解析异常都不该影响主流程
            detail = f"{type(e).__name__}: {e}"
        if attempt < retries - 1:
            time.sleep(1 if attempt == 0 else 3)
    return False, detail


def _start_notify_job(title: str) -> str | None:
    try:
        return db.start_job(kind="notify", session_id=None, input_summary=title)
    except Exception as e:
        logger.warning("notify job start failed: %s", e)
        return None


def _finish_notify_job(jid: str | None, ok: bool, detail: str) -> None:
    if not jid:
        return
    try:
        db.finish_job(
            jid,
            "success" if ok else "error",
            output=detail if ok else "",
            error=detail if not ok else "",
        )
    except Exception as e:
        logger.warning("notify job finish failed: %s", e)


async def notify(*, title: str, user_text: str, reply_text: str,
                 status: str, duration_ms: int | None = None,
                 num_turns: int | None = None) -> tuple[bool, str]:
    """异步发送企业微信通知。永不抛异常——失败只返回 (False, 原因) 供调用方记日志。"""
    jid = _start_notify_job(title)
    if not config.WECOM_ENABLED or not config.WECOM_WEBHOOK_KEY:
        ok, detail = False, "disabled"
        _finish_notify_job(jid, ok, detail)
        logger.warning("wecom notify failed: %s", detail)
        return ok, detail
    content = build_markdown(
        title=title, user_text=user_text, reply_text=reply_text,
        status=status, duration_ms=duration_ms, num_turns=num_turns,
    )
    try:
        ok, detail = await asyncio.to_thread(_post_sync, content)
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    _finish_notify_job(jid, ok, detail)
    if not ok:
        logger.warning("wecom notify failed: %s", detail)
    return ok, detail


async def notify_memo(*, title: str, memos: list) -> tuple[bool, str]:
    """备忘提醒专用推送，用橙色警示格式，与 agent 任务完成通知视觉区分。永不抛异常。"""
    jid = _start_notify_job(title)
    if not config.WECOM_ENABLED or not config.WECOM_WEBHOOK_KEY:
        ok, detail = False, "disabled"
        _finish_notify_job(jid, ok, detail)
        logger.warning("wecom memo notify failed: %s", detail)
        return ok, detail
    lines = [f"> **{i + 1}.** {m['content']}" for i, m in enumerate(memos)]
    body = "\n".join(lines)
    content = _clip(f'<font color="warning">📌 {title}</font>\n\n{body}', 4096)
    try:
        ok, detail = await asyncio.to_thread(_post_sync, content)
    except Exception as e:
        ok, detail = False, f"{type(e).__name__}: {e}"
    _finish_notify_job(jid, ok, detail)
    if not ok:
        logger.warning("wecom memo notify failed: %s", detail)
    return ok, detail
