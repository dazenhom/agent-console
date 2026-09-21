"""Stall Watch（停滞事项主动检测与回收，H3 triage 增强）。

纯 DB 启发式，检测阶段零 LLM：周期扫描 schedules / todos / dispatch_subtasks 三张表，
找出"停在半路没人管"的事项，落 stall_alerts 表并经 monitor WS + 企微提醒。检测与处置
分离——只提醒不自动处置，用户经收件箱决定 继续/稍后/跳过（REST 在 main.py /api/stalls*）。

信号一览（kind -> 判据，阈值全部见 config.STALL_*）：
  S1 goal_exhausted  kind=goal、exhausted、enabled=0、last_run 距今超阈值
     goal_paused     kind=goal、enabled=0、非终态非 failed（人工暂停）、同阈值
  S2 goal_stuck      kind=goal、enabled=1、状态机停在 running/producing/verifying、
                     last_run 距今超阈值；硬否定：会话仍在跑（hub.is_running）则跳过
  S3 todo_idle       in_progress 且未归档、max(updated_at, progress_at) 距今超阈值。
                     带 dispatched_schedule_id 且该 schedule 已终态（exhausted/failed）的，
                     归并成该 schedule 自身的 S1 告警（同一 dedupe_key，同一件事只报一条）；
                     schedule 还在推进的不报（S1/S2 已覆盖）；已 done 的不报（工作已完成，
                     无跟进价值）；无关联 schedule 的纯手工待办才独立成 todo_idle。
  S4（会话有未完成承诺但已静默）一期不做：S1+S3 已覆盖有价值场景，S4 的增量主要是
     噪音（"我稍后会…"句式承诺的识别误报率高）。二期扩展点：扫描 messages 尾部的
     承诺句式 + 到期未兑现判定，需先解决误报率再上。
  S5 dispatch_failed / dispatch_stuck  失败堆积/卡死的 dispatch 子任务按 plan_id 聚合
     成一条（31 条 failed 逐条报会淹掉收件箱）。

文案默认走中文模板；STALL_LLM_SUMMARY 是二期扩展点（默认 false，本期不实现任何
LLM 调用路径）。

工程约束：
  - scan_once 是同步纯 DB 函数，调用方 asyncio.to_thread 包装（参照 main.py 的
    backlog_import.scan_backlog 先例）；hub.is_running 只是内存 dict 读，允许在
    worker 线程里同步调用。
  - 循环导入：对 session_hub / wecom_notify 的引用一律函数内延迟 import。
  - 去重语义：dedupe_key = kind + ':' + ref_id 全局唯一。已有 open/snoozed/dismissed
    记录的不再动；resolved/acted 的在条件再次满足时复活（同一件事新一轮停滞重新可见）；
    dismissed 是用户显式的"永不再报"，是唯一永久抑制态。
"""
import time

from . import config, db
from .logging_util import get_logger

logger = get_logger(__name__)

# 时间闸初值取模块导入时刻而非 0：重启后 hub 内存态为空（is_running 恒 False），立刻扫
# 会把"其实正在跑"的会话误报成卡死；给一个完整扫描间隔的启动宽限。
_stall_last_scan = time.time()
_stall_last_notify = 0.0

# 候选排序的 kind 优先级：耗尽/暂停最要紧（用户明确该做决定），卡死次之，待办/分派垫底。
_KIND_PRIORITY = {
    "goal_exhausted": 0, "goal_paused": 0, "goal_stuck": 1,
    "todo_idle": 2, "dispatch_failed": 3, "dispatch_stuck": 3,
}


def _clip(s: str, n: int) -> str:
    s = (s or "").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def _fmt_dur(sec: float) -> str:
    sec = max(0.0, float(sec))
    if sec < 3600:
        return f"{sec / 60:.0f} 分钟"
    if sec < 86400:
        return f"{sec / 3600:.1f} 小时"
    return f"{sec / 86400:.1f} 天"


def _goal_anchor(sch: dict) -> float:
    """目标循环的停滞计时锚点：last_run（最近一轮推进时刻），老数据为空回退 created_at。"""
    return float(sch.get("last_run") or sch.get("created_at") or 0)


