"""server.scheduler._maybe_finalize_fanout 的聚合收尾逻辑。

用 temp_db 走真实的 work_item 写入路径，用 monkeypatch 打桩
db.list_dispatch_subtasks 来构造子任务终态组合，避免依赖真实 dispatch 扇出。
"""
from server import scheduler, db


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
