"""秘书 Agent：定时日报生成与推送。"""
import asyncio
import json
import time
from datetime import date, datetime, timedelta

from . import config, db

_running_lock = asyncio.Lock()


def gather_day_data(day: date) -> dict:
    start_ts = datetime(day.year, day.month, day.day, 0, 0, 0).timestamp()
    end_ts = datetime(day.year, day.month, day.day, 23, 59, 59).timestamp()

    sessions = db._query(
        "SELECT * FROM sessions WHERE updated_at BETWEEN ? AND ? AND (is_secretary=0 OR is_secretary IS NULL)",
        (start_ts, end_ts),
    )
    tasks = db._query(
        "SELECT * FROM tasks WHERE started_at BETWEEN ? AND ?",
        (start_ts, end_ts),
    )

    session_briefs = []
    for s in sessions:
        msgs = db._query(
            "SELECT content FROM messages WHERE session_id=? AND role='user' "
            "AND created_at BETWEEN ? AND ? ORDER BY created_at ASC LIMIT 3",
            (s["id"], start_ts, end_ts),
        )
        texts = []
        for m in msgs:
            try:
                c = json.loads(m["content"]) if isinstance(m["content"], str) else m["content"]
                t = c.get("text") or c.get("content") or ""
                if t:
                    texts.append(str(t)[:200])
            except Exception:
                pass
        session_briefs.append({
            "id": s["id"],
            "title": s["title"],
            "workdir": s["workdir"] if "workdir" in s.keys() else "",
            "status": s["status"],
            "msgs": texts,
        })

    return {
        "date": day.isoformat(),
        "sessions": session_briefs,
        "tasks": [dict(t) for t in tasks],
        "total_cost": sum((t["cost_usd"] or 0) for t in tasks),
        "total_duration_ms": sum((t["duration_ms"] or 0) for t in tasks),
    }


def _format_sessions(sessions: list) -> str:
    if not sessions:
        return "  （今日无会话活动）"
    lines = []
    for s in sessions:
        brief = "; ".join(s["msgs"])[:200] if s["msgs"] else "（无用户指令记录）"
        lines.append(f"  - 【{s['title']}】{brief}")
    return "\n".join(lines)


def _format_todos(todos: list) -> str:
    if not todos:
        return "  （暂无待办）"
    lines = []
    for t in todos:
        pri = "⚡高优" if t.get("priority") else "普通"
        desc = t.get("description") or "（无详情）"
        lines.append(f"  - [{pri}] {t['title']}: {desc}")
    return "\n".join(lines)


def build_evening_prompt(today_data: dict, todos: list) -> str:
    success_cnt = len([t for t in today_data["tasks"] if t.get("status") == "success"])
    error_cnt = len([t for t in today_data["tasks"] if t.get("status") == "error"])
    return f"""你是一个工作秘书。请根据以下今日数据生成简洁日报（适合手机阅读，总长不超过500字）。

## 今日工作数据（{today_data['date']}）

### 会话活动
{_format_sessions(today_data['sessions'])}

### 任务统计
- 完成任务数：{success_cnt}
- 失败任务数：{error_cnt}
- 总耗时：{today_data['total_duration_ms']/1000:.0f}s
- 总花费：${today_data['total_cost']:.4f}

### 待办事项
{_format_todos(todos)}

---
请输出：
1. **今日成果摘要**（一两句话）
2. **主要完成事项**（要点列表）
3. **待办清单评估** - 对上方待办逐一评估优先级
4. **明日建议**（可选）
"""


