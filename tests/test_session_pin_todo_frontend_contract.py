import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "web" / "style.css").read_text(encoding="utf-8")


def _function_body(name: str, next_marker: str) -> str:
    match = re.search(
        rf"function {name}\([^)]*\) \{{(?P<body>.*?)\n  \}}\n\n  {next_marker}",
        APP_JS,
        re.DOTALL,
    )
    assert match
    return match.group("body")


def test_session_row_contains_pin_and_linked_todo_titles():
    body = _function_body("renderSessionRow", r"// 路径缩写")
    assert "s-pin" in body
    assert "linked_todo_titles" in body
    assert 'el("div", "s-todo"' in body


def test_grouped_session_sort_compares_pinned_before_timestamp():
    body = _function_body("fillListGrouped", r"// 渲染一个调度批次分组")
    sort = re.search(r"items\.sort\((?P<sort>.*?)\);", body, re.DOTALL)
    assert sort
    comparator = sort.group("sort")
    assert "pinned" in comparator
    assert "ts" in comparator
    assert comparator.index("pinned") < comparator.index("ts")


def test_pin_button_selector_has_desktop_and_mobile_rules():
    assert STYLE_CSS.count(".s-peek, .s-del, .s-pin") == 2


def test_archived_session_actions_do_not_include_pin_button():
    body = _function_body("renderSessionRow", r"// 路径缩写")
    archived = re.search(
        r"if \(isArchived\) \{(?P<body>.*?)\n    \} else \{",
        body,
        re.DOTALL,
    )
    assert archived
    assert "s-pin" not in archived.group("body")


def test_linked_todo_bar_is_between_detail_meta_and_engine_select():
    detail_meta = INDEX_HTML.index('class="detail-meta"')
    todo_bar = INDEX_HTML.index('id="linked-todo-bar"')
    engine_select = INDEX_HTML.index('id="engine-select"')
    assert detail_meta < todo_bar < engine_select


def test_linked_todo_updates_cover_definition_and_both_switch_branches():
    assert APP_JS.count("updateLinkedTodoBar(") >= 3


def test_linked_todo_text_is_shared_by_multiple_consumers():
    assert APP_JS.count("linkedTodoText(") >= 3


def test_todo_bar_has_base_and_mobile_styles():
    assert STYLE_CSS.count(".todo-bar") >= 2


def test_linked_todo_update_resets_expanded_state():
    body = _function_body("updateLinkedTodoBar", r'\$\("workdir-bar"\)\.onclick')
    assert 'classList.remove("expanded")' in body


def test_pinned_sessions_sort_first_without_changing_updated_at(temp_db):
    older = temp_db.create_session("older", "/tmp")
    newer = temp_db.create_session("newer", "/tmp")
    temp_db._exec("UPDATE sessions SET updated_at=? WHERE id=?", (100.0, older["id"]))
    temp_db._exec("UPDATE sessions SET updated_at=? WHERE id=?", (200.0, newer["id"]))

    before = temp_db.get_session(older["id"])["updated_at"]
    temp_db.set_session_pinned(older["id"], True)
    rows = temp_db.list_sessions()

    assert [row["id"] for row in rows[:2]] == [older["id"], newer["id"]]
    assert temp_db.get_session(older["id"])["updated_at"] == before

    temp_db.set_session_pinned(older["id"], False)
    rows = temp_db.list_sessions()
    assert [row["id"] for row in rows[:2]] == [newer["id"], older["id"]]
    assert temp_db.get_session(older["id"])["updated_at"] == before


def test_linked_todo_count_matches_titles_for_every_session(temp_db):
    first = temp_db.create_session("first", "/tmp")
    second = temp_db.create_session("second", "/tmp")
    early = temp_db.create_todo("early")
    later = temp_db.create_todo("later")
    archived = temp_db.create_todo("archived")
    temp_db.set_todo_sessions(early["id"], [first["id"]])
    temp_db.set_todo_sessions(later["id"], [first["id"], second["id"]])
    temp_db.set_todo_sessions(archived["id"], [first["id"]])
    temp_db.update_todo(archived["id"], archived=1)
    temp_db._exec(
        "UPDATE todo_sessions SET created_at=? WHERE todo_id=?",
        (100.0, early["id"]),
    )
    temp_db._exec(
        "UPDATE todo_sessions SET created_at=? WHERE todo_id=?",
        (200.0, later["id"]),
    )

    rows = temp_db.list_sessions()
    by_id = {row["id"]: row for row in rows}

    assert by_id[first["id"]]["linked_todo_titles"] == ["early", "later"]
    assert by_id[second["id"]]["linked_todo_titles"] == ["later"]
    assert all(
        row["linked_todo_count"] == len(row["linked_todo_titles"])
        for row in rows
    )
