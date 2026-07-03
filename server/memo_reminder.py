"""备忘录每日提醒。

汇总所有开启提醒的活跃备忘，同一天只推一次（reminded_date 去重）：
  - 写一条 system 消息进秘书会话（留痕，网页可见）
  - 走监控通道广播 memo_reminder（在线端 toast 提示）
  - 若企微启用，再推一条通知卡片

由 scheduler._memo_loop 定点触发；也可经 /api/memos/remind 手动触发（force=True 忽略去重）。
"""
import time
from datetime import date

from . import db, config


async def run_reminder(force: bool = False) -> int:
    today = date.today().isoformat()
    memos = db.list_memos_to_remind()
    if not force:
        memos = [m for m in memos if m.get("reminded_date") != today]
    if not memos:
        return 0

    lines = [f"{i + 1}. {m['content']}" for i, m in enumerate(memos)]
    body = "\n".join(lines)
    title = f"📝 备忘提醒 {today}（{len(memos)} 条）"

    now = time.time()
    for m in memos:
        db.update_memo(m["id"], reminded_date=today, last_reminded_at=now)

    # 写进秘书会话留痕
    sec = db.ensure_secretary_session(config.DEFAULT_WORKDIR)
    db.add_message(sec["id"], "system", {"text": f"{title}\n{body}"})

    # WebSocket 广播（无论企微是否启用，在线端都能收到 toast）
    try:
        from .session_hub import hub
        await hub.broadcast_monitor({
            "type": "memo_reminder",
            "title": title,
            "preview": body[:200],
            "count": len(memos),
        })
    except Exception:
        pass

    # 企微推送（如果启用）
    try:
        from . import wecom_notify
        if getattr(config, "WECOM_ENABLED", False):
            await wecom_notify.notify(
                title=title,
                user_text="",
                reply_text=body[:1200],
                status="success",
            )
    except Exception:
        pass

    return len(memos)
