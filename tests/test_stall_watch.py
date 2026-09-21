"""Stall Watch（停滞事项主动检测）单测。

temp_db 走真实 schedules/todos/dispatch_subtasks 写入路径，直接驱动同步纯 DB 的
stall_watch.scan_once；hub.is_running 用真实 hub——未注册回合恒为 False，正好等于
"会话空闲"口径，无需打桩。
"""
import time

from server import config, db, stall_watch


def _backdate(table, row_id, age):
    """把 updated_at 回拨到 age 秒前。必须走裸 SQL：create_*/update_* 系列都会强制
    覆写 updated_at=_now()，传参进去会被静默冲掉。"""
    db._exec(f"UPDATE {table} SET updated_at=? WHERE id=?", (time.time() - age, row_id))


def _mk_goal(goal_status="exhausted", enabled=0, age=3 * 3600, prompt="把训练曲线画出来"):
    """建一条 kind=goal 的 schedule 并把 last_run 拨到 age 秒前。"""
    sess = db.create_session("测试会话", "/tmp/stall-test")
    sch = db.create_schedule(sess["id"], prompt, "goal", None, None, 0,
                             stop_condition="图落盘", max_iterations=6,
                             goal_status=goal_status)
    db.update_schedule(sch["id"], enabled=enabled, last_run=time.time() - age)
    return sch


def _mk_stale_todo(age=2 * 86400, title="补一份数据清单", dispatched_schedule_id=""):
    todo = db.create_todo(title, "盘点缺失的测试集", status="in_progress")
    if dispatched_schedule_id:
        # update_todo 会重置 updated_at，必须在回拨之前做
        db.update_todo(todo["id"], dispatched_schedule_id=dispatched_schedule_id)
    _backdate("todos", todo["id"], age)
    return todo


def _mk_failed_subtask(plan_id, age=7 * 3600, status="failed"):
    sub = db.create_dispatch_subtask(
        plan_id=plan_id, parent_session_id="parent", seq=0,
        title="子任务", instruction="干活", category="dev",
        engine="claude", model="strong", child_session_id="sess-x",
        status=status,
    )
    _backdate("dispatch_subtasks", sub["id"], age)
    return sub


# ---------- S1：goal exhausted ----------
def test_goal_exhausted_detected(temp_db):
    sch = _mk_goal(goal_status="exhausted", enabled=0, age=3 * 3600)
    result = stall_watch.scan_once()
    alerts = db.list_stall_alerts()
    assert len(alerts) == 1
    a = alerts[0]
    assert a["kind"] == "goal_exhausted" and a["ref_id"] == sch["id"]
    assert a["status"] == "open"
    assert a["dedupe_key"] == f"goal_exhausted:{sch['id']}"
    assert result["new"] and result["new"][0]["id"] == a["id"]


def test_goal_exhausted_below_threshold_not_reported(temp_db):
    _mk_goal(goal_status="exhausted", enabled=0, age=3600)  # < 默认 7200s
    stall_watch.scan_once()
    assert db.list_stall_alerts() == []


def test_goal_paused_branch(temp_db):
    # enabled=0 且非终态非 failed = 人工暂停，走 goal_paused
    sch = _mk_goal(goal_status="running", enabled=0, age=3 * 3600)
    stall_watch.scan_once()
    alerts = db.list_stall_alerts()
    assert len(alerts) == 1 and alerts[0]["kind"] == "goal_paused"
    assert alerts[0]["ref_id"] == sch["id"]


# ---------- 去重 ----------
def test_dedupe_same_ref_only_one_alert(temp_db):
    _mk_goal()
    stall_watch.scan_once()
    stall_watch.scan_once()
    rows = db._query("SELECT * FROM stall_alerts")
    assert len(rows) == 1


def test_s3a_merges_into_s1_dedupe_key(temp_db):
    """S3a 归并：todo 挂着已终态的 schedule 时，与 S1 报同一条（同 dedupe_key），不产生两条。"""
    sch = _mk_goal(goal_status="exhausted", enabled=0, age=30 * 3600)
    _mk_stale_todo(age=5 * 86400, dispatched_schedule_id=sch["id"])
    stall_watch.scan_once()
    rows = db._query("SELECT * FROM stall_alerts")
    assert len(rows) == 1
    assert rows[0]["kind"] == "goal_exhausted" and rows[0]["ref_id"] == sch["id"]


def test_todo_without_schedule_is_todo_idle(temp_db):
    todo = _mk_stale_todo(age=2 * 86400)
    stall_watch.scan_once()
    alerts = db.list_stall_alerts()
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "todo_idle" and alerts[0]["ref_id"] == todo["id"]


def test_todo_with_active_schedule_not_reported(temp_db):
    """schedule 还在推进（非终态、enabled=1）→ todo 不报（S2 覆盖 schedule 侧视角）。"""
    sch = _mk_goal(goal_status="running", enabled=1, age=3600)  # 未到卡死阈值
    _mk_stale_todo(age=5 * 86400, dispatched_schedule_id=sch["id"])
    stall_watch.scan_once()
    assert db.list_stall_alerts() == []


