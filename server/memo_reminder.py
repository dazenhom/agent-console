"""备忘录提醒。

按每条备忘的 remind_mode 判断今天是否该提醒（daily / weekly / monthly /
once / deadline / none），同一天只推一次（reminded_date 去重）：
  - 写一条 system 消息进秘书会话（留痕，网页可见）
  - 走监控通道广播 memo_reminder（在线端 toast 提示）
  - 若企微启用，再推一条通知卡片

deadline 模式过期后自动归档为 done；once 模式推送后自动关闭提醒。
由 scheduler._memo_loop 定点触发；也可经 /api/memos/remind 手动触发（force=True 忽略去重）。
"""
import time
from datetime import date, timedelta

from . import db, config


def _should_remind(m: dict, today) -> bool:
    if not m.get("remind_enabled"):
        return False
    mode = m.get("remind_mode") or "daily"
    at = (m.get("remind_at") or "").strip()
    if mode == "none":
        return False
    if mode == "daily":
        return True
    if mode == "weekly":
        try:
            return today.weekday() == int(at)
        except (ValueError, TypeError):
            return False
    if mode == "monthly":
        try:
            return today.day == int(at)
        except (ValueError, TypeError):
            return False
    if mode == "once":
        return at == today.isoformat()
    if mode == "deadline":
        try:
            deadline = date.fromisoformat(at)
        except (ValueError, TypeError):
            return False
        try:
            days = int(m.get("remind_days_before") or 0)
        except (ValueError, TypeError):
            days = 0
        start = deadline - timedelta(days=days)
        return start <= today <= deadline
    return False


async def run_reminder(force: bool = False) -> int:
    today = date.today()
    today_str = today.isoformat()
    all_active = db.list_memos_to_remind()

    # deadline 过期归档
    remaining = []
    for m in all_active:
        if m.get("remind_mode") == "deadline" and m.get("remind_at"):
            try:
                if date.fromisoformat(m["remind_at"].strip()) < today:
                    db.update_memo(m["id"], status="done")
                    continue
            except (ValueError, TypeError):
                pass
        remaining.append(m)

    # 今天该提醒的
    memos = [m for m in remaining if _should_remind(m, today)]
    if not force:
        memos = [m for m in memos if m.get("reminded_date") != today_str]
    if not memos:
        return 0

    lines = [f"{i + 1}. {m['content']}" for i, m in enumerate(memos)]
    body = "\n".join(lines)
    title = f"📝 备忘提醒 {today_str}（{len(memos)} 条）"

    now = time.time()
    for m in memos:
        db.update_memo(m["id"], reminded_date=today_str, last_reminded_at=now)
        if m.get("remind_mode") == "once":
            db.update_memo(m["id"], remind_mode="none", remind_enabled=0)

    # 写进秘书会话留痕
    from .session_hub import hub
    sec = db.ensure_secretary_session(config.DEFAULT_WORKDIR)
    db.add_message(sec["id"], "system", {"text": f"{title}\n{body}"})

    # WebSocket 广播（无论企微是否启用，在线端都能收到 toast）
    await hub.broadcast_monitor({
        "type": "memo_reminder",
        "title": title,
        "preview": body[:200],
        "count": len(memos),
        "memos": [{"id": m["id"], "content": m["content"]} for m in memos],
    })

    # 企微推送（如果启用）
    try:
        from . import wecom_notify
        if getattr(config, "WECOM_ENABLED", False):
            await wecom_notify.notify_memo(title=title, memos=memos)
    except Exception:
        pass

    return len(memos)
