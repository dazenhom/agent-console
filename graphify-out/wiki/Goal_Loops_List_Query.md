# Goal Loops List Query

> 3 nodes

## Key Concepts

- **list_goal_loops()** (5 connections) — `server/db.py`
- **goals_list()** (2 connections) — `server/main.py`
- **目标循环历史列表态：所有 kind=goal 的 schedule，附带已记录的轮次数。** (1 connections) — `server/db.py`

## Relationships

- [DB Write/Insert Helpers](DB_Write-Insert_Helpers.md) (1 shared connections)
- [DB Query Helpers](DB_Query_Helpers.md) (1 shared connections)
- [Codex Oneshot Helper & Config](Codex_Oneshot_Helper_%26_Config.md) (1 shared connections)
- [REST API Routes (Todos/Memos/Queue)](REST_API_Routes_%28Todos-Memos-Queue%29.md) (1 shared connections)

## Source Files

- `server/db.py`
- `server/main.py`

## Audit Trail

- EXTRACTED: 8 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*