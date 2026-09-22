"""回合成本增量语义：result 事件的 total_cost_usd 是常驻进程的累计值，落库必须是回合增量。

背景（2026-09-22 实测）：常驻 claude 进程跨回合复用，CLI 报的 total_cost_usd 是「从进程启动
至今的累计花费」，每回合单调递增、进程重生才归零。原样落库后被 db.sum_session_cost SUM 成全站
虚高 10.4x（$43,987 vs 真实 $4,240），并让 3 条 goal 按虚高金额误熔断。

修法：runner 在事件转发前用进程内 baseline 换算成 turn_cost_usd
（server/claude_runner.stamp_turn_cost），session_hub.translate_event 优先取该字段落库。

本测试不碰生产库、不真起子进程：_reader 用假 stdout 驱动，覆盖 ①正常递增 ②进程重生的回退
③首次 ④delta 永不为负 ⑤translate_event 的取值口径（含 codex 回落到 total_cost_usd）。
"""
import asyncio
import json
from types import SimpleNamespace

from server import config
from server.claude_runner import ClaudeRunner, LoopDetector, stamp_turn_cost
from server.session_hub import translate_event


# ---------------- stamp_turn_cost：三种 delta 分支 ----------------

def test_first_turn_delta_is_raw_cost():
    """首次回合：baseline=0，delta 就是 CLI 报的值。"""
    sess = {"cost_baseline": 0.0}
    evt = {"type": "result", "total_cost_usd": 5.0}
    stamp_turn_cost(sess, evt)
    assert evt["turn_cost_usd"] == 5.0
    assert sess["cost_baseline"] == 5.0


def test_same_process_two_turns_accumulate_to_increment():
    """同进程两回合：CLI 报累计 5、10 → 落库口径 5、5。"""
    sess = {"cost_baseline": 0.0}
    first = {"type": "result", "total_cost_usd": 5.0}
    second = {"type": "result", "total_cost_usd": 10.0}
    stamp_turn_cost(sess, first)
    stamp_turn_cost(sess, second)
    assert first["turn_cost_usd"] == 5.0
    assert second["turn_cost_usd"] == 5.0
    assert sess["cost_baseline"] == 10.0


def test_process_respawn_regression_yields_raw_not_negative():
    """进程重生：累计值从 10 掉回 3 → delta 取 3（绝不落负数），游标跟着回退。"""
    sess = {"cost_baseline": 10.0}
    evt = {"type": "result", "total_cost_usd": 3.0}
    stamp_turn_cost(sess, evt)
    assert evt["turn_cost_usd"] == 3.0
    assert sess["cost_baseline"] == 3.0


def test_full_sequence_matching_production_bug():
    """5、10、3 一路走下来：落库 5、5、3（对应「同进程两回合 + 重生一回合」）。"""
    sess = {"cost_baseline": 0.0}
    evts = [{"type": "result", "total_cost_usd": v} for v in (5.0, 10.0, 3.0)]
    for e in evts:
        stamp_turn_cost(sess, e)
    assert [e["turn_cost_usd"] for e in evts] == [5.0, 5.0, 3.0]


def test_delta_never_negative_across_monotone_regressions():
    """任意序列下 delta 都 ≥ 0：重生点可以反复出现（崩溃→resume→再崩溃）。"""
    sess = {"cost_baseline": 0.0}
    seen = []
    for v in (0.0, 12.5, 2.0, 9.0, 9.0, 0.5):
        evt = {"type": "result", "total_cost_usd": v}
        stamp_turn_cost(sess, evt)
        seen.append(evt["turn_cost_usd"])
    assert all(d >= 0 for d in seen)
    assert seen == [0.0, 12.5, 2.0, 7.0, 0.0, 0.5]


def test_non_numeric_cost_is_ignored():
    """成本缺失/非数值（CLI 未报、或报了字符串）不盖章、不动游标，让上层回落原字段。"""
    for raw in (None, "", "1.23", True):
        sess = {"cost_baseline": 7.0}
        evt = {"type": "result", "total_cost_usd": raw}
        stamp_turn_cost(sess, evt)
        assert "turn_cost_usd" not in evt
        assert sess["cost_baseline"] == 7.0


# ---------------- translate_event：落库取值口径 ----------------

def test_translate_event_prefers_turn_cost():
    """有 turn_cost_usd（claude 常驻路径）时落库字段取增量，不是累计值。"""
    msgs = translate_event({"type": "result", "subtype": "success",
                            "total_cost_usd": 26.89, "turn_cost_usd": 5.01,
                            "num_turns": 3})
    assert len(msgs) == 1
    assert msgs[0]["role"] == "result"
    assert msgs[0]["content"]["cost_usd"] == 5.01


