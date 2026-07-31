# Queue Item CRUD

> 5 nodes

## Key Concepts

- **get_queue_item()** (6 connections) — `server/db.py`
- **update_queue_item()** (4 connections) — `server/db.py`
- **delete_queue_item()** (4 connections) — `server/db.py`
- **queue_edit()** (3 connections) — `server/main.py`
- **queue_delete()** (3 connections) — `server/main.py`

## Relationships

- [DB Write/Insert Helpers](DB_Write-Insert_Helpers.md) (5 shared connections)
- [REST API Routes (Todos/Memos/Queue)](REST_API_Routes_%28Todos-Memos-Queue%29.md) (2 shared connections)
- [DB Query Helpers](DB_Query_Helpers.md) (1 shared connections)

## Source Files

- `server/db.py`
- `server/main.py`

## Audit Trail

- EXTRACTED: 20 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*