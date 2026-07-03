"""定时/周期任务调度器。

不引 croniter（保持依赖闭包只有 fastapi/uvicorn/httpx）。只支持两种足够覆盖
「每小时查训练进度」类需求的规格：
  - interval：每 N 分钟跑一次
  - daily：每天 HH:MM 跑一次

后台 asyncio 循环每 ~30s tick：到点的 schedule → hub.start_turn（复用回合执行，
跑完自然走企业微信通知），重算 next_run。在 main 的 lifespan 启动里挂起。

时间用本地时区（服务器时间）。next_run 存 epoch 秒。
"""
import asyncio
import time
from datetime import datetime, timedelta, date

from . import db, config

TICK_SEC = 30


def compute_next_run(kind: str, interval_min, at_hhmm, *, after: float | None = None) -> float | None:
    """算下一次运行的 epoch 秒。after 不传则用当前时间。"""
    now = after if after is not None else time.time()
    if kind == "interval":
        try:
            m = int(interval_min)
        except (TypeError, ValueError):
            return None
        if m <= 0:
            return None
        return now + m * 60
    if kind == "daily":
        try:
            hh, mm = str(at_hhmm).split(":")
            hh, mm = int(hh), int(mm)
        except Exception:
            return None
        base = datetime.fromtimestamp(now)
        cand = base.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if cand.timestamp() <= now:
            cand = cand + timedelta(days=1)
        return cand.timestamp()
    return None


async def _run_loop():
    # 启动时给到点但 next_run 为空的补算一次（兼容手动改库）
    from .session_hub import hub
    while True:
        try:
            now = time.time()
            for sch in db.due_schedules(now):
                sid = sch["session_id"]
                sess = db.get_session(sid)
                if not sess:
                    # 会话已删 → 顺手禁用这条定时任务，避免空转
                    db.update_schedule(sch["id"], enabled=0)
                    continue
                # 已在跑就跳过本次（下个 tick 再看），避免堆叠
                if not hub.is_running(sid):
                    try:
                        await hub.start_turn(sid, sch["prompt"])
                    except Exception:
                        pass
                nxt = compute_next_run(sch["kind"], sch.get("interval_min"), sch.get("at_hhmm"), after=now)
                db.update_schedule(sch["id"], last_run=now, next_run=nxt)
        except Exception:
            pass
        await asyncio.sleep(TICK_SEC)


_task = None
_sec_last: dict = {}  # report_type -> last run date string
_sec_task = None
_memo_last: dict = {}  # date -> 已触发标记
_memo_task = None


async def _secretary_loop():
    if not config.SECRETARY_ENABLED:
        return
    while True:
        try:
            now_dt = datetime.now()
            now_min = now_dt.hour * 60 + now_dt.minute
            today_str = date.today().isoformat()
            from . import secretary
            for report_type, time_cfg in [
                ("evening", config.SECRETARY_EVENING_TIME),
                ("morning", config.SECRETARY_MORNING_TIME),
            ]:
                cfg_h, cfg_m = map(int, time_cfg.split(":"))
                cfg_min = cfg_h * 60 + cfg_m
                if now_min >= cfg_min and _sec_last.get(report_type) != today_str:
                    _sec_last[report_type] = today_str
                    asyncio.ensure_future(secretary.run_report(report_type))
        except Exception:
            pass
        await asyncio.sleep(60)


async def _memo_loop():
    if not config.MEMO_REMIND_ENABLED:
        return
    from . import memo_reminder
    while True:
        try:
            rh, rm = map(int, config.MEMO_REMIND_TIME.split(":"))
            now_dt = datetime.now()
            now_min = now_dt.hour * 60 + now_dt.minute
            target_min = rh * 60 + rm
            today_str = date.today().isoformat()
            if now_min >= target_min and _memo_last.get("date") != today_str:
                _memo_last["date"] = today_str
                await memo_reminder.run_reminder()
        except Exception as e:
            print(f"[memo_loop] error: {e}")
        await asyncio.sleep(30)


def start():
    """在 FastAPI lifespan 里调用，挂起后台调度循环。"""
    global _task, _sec_task, _memo_task
    if _task is None or _task.done():
        _task = asyncio.ensure_future(_run_loop())
    if _sec_task is None or _sec_task.done():
        _sec_task = asyncio.ensure_future(_secretary_loop())
    if _memo_task is None or _memo_task.done():
        _memo_task = asyncio.ensure_future(_memo_loop())
    return _task