# ---------- auto_resolve ----------
def test_auto_resolve_when_condition_cleared(temp_db):
    sch = _mk_goal()
    stall_watch.scan_once()
    assert db.list_stall_alerts(status="open")
    # 用户从目标详情页续跑 → 翻回 running/enabled=1，下一轮扫描应自动了结
    db.update_schedule(sch["id"], goal_status="running", enabled=1)
    stall_watch.scan_once()
    assert db.list_stall_alerts() == []
    rows = db._query("SELECT status FROM stall_alerts")
    assert rows and rows[0]["status"] == "resolved"


def test_auto_resolve_when_source_deleted(temp_db):
    sch = _mk_goal()
    stall_watch.scan_once()
    aid = db.list_stall_alerts(status="open")[0]["id"]
    db.delete_schedule(sch["id"])
    stall_watch.scan_once()  # 源对象已删除：置 resolved，不抛异常
    assert db.get_stall_alert(aid)["status"] == "resolved"


def test_auto_resolve_never_touches_dismissed(temp_db):
    sch = _mk_goal()
    stall_watch.scan_once()
    aid = db.list_stall_alerts(status="open")[0]["id"]
    db.update_stall_alert(aid, status="dismissed")
    db.update_schedule(sch["id"], goal_status="running", enabled=1)
    stall_watch.scan_once()
    assert db.get_stall_alert(aid)["status"] == "dismissed"


# ---------- snooze / dismissed ----------
def test_snoozed_hidden_until_due(temp_db):
    _mk_goal()
    stall_watch.scan_once()
    aid = db.list_stall_alerts(status="open")[0]["id"]
    db.update_stall_alert(aid, status="snoozed", snooze_until=time.time() + 3600)
    assert db.list_stall_alerts() == []          # 未到期：不出现在默认列表
    db.update_stall_alert(aid, snooze_until=time.time() - 1)
    alerts = db.list_stall_alerts()              # 到期：重新出现（status 仍是 snoozed，不改库）
    assert len(alerts) == 1 and alerts[0]["id"] == aid


def test_dismissed_never_returns(temp_db):
    _mk_goal()
    stall_watch.scan_once()
    aid = db.list_stall_alerts(status="open")[0]["id"]
    db.update_stall_alert(aid, status="dismissed")
    stall_watch.scan_once()
    stall_watch.scan_once()
    rows = db._query("SELECT * FROM stall_alerts")
    assert len(rows) == 1 and rows[0]["status"] == "dismissed"
    assert db.list_stall_alerts() == []


def test_resolved_alert_revives_on_new_stall(temp_db):
    """resolved 后条件再次满足（如续跑完又耗尽）：复活同一条而非被唯一索引吞掉。"""
    sch = _mk_goal()
    stall_watch.scan_once()
    aid = db.list_stall_alerts(status="open")[0]["id"]
    db.update_schedule(sch["id"], goal_status="running", enabled=1)
    stall_watch.scan_once()
    assert db.get_stall_alert(aid)["status"] == "resolved"
    # 再次耗尽（续跑的轮数又烧完了）
    db.update_schedule(sch["id"], goal_status="exhausted", enabled=0,
                       last_run=time.time() - 3 * 3600)
    result = stall_watch.scan_once()
    rows = db._query("SELECT * FROM stall_alerts")
    assert len(rows) == 1 and rows[0]["id"] == aid and rows[0]["status"] == "open"
    assert result["new"]


# ---------- S5：dispatch ----------
def test_dispatch_failed_aggregated_by_plan(temp_db):
    plan_id = db.new_id()
    for seq in range(3):
        sub = db.create_dispatch_subtask(
            plan_id=plan_id, parent_session_id="parent", seq=seq,
            title=f"子任务{seq}", instruction="干活", category="dev",
            engine="claude", model="strong", child_session_id=f"sess-{seq}",
            status="failed",
        )
        _backdate("dispatch_subtasks", sub["id"], 7 * 3600)
    stall_watch.scan_once()
    alerts = db.list_stall_alerts()
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "dispatch_failed" and alerts[0]["ref_id"] == plan_id


def test_dispatch_failed_below_threshold_not_reported(temp_db):
    plan_id = db.new_id()
    _mk_failed_subtask(plan_id, age=3600)  # < 默认 21600s
    stall_watch.scan_once()
    assert db.list_stall_alerts() == []


# ---------- 截断 ----------
def test_truncated_to_max_alerts_per_scan(temp_db, monkeypatch):
    monkeypatch.setattr(config, "STALL_MAX_ALERTS_PER_SCAN", 3)
    for _ in range(5):
        _mk_goal()
    result = stall_watch.scan_once()
    assert len(db.list_stall_alerts()) == 3
    assert len(result["new"]) == 3
