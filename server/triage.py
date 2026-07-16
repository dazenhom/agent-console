"""Triage 自动分流（H3）：秘书晚报后，用便宜模型对当日数据做一次性分诊。

与 kanban.summarize_progress / goal_verifier.verify 一脉相承：独立的一次性子进程，
绝不碰会话常驻上下文；复用 claude_runner._child_env() 剔除编排态环境变量（否则 403），
用 CLAUDE_MODEL_KANBAN（便宜模型），start_new_session=True + timeout 兜底。

设计原则：默认保守。任何分诊失败/超时/解析不到一律返回空、什么都不做，绝不误派。
只有开了 TRIAGE_AUTO_DISPATCH、置信度够高、范围小、有明确完成标准且未超每日上限时，
才自动建隔离会话 + 目标循环开工；否则一律进"待分诊收件箱"等人工确认。
自动派单任何一步出错都降级进收件箱，不留半成品、不吞成"已派"。

注意：scheduler 用函数内延迟 import，避免 scheduler→secretary→triage→scheduler 顶层循环导入。
"""
import asyncio
import json
import re
from datetime import date, datetime

from . import config, db, worktree
from .job_store import run_logged_oneshot

_VALID_CATEGORIES = {"bugfix", "test", "chore", "investigate", "other"}
_VALID_ACTIONS = {"auto", "triage", "ignore"}


def _today_start_ts() -> float:
    d = date.today()
    return datetime(d.year, d.month, d.day, 0, 0, 0).timestamp()


def _build_triage_prompt(day_data: dict, todos: list) -> str:
    """把当日会话 briefs + 任务统计 + 现有 pending todos 拼进 prompt，要求只输出一行 JSON。"""
    sessions = day_data.get("sessions", [])
    sess_lines = []
    for s in sessions[:20]:
        brief = "; ".join(s.get("msgs") or [])[:200] or "（无用户指令记录）"
        wd = s.get("workdir") or ""
        sess_lines.append(f"  - 【{s.get('title')}】(status={s.get('status')}, workdir={wd}) {brief}")
    sess_text = "\n".join(sess_lines) if sess_lines else "  （今日无会话活动）"

    tasks = day_data.get("tasks", [])
    success_cnt = len([t for t in tasks if t.get("status") == "success"])
    error_cnt = len([t for t in tasks if t.get("status") == "error"])

    todo_lines = []
    for t in todos[:30]:
        todo_lines.append(f"  - {t.get('title')}: {(t.get('description') or '')[:100]}")
    todo_text = "\n".join(todo_lines) if todo_lines else "  （暂无待办）"

    return (
        "你是一个严格的工作分诊员。下面是某人今天的 AI 开发会话活动、任务统计和现有待办清单。"
        "请判断今天暴露出哪些值得跟进的事项（如未修的 bug、缺失的测试、遗留的小杂务、需进一步调查的问题），"
        "逐项给出分类、置信度和建议动作。\n\n"
        "输出格式要求（务必严格遵守）：\n"
        "- 只输出一行 JSON，不要 markdown 代码块、不要任何前后缀说明。\n"
        '- 结构：{"items":[{"title":"简短标题","category":"bugfix|test|chore|investigate|other",'
        '"confidence":0到1的小数,"action":"auto|triage|ignore","reason":"判断理由",'
        '"goal_prompt":"若自动开工，交给 Agent 的完整目标指令","stop_condition":"明确的完成标准",'
        '"workdir":"最相关会话的 workdir 绝对路径，不确定则空字符串"}]}\n'
        f"- 最多 {config.TRIAGE_MAX_ITEMS} 项。宁可少报、宁可判 triage，也不要误报或轻易判 auto。\n"
        "- 只有当你非常有把握、事项范围小、有明确可验证的完成标准、且低风险时，"
        "才给 action=auto 并配高 confidence（>=0.85）；任何不确定、范围大、需要人拍板的，一律 action=triage。\n"
        "- 明显不值得跟进的（闲聊、已完成、无意义）给 action=ignore。\n\n"
        f"## 今日会话活动（{day_data.get('date')}）\n{sess_text}\n\n"
        f"## 任务统计\n- 完成：{success_cnt}，失败：{error_cnt}\n\n"
        f"## 现有待办清单\n{todo_text}\n"
    )