def build_morning_prompt(yesterday_data: dict, todos: list) -> str:
    success_cnt = len([t for t in yesterday_data["tasks"] if t.get("status") == "success"])
    error_cnt = len([t for t in yesterday_data["tasks"] if t.get("status") == "error"])
    return f"""你是一个工作秘书。请根据以下数据生成今日工作早报（适合手机阅读，总长不超过500字）。

## 昨日工作回顾（{yesterday_data['date']}）

### 会话活动
{_format_sessions(yesterday_data['sessions'])}

### 任务统计
- 完成：{success_cnt}，失败：{error_cnt}
- 总花费：${yesterday_data['total_cost']:.4f}

### 当前待办事项
{_format_todos(todos)}

---
请输出：
1. **昨日回顾**（一两句）
2. **今日规划建议** - 基于待办清单，给出今日优先处理顺序（最多5条）
3. **注意事项**（如有未完成的关键任务）
"""


async def run_report(report_type: str) -> None:
    async with _running_lock:
        today = date.today()
        date_str = today.isoformat()

        if db.get_report_by_date_type(date_str, report_type):
            return

        todos = db.list_todos(status="pending")

        if report_type == "evening":
            data = gather_day_data(today)
            prompt = build_evening_prompt(data, todos)
            wecom_title = f"📋 日报 {date_str}"
        else:
            yesterday = today - timedelta(days=1)
            data = gather_day_data(yesterday)
            prompt = build_morning_prompt(data, todos)
            wecom_title = f"🌅 早报 {date_str}"

        sec_sess = db.ensure_secretary_session(config.DEFAULT_WORKDIR)
        sec_sid = sec_sess["id"]

        try:
            from .session_hub import hub
            started = await hub.start_turn(sec_sid, prompt)
            if not started:
                return
        except Exception:
            return

        # 等回合完成（最多 5 分钟）
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline:
            try:
                from .session_hub import hub as _hub
                if not _hub.is_running(sec_sid):
                    break
            except Exception:
                break
            await asyncio.sleep(5)

        # 检查是否超时（回合仍在运行）——若超时则放弃本次，避免读到上一份旧报告
        try:
            from .session_hub import hub as _hub2
            if _hub2.is_running(sec_sid):
                return
        except Exception:
            pass

        # 读最后一条 assistant 消息
        msgs = db.list_messages(sec_sid)
        assistant_msgs = [m for m in msgs if m.get("role") == "assistant"]
        if not assistant_msgs:
            return

        last = assistant_msgs[-1]
        content_raw = last.get("content", "")
        if isinstance(content_raw, str):
            try:
                c = json.loads(content_raw)
                report_content = c.get("text") or c.get("content") or content_raw
            except Exception:
                report_content = content_raw
        elif isinstance(content_raw, dict):
            report_content = content_raw.get("text") or content_raw.get("content") or str(content_raw)
        else:
            report_content = str(content_raw)

        if not report_content:
            return

        db.create_report(date_str, report_type, report_content, session_id=sec_sid)

        # WebSocket 广播通知（无论企微是否启用，在线用户都能收到）。
        # 走监控通道（每个登录客户端常驻一条 /ws/monitor），才能广播给所有在线端，
        # 而非只发给某个会话的订阅者。
        try:
            from .session_hub import hub as _hub_ws
            await _hub_ws.broadcast_monitor({
                "type": "secretary_report",
                "report_type": report_type,
                "report_date": date_str,
                "title": wecom_title,
                "preview": report_content[:200],
            })
        except Exception:
            pass

        # 企微推送（如果启用）
        try:
            from . import wecom_notify
            if getattr(config, "WECOM_ENABLED", False):
                await wecom_notify.notify(
                    title=wecom_title,
                    user_text="",
                    reply_text=report_content[:1200],
                    status="success",
                )
        except Exception:
            pass

        # Triage 自动分流（H3）：晚报生成后，用便宜模型对当日数据做一次性分诊，
        # 逐项进"待分诊收件箱"或（开了自动派单且够有把握时）自动建目标循环开工。
        # 独立子进程、失败静默，绝不影响日报主流程。
        if report_type == "evening" and config.TRIAGE_ENABLED:
            try:
                from . import triage
                items = await triage.run_triage(data, todos)
                if items:
                    await triage.dispatch_triage(items)
            except Exception:
                pass
