# WebSocket Session Monitoring

> 52 nodes

## Key Concepts

- **SessionHub** (29 connections) — `server/session_hub.py`
- **Subscriber** (11 connections) — `server/session_hub.py`
- **._run_turn()** (11 connections) — `server/session_hub.py`
- **_runner_for()** (9 connections) — `server/session_hub.py`
- **._emit_session_update()** (8 connections) — `server/session_hub.py`
- **.broadcast()** (7 connections) — `server/session_hub.py`
- **.start_turn()** (7 connections) — `server/session_hub.py`
- **._start_expanded()** (7 connections) — `server/session_hub.py`
- **._drain_queue()** (7 connections) — `server/session_hub.py`
- **.compact()** (6 connections) — `server/session_hub.py`
- **.is_running()** (5 connections) — `server/session_hub.py`
- **.submit_user_message()** (5 connections) — `server/session_hub.py`
- **._auto_title_by_ai()** (5 connections) — `server/session_hub.py`
- **NoCacheStatic** (4 connections) — `server/main.py`
- **.emit_queue_update()** (4 connections) — `server/session_hub.py`
- **._auto_progress_by_ai()** (4 connections) — `server/session_hub.py`
- **._run_compact()** (4 connections) — `server/session_hub.py`
- **._summarize_and_emit()** (4 connections) — `server/session_hub.py`
- **.respond_permission()** (4 connections) — `server/session_hub.py`
- **resume_session()** (3 connections) — `server/main.py`
- **.broadcast_monitor()** (3 connections) — `server/session_hub.py`
- **._has_foreground_sub()** (3 connections) — `server/session_hub.py`
- **._emit_todo_progress()** (3 connections) — `server/session_hub.py`
- **._build_resume_recovery_prompt()** (3 connections) — `server/session_hub.py`
- **._build_title_convo()** (3 connections) — `server/session_hub.py`
- *... and 27 more nodes in this community*

## Relationships

- [REST API Routes (Todos/Memos/Queue)](REST_API_Routes_%28Todos-Memos-Queue%29.md) (6 shared connections)
- [AgentProvider Abstract Base](AgentProvider_Abstract_Base.md) (3 shared connections)
- [Session-Todo Linking](Session-Todo_Linking.md) (2 shared connections)

## Source Files

- `server/main.py`
- `server/session_hub.py`

## Audit Trail

- EXTRACTED: 191 (99%)
- INFERRED: 2 (1%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*