async def run_triage(day_data: dict, todos: list) -> list:
    """一次性子进程跑分诊，返回校验后的 items 列表。失败/超时/解析不到 → []。"""
    prompt = _build_triage_prompt(day_data, todos)
    cmd = [
        config.CLAUDE_BIN, "--", "-p", prompt,
        "--model", config.CLAUDE_MODEL_KANBAN, "--output-format", "json",
        "--effort", config.CLAUDE_ONESHOT_EFFORT,
    ]
    jid, text, stderr_text, status = await run_logged_oneshot(
        "triage", cmd, config.TRIAGE_TIMEOUT,
        model=config.CLAUDE_MODEL_KANBAN, input_summary="每日分诊",
    )
    if status == "timeout":
        print("[triage] run_triage: timeout")
        return []
    if status == "error":
        print("[triage] run_triage: subprocess error")
        return []

    # 挑出 JSON 那行取 result（与 kanban.summarize_progress 一致）
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
    if not result:
        err_text = stderr_text[:200]
        print(f"[triage] run_triage: no result, stderr={err_text!r}")
        return []

    # result 本身应是一行 JSON；模型偶尔套 markdown 代码块，剥一下再解析
    result = re.sub(r"^```(?:json)?|```$", "", result.strip()).strip()
    try:
        parsed = json.loads(result)
    except json.JSONDecodeError:
        print(f"[triage] run_triage: result not json: {result[:200]!r}")
        return []
    raw_items = parsed.get("items") if isinstance(parsed, dict) else None
    if not isinstance(raw_items, list):
        return []

    items = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        title = (it.get("title") or "").strip()
        if not title:
            continue
        # confidence clamp 到 0..1
        try:
            conf = float(it.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))
        # action 非法当 triage；ignore 直接丢弃
        action = (it.get("action") or "").strip().lower()
        if action == "ignore":
            continue
        if action not in _VALID_ACTIONS:
            action = "triage"
        category = (it.get("category") or "other").strip().lower()
        if category not in _VALID_CATEGORIES:
            category = "other"
        items.append({
            "title": title[:200],
            "category": category,
            "confidence": conf,
            "action": action,
            "reason": (it.get("reason") or "").strip()[:500],
            "goal_prompt": (it.get("goal_prompt") or "").strip(),
            "stop_condition": (it.get("stop_condition") or "").strip(),
            "workdir": (it.get("workdir") or "").strip(),
        })
        if len(items) >= config.TRIAGE_MAX_ITEMS:
            break
    # 补写分诊结论：解析出的各项标题 + 建议动作，供事后追溯
    summary = "; ".join(f"{it['title']}({it['action']},conf={it['confidence']})" for it in items) \
        or "无可跟进事项"
    db.set_job_output(jid, summary)
    return items


async def dispatch_triage(items: list) -> dict:
    """分流主逻辑：逐项决定自动派单 or 进收件箱。返回 {auto, triage, skipped} 计数。"""
    counts = {"auto": 0, "triage": 0, "skipped": 0}
    dispatched_today = db.count_triage_dispatched_today(_today_start_ts())
    for it in items:
        # 当天同 title 去重，避免每晚重复灌进收件箱
        if _dup_triage_title(it["title"]):
            counts["skipped"] += 1
            _log_triage("skip(dup)", it)
            continue
        can_auto = (
            config.TRIAGE_AUTO_DISPATCH
            and it["action"] == "auto"
            and it["confidence"] >= config.TRIAGE_AUTO_CONFIDENCE
            and it["goal_prompt"]
            and it["stop_condition"]
            and (dispatched_today + counts["auto"]) < config.TRIAGE_MAX_AUTO_PER_DAY
        )
        if can_auto:
            ok = await _auto_dispatch_one(it)
            if ok:
                counts["auto"] += 1
                _log_triage("auto", it)
                continue
            # 自动派单失败 → 降级进收件箱，不留半成品
            _log_triage("auto-failed→inbox", it)
        _to_inbox(it)
        counts["triage"] += 1
        _log_triage("inbox", it)
    print(f"[triage] dispatch done: {counts}")
    return counts


