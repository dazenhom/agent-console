"""Stall Watch（停滞事项主动检测）单测。

temp_db 走真实 schedules/todos/dispatch_subtasks 写入路径，直接驱动同步纯 DB 的
stall_watch.scan_once；hub.is_running 用真实 hub——未注册回合恒为 False，正好等于
"会话空闲"口径；"会话在跑"分支（S2/S5 硬否定）在 hub 单例实例上 monkeypatch
is_running=True 覆盖。REST 集成测试走 TestClient（先例 test_notify_auth.py）：不进
with 上下文不触发 lifespan，不会拉起 scheduler 后台循环。
"""
import time

import pytest

from server import config, db, session_hub, stall_watch


def _backdate(table, row_id, age):
    """把 updated_at 回拨到 age 秒前。必须走裸 SQL：create_*/update_* 系列都会强制
    覆写 updated_at=_now()，传参进去会被静默冲掉。"""
    db._exec(f"UPDATE {table} SET updated_at=? WHERE id=?", (time.time() - age, row_id))


def _mk_goal(goal_status="exhausted", enabled=0, age=3 * 3600, prompt="把训练曲线画出来",
             finish_reason=""):
    """建一条 kind=goal 的 schedule 并把 last_run 拨到 age 秒前。

    finish_reason 是新的结构化终止原因列（cost_cap/iter_cap/...）：只有传了才写，
    默认空串等于"非成本熔断"，与历史数据口径一致。"""
    sess = db.create_session("测试会话", "/tmp/stall-test")
    sch = db.create_schedule(sess["id"], prompt, "goal", None, None, 0,
                             stop_condition="图落盘", max_iterations=6,
                             goal_status=goal_status)
    fields = {"enabled": enabled, "last_run": time.time() - age}
    if finish_reason:
        fields["finish_reason"] = finish_reason
    db.update_schedule(sch["id"], **fields)
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
    assert a["dedupe_key"] == f"goal:{sch['id']}"
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


# ---------- S1b：goal cost_capped（成本熔断独立分型 + 更短阈值） ----------
def test_goal_cost_capped_kind_and_threshold(temp_db):
    """成本熔断（finish_reason='cost_cap'）用更短的阈值（默认 3600s）：3700s 就该报，
    而同样时长在迭代耗尽阈值（7200s）下不该出——两个阈值必须真的分流。"""
    sch = _mk_goal(goal_status="exhausted", enabled=0, age=3700, finish_reason="cost_cap")
    stall_watch.scan_once()
    alerts = db.list_stall_alerts()
    assert len(alerts) == 1
    a = alerts[0]
    assert a["kind"] == "goal_cost_capped" and a["ref_id"] == sch["id"]
    assert a["dedupe_key"] == f"goal:{sch['id']}"
    # 同一时长、改判成迭代耗尽 → 未到 7200s，不再满足任何判据（原告警被 auto_resolve 了结）
    db.update_schedule(sch["id"], finish_reason="iter_cap")
    stall_watch.scan_once()
    assert db.list_stall_alerts() == []


def test_goal_iter_cap_keeps_exhausted_kind(temp_db):
    """非成本熔断的耗尽仍是 goal_exhausted，且阈值仍是 7200s（不被 cost_cap 的短阈值误伤）。"""
    sch = _mk_goal(goal_status="exhausted", enabled=0, age=3700, finish_reason="iter_cap")
    stall_watch.scan_once()
    assert db.list_stall_alerts() == []
    db.update_schedule(sch["id"], last_run=time.time() - 7300)
    stall_watch.scan_once()
    alerts = db.list_stall_alerts()
    assert len(alerts) == 1 and alerts[0]["kind"] == "goal_exhausted"
    assert alerts[0]["dedupe_key"] == f"goal:{sch['id']}"


