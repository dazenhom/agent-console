"""server.scheduler._tick_fanout 的时序竞态门槛：子会话空闲后，还要最近一条 task 已到终态
才转 verifying，避免 start_turn（fire-and-forget）"还没起跑就被误判成已跑完"。

用 temp_db 走真实 dispatch_subtasks / tasks 写入路径，只 monkeypatch 掉 hub.is_running 与
后台判定协程 _run_dispatch_verify（避免真实验收）。
"""
import asyncio

from server import scheduler, db, session_hub


def _seed_subtask(child_session_id="sess-x"):
    """建一条 dispatched 子任务，返回其 id 与 plan_id。"""
    plan_id = db.new_id()
    sub = db.create_dispatch_subtask(
        plan_id=plan_id, parent_session_id="parent", seq=0,
        title="子任务", instruction="干活", category="dev",
        engine="claude", model="strong", child_session_id=child_session_id,
        status="dispatched",
    )
    return sub["id"], plan_id


def _run_tick(monkeypatch, is_running):
    """打桩 hub.is_running 与后台判定协程后驱动一次 _tick_fanout。"""
    monkeypatch.setattr(session_hub.hub, "is_running", lambda sid: is_running)

    async def _noop_verify(subtask_id):
        return None

    monkeypatch.setattr(scheduler, "_run_dispatch_verify", _noop_verify)
    asyncio.run(scheduler._tick_fanout())


def test_tick_skips_when_child_still_running(temp_db, monkeypatch):
    sub_id, _ = _seed_subtask()
    db.start_task("sess-x", "回合")
    _run_tick(monkeypatch, is_running=True)
    # 子会话还在跑，不转 verifying
    assert db.get_dispatch_subtask(sub_id)["status"] == "dispatched"


def test_tick_skips_when_no_task_yet(temp_db, monkeypatch):
    # 子会话空闲但还没有任何 task：start_turn 尚未写库（竞态窗口），不能误判
    sub_id, _ = _seed_subtask()
    _run_tick(monkeypatch, is_running=False)
    assert db.get_dispatch_subtask(sub_id)["status"] == "dispatched"


def test_tick_skips_when_latest_task_running(temp_db, monkeypatch):
    # is_running 已翻 False 但 task 仍 running（task 注册进 hub._turns 前的让出点）
    sub_id, _ = _seed_subtask()
    db.start_task("sess-x", "回合")
    _run_tick(monkeypatch, is_running=False)
    assert db.get_dispatch_subtask(sub_id)["status"] == "dispatched"


def test_tick_advances_when_task_terminal_and_idle(temp_db, monkeypatch):
    # 子会话空闲 + 最近一条 task 已终态 → 转 verifying
    sub_id, _ = _seed_subtask()
    tid = db.start_task("sess-x", "回合")
    db.finish_task(tid, "success")
    _run_tick(monkeypatch, is_running=False)
    assert db.get_dispatch_subtask(sub_id)["status"] == "verifying"
