# Work Items CRUD Tests

> 11 nodes

## Key Concepts

- **test_db_work_items.py** (10 connections) — `tests/test_db_work_items.py`
- **test_create_and_get_work_item()** (1 connections) — `tests/test_db_work_items.py`
- **test_get_work_item_missing_returns_none()** (1 connections) — `tests/test_db_work_items.py`
- **test_update_status_by_ref()** (1 connections) — `tests/test_db_work_items.py`
- **test_update_status_by_ref_unknown_is_silent()** (1 connections) — `tests/test_db_work_items.py`
- **test_work_item_id_for_ref()** (1 connections) — `tests/test_db_work_items.py`
- **test_list_work_items_filters_by_origin_and_status()** (1 connections) — `tests/test_db_work_items.py`
- **test_list_work_items_respects_limit()** (1 connections) — `tests/test_db_work_items.py`
- **test_create_work_item_safe_returns_wid_on_success()** (1 connections) — `tests/test_db_work_items.py`
- **test_create_work_item_safe_swallows_exception()** (1 connections) — `tests/test_db_work_items.py`
- **server.db 的 work_items CRUD：create / update_by_ref / list（过滤）/ get / id_for_ref。** (1 connections) — `tests/test_db_work_items.py`

## Relationships

- No strong cross-community connections detected

## Source Files

- `tests/test_db_work_items.py`

## Audit Trail

- EXTRACTED: 20 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*