def test_cost_capped_detail_has_spent_and_limit(temp_db):
    """成本熔断的文案要带上钱包上下文：本窗口已花多少 / 上限多少（续跑 prompt 新上限的
    主要依据）。"""
    sch = _mk_goal(goal_status="exhausted", enabled=0, age=3700, finish_reason="cost_cap",
                   prompt="把压缩数据交付核验完")
    sid = db.get_schedule(sch["id"])["session_id"]
    tid = db.start_task(sid, "第 5/6 轮迭代")
    db.finish_task(tid, "success", cost_usd=20.69)
    stall_watch.scan_once()
    a = db.list_stall_alerts(status="open")[0]
    assert a["kind"] == "goal_cost_capped"
    assert "成本上限" in a["title"]
    assert "已花 $20.69" in a["detail"]
    # 未设 per-goal 上限 → 回退全局 config.GOAL_MAX_COST_USD
    assert f"上限 ${config.GOAL_MAX_COST_USD:g}" in a["detail"]


def test_goal_dedupe_key_is_ref_scoped(temp_db):
    """dedupe_key 不含 kind：同一 schedule 从 paused 漂移成 exhausted，仍只有一条告警
    （key 里带 kind 的旧口径会在这里多报一条）。"""
    sch = _mk_goal(goal_status="running", enabled=0, age=3 * 3600)  # 人工暂停
    stall_watch.scan_once()
    rows = db._query("SELECT * FROM stall_alerts")
    assert len(rows) == 1 and rows[0]["dedupe_key"] == f"goal:{sch['id']}"
    assert rows[0]["kind"] == "goal_paused"
    # 同一件事换分型：改成迭代耗尽
    db.update_schedule(sch["id"], goal_status="exhausted", finish_reason="iter_cap")
    stall_watch.scan_once()
    rows = db._query("SELECT * FROM stall_alerts")
    assert len(rows) == 1 and rows[0]["ref_id"] == sch["id"]
    assert rows[0]["dedupe_key"] == f"goal:{sch['id']}"  # 同 key → 复活原记录而非新插一条
    assert rows[0]["kind"] == "goal_exhausted"


# ---------- S2：goal stuck ----------
def test_goal_stuck_detected(temp_db):
    # enabled=1 + 状态机停在 running 超 STALL_GOAL_STUCK_SEC（默认 21600s）+ 会话空闲 → 检出
    sch = _mk_goal(goal_status="running", enabled=1, age=7 * 3600)
    stall_watch.scan_once()
    alerts = db.list_stall_alerts()
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "goal_stuck" and alerts[0]["ref_id"] == sch["id"]


def test_goal_stuck_skipped_when_session_running(temp_db, monkeypatch):
    # S2 硬否定：会话仍在跑（长回合是正常态，另有看门狗管）→ 不报。
    # hub 是函数内延迟 import 的单例，直接在实例上打桩 is_running
    monkeypatch.setattr(session_hub.hub, "is_running", lambda sid: True)
    _mk_goal(goal_status="running", enabled=1, age=7 * 3600)
    stall_watch.scan_once()
    assert db.list_stall_alerts() == []


# ---------- 去重 ----------
def test_dedupe_same_ref_only_one_alert(temp_db):
    sch = _mk_goal()
    stall_watch.scan_once()
    stall_watch.scan_once()
    rows = db._query("SELECT * FROM stall_alerts")
    assert len(rows) == 1
    assert rows[0]["dedupe_key"] == f"goal:{sch['id']}"


def test_s3a_merges_into_s1_dedupe_key(temp_db):
    """S3a 归并：todo 挂着已终态的 schedule 时，与 S1 报同一条（同 dedupe_key），不产生两条。"""
    sch = _mk_goal(goal_status="exhausted", enabled=0, age=30 * 3600)
    _mk_stale_todo(age=5 * 86400, dispatched_schedule_id=sch["id"])
    stall_watch.scan_once()
    rows = db._query("SELECT * FROM stall_alerts")
    assert len(rows) == 1
    assert rows[0]["kind"] == "goal_exhausted" and rows[0]["ref_id"] == sch["id"]
    assert rows[0]["dedupe_key"] == f"goal:{sch['id']}"


def test_s3a_cost_capped_merge_uses_cost_kind(temp_db):
    """S3a 归并也要按 finish_reason 分型：挂着成本熔断 schedule 的待办，归并出的告警
    同样是 goal_cost_capped（两路判据必须一致，否则 todo 侧会报出一个不存在的分型）。"""
    sch = _mk_goal(goal_status="exhausted", enabled=0, age=3700, finish_reason="cost_cap")
    _mk_stale_todo(age=5 * 86400, dispatched_schedule_id=sch["id"])
    stall_watch.scan_once()
    rows = db._query("SELECT * FROM stall_alerts")
    assert len(rows) == 1
    assert rows[0]["kind"] == "goal_cost_capped" and rows[0]["ref_id"] == sch["id"]
    assert rows[0]["dedupe_key"] == f"goal:{sch['id']}"


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


def test_still_stalled_cost_capped_not_auto_resolved(temp_db):
    """白名单回归（本改动最易漏的刀）：goal_cost_capped 必须登记进 _still_stalled 的
    kind 白名单，否则条件仍满足的告警会被 auto_resolve 当"不认识的僵尸"静默清空。"""
    sch = _mk_goal(goal_status="exhausted", enabled=0, age=3700, finish_reason="cost_cap")
    stall_watch.scan_once()
    aid = db.list_stall_alerts(status="open")[0]["id"]
    assert stall_watch._still_stalled("goal_cost_capped", sch["id"], time.time())
    stall_watch.scan_once()
    a = db.get_stall_alert(aid)
    assert a["status"] == "open" and a["kind"] == "goal_cost_capped"
    assert len(db.list_stall_alerts()) == 1  # 既没被清掉，也没重复报一条


def test_still_stalled_cost_capped_cleared_once_finish_reason_changes(temp_db):
    """反向：finish_reason 一旦不再是 cost_cap（如用户续跑后再耗尽成 iter_cap），
    cost_capped 告警必须被 auto_resolve 了结，不能靠 kind 漂移长期挂着。"""
    sch = _mk_goal(goal_status="exhausted", enabled=0, age=3700, finish_reason="cost_cap")
    stall_watch.scan_once()
    aid = db.list_stall_alerts(status="open")[0]["id"]
    db.update_schedule(sch["id"], finish_reason="iter_cap")
    stall_watch.scan_once()
    assert db.get_stall_alert(aid)["status"] == "resolved"


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


def test_resolve_stall_alerts_by_ref_covers_snoozed(temp_db):
    """P1：resolve_stall_alerts_by_ref（详情页续跑等路径的即时清除）必须覆盖 snoozed——
    用户点过「稍后」的告警在问题经其他渠道解决后也要即刻了结，否则到期又翻出来误报。
    dismissed 是用户显式的"永不再报"，仍绝不碰。"""
    sch = _mk_goal()
    open_a = db.create_stall_alert(kind="goal_exhausted", ref_id=sch["id"], title="t1",
                                   dedupe_key=f"goal_exhausted:{sch['id']}")
    snoozed_a = db.create_stall_alert(kind="goal_paused", ref_id=sch["id"], title="t2",
                                      dedupe_key=f"goal_paused:{sch['id']}")
    dismissed_a = db.create_stall_alert(kind="goal_stuck", ref_id=sch["id"], title="t3",
                                        dedupe_key=f"goal_stuck:{sch['id']}")
    db.update_stall_alert(snoozed_a["id"], status="snoozed", snooze_until=time.time() + 3600)
    db.update_stall_alert(dismissed_a["id"], status="dismissed")
    n = db.resolve_stall_alerts_by_ref(sch["id"])
    assert n == 2  # open + snoozed 置 resolved，dismissed 不动
    assert db.get_stall_alert(open_a["id"])["status"] == "resolved"
    assert db.get_stall_alert(snoozed_a["id"])["status"] == "resolved"
    assert db.get_stall_alert(dismissed_a["id"])["status"] == "dismissed"


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


def test_snoozed_alert_resolved_when_condition_cleared_before_due(temp_db):
    """P1 回归：snooze 后问题经其他渠道解决（如从目标详情页续跑），snoozed 告警必须被
    _auto_resolve 自愈置 resolved——否则 snooze 到期会把已解决的旧告警重新翻出来误报。"""
    sch = _mk_goal()
    stall_watch.scan_once()
    aid = db.list_stall_alerts(status="open")[0]["id"]
    db.update_stall_alert(aid, status="snoozed", snooze_until=time.time() + 3600)
    # 条件解除：用户从目标详情页续跑（翻回 running/enabled=1），收件箱里是 snoozed 态
    db.update_schedule(sch["id"], goal_status="running", enabled=1)
    stall_watch.scan_once()  # _auto_resolve 复查范围含 snoozed → 置 resolved
    assert db.get_stall_alert(aid)["status"] == "resolved"
    # 到期后（snooze_until 已拨过）默认列表不再出现该告警
    db.update_stall_alert(aid, snooze_until=time.time() - 1)
    assert db.list_stall_alerts() == []


