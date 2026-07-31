# Task DB Tests

> 7 nodes

## Key Concepts

- **test_db_tasks.py** (6 connections) — `tests/test_db_tasks.py`
- **test_get_latest_task_none_when_empty()** (1 connections) — `tests/test_db_tasks.py`
- **test_get_latest_task_returns_running_task()** (1 connections) — `tests/test_db_tasks.py`
- **test_get_latest_task_reflects_terminal_status()** (1 connections) — `tests/test_db_tasks.py`
- **test_get_latest_task_picks_most_recent()** (1 connections) — `tests/test_db_tasks.py`
- **test_get_latest_task_scoped_to_session()** (1 connections) — `tests/test_db_tasks.py`
- **server.db.get_latest_task 只读 helper：取会话最近一条 task（含 status）。  全部落临时库（temp_db fixt** (1 connections) — `tests/test_db_tasks.py`

## Relationships

- No strong cross-community connections detected

## Source Files

- `tests/test_db_tasks.py`

## Audit Trail

- EXTRACTED: 12 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*