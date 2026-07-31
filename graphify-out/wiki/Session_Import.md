# Session Import

> 17 nodes

## Key Concepts

- **session_import.py** (11 connections) — `server/session_import.py`
- **import_sessions()** (8 connections) — `server/session_import.py`
- **list_importable()** (7 connections) — `server/session_import.py`
- **list_sessions()** (6 connections) — `server/db.py`
- **_project_dir()** (5 connections) — `server/session_import.py`
- **_slug()** (4 connections) — `server/session_import.py`
- **latest_ai_title()** (4 connections) — `server/session_import.py`
- **import_do()** (3 connections) — `server/main.py`
- **_first_user_text()** (3 connections) — `server/session_import.py`
- **get_sessions()** (2 connections) — `server/main.py`
- **import_list()** (2 connections) — `server/main.py`
- **Path** (2 connections)
- **接续电脑终端交互模式聊过的会话。  用户在服务器终端跑交互式 tclaude 的会话存在   <TCLAUDE_HOME>/projects/<slug>/<c** (1 connections) — `server/session_import.py`
- **workdir → tclaude 项目目录 slug（与 memory_store 一致：/ _ . → -）。** (1 connections) — `server/session_import.py`
- **列出可接续的终端会话，按最近修改排序，标注已接续的。** (1 connections) — `server/session_import.py`
- **把指定终端会话接续进 Console（建会话 + 绑 claude_session_id）。返回导入数 + 首个新会话 id。** (1 connections) — `server/session_import.py`
- **读会话 JSONL，返回最后一条 ai-title 的 aiTitle；无则返回空字符串。按文件 mtime 缓存。** (1 connections) — `server/session_import.py`

## Relationships

- [REST API Routes (Todos/Memos/Queue)](REST_API_Routes_%28Todos-Memos-Queue%29.md) (5 shared connections)
- [DB Write/Insert Helpers](DB_Write-Insert_Helpers.md) (3 shared connections)
- [Codex Oneshot Helper & Config](Codex_Oneshot_Helper_%26_Config.md) (2 shared connections)
- [DB Query Helpers](DB_Query_Helpers.md) (1 shared connections)
- [Session-Todo Linking](Session-Todo_Linking.md) (1 shared connections)

## Source Files

- `server/db.py`
- `server/main.py`
- `server/session_import.py`

## Audit Trail

- EXTRACTED: 62 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*