def test_snoozed_still_stalled_stays_snoozed_after_scan(temp_db):
    """仍停滞的 snoozed：复查通过，保持 snoozed 原状——不翻回 open、不动 snooze_until，
    不打扰用户的稍后决定。"""
    _mk_goal()
    stall_watch.scan_once()
    aid = db.list_stall_alerts(status="open")[0]["id"]
    until = time.time() + 3600
    db.update_stall_alert(aid, status="snoozed", snooze_until=until)
    stall_watch.scan_once()
    a = db.get_stall_alert(aid)
    assert a["status"] == "snoozed" and a["snooze_until"] == until


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


def test_dispatch_stuck_detected(temp_db):
    # dispatched 挂着超 STALL_DISPATCH_STUCK_SEC（默认 10800s）且子会话不在跑 → 检出
    #（_mk_failed_subtask 带 status 参数，可造任意状态子任务）
    plan_id = db.new_id()
    _mk_failed_subtask(plan_id, age=4 * 3600, status="dispatched")
    stall_watch.scan_once()
    alerts = db.list_stall_alerts()
    assert len(alerts) == 1
    assert alerts[0]["kind"] == "dispatch_stuck" and alerts[0]["ref_id"] == plan_id


def test_dispatch_stuck_below_threshold_not_reported(temp_db):
    plan_id = db.new_id()
    _mk_failed_subtask(plan_id, age=3600, status="dispatched")  # < 默认 10800s
    stall_watch.scan_once()
    assert db.list_stall_alerts() == []


def test_dispatch_stuck_skipped_when_session_running(temp_db, monkeypatch):
    # S5 卡死的硬否定：子会话仍在跑 = 正常长回合 → 不报（与 S2 同款实例打桩）
    monkeypatch.setattr(session_hub.hub, "is_running", lambda sid: True)
    plan_id = db.new_id()
    _mk_failed_subtask(plan_id, age=4 * 3600, status="dispatched")
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


# ---------- REST 集成（/api/stalls* happy path） ----------
@pytest.fixture
def api_client(temp_db, monkeypatch):
    """带鉴权的 TestClient（先例 test_notify_auth.py）。不进 with 上下文 → 不触发
    lifespan → 不拉起 scheduler 后台循环；db 由 temp_db fixture 指向临时库。"""
    from fastapi.testclient import TestClient

    from server import main as main_module

    monkeypatch.setattr(config, "AUTH_TOKEN", "unit-token")
    return TestClient(main_module.app)


def _post(client, path, json=None):
    return client.post(path, headers={"Authorization": "Bearer unit-token"}, json=json)


def test_api_continue_goal_exhausted_happy_path(api_client):
    """继续（goal_exhausted）：走 _continue_goal_impl（与目标详情页续跑共用实现），
    复位 running/enabled=1 + 抬 max_iterations，告警置 acted。"""
    sch = _mk_goal()
    stall_watch.scan_once()
    aid = db.list_stall_alerts(status="open")[0]["id"]
    resp = _post(api_client, f"/api/stalls/{aid}/continue", json={})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "kind": "goal_exhausted", "ref": sch["id"]}
    fresh = db.get_schedule(sch["id"])
    assert fresh["goal_status"] == "running" and fresh["enabled"] == 1
    assert int(fresh["max_iterations"]) == 3  # iter_count=0 + 默认追加 3 轮
    a = db.get_stall_alert(aid)
    assert a["status"] == "acted" and a["acted_kind"] == "continue"


