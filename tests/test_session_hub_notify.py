"""_notify_turn_done 的企微发送条件矩阵：前台订阅 × 回合时长 两个维度的组合。

reviewer P1-1 指出：改动前只按 `_has_foreground_sub` 一刀切——只要前台有 WS 订阅
就永不发企微，等于用户开着页面切去干别事、或手机锁屏但 WS 仍连着（痛点 2 最典型的
场景）永远收不到提醒。修复后长回合（elapsed >= config.NOTIFY_LONG_TURN_SEC）即使
有前台订阅者也要发。这里直接测 SessionHub._notify_turn_done，不起真实 WS 连接，
用 Subscriber 假对象控制 hidden 状态。
"""
import asyncio

from server import config, session_hub, db
from server.session_hub import SessionHub, Subscriber


def _make_hub_with_sub(sid: str, *, hidden: bool):
    hub = SessionHub()

    async def _send(_obj):
        pass

    if not hidden:
        # _has_foreground_sub 判定"有订阅者且非 hidden"才算前台在场；hidden=True 的
        # 订阅者（锁屏/切走）不算前台，走的是"无前台"分支，故这里只在 hidden=False 时注册。
        hub.subscribe(sid, Subscriber(_send, hidden=False))
    else:
        # 仍然注册一个 hidden 订阅者，模拟"锁屏但 WS 还连着"——不算前台在场。
        hub.subscribe(sid, Subscriber(_send, hidden=True))
    return hub


def _notify(monkeypatch, temp_db, *, foreground: bool, elapsed):
    """跑一次 _notify_turn_done，返回是否真的调用了 wecom_notify.notify。"""
    sess = temp_db.create_session("测试会话", "/tmp", engine="claude")
    sid = sess["id"]

    monkeypatch.setattr(config, "WECOM_ENABLED", True)
    monkeypatch.setattr(config, "NOTIFY_LONG_TURN_SEC", 600)

    called = {"n": 0}

    async def fake_notify(**kwargs):
        called["n"] += 1
        return True, "ok"

    monkeypatch.setattr(session_hub.wecom_notify, "notify", fake_notify)

    hub = _make_hub_with_sub(sid, hidden=not foreground)
    asyncio.run(hub._notify_turn_done(
        sid, "指令", "回复", {"status": "success"}, kind="turn", elapsed=elapsed,
    ))
    return called["n"]


def test_foreground_and_short_turn_does_not_notify(monkeypatch, temp_db):
    n = _notify(monkeypatch, temp_db, foreground=True, elapsed=30.0)
    assert n == 0


def test_no_foreground_and_short_turn_notifies(monkeypatch, temp_db):
    n = _notify(monkeypatch, temp_db, foreground=False, elapsed=30.0)
    assert n == 1


def test_foreground_but_long_turn_still_notifies(monkeypatch, temp_db):
    n = _notify(monkeypatch, temp_db, foreground=True, elapsed=900.0)
    assert n == 1


def test_foreground_and_turn_just_under_threshold_does_not_notify(monkeypatch, temp_db):
    # elapsed 恰好卡在阈值之下：不该被误判为长回合。
    n = _notify(monkeypatch, temp_db, foreground=True, elapsed=599.9)
    assert n == 0
