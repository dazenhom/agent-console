"""server.db.bulk_cleanup_todos：批量归档、清理及关联行删除。

全部落临时库（temp_db fixture），不碰生产 data/console.db。
"""

import pytest


def test_archive_finished_only_archives_unarchived_finished_todos(temp_db):
    pending = temp_db.create_todo("pending", status="pending")
    in_progress = temp_db.create_todo("in progress", status="in_progress")
    done = temp_db.create_todo("done", status="done")
    cancelled = temp_db.create_todo("cancelled", status="cancelled")
    already_archived = temp_db.create_todo("already archived", status="done")
    temp_db.update_todo(already_archived["id"], archived=1)

    result = temp_db.bulk_cleanup_todos("archive_finished")

    assert result == {"scope": "archive_finished", "affected": 2, "skipped": 0}
    rows = {
        row["id"]: dict(row)
        for row in temp_db._query(
            "SELECT id, archived FROM todos WHERE id IN (?,?,?,?,?)",
            (
                pending["id"],
                in_progress["id"],
                done["id"],
                cancelled["id"],
                already_archived["id"],
            ),
        )
    }
    assert rows[pending["id"]]["archived"] == 0
    assert rows[in_progress["id"]]["archived"] == 0
    assert rows[done["id"]]["archived"] == 1
    assert rows[cancelled["id"]]["archived"] == 1
    assert rows[already_archived["id"]]["archived"] == 1


def test_cleanup_scopes_do_not_modify_or_delete_triage_todos(temp_db):
    triage = temp_db.create_triage_todo(
        "triage",
        "needs review",
        0.8,
        "review",
        {"source": "test"},
    )

    assert temp_db.bulk_cleanup_todos("archive_finished") == {
        "scope": "archive_finished",
        "affected": 0,
        "skipped": 0,
    }
    row = temp_db._query(
        "SELECT status, archived FROM todos WHERE id=?", (triage["id"],)
    )[0]
    assert row["status"] == "triage"
    assert row["archived"] == 0

    temp_db.update_todo(triage["id"], archived=1)
    assert temp_db.bulk_cleanup_todos("purge_archived") == {
        "scope": "purge_archived",
        "affected": 0,
        "skipped": 0,
    }
    row = temp_db._query(
        "SELECT status, archived FROM todos WHERE id=?", (triage["id"],)
    )[0]
    assert row["status"] == "triage"
    assert row["archived"] == 1


def test_purge_archived_removes_todo_session_rows(temp_db):
    session = temp_db.create_session("session", "/tmp")
    todo = temp_db.create_todo("finished", status="done")
    temp_db.update_todo(todo["id"], archived=1)
    temp_db.set_todo_sessions(todo["id"], [session["id"]])

    assert temp_db._query(
        "SELECT todo_id FROM todo_sessions WHERE todo_id=?", (todo["id"],)
    )

    assert temp_db.bulk_cleanup_todos("purge_archived") == {
        "scope": "purge_archived",
        "affected": 1,
        "skipped": 0,
    }
    assert temp_db._query("SELECT id FROM todos WHERE id=?", (todo["id"],)) == []
    assert temp_db._query(
        "SELECT todo_id FROM todo_sessions WHERE todo_id=?", (todo["id"],)
    ) == []


def test_purge_archived_removes_in_progress_todo_linked_to_idle_session(temp_db):
    session = temp_db.create_session("idle session", "/tmp")
    todo = temp_db.create_todo("stale in progress", status="in_progress")
    temp_db.update_todo(todo["id"], archived=1)
    temp_db.set_todo_sessions(todo["id"], [session["id"]])

    assert temp_db.bulk_cleanup_todos("purge_archived") == {
        "scope": "purge_archived",
        "affected": 1,
        "skipped": 0,
    }
    assert temp_db._query("SELECT id FROM todos WHERE id=?", (todo["id"],)) == []
    assert temp_db._query(
        "SELECT todo_id FROM todo_sessions WHERE todo_id=?", (todo["id"],)
    ) == []


def test_purge_archived_skips_in_progress_todo_linked_to_running_session(temp_db):
    session = temp_db.create_session("running session", "/tmp")
    temp_db.update_session(session["id"], status="running")
    todo = temp_db.create_todo("running in progress", status="in_progress")
    temp_db.update_todo(todo["id"], archived=1)
    temp_db.set_todo_sessions(todo["id"], [session["id"]])

    assert temp_db.bulk_cleanup_todos("purge_archived") == {
        "scope": "purge_archived",
        "affected": 0,
        "skipped": 1,
    }
    assert temp_db._query("SELECT id FROM todos WHERE id=?", (todo["id"],))
    assert temp_db._query(
        "SELECT todo_id FROM todo_sessions WHERE todo_id=?", (todo["id"],)
    )


def test_bulk_cleanup_rejects_invalid_scope(temp_db):
    with pytest.raises(ValueError, match="invalid cleanup scope"):
        temp_db.bulk_cleanup_todos("bogus")