def test_translate_event_falls_back_to_total_for_codex():
    """codex 侧只给 total_cost_usd（本就已是增量）→ 回落取它，行为不变。"""
    msgs = translate_event({"type": "result", "subtype": "success",
                            "total_cost_usd": 0.42, "num_turns": 1})
    assert msgs[0]["content"]["cost_usd"] == 0.42


def test_translate_event_missing_cost_stays_none():
    """两边都没有成本 → None（前端显示"未计费"，不是 0）。"""
    msgs = translate_event({"type": "result", "subtype": "success", "num_turns": 1})
    assert msgs[0]["content"]["cost_usd"] is None


# ---------------- _reader 接线：确保 helper 真被调用 ----------------

class _FakeStdout:
    """按行吐给定字节的假 stdout；吐完返回 b"" 表示进程收尾，_reader 自然退出。"""

    def __init__(self, payloads: list[dict]):
        self._lines = [json.dumps(p).encode() + b"\n" for p in payloads]

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""


def _drive_reader(payloads: list[dict]) -> list[dict]:
    """用假 stdout 跑一遍 ClaudeRunner._reader，返回转发出去的原始事件。"""
    runner = ClaudeRunner()
    seen: list[dict] = []

    async def on_event(evt):
        seen.append(evt)

    sess = {
        "proc": SimpleNamespace(stdout=_FakeStdout(payloads)),
        "claude_sid": None, "last_active": 0.0, "turn_active": True,
        "on_event": on_event, "on_permission": None, "on_session_id": None,
        "out_of_turn_cb": None, "result_evt": None,
        "cost_baseline": 0.0,
        "loop_detector": LoopDetector(config.CLAUDE_LOOP_REPEAT, config.CLAUDE_LOOP_ERRORS,
                                      config.CLAUDE_LOOP_WARN_STEPS),
    }
    runner._sessions["sess-x"] = sess
    asyncio.run(runner._reader("sess-x"))
    return seen


def test_reader_stamps_turn_cost_before_forwarding():
    """三回合累计 5/10/3 经 _reader 转发后各自带上 turn_cost_usd=5/5/3（接线不靠人记）。"""
    seen = _drive_reader([
        {"type": "result", "subtype": "success", "session_id": "sid-1", "total_cost_usd": 5.0},
        {"type": "result", "subtype": "success", "total_cost_usd": 10.0},
        {"type": "result", "subtype": "success", "total_cost_usd": 3.0},
    ])
    results = [e for e in seen if e.get("type") == "result"]
    assert [r["turn_cost_usd"] for r in results] == [5.0, 5.0, 3.0]
    # 原始累计值保留，便于排查时对照 CLI 口径
    assert [r["total_cost_usd"] for r in results] == [5.0, 10.0, 3.0]


def test_reader_resume_failed_stub_does_not_move_baseline():
    """resume 失败的坏 result 被丢弃、也不该动成本游标（否则下一回合增量凭空多算）。"""
    seen = _drive_reader([
        {"type": "result", "subtype": "error_during_execution", "num_turns": 0,
         "errors": "No conversation found with session ID xyz", "total_cost_usd": 0.0},
        {"type": "result", "subtype": "success", "num_turns": 2, "total_cost_usd": 5.0},
    ])
    results = [e for e in seen if e.get("type") == "result"]
    assert len(results) == 1
    assert results[0]["turn_cost_usd"] == 5.0


# ---------------- 端到端：CLI 事件 → 落库 → 熔断用的 SUM ----------------

def test_two_turns_of_5_and_10_land_as_10_total(temp_db):
    """照生产路径走一遍：累计 5、10 两回合落库后 SUM=10（修复前是 15，虚高即源于此）。

    runner 盖章 → translate_event 取口径 → tasks.cost_usd → sum_session_cost，
    目标循环的成本熔断读的就是最后这个数。
    """
    sess = {"cost_baseline": 0.0}
    for raw in (5.0, 10.0):
        evt = {"type": "result", "subtype": "success", "num_turns": 1, "total_cost_usd": raw}
        stamp_turn_cost(sess, evt)
        content = translate_event(evt)[0]["content"]
        tid = temp_db.start_task("sess-x", "回合")
        temp_db.finish_task(tid, "success", cost_usd=content["cost_usd"],
                            num_turns=content["num_turns"])
    assert temp_db.sum_session_cost("sess-x", 0) == 10.0
