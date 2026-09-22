"""「继续（带记忆续跑）」：schedules.resume_note 的拼装、注入与清空。

覆盖三件事：
1) scheduler.build_resume_note 的文案口径——停滞原因读 finish_reason（人工暂停不许读，
   那是上次终止的残留）、末轮验收反馈取 goal_iterations 里 verdict 非空的那条（不是
   sch.last_feedback，_finish_goal 会把熔断文案盖上去）、成本熔断才追加省钱提示。
2) 注入位置与两条 prompt 链（flat 的 _build_goal_prompt / planned 的 _build_subtask_prompt）。
3) 一次性语义：start_turn 成功即清空（只影响第一轮），失败分支不清（下个 tick 重试不丢），
   PUT enabled=1 从头重跑也清空（防残留背景窜进新循环第一轮）。

temp_db 走真实库；hub.start_turn / broadcast_monitor 打桩成 no-op（真跑会建会话+发企微）。
REST 边界（2000 字上限）走 TestClient，先例 test_stall_watch.py。
"""
import asyncio
import time

import pytest

from server import config, db, main, scheduler, session_hub


def _mk_goal(goal_status="exhausted", enabled=0, finish_reason="", iter_count=2,
             max_iterations=6, last_run_age=7200, last_feedback=""):
    """建一条 kind=goal 的 schedule，并把停滞锚点（last_run）拨到 age 秒前。"""
    sess = db.create_session("测试会话", "/tmp/resume-note-test")
    sch = db.create_schedule(sess["id"], "把交付核验报告写完", "goal", None, None, 0,
                             stop_condition="报告落盘", max_iterations=max_iterations)
    db.update_schedule(sch["id"], enabled=enabled, goal_status=goal_status, iter_count=iter_count,
                       last_run=time.time() - last_run_age, finish_reason=finish_reason,
                       last_feedback=last_feedback)
    return db.get_schedule(sch["id"]), sess


def _stub_hub(monkeypatch, captured=None, boom=False):
    """hub 打桩：start_turn 记下 prompt（或按需抛错），monitor 广播 no-op。"""
    async def _start_turn(sid, prompt):
        if boom:
            raise RuntimeError("启动回合失败")
        if captured is not None:
            captured.append(prompt)

    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(session_hub.hub, "start_turn", _start_turn)
    monkeypatch.setattr(session_hub.hub, "broadcast_monitor", _noop)


# ---------- build_resume_note：停滞原因人话 ----------
def test_resume_note_cost_cap_text_with_saving_hint(temp_db):
    sch, _ = _mk_goal(finish_reason="cost_cap", iter_count=4, max_iterations=6, last_run_age=7200)
    note = scheduler.build_resume_note(sch, "先补上 B 部分的验证证据")
    assert "成本达到上限" in note
    assert "第 4/6 轮" in note
    assert "2.0 小时" in note          # 复用 stall_watch._fmt_dur 的口径
    assert "优先选省钱的做法" in note   # 成本类专属：这次重开了成本窗口
    assert "先补上 B 部分的验证证据" in note and "【本次续跑的额外指示｜优先遵守】" in note


def test_resume_note_iter_cap_has_no_saving_hint(temp_db):
    sch, _ = _mk_goal(finish_reason="iter_cap", iter_count=6, max_iterations=6)
    note = scheduler.build_resume_note(sch, "")
    assert "迭代轮数达到上限" in note
    assert "省钱" not in note  # 只有成本熔断才提醒省预算


def test_resume_note_paused_does_not_read_stale_finish_reason(temp_db):
    """人工暂停（enabled=0 且非终态）走的是 PUT，库里 finish_reason 残留着上次终止的
    cost_cap——照读会编出"成本达到上限"的假背景，误导用户以为又要定预算。"""
    sch, _ = _mk_goal(goal_status="running", enabled=0, finish_reason="cost_cap")
    note = scheduler.build_resume_note(sch, "")
    assert "被人工暂停" in note
    assert "成本" not in note and "省钱" not in note


def test_resume_note_unknown_finish_reason_falls_back(temp_db):
    sch, _ = _mk_goal(finish_reason="", iter_count=3)
    assert "验收未通过且轮次耗尽" in scheduler.build_resume_note(sch, "")


# ---------- 末轮反馈的取数口径 ----------
def test_resume_note_uses_last_scored_iteration_not_last_feedback(temp_db):
    """_finish_goal 进终态时把熔断文案写进 last_feedback，真实末轮验收反馈只留在
    goal_iterations 里——背景必须取后者，否则续跑拿到的是"已达成本上限"这种没营养的话。"""
    sch, _ = _mk_goal(finish_reason="cost_cap", last_feedback="已达成本上限（$20 ≥ $20）")
    first = db.create_goal_iteration(sch["id"], 1, "第一轮 prompt")
    db.update_goal_iteration(first["id"], verdict="continue", feedback="还差一段验证证据")
    second = db.create_goal_iteration(sch["id"], 2, "第二轮 prompt")
    db.update_goal_iteration(second["id"], verdict="", feedback="")  # 没验收的新轮，不算
    note = scheduler.build_resume_note(sch, "")
    assert "最后一轮验收反馈：还差一段验证证据" in note
    assert "已达成本上限" not in note


def test_last_scored_iteration_feedback_picks_max_iter_no(temp_db):
    sch, _ = _mk_goal(finish_reason="iter_cap")
    for no, fb in ((1, "旧反馈"), (3, "新反馈")):
        row = db.create_goal_iteration(sch["id"], no, "p")
        db.update_goal_iteration(row["id"], verdict="continue", feedback=fb)
    assert db.last_scored_iteration_feedback(sch["id"]) == "新反馈"
    assert db.last_scored_iteration_feedback("不存在") == ""


# ---------- 注入位置（两条 prompt 链） ----------
def test_note_injected_between_feedback_and_iteration_hint(temp_db):
    sch, _ = _mk_goal(finish_reason="cost_cap", last_feedback="上轮验收：还差证据")
    sch = db.get_schedule(sch["id"])
    db.update_schedule(sch["id"], resume_note=scheduler.build_resume_note(sch, "先做 A 再做 B"))
    prompt = scheduler._build_goal_prompt(db.get_schedule(sch["id"]))
    i_fb = prompt.index("【上一轮验收反馈")
    i_note = prompt.index("【续跑背景")
    i_hint = prompt.index("这是第")
    assert i_fb < i_note < i_hint


def test_planned_subtask_prompt_also_injects_note(temp_db):
    """planned 链过去完全不读 last_feedback，续跑背景只能从 resume_note 走——漏了这条
    「这次换个跑法」在拆解型目标上会被静默丢掉。"""
    sch, _ = _mk_goal(finish_reason="iter_cap")
    sch = db.get_schedule(sch["id"])
    db.update_schedule(sch["id"], resume_note=scheduler.build_resume_note(sch, "先做 A 再做 B"))
    prompt = scheduler._build_subtask_prompt(db.get_schedule(sch["id"]),
                                            {"title": "第一步", "instruction": "把 A 做完"}, 0, 3)
    assert "【续跑背景" in prompt and "先做 A 再做 B" in prompt


# ---------- 一次性语义 ----------
def test_note_consumed_and_cleared_after_start_turn(temp_db, monkeypatch):
    sch, sess = _mk_goal(finish_reason="cost_cap")
    it = db.create_goal_iteration(sch["id"], 2, "上一轮")
    db.update_goal_iteration(it["id"], verdict="continue", feedback="还差 B 部分")
    asyncio.run(main._continue_goal_impl(sch["id"], {"resume_note": "先做完 A 再动 B"}))
    assert "先做完 A 再动 B" in db.get_schedule(sch["id"])["resume_note"]

    captured = []
    _stub_hub(monkeypatch, captured)
    asyncio.run(scheduler._tick_goal(db.get_schedule(sch["id"]), sess, time.time()))
    # 本轮 prompt 带上了背景与指示
    assert len(captured) == 1
    assert "【续跑背景" in captured[0] and "先做完 A 再动 B" in captured[0]
    assert "还差 B 部分" in captured[0]
    assert "先做完 A 再动 B" in db.list_goal_iterations(sch["id"])[-1]["prompt"]
    fresh = db.get_schedule(sch["id"])
    assert fresh["resume_note"] == "" and fresh["goal_status"] == "producing"
    # 第二轮回到常规 prompt：不再重复注入（目标/完成标准每轮本就现读）
    assert "【续跑背景" not in scheduler._build_goal_prompt(fresh)


def test_note_kept_when_start_turn_fails(temp_db, monkeypatch):
    """起回合失败 → 状态不推进、resume_note 也留着，下个 tick 重试时背景不丢。"""
    sch, sess = _mk_goal(finish_reason="iter_cap")
    asyncio.run(main._continue_goal_impl(sch["id"], {"resume_note": "先做 A"}))
    _stub_hub(monkeypatch, boom=True)
    asyncio.run(scheduler._tick_goal(db.get_schedule(sch["id"]), sess, time.time()))
    fresh = db.get_schedule(sch["id"])
    assert "先做 A" in fresh["resume_note"]
    assert fresh["goal_status"] == "running"  # 未推进，等下一 tick


def test_second_continue_overwrites_previous_note(temp_db):
    """两次续跑各带指示：第二次覆盖第一次，不累积（resume_note 是整段重拼的占位字段）。"""
    sch, _ = _mk_goal(finish_reason="cost_cap")
    asyncio.run(main._continue_goal_impl(sch["id"], {"resume_note": "第一次的指示"}))
    assert "第一次的指示" in db.get_schedule(sch["id"])["resume_note"]
    # 调度器还没消费就又停了一次（真实路径：续跑后再次耗尽/被人工暂停）
    db.update_schedule(sch["id"], goal_status="exhausted", enabled=0, finish_reason="iter_cap")
    asyncio.run(main._continue_goal_impl(sch["id"], {"resume_note": "第二次的指示"}))
    note = db.get_schedule(sch["id"])["resume_note"]
    assert "第二次的指示" in note and "第一次的指示" not in note
    assert "迭代轮数达到上限" in note  # 背景按最新一次停止原因重拼


def test_put_reenable_clears_resume_note(temp_db):
    """PUT enabled=1 是"从头重跑"：残留的续跑背景会窜进新循环第一轮 prompt，必须清。"""
    sch, _ = _mk_goal(finish_reason="cost_cap", iter_count=5)
    db.update_schedule(sch["id"], resume_note="上一条目标留下的背景")
    asyncio.run(main.schedules_update(sch["id"], {"enabled": True}))
    fresh = db.get_schedule(sch["id"])
    assert fresh["resume_note"] == ""
    assert int(fresh["iter_count"]) == 0 and fresh["goal_status"] == "running"


# ---------- 边界：2000 字 ----------
@pytest.fixture
def api_client(temp_db, monkeypatch):
    """带鉴权的 TestClient（先例 test_stall_watch.py），不触发 lifespan。"""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(config, "AUTH_TOKEN", "unit-token")
    return TestClient(main.app)


def test_resume_note_length_limit(api_client):
    sch, _ = _mk_goal(finish_reason="cost_cap")
    url = f"/api/schedules/{sch['id']}/continue"
    headers = {"Authorization": "Bearer unit-token"}
    too_long = api_client.post(url, headers=headers, json={"resume_note": "字" * 2001})
    assert too_long.status_code == 400
    assert "2000" in too_long.json()["detail"]
    # 400 不改库：告警/调度都还在原状态，用户改短了能直接重试
    assert db.get_schedule(sch["id"])["goal_status"] == "exhausted"
    ok = api_client.post(url, headers=headers, json={"resume_note": "字" * 2000})
    assert ok.status_code == 200
    assert "字" * 2000 in ok.json()["resume_note"]
