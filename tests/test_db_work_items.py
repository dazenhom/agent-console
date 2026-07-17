"""server.db 的 work_items CRUD：create / update_by_ref / list（过滤）/ get / id_for_ref。

全部落临时库（temp_db fixture），不碰生产 data/console.db。
"""


def test_create_and_get_work_item(temp_db):
    wid = temp_db.create_work_item(
        origin="dispatch", topology="fanout", isolation="worktree",
        verify_mode="nl", status="pending", ref_id="plan-1",
        session_id="sess-1", summary="做点事",
    )
    assert wid
    item = temp_db.get_work_item(wid)
    assert item is not None
    assert item["id"] == wid
    assert item["origin"] == "dispatch"
    assert item["topology"] == "fanout"
    assert item["status"] == "pending"
    assert item["ref_id"] == "plan-1"
    assert item["session_id"] == "sess-1"
    assert item["summary"] == "做点事"


def test_get_work_item_missing_returns_none(temp_db):
    assert temp_db.get_work_item("nope") is None


def test_update_status_by_ref(temp_db):
    wid = temp_db.create_work_item(
        origin="goal", topology="iterate", isolation="shared",
        verify_mode="nl", status="running", ref_id="ref-42",
    )
    temp_db.update_work_item_status_by_ref("ref-42", "done")
    assert temp_db.get_work_item(wid)["status"] == "done"


def test_update_status_by_ref_unknown_is_silent(temp_db):
    # ref 不存在时静默跳过，不抛错。
    temp_db.update_work_item_status_by_ref("ghost", "done")


def test_work_item_id_for_ref(temp_db):
    wid = temp_db.create_work_item(
        origin="arbiter", topology="candidates", isolation="shared",
        verify_mode="candidates", status="pending", ref_id="arb-9",
    )
    assert temp_db.work_item_id_for_ref("arb-9") == wid
    assert temp_db.work_item_id_for_ref("missing") is None
    # 空 ref 直接返回 None，不查库
    assert temp_db.work_item_id_for_ref("") is None


def test_list_work_items_filters_by_origin_and_status(temp_db):
    temp_db.create_work_item(
        origin="goal", topology="iterate", isolation="shared",
        verify_mode="nl", status="done", ref_id="a",
    )
    temp_db.create_work_item(
        origin="dispatch", topology="fanout", isolation="worktree",
        verify_mode="nl", status="pending", ref_id="b",
    )
    temp_db.create_work_item(
        origin="dispatch", topology="fanout", isolation="worktree",
        verify_mode="nl", status="done", ref_id="c",
    )

    # 无过滤：全部三条
    assert len(temp_db.list_work_items()) == 3
    # 只按 origin
    dispatch = temp_db.list_work_items(origin="dispatch")
    assert {i["ref_id"] for i in dispatch} == {"b", "c"}
    # origin + status 联合过滤
    done_dispatch = temp_db.list_work_items(origin="dispatch", status="done")
    assert [i["ref_id"] for i in done_dispatch] == ["c"]
    # 只按 status
    done = temp_db.list_work_items(status="done")
    assert {i["ref_id"] for i in done} == {"a", "c"}


def test_list_work_items_respects_limit(temp_db):
    for i in range(5):
        temp_db.create_work_item(
            origin="goal", topology="iterate", isolation="shared",
            verify_mode="nl", status="pending", ref_id=f"r{i}",
        )
    assert len(temp_db.list_work_items(limit=2)) == 2


def test_create_work_item_safe_returns_wid_on_success(temp_db):
    wid = temp_db.create_work_item_safe(
        origin="goal", topology="iterate", isolation="shared",
        verify_mode="nl", status="pending", ref_id="safe-ok",
    )
    assert wid
    assert temp_db.get_work_item(wid)["ref_id"] == "safe-ok"


def test_create_work_item_safe_swallows_exception(temp_db, monkeypatch):
    # 底层 create_work_item 抛异常时，safe 版本吞掉异常并返回空串，绝不上抛（work_items 纯观测）。
    def _boom(*args, **kwargs):
        raise RuntimeError("db exploded")
    monkeypatch.setattr(temp_db, "create_work_item", _boom)
    wid = temp_db.create_work_item_safe(
        origin="arbiter", topology="candidates", isolation="shared",
        verify_mode="candidates", status="running", ref_id="safe-fail",
    )
    assert wid == ""

