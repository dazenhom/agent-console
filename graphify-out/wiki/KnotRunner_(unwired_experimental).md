# KnotRunner (unwired experimental)

> 9 nodes

## Key Concepts

- **KnotRunner** (6 connections) — `server/knot_runner.py`
- **_strip_sse_prefix()** (3 connections) — `server/knot_runner.py`
- **.run_turn()** (3 connections) — `server/knot_runner.py`
- **.__init__()** (1 connections) — `server/knot_runner.py`
- **.is_running()** (1 connections) — `server/knot_runner.py`
- **.was_cancelled()** (1 connections) — `server/knot_runner.py`
- **.cancel()** (1 connections) — `server/knot_runner.py`
- **EventCallback** (1 connections)
- **SSE 行: 'data: {...}' / 'data:{...}'。心跳冒号行返回空串。** (1 connections) — `server/knot_runner.py`

## Relationships

- [Codex Oneshot Helper & Config](Codex_Oneshot_Helper_%26_Config.md) (2 shared connections)

## Source Files

- `server/knot_runner.py`

## Audit Trail

- EXTRACTED: 18 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*