def _goal_candidate(sch: dict, kind: str, now: float) -> dict:
    """由 schedule 行构造 goal 类告警候选。S1 与 S3a 归并共用：不管从哪路扫出来，
    同一 schedule 的告警内容保持一致，先落库的版本即最终版本。"""
    prompt = (sch.get("prompt") or "").strip()
    short = _clip(prompt, 40) or "（无目标描述）"
    idle = max(0.0, now - _goal_anchor(sch))
    iter_count = int(sch.get("iter_count") or 0)
    max_iter = int(sch.get("max_iterations") or 0)
    dur = _fmt_dur(idle)
    if kind == "goal_exhausted":
        title = f"目标循环已耗尽：{short}"
        fb = _clip(sch.get("last_feedback") or "", 120)
        detail = f"已迭代 {iter_count}/{max_iter} 轮后耗尽，停滞 {dur}无人续跑。"
        if fb:
            detail += f"最后一轮验收：{fb}"
    elif kind == "goal_paused":
        st = (sch.get("goal_status") or "").strip() or "running"
        title = f"目标循环被暂停：{short}"
        detail = f"状态停在 {st}、已停用 {dur}未处理。可续跑或明确放弃。"
    else:  # goal_stuck
        st = (sch.get("goal_status") or "").strip() or "running"
        title = f"目标循环疑似卡死：{short}"
        detail = f"启用中但状态机停在 {st} 已 {dur}无推进（会话空闲）。可复位重跑。"
    return {
        "kind": kind, "ref_id": sch.get("id") or "",
        "session_id": sch.get("session_id") or "",
        "dedupe_key": f"{kind}:{sch.get('id')}",
        "title": title, "detail": detail, "idle_sec": idle,
    }


def _scan_goals(now: float) -> list[dict]:
    """S1 + S2：一条 SQL 取全部命中阈值的 goal schedule，再在 Python 侧分型。
    S2 的硬否定（会话在跑）放这里判，is_running 是内存 dict 读不产生额外查询。"""
    from .session_hub import hub
    thr_exhausted = config.STALL_GOAL_EXHAUSTED_SEC
    thr_stuck = config.STALL_GOAL_STUCK_SEC
    rows = db._query(
        "SELECT id, session_id, prompt, goal_status, enabled, iter_count, max_iterations,"
        " last_feedback, last_run, created_at,"
        " (? - COALESCE(last_run, created_at)) AS idle_sec"
        " FROM schedules WHERE kind='goal' AND ("
        "  (enabled=0 AND goal_status='exhausted' AND (? - COALESCE(last_run, created_at)) > ?)"
        "  OR (enabled=0 AND COALESCE(goal_status,'') NOT IN ('done','exhausted','failed')"
        "      AND (? - COALESCE(last_run, created_at)) > ?)"
        "  OR (enabled=1 AND COALESCE(goal_status,'') IN ('running','producing','verifying')"
        "      AND (? - COALESCE(last_run, created_at)) > ?)"
        " )",
        (now, now, thr_exhausted, now, thr_exhausted, now, thr_stuck),
    )
    out = []
    for r in rows:
        sch = dict(r)
        if sch.get("enabled"):
            # S2：仍在跑的会话不算卡死（长回合是正常态，另有看门狗管）
            if hub.is_running(sch.get("session_id") or ""):
                continue
            out.append(_goal_candidate(sch, "goal_stuck", now))
        elif (sch.get("goal_status") or "") == "exhausted":
            out.append(_goal_candidate(sch, "goal_exhausted", now))
        else:
            out.append(_goal_candidate(sch, "goal_paused", now))
    return out


