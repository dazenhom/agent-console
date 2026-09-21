"""server.scheduler 的两个收尾路径。

1) _maybe_finalize_fanout 的聚合收尾逻辑：用 temp_db 走真实的 work_item 写入路径，用
   monkeypatch 打桩 db.list_dispatch_subtasks 来构造子任务终态组合，避免依赖真实 dispatch 扇出。
2) _finish_goal 的结构化终止原因（finish_reason）：终端判定与状态落地直接读库断言，
   企微通知/monitor 广播（fire-and-forget 后台任务）打桩，避免真发网络请求。
"""
import asyncio

import pytest

from server import scheduler, db, session_hub


def _seed_work_item(plan_id, status="running"):
    """建一条 ref_id=plan_id 的 work_item，返回其 id。"""
    return db.create_work_item(
        origin="dispatch", topology="fanout", isolation="worktree",
        verify_mode="nl", status=status, ref_id=plan_id,
    )


def _patch_subs(monkeypatch, statuses):
    monkeypatch.setattr(
        db, "list_dispatch_subtasks",
        lambda plan_id: [{"status": s} for s in statuses],
    )


def test_finalize_all_done_marks_work_item_done(temp_db, monkeypatch):
    wid = _seed_work_item("plan-done")
    _patch_subs(monkeypatch, ["done", "done", "done"])
    scheduler._maybe_finalize_fanout("plan-done")
    assert db.get_work_item(wid)["status"] == "done"


def test_finalize_with_failure_marks_exhausted(temp_db, monkeypatch):
    wid = _seed_work_item("plan-fail")
    _patch_subs(monkeypatch, ["done", "failed", "done"])
    scheduler._maybe_finalize_fanout("plan-fail")
    assert db.get_work_item(wid)["status"] == "exhausted"


def test_finalize_with_error_also_exhausted(temp_db, monkeypatch):
    wid = _seed_work_item("plan-err")
    _patch_subs(monkeypatch, ["done", "error"])
    scheduler._maybe_finalize_fanout("plan-err")
    assert db.get_work_item(wid)["status"] == "exhausted"


def test_finalize_skips_when_nonterminal_present(temp_db, monkeypatch):
    wid = _seed_work_item("plan-open", status="running")
    _patch_subs(monkeypatch, ["done", "dispatched", "failed"])
    scheduler._maybe_finalize_fanout("plan-open")
    # 还有非终态子任务，不聚合：work_item 状态保持不变
    assert db.get_work_item(wid)["status"] == "running"


def test_finalize_skips_when_no_subtasks(temp_db, monkeypatch):
    wid = _seed_work_item("plan-empty", status="running")
    _patch_subs(monkeypatch, [])
    scheduler._maybe_finalize_fanout("plan-empty")
    assert db.get_work_item(wid)["status"] == "running"


def test_finalize_empty_plan_id_is_noop(temp_db, monkeypatch):
    # plan_id 为空直接返回，不访问 db。
    called = {"hit": False}

    def _boom(plan_id):
        called["hit"] = True
        return []

    monkeypatch.setattr(db, "list_dispatch_subtasks", _boom)
    scheduler._maybe_finalize_fanout("")
    assert called["hit"] is False


# ---------- _finish_goal：finish_reason 结构化终止原因 ----------
def _mk_goal_schedule(goal_status="running"):
    sess = db.create_session("测试会话", "/tmp/finish-goal-test")
    sch = db.create_schedule(sess["id"], "把交付核验报告写完", "goal", None, None, 0,
                             stop_condition="报告落盘", max_iterations=6,
                             goal_status=goal_status)
    return sch, sess


def _run_finish_goal(monkeypatch, scid, sess, status, reason, finish_reason=""):
    """驱动一次 _finish_goal。_notify_and_log 与 monitor 广播都是 ensure_future 起的
    后台协程（会真发企微），一律打桩成 no-op。"""
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(scheduler, "_notify_and_log", _noop)
    monkeypatch.setattr(session_hub.hub, "broadcast_monitor", _noop)
    asyncio.run(scheduler._finish_goal(scid, sess, status, reason, finish_reason))


@pytest.mark.parametrize("finish_reason", [
    "cost_cap", "iter_cap", "verify_fail", "plan_fail", "plan_error",
])
def test_finish_goal_records_finish_reason(temp_db, monkeypatch, finish_reason):
    """每种异常终止都要把自己的结构化原因写进 schedules.finish_reason：Stall Watch
    据此分级（成本熔断独立告警类型、更短阈值），不能再靠 LIKE 匹配 last_feedback 文案。"""
    sch, sess = _mk_goal_schedule()
    reason = "本轮未达标，原因描述随便写一段自然语言"
    _run_finish_goal(monkeypatch, sch["id"], sess, "exhausted", reason, finish_reason)
    fresh = db.get_schedule(sch["id"])
    assert fresh["finish_reason"] == finish_reason
    assert fresh["goal_status"] == "exhausted" and fresh["enabled"] == 0
    assert fresh["last_feedback"] == reason  # 自然语言反馈仍照写（结构化原因与它并存）


def test_finish_goal_done_keeps_finish_reason_empty(temp_db, monkeypatch):
    """done 是正常达成，不传 finish_reason → 留空（空串即"无异常原因"，前端/告警都不该
    把它当成本熔断）。"""
    sch, sess = _mk_goal_schedule()
    _run_finish_goal(monkeypatch, sch["id"], sess, "done", "验收通过：报告已落盘")
    fresh = db.get_schedule(sch["id"])
    assert fresh["goal_status"] == "done" and fresh["enabled"] == 0
    assert (fresh["finish_reason"] or "") == ""
    assert fresh["last_feedback"] == "验收通过：报告已落盘"


def test_finish_goal_done_does_not_clear_previous_reason(temp_db, monkeypatch):
    """续跑后再达成：上一轮的 cost_cap 不该残留在 done 记录上误导告警——done 分支
    显式写入空串（而非"不传就保留旧值"）。"""
    sch, sess = _mk_goal_schedule()
    _run_finish_goal(monkeypatch, sch["id"], sess, "exhausted", "已达成本上限", "cost_cap")
    assert db.get_schedule(sch["id"])["finish_reason"] == "cost_cap"
    _run_finish_goal(monkeypatch, sch["id"], sess, "done", "验收通过")
    assert (db.get_schedule(sch["id"])["finish_reason"] or "") == ""