def test_api_continue_cost_capped_applies_new_limit(api_client):
    """继续（goal_cost_capped）：payload 原样透传给 _continue_goal_impl，新成本上限落到
    schedule 上并刷新成本窗口（cost_base_ts）；负数仍按既有校验拒掉且告警保持 open。"""
    sch = _mk_goal(goal_status="exhausted", enabled=0, age=3700, finish_reason="cost_cap")
    stall_watch.scan_once()
    aid = db.list_stall_alerts(status="open")[0]["id"]
    # 负数 → 400（告警不被消耗，用户可改个值重试）
    bad = _post(api_client, f"/api/stalls/{aid}/continue", json={"max_cost_usd": -1})
    assert bad.status_code == 400
    assert db.get_stall_alert(aid)["status"] == "open"
    resp = _post(api_client, f"/api/stalls/{aid}/continue", json={"max_cost_usd": 40})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "kind": "goal_cost_capped", "ref": sch["id"]}
    fresh = db.get_schedule(sch["id"])
    assert float(fresh["max_cost_usd"]) == 40
    assert fresh["goal_status"] == "running" and fresh["enabled"] == 1
    assert fresh["cost_base_ts"] > 0  # 新成本窗口：续跑时刻起算
    assert db.get_stall_alert(aid)["status"] == "acted"


def test_api_stalls_list_exposes_cost_context(api_client):
    """收件箱列表给 goal 类附成本上下文：前端 prompt 新上限时要拿它预填。"""
    sch = _mk_goal(goal_status="exhausted", enabled=0, age=3700, finish_reason="cost_cap")
    sid = db.get_schedule(sch["id"])["session_id"]
    tid = db.start_task(sid, "第 5/6 轮迭代")
    db.finish_task(tid, "success", cost_usd=12.5)
    stall_watch.scan_once()
    items = api_client.get("/api/stalls",
                           headers={"Authorization": "Bearer unit-token"}).json()
    assert len(items) == 1
    rel = items[0]["related"]
    assert rel["finish_reason"] == "cost_cap"
    assert rel["cost_limit"] == config.GOAL_MAX_COST_USD  # 未设 per-goal 上限 → 全局默认
    assert rel["spent_usd"] == pytest.approx(12.5)


def test_api_stalls_list_cost_context_only_for_cost_capped(api_client):
    """成本上下文只给 goal_cost_capped（与 stall_watch 侧"只在成本熔断分支查花费"同口径）：
    其他 goal 分型不白查一次聚合花费，但迭代/反馈上下文照旧带。"""
    _mk_goal(goal_status="exhausted", enabled=0, age=7300, finish_reason="iter_cap")
    stall_watch.scan_once()
    items = api_client.get("/api/stalls",
                           headers={"Authorization": "Bearer unit-token"}).json()
    assert len(items) == 1 and items[0]["kind"] == "goal_exhausted"
    rel = items[0]["related"]
    assert rel["finish_reason"] == "iter_cap"
    assert rel["max_iterations"] == 6 and "iter_count" in rel
    assert "cost_limit" not in rel and "spent_usd" not in rel


def test_api_continue_goal_stuck_happy_path(api_client):
    """继续（goal_stuck）：复位状态机 + 立即到期，交 scheduler tick 接管。"""
    sch = _mk_goal(goal_status="running", enabled=1, age=7 * 3600)
    stall_watch.scan_once()
    assert db.list_stall_alerts(status="open")[0]["kind"] == "goal_stuck"
    aid = db.list_stall_alerts(status="open")[0]["id"]
    resp = _post(api_client, f"/api/stalls/{aid}/continue", json={})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "kind": "goal_stuck", "ref": sch["id"]}
    fresh = db.get_schedule(sch["id"])
    assert fresh["goal_status"] == "running" and fresh["next_run"] > 0
    assert db.get_stall_alert(aid)["status"] == "acted"


def test_api_skip_happy_path(api_client):
    _mk_goal()
    stall_watch.scan_once()
    aid = db.list_stall_alerts(status="open")[0]["id"]
    resp = _post(api_client, f"/api/stalls/{aid}/skip")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert db.get_stall_alert(aid)["status"] == "dismissed"
    assert db.list_stall_alerts() == []  # 默认列表即刻消失