def _scan_todos(now: float) -> list[dict]:
    """S3：in_progress 待办无进展。一条 SQL LEFT JOIN 出待办与其关联 schedule 的状态，
    Python 侧分型：终态 schedule 归并进 S1 同一 dedupe_key，无关联/孤儿才独立 todo_idle。"""
    rows = db._query(
        "SELECT t.id AS todo_id, t.title, t.description, t.progress, t.session_id AS todo_session_id,"
        " t.dispatched_schedule_id,"
        " s.kind AS sch_kind, s.goal_status AS sch_goal_status, s.enabled AS sch_enabled,"
        " s.session_id AS sch_session_id, s.prompt AS sch_prompt, s.iter_count AS sch_iter_count,"
        " s.max_iterations AS sch_max_iter, s.last_feedback AS sch_last_feedback,"
        " s.last_run AS sch_last_run, s.created_at AS sch_created,"
        " (? - MAX(COALESCE(t.updated_at, t.created_at), COALESCE(t.progress_at, 0))) AS idle_sec"
        " FROM todos t"
        " LEFT JOIN schedules s ON t.dispatched_schedule_id != '' AND s.id = t.dispatched_schedule_id"
        " WHERE t.status='in_progress' AND (t.archived=0 OR t.archived IS NULL)"
        "   AND (? - MAX(COALESCE(t.updated_at, t.created_at), COALESCE(t.progress_at, 0))) > ?",
        (now, now, config.STALL_TODO_SEC),
    )
    out = []
    for r in rows:
        row = dict(r)
        sid = (row.get("dispatched_schedule_id") or "").strip()
        if sid and row.get("sch_kind") == "goal":
            gs = (row.get("sch_goal_status") or "").strip()
            if gs in ("exhausted", "failed"):
                # 归并进 S1 同一 dedupe_key：exhausted -> goal_exhausted:sid（与 S1 完全
                # 一致）；failed（派单回滚产物）-> goal_paused:sid（enabled=0 未完成态，
                # 续跑 impl 的 is_paused 分支可接）。先落库的版本生效，同一件事只报一条。
                sch_view = {
                    "id": sid, "session_id": row.get("sch_session_id"),
                    "prompt": row.get("sch_prompt"), "goal_status": gs,
                    "iter_count": row.get("sch_iter_count"),
                    "max_iterations": row.get("sch_max_iter"),
                    "last_feedback": row.get("sch_last_feedback"),
                    "last_run": row.get("sch_last_run"), "created_at": row.get("sch_created"),
                }
                out.append(_goal_candidate(
                    sch_view, "goal_exhausted" if gs == "exhausted" else "goal_paused", now))
            # 其余（还在推进 / 已 done）不报：S1/S2 覆盖 schedule 侧视角，
            # done 意味着工作已完成、无跟进价值
            continue
        # 无关联 schedule / schedule 已被删 / 指向非 goal：纯手工待办信号
        idle = float(row.get("idle_sec") or 0)
        detail = f"进行中的待办已 {_fmt_dur(idle)}无任何进展更新。"
        prog = (row.get("progress") or "").strip()
        if prog:
            detail += f"最近进展：{_clip(prog, 120)}"
        out.append({
            "kind": "todo_idle", "ref_id": row["todo_id"],
            "session_id": row.get("todo_session_id") or "",
            "dedupe_key": f"todo_idle:{row['todo_id']}",
            "title": row.get("title") or "未命名待办",
            "detail": detail, "idle_sec": idle,
        })
    return out


def _scan_dispatch(now: float) -> list[dict]:
    """S5：dispatch 子任务失败堆积 / 卡死，各一条聚合 SQL，按 plan_id 归并成一条告警。"""
    out = []
    # 失败堆积：failed/error 搁置没人重派
    rows = db._query(
        "SELECT plan_id, COUNT(*) AS cnt,"
        " (? - MIN(COALESCE(updated_at, created_at))) AS idle_sec"
        " FROM dispatch_subtasks"
        " WHERE status IN ('failed','error') AND (? - COALESCE(updated_at, created_at)) > ?"
        " GROUP BY plan_id",
        (now, now, config.STALL_DISPATCH_SEC),
    )
    for r in rows:
        row = dict(r)
        cnt = int(row["cnt"] or 0)
        out.append({
            "kind": "dispatch_failed", "ref_id": row["plan_id"], "session_id": "",
            "dedupe_key": f"dispatch_failed:{row['plan_id']}",
            "title": f"智能分派：{cnt} 个子任务失败待处理",
            "detail": f"共 {cnt} 个子任务判定失败/出错，最长已搁置 {_fmt_dur(row['idle_sec'])}。"
                      f"点「继续」逐个重派。",
            "idle_sec": float(row["idle_sec"] or 0),
        })
    # 卡死：dispatched 挂着、超过阈值、且子会话不在跑（会话在跑的是正常长回合）
    from .session_hub import hub
    rows = db._query(
        "SELECT plan_id, child_session_id, (? - COALESCE(updated_at, created_at)) AS idle_sec"
        " FROM dispatch_subtasks"
        " WHERE status='dispatched' AND (? - COALESCE(updated_at, created_at)) > ?",
        (now, now, config.STALL_DISPATCH_STUCK_SEC),
    )
    stuck: dict[str, list] = {}
    for r in rows:
        row = dict(r)
        sid = row.get("child_session_id") or ""
        if sid and hub.is_running(sid):
            continue
        stuck.setdefault(row["plan_id"], []).append(row)
    for plan_id, subs in stuck.items():
        worst = max(float(s.get("idle_sec") or 0) for s in subs)
        out.append({
            "kind": "dispatch_stuck", "ref_id": plan_id, "session_id": "",
            "dedupe_key": f"dispatch_stuck:{plan_id}",
            "title": f"智能分派：{len(subs)} 个子任务疑似卡死",
            "detail": f"{len(subs)} 个子任务派发后 {_fmt_dur(worst)}无进展且子会话不在运行。"
                      f"点「继续」标记失败并重派。",
            "idle_sec": worst,
        })
    return out


