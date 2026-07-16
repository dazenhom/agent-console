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
import os
import time
from datetime import datetime, timedelta, date

from . import db, config

TICK_SEC = 30


def compute_next_run(kind: str, interval_min, at_hhmm, *, after: float | None = None) -> float | None:
    """算下一次运行的 epoch 秒。after 不传则用当前时间。"""
    now = after if after is not None else time.time()
    if kind == "goal":
        return now + config.GOAL_POLL_SEC
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
                if sch.get("kind") == "goal":
                    await _tick_goal(sch, sess, now)
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


# ---------- 目标循环（kind=goal）状态机 ----------
# 每个 tick 只推进一步，转换先写库再动作（防重复触发）：
#   running   ─[会话空闲 & 未触顶]→ start_turn(迭代prompt), iter_count+1, goal_status=producing
#   producing ─[会话跑完]→ goal_status=verifying(先落库), ensure_future 后台 verify
#   verifying ─[后台任务完成]→ DONE→done / CONTINUE→running(存反馈)或 exhausted
#   done/exhausted：终态，enabled=0
# verifier 是 fire-and-forget（后台 ensure_future），绝不在 tick 主体 await——否则阻塞调度循环。

def _build_goal_prompt(sch: dict) -> str:
    """拼一轮迭代指令：目标 + 完成标准 +（有则）上轮验收反馈 + 轮次提示。"""
    iter_no = int(sch.get("iter_count") or 0) + 1
    parts = [
        "【目标】\n" + (sch.get("prompt") or "").strip(),
        "\n【完成标准】\n" + (sch.get("stop_condition") or "").strip(),
    ]
    feedback = (sch.get("last_feedback") or "").strip()
    if feedback:
        parts.append("\n【上一轮验收反馈，请针对性改进】\n" + feedback)
    parts.append(f"\n（这是第 {iter_no} 轮迭代，请朝完成标准推进，做完即可，不必啰嗦汇报。）")
    return "".join(parts)


async def _tick_goal(sch: dict, sess: dict, now: float) -> None:
    """按状态机推进一步。"""
    from .session_hub import hub
    scid = sch["id"]
    sid = sess["id"]
    status = sch.get("goal_status") or "running"

    # 终态：确保停用
    if status in ("done", "exhausted"):
        db.update_schedule(scid, enabled=0)
        return

    # verifying：后台验收任务在跑，本 tick 什么都不做，只顺延 next_run 避免反复被 due
    if status == "verifying":
        db.update_schedule(scid, next_run=compute_next_run("goal", None, None, after=now))
        return

    # producing：等本轮会话跑完
    if status == "producing":
        if hub.is_running(sid):
            db.update_schedule(scid, next_run=compute_next_run("goal", None, None, after=now))
            return
        # 会话已空闲 → 转 verifying（先落库再起后台任务，防重复触发）
        db.update_schedule(scid, goal_status="verifying",
                           next_run=compute_next_run("goal", None, None, after=now))
        asyncio.ensure_future(_run_goal_verify(scid))
        return

    # running：发起新一轮迭代（若未在跑、未触顶）
    if hub.is_running(sid):
        db.update_schedule(scid, next_run=compute_next_run("goal", None, None, after=now))
        return
    iter_count = int(sch.get("iter_count") or 0)
    max_iter = int(sch.get("max_iterations") or config.GOAL_MAX_ITERATIONS)
    if iter_count >= max_iter:
        await _finish_goal(scid, sess, "exhausted", f"已达迭代上限（{max_iter} 轮）仍未完成")
        return
    # 成本熔断
    if config.GOAL_MAX_COST_USD > 0:
        # 已知局限（一期接受）：sum_session_cost 从 created_at 起算，会把用户在同一会话里的
        # 手动回合成本也计入目标循环预算——可能偏保守提前熔断。二期若要精确应按 goal 启动时间戳起算。
        spent = db.sum_session_cost(sid, sch.get("created_at") or 0)
        if spent >= config.GOAL_MAX_COST_USD:
            await _finish_goal(scid, sess, "exhausted",
                               f"已达成本上限（${spent:.2f} ≥ ${config.GOAL_MAX_COST_USD}）")
            return
    prompt = _build_goal_prompt(sch)
    try:
        await hub.start_turn(sid, prompt)
    except Exception:
        # 起回合失败：不推进状态，仅顺延 next_run，下个 tick 重试
        db.update_schedule(scid, next_run=compute_next_run("goal", None, None, after=now))
        return
    db.update_schedule(scid, goal_status="producing", iter_count=iter_count + 1,
                       last_run=now, next_run=compute_next_run("goal", None, None, after=now))


async def _run_goal_verify(scid: str) -> None:
    """后台验收本轮产出，是唯一把状态推回 running/done 的地方。
    整体 try/except 兜底：任何异常都复位 running，绝不让状态卡死在 verifying。"""
    from . import goal_verifier, kanban
    try:
        sch = db.get_schedule(scid)
        if not sch or sch.get("goal_status") != "verifying":
            return
        sess = db.get_session(sch["session_id"])
        if not sess:
            db.update_schedule(scid, enabled=0)
            return
        # 读本轮产出片段
        produced = ""
        if sess.get("claude_session_id"):
            p = kanban._session_jsonl_path(sess["claude_session_id"], sess.get("workdir"))
            if p:
                produced = kanban._extract_recent_text(p)
        done, reason = await goal_verifier.verify(sch.get("prompt") or "",
                                                  sch.get("stop_condition") or "", produced,
                                                  session_id=sess.get("id"), schedule_id=scid)
        iter_count = int(sch.get("iter_count") or 0)
        max_iter = int(sch.get("max_iterations") or config.GOAL_MAX_ITERATIONS)
        if done:
            await _finish_goal(scid, sess, "done", reason)
        elif iter_count >= max_iter:
            await _finish_goal(scid, sess, "exhausted",
                               f"已达迭代上限（{max_iter} 轮）：{reason}")
        else:
            db.update_schedule(scid, goal_status="running", last_feedback=reason)
    except Exception as e:
        db.update_schedule(scid, goal_status="running",
                          last_feedback=f"[verify异常:{type(e).__name__}]")


async def _finish_goal(scid: str, sess: dict, status: str, reason: str) -> None:
    """落终态并停用，推企微 + 广播 monitor。"""
    from . import wecom_notify
    from .session_hub import hub
    db.update_schedule(scid, goal_status=status, enabled=0)
    title = (sess or {}).get("title") or "会话"
    head = "🎯 目标已达成" if status == "done" else "⏹️ 目标循环终止"
    asyncio.ensure_future(wecom_notify.notify(
        title=title,
        user_text=head,
        reply_text=reason,
        status="success" if status == "done" else "cancelled",
    ))
    try:
        asyncio.ensure_future(hub.broadcast_monitor({
            "type": "goal_update", "schedule_id": scid,
            "goal_status": status, "reason": reason,
        }))
    except Exception:
        pass



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
    # 只有显式设置 RUN_SCHEDULER 的进程才真正跑调度，避免双进程重复调度。
    if not os.environ.get("RUN_SCHEDULER"):
        return
    global _task, _sec_task, _memo_task
    if _task is None or _task.done():
        _task = asyncio.ensure_future(_run_loop())
    if _sec_task is None or _sec_task.done():
        _sec_task = asyncio.ensure_future(_secretary_loop())
    if _memo_task is None or _memo_task.done():
        _memo_task = asyncio.ensure_future(_memo_loop())
    return _task