def test_api_snooze_happy_path(api_client):
    _mk_goal()
    stall_watch.scan_once()
    aid = db.list_stall_alerts(status="open")[0]["id"]
    before = time.time()
    resp = _post(api_client, f"/api/stalls/{aid}/snooze", json={"hours": 2})
    after = time.time()
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert before + 2 * 3600 <= body["snooze_until"] <= after + 2 * 3600
    a = db.get_stall_alert(aid)
    assert a["status"] == "snoozed" and a["snooze_until"] == body["snooze_until"]
    assert db.list_stall_alerts() == []  # 未到期：不出现在默认列表


# ---------- 老库迁移回归（finish_reason 回填 → dedupe_key 去 kind → kind 改判） ----------
def _mk_legacy_goal(session_id, feedback):
    """按本次改动前的数据形态直写库：finish_reason 是新列，老行里恒为空串，当时只有
    last_feedback 的自然语言能表达"为什么终止"，告警侧也只有 kind + ref 拼出的旧 dedupe_key。"""
    sid = db.new_id()
    db._exec(
        "INSERT INTO schedules(id,session_id,prompt,kind,enabled,created_at,goal_status,"
        "last_feedback,finish_reason) VALUES(?,?,?,?,?,?,?,?,'')",
        (sid, session_id, "把训练曲线画出来", "goal", 0, time.time(), "exhausted", feedback),
    )
    return sid


def test_init_db_migrates_legacy_rows_and_is_idempotent(temp_db):
    """temp_db 建的是空库，三段迁移都命中 0 行——存量库的迁移行为零覆盖。这里先按旧格式
    落库（finish_reason 空串、dedupe_key 带 kind 前缀、kind=goal_exhausted），再跑 init_db
    让迁移真正落在已有旧数据上，最后再跑一次确认幂等（迁移重跑不能刷新记录/撞唯一索引）。"""
    sess = db.create_session("老会话", "/tmp/stall-test")
    cost_sid = _mk_legacy_goal(sess["id"], "已达成本上限（$25 ≥ $20），如需继续请提高上限")
    iter_sid = _mk_legacy_goal(sess["id"], "已达迭代上限（6/6 轮），请人工介入")
    cost_a = db.create_stall_alert(kind="goal_exhausted", ref_id=cost_sid, title="目标循环已耗尽",
                                   detail="老文案", dedupe_key=f"goal_exhausted:{cost_sid}")
    iter_a = db.create_stall_alert(kind="goal_exhausted", ref_id=iter_sid, title="目标循环已耗尽",
                                   detail="老文案", dedupe_key=f"goal_exhausted:{iter_sid}")
    # 前置断言：迁移前确实长成旧格式。少了这步，将来 fixture 若顺手升级成"新列已填"，
    # 本测试会退化成空转（迁移一行没碰也算通过）。
    assert db.get_schedule(cost_sid)["finish_reason"] == ""
    assert db.get_stall_alert(cost_a["id"])["dedupe_key"] == f"goal_exhausted:{cost_sid}"

    db.init_db()

    # 1) 按 last_feedback 文案回填结构化终止原因
    assert db.get_schedule(cost_sid)["finish_reason"] == "cost_cap"
    assert db.get_schedule(iter_sid)["finish_reason"] == "iter_cap"
    # 2)+3) 成本熔断那行：dedupe_key 去 kind 化 + kind 按 finish_reason 改判（顺序敏感：
    # 若 kind 迁移跑在回填之前，这里拿到的还是 goal_exhausted）
    c = db.get_stall_alert(cost_a["id"])
    assert c["dedupe_key"] == f"goal:{cost_sid}" and c["kind"] == "goal_cost_capped"
    # 对照行：非成本熔断的分型与阈值都不变，dedupe_key 同样去 kind 化
    i = db.get_stall_alert(iter_a["id"])
    assert i["dedupe_key"] == f"goal:{iter_sid}" and i["kind"] == "goal_exhausted"
    assert c["status"] == "open" and i["status"] == "open"

    # 幂等：二次 init_db 后逐字段原样（含 updated_at——迁移若重跑会把它再刷一遍）
    db.init_db()
    assert db.get_stall_alert(cost_a["id"]) == c
    assert db.get_stall_alert(iter_a["id"]) == i
    assert db.get_schedule(cost_sid)["finish_reason"] == "cost_cap"
    assert len(db._query("SELECT * FROM stall_alerts")) == 2
