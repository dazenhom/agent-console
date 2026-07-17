"""server.db.get_latest_task 只读 helper：取会话最近一条 task（含 status）。

全部落临时库（temp_db fixture），不碰生产 data/console.db。
"""


def test_get_latest_task_none_when_empty(temp_db):
    assert temp_db.get_latest_task("sess-x") is None


def test_get_latest_task_returns_running_task(temp_db):
    tid = temp_db.start_task("sess-x", "做点事")
    task = temp_db.get_latest_task("sess-x")
    assert task is not None
    assert task["id"] == tid
    assert task["status"] == "running"


def test_get_latest_task_reflects_terminal_status(temp_db):
    tid = temp_db.start_task("sess-x", "做点事")
    temp_db.finish_task(tid, "success", duration_ms=100, cost_usd=0.1, num_turns=1)
    task = temp_db.get_latest_task("sess-x")
    assert task["status"] == "success"


def test_get_latest_task_picks_most_recent(temp_db):
    temp_db.start_task("sess-x", "第一回合")
    tid2 = temp_db.start_task("sess-x", "第二回合")
    # 按 started_at DESC 取最新一条
    assert temp_db.get_latest_task("sess-x")["id"] == tid2


def test_get_latest_task_scoped_to_session(temp_db):
    temp_db.start_task("sess-a", "a")
    assert temp_db.get_latest_task("sess-b") is None