def _still_stalled(kind: str, ref_id: str, now: float) -> bool:
    """复查某告警的源对象是否仍满足停滞判据（auto_resolve 用）。源对象已删除返回
    False（视为已了结）；kind 不认识也返回 False（防僵尸告警）。"""
    from .session_hub import hub
    if kind in ("goal_exhausted", "goal_paused", "goal_stuck"):
        sch = db.get_schedule(ref_id)
        if not sch or sch.get("kind") != "goal":
            return False
        idle = now - _goal_anchor(sch)
        if kind == "goal_exhausted":
            return (not sch.get("enabled")) and (sch.get("goal_status") == "exhausted") \
                and idle > config.STALL_GOAL_EXHAUSTED_SEC
        if kind == "goal_paused":
            return (not sch.get("enabled")) \
                and (sch.get("goal_status") or "") not in ("done", "exhausted", "failed") \
                and idle > config.STALL_GOAL_EXHAUSTED_SEC
        if hub.is_running(sch.get("session_id") or ""):
            return False
        return bool(sch.get("enabled")) \
            and (sch.get("goal_status") or "") in ("running", "producing", "verifying") \
            and idle > config.STALL_GOAL_STUCK_SEC
    if kind == "todo_idle":
        rows = db._query("SELECT * FROM todos WHERE id=?", (ref_id,))
        if not rows:
            return False
        t = dict(rows[0])
        idle = now - max(float(t.get("updated_at") or t.get("created_at") or 0),
                         float(t.get("progress_at") or 0))
        return t.get("status") == "in_progress" and not t.get("archived") \
            and idle > config.STALL_TODO_SEC
    if kind in ("dispatch_failed", "dispatch_stuck"):
        subs = db.list_dispatch_subtasks(ref_id)
        if not subs:
            return False
        if kind == "dispatch_failed":
            return any(
                s.get("status") in ("failed", "error")
                and (now - float(s.get("updated_at") or s.get("created_at") or 0)) > config.STALL_DISPATCH_SEC
                for s in subs
            )
        for s in subs:
            if s.get("status") != "dispatched":
                continue
            if (now - float(s.get("updated_at") or s.get("created_at") or 0)) <= config.STALL_DISPATCH_STUCK_SEC:
                continue
            sid = s.get("child_session_id") or ""
            if sid and hub.is_running(sid):
                continue
            return True
        return False
    return False


def _auto_resolve(now: float) -> int:
    """复查所有 open 告警：源对象已删除、或不再满足停滞判据的置 resolved（如用户从
    目标详情页续跑后，收件箱对应告警在下一轮扫描自动消失）。dismissed/snoozed/acted
    是用户显式决定，绝不碰。单条异常吞掉继续下一条，绝不让一条坏数据中断整轮。"""
    resolved = 0
    for alert in db.list_stall_alerts(status="open", limit=500):
        try:
            if not _still_stalled(alert["kind"], alert["ref_id"], now):
                db.update_stall_alert(alert["id"], status="resolved")
                resolved += 1
        except Exception as e:
            print(f"[stall_watch] auto_resolve {alert['kind']}:{alert['ref_id']}"
                  f" error: {type(e).__name__}: {e}")
    return resolved


