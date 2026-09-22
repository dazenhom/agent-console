"""messages 尾部分页的游标平局回归。

现状：created_at 只有秒级精度，`ORDER BY created_at DESC` 无次级键时同秒消息之间的
顺序不确定，而游标只比较 created_at → 边界那一秒的消息跨页被静默跳过（ops 复现：
1+8+1 条、limit=5 翻两页实得 6/10）。修法是把排序键固定成 (created_at, id) 全序，
游标用 (created_at, id) 复合比较，并让 API 暴露可选的 before_id。
"""
import asyncio

import pytest

from server import db, main


def _insert(sid: str, mid: str, ts: float) -> str:
    """直插一条指定 created_at 的消息（add_message 里 created_at 恒取 now，造平局用不上）。"""
    db._exec("INSERT INTO messages(id,session_id,role,content,created_at) VALUES(?,?,?,?,?)",
             (mid, sid, "user", '"x"', ts))
    return mid


@pytest.fixture
def same_second_messages(temp_db):
    """一个会话里 10 条 created_at 完全相同的消息（真实同秒写入的形状）。"""
    ts = 1_700_000_000.0
    sid = temp_db.create_session("同秒消息会话", "/tmp")["id"]
    ids = [_insert(sid, f"m{i:02d}", ts) for i in range(10)]
    return sid, ids


def test_same_created_at_pagination_returns_all(same_second_messages, temp_db):
    sid, ids = same_second_messages

    page1 = temp_db.list_messages(sid, limit=5)
    assert len(page1) == 5
    # 游标带 id：同秒区间也能继续往下翻，页与页之间不重不漏
    page2 = temp_db.list_messages(sid, limit=5, before=page1[0]["created_at"],
                                  before_id=page1[0]["id"])
    assert len(page2) == 5
    assert {m["id"] for m in page1 + page2} == set(ids)   # 10 条全取回
    # 再往前确实没有了
    assert temp_db.list_messages(sid, limit=5, before=page2[0]["created_at"],
                                 before_id=page2[0]["id"]) == []


def test_cursor_without_id_keeps_legacy_semantics(same_second_messages, temp_db):
    # 不传 before_id：保持旧的单值游标语义（只比 created_at），老调用方行为不变——
    # 同秒区间一次翻不完是修复前就有的已知行为，由前端补 id 解决。
    sid, _ = same_second_messages
    page1 = temp_db.list_messages(sid, limit=5)
    assert temp_db.list_messages(sid, limit=5, before=page1[0]["created_at"]) == []


def test_pagination_across_distinct_seconds_still_correct(temp_db):
    # 跨秒的常规翻页（老路径）不能被改坏：逐页取全、各页内升序、末尾为空
    sid = temp_db.create_session("跨秒会话", "/tmp")["id"]
    seconds = [1000.0, 1000.0, 1001.0, 1002.0, 1002.0, 1003.0, 1004.0]
    ids = [_insert(sid, f"m{i}", ts) for i, ts in enumerate(seconds)]

    page1 = temp_db.list_messages(sid, limit=3)
    assert [m["created_at"] for m in page1] == [1002.0, 1003.0, 1004.0]
    page2 = temp_db.list_messages(sid, limit=3, before=page1[0]["created_at"],
                                  before_id=page1[0]["id"])
    assert [m["created_at"] for m in page2] == [1000.0, 1001.0, 1002.0]
    page3 = temp_db.list_messages(sid, limit=3, before=page2[0]["created_at"],
                                  before_id=page2[0]["id"])
    assert [m["created_at"] for m in page3] == [1000.0]
    assert {m["id"] for m in page1 + page2 + page3} == set(ids)
    assert temp_db.list_messages(sid, limit=3, before=page3[0]["created_at"],
                                 before_id=page3[0]["id"]) == []
    # 全量模式仍是升序，且不传游标的行为逐字不变
    assert [m["created_at"] for m in temp_db.list_messages(sid)] == sorted(seconds)


def test_api_forwards_before_id_and_keeps_full_mode(temp_db, same_second_messages):
    # 路由层透传：不带 before_id 的旧调用（速览/搜索 limit=0 全量路径）行为不变，
    # 带 before_id 时能翻完整个同秒区间
    sid, ids = same_second_messages
    page1 = asyncio.run(main.get_messages(sid, limit=5))
    assert len(page1) == 5
    page2 = asyncio.run(main.get_messages(sid, limit=5, before=page1[0]["created_at"],
                                          before_id=page1[0]["id"]))
    assert {m["id"] for m in page1 + page2} == set(ids)
    assert len(asyncio.run(main.get_messages(sid, limit=0))) == 10
