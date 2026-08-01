"""DELETE /api/sessions/{sid}：回合进行中不能删（reviewer P1-2）。

改动前 remove_session 只检查了看板任务关联，没检查回合状态就直接 db.delete_session。
现在 out_of_turn_cb 之类的跨回合回调会继续对已删 sid 调 db.add_message/update_session/
start_task（无外键约束，静默产生孤儿行），所以必须在真正跑着的时候拒绝删除。

用 TestClient 走真实路由 + monkeypatch hub.is_running 模拟"回合在跑"，不起真实 Agent 进程。
"""
from fastapi.testclient import TestClient

from server import config
from server import main as main_module
from server.session_hub import hub


def test_delete_running_session_returns_409(monkeypatch, temp_db):
    monkeypatch.setattr(config, "AUTH_TOKEN", "unit-token")
    sess = temp_db.create_session("跑着的会话", "/tmp", engine="claude")
    sid = sess["id"]

    monkeypatch.setattr(hub, "is_running", lambda s: s == sid)

    client = TestClient(main_module.app)
    resp = client.delete(
        f"/api/sessions/{sid}",
        headers={"Authorization": "Bearer unit-token"},
    )
    assert resp.status_code == 409
    # 确认真的没删：会话记录还在
    assert temp_db.get_session(sid) is not None


def test_delete_idle_session_succeeds(monkeypatch, temp_db):
    monkeypatch.setattr(config, "AUTH_TOKEN", "unit-token")
    sess = temp_db.create_session("空闲会话", "/tmp", engine="claude")
    sid = sess["id"]

    monkeypatch.setattr(hub, "is_running", lambda s: False)

    async def fake_forget_session(*args, **kwargs):
        return None

    # 不真的起常驻进程/杀进程，forget_session 打桩成 no-op。
    from server.claude_runner import runner as claude_runner
    monkeypatch.setattr(claude_runner, "forget_session", fake_forget_session)

    client = TestClient(main_module.app)
    resp = client.delete(
        f"/api/sessions/{sid}",
        headers={"Authorization": "Bearer unit-token"},
    )
    assert resp.status_code == 200
    assert temp_db.get_session(sid) is None