def scan_once() -> dict:
    """一轮完整扫描（同步纯 DB，调用方 asyncio.to_thread 包装）：
    auto_resolve → 三路扫描 → 超龄排除 → 去重/复活 → 排序截断 → 落库。
    返回 {scanned, new, alerts}：scanned=本轮候选总数（去重前），new=本轮新落库
    （含复活）的告警，alerts=当前需关注的完整列表。"""
    now = time.time()
    _auto_resolve(now)
    candidates = _scan_goals(now) + _scan_todos(now) + _scan_dispatch(now)
    # 超龄排除：停滞超过 STALL_MAX_AGE_SEC 的老黄历不再翻出来提醒
    candidates = [c for c in candidates if float(c["idle_sec"]) <= config.STALL_MAX_AGE_SEC]
    # 去重/复活：同 key 多路命中只留最先出现的一条（S1 优先于 S3a 归并）；已有
    # open/snoozed/dismissed 的跳过，resolved/acted 的标记待复活（条件重新满足=新一轮停滞）
    pending: list[dict] = []
    seen: set = set()
    for c in candidates:
        key = c["dedupe_key"]
        if key in seen:
            continue
        seen.add(key)
        existing = db.get_stall_alert_by_dedupe(key)
        if not existing:
            pending.append(c)
        elif existing.get("status") in ("resolved", "acted"):
            c = dict(c)
            c["_revive_id"] = existing["id"]
            pending.append(c)
        # open / snoozed / dismissed：已有活跃或用户已表态，不再动
    # 排序截断：kind 优先级 → idle 降序，防一波失败潮淹掉收件箱
    pending.sort(key=lambda c: (_KIND_PRIORITY.get(c["kind"], 9), -float(c["idle_sec"])))
    pending = pending[: max(1, config.STALL_MAX_ALERTS_PER_SCAN)]
    new: list[dict] = []
    for c in pending:
        revive_id = c.get("_revive_id")
        if revive_id:
            db.update_stall_alert(
                revive_id, status="open", title=c["title"], detail=c["detail"],
                idle_sec=c["idle_sec"], session_id=c.get("session_id") or "",
                snooze_until=0, acted_kind="", acted_ref="", notified_at=0, created_at=now,
            )
            revived = db.get_stall_alert(revive_id)
            if revived:
                new.append(revived)
        else:
            alert = db.create_stall_alert(
                kind=c["kind"], ref_id=c["ref_id"], title=c["title"],
                session_id=c.get("session_id") or "", detail=c.get("detail") or "",
                idle_sec=c.get("idle_sec") or 0, dedupe_key=c["dedupe_key"],
            )
            if alert:
                new.append(alert)
    return {"scanned": len(candidates), "new": new, "alerts": db.list_stall_alerts()}


async def notify_stalls(alerts: list) -> None:
    """推送一轮新增停滞告警：monitor WS（在线端 toast + 收件箱刷新）+ 企微摘要（离线
    触达）。fire-and-forget 调用，整体吞异常，绝不影响扫描循环。"""
    from .session_hub import hub
    from . import wecom_notify
    if not alerts:
        return
    now = time.time()
    for a in alerts:
        try:
            db.update_stall_alert(a["id"], notified_at=now)
        except Exception:
            pass
    try:
        await hub.broadcast_monitor({
            "type": "stall_alert",
            "count": len(alerts),
            "items": [{"id": a["id"], "kind": a["kind"], "title": a["title"]} for a in alerts],
        })
    except Exception:
        pass
    # 企微摘要卡片（未启用时 notify 内部直接返回 disabled，不会抛异常）
    try:
        lines = "\n".join(f"{i + 1}. {_clip(a.get('title') or '', 60)}"
                          for i, a in enumerate(alerts[:10]))
        ok, detail = await wecom_notify.notify(
            title=f"⏳ 停滞事项提醒（{len(alerts)} 件）",
            user_text="以下事项停在半路，待你决定 继续/稍后/跳过：",
            reply_text=lines,
            status="cancelled",
        )
        if not ok:
            logger.warning("stall wecom push failed: %s", detail)
    except Exception as e:
        logger.warning("stall wecom push error: %s", e)