def _build_and_start(payload: dict):
    """建隔离会话 + 目标循环，返回 (sess, sch)。给自动派单和人工派单共用。

    scheduler 用函数内延迟 import 打破顶层循环导入。任何异常向上抛，由调用方兜底。
    """
    from . import scheduler
    title = (payload.get("title") or "分诊任务")[:80]
    workdir = payload.get("workdir") or config.DEFAULT_WORKDIR
    goal_prompt = payload.get("goal_prompt") or title
    stop_condition = payload.get("stop_condition") or "完成该任务"

    # 建隔离 worktree（降级共享工作区）
    wd, branch, is_wt, wt_base, _ = worktree.provision_workdir(workdir, title[:24], True)
    sess = db.create_session(f"[自动派单] {title}", wd,
                             worktree_branch=branch, is_worktree=is_wt, worktree_base=wt_base)

    nxt = scheduler.compute_next_run("goal", None, None)
    sch = db.create_schedule(
        sess["id"], goal_prompt, "goal", None, None, nxt,
        stop_condition=stop_condition,
        max_iterations=config.TRIAGE_GOAL_MAX_ITERATIONS,
        goal_status="running",
    )
    # 阶段2 影子表：纯附加观测，写失败只记日志绝不影响派单主流程
    try:
        db.create_work_item(
            origin="triage", topology="iterate",
            isolation="worktree" if is_wt else "shared",
            verify_mode="nl", status="pending",
            ref_id=sch["id"], session_id=sess["id"], summary=goal_prompt,
        )
    except Exception as e:
        print(f"[work_items] triage insert failed: {type(e).__name__}: {e}")
    return sess, sch


async def _auto_dispatch_one(it: dict) -> bool:
    """自动派单一项：建隔离会话 + 目标循环 + in_progress todo。整体 try 包裹，异常 return False。

    _build_and_start 成功后已在 DB 落地一条 running/enabled 的 goal schedule + 一个 idle 会话；
    若后续 create_todo/update_todo 抛异常，必须在 except 里回滚，否则会留下无 todo 跟踪的孤立
    schedule，被 scheduler._tick_goal 接管后静默烧 cost。
    """
    sess = sch = None
    try:
        sess, sch = await asyncio.to_thread(_build_and_start, it)
        todo = db.create_todo(
            it["title"], it.get("reason", ""), priority=0,
            session_id=sess["id"], status="in_progress",
        )
        db.update_todo(
            todo["id"], source="triage",
            confidence=it["confidence"], suggested_action="auto",
            dispatched_schedule_id=sch["id"],
            triage_payload=json.dumps(it, ensure_ascii=False),
        )
        db.set_todo_sessions(todo["id"], [sess["id"]])
        return True
    except Exception as e:
        print(f"[triage] _auto_dispatch_one error: {type(e).__name__}: {e}")
        # 回滚半成品：孤立 schedule 必须禁用（防被 _tick_goal 接管），孤立 idle 会话一并删除。
        # 清理各自兜底，别让清理再抛异常盖掉原始错误。
        if sch:
            try:
                db.update_schedule(sch["id"], enabled=0, goal_status="failed")
            except Exception as ce:
                print(f"[triage] _auto_dispatch_one cleanup schedule failed: {type(ce).__name__}")
        if sess:
            try:
                db.delete_session(sess["id"])
            except Exception as ce:
                print(f"[triage] _auto_dispatch_one cleanup session failed: {type(ce).__name__}")
        return False


async def _dispatch_existing_todo(tid: str, payload: dict) -> bool:
    """人工从收件箱派单：建隔离会话 + 目标循环，更新既有 triage todo（不新建）。异常 return False。"""
    try:
        sess, sch = await asyncio.to_thread(_build_and_start, payload)
        db.update_todo(
            tid, status="in_progress", session_id=sess["id"],
            dispatched_schedule_id=sch["id"],
        )
        db.set_todo_sessions(tid, [sess["id"]])
        return True
    except Exception as e:
        print(f"[triage] _dispatch_existing_todo error: {type(e).__name__}: {e}")
        return False


def _to_inbox(it: dict) -> None:
    """降级进待分诊收件箱。"""
    db.create_triage_todo(
        it["title"], it.get("reason", ""), it["confidence"],
        it["action"], it,
    )


def _dup_triage_title(title: str) -> bool:
    """当天是否已有同 title 的 status='triage' 项（避免每晚重复灌收件箱）。"""
    rows = db._query(
        "SELECT 1 FROM todos WHERE status='triage' AND title=? AND created_at>=? LIMIT 1",
        (title, _today_start_ts()),
    )
    return bool(rows)


def _log_triage(action: str, it: dict, **kw) -> None:
    print(f"[triage] {action}: {it.get('title')!r} conf={it.get('confidence')} act={it.get('action')} {kw or ''}")
