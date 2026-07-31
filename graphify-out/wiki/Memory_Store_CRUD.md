# Memory Store CRUD

> 22 nodes

## Key Concepts

- **memory_store.py** (17 connections) — `server/memory_store.py`
- **_path()** (9 connections) — `server/memory_store.py`
- **_read()** (7 connections) — `server/memory_store.py`
- **_reindex()** (7 connections) — `server/memory_store.py`
- **update_memory()** (7 connections) — `server/memory_store.py`
- **_memory_dir()** (6 connections) — `server/memory_store.py`
- **create_memory()** (5 connections) — `server/memory_store.py`
- **list_memories()** (4 connections) — `server/memory_store.py`
- **get_memory()** (4 connections) — `server/memory_store.py`
- **delete_memory()** (4 connections) — `server/memory_store.py`
- **_slug()** (3 connections) — `server/memory_store.py`
- **memory_list()** (2 connections) — `server/main.py`
- **memory_create()** (2 connections) — `server/main.py`
- **memory_get()** (2 connections) — `server/main.py`
- **memory_update()** (2 connections) — `server/main.py`
- **memory_delete()** (2 connections) — `server/main.py`
- **Path** (1 connections)
- **Memory 管理：读写 tclaude 的 project memory 文件。  tclaude 把记忆按 cwd（workdir）分项目存到   <TCL** (1 connections) — `server/memory_store.py`
- **workdir 路径 → tclaude 项目目录 slug。** (1 connections) — `server/memory_store.py`
- **name（带不带 .md 都行）→ 安全的 .md 路径。** (1 connections) — `server/memory_store.py`
- **扫描目录里所有 memory 文件，全量重建 MEMORY.md。须在锁内调用。** (1 connections) — `server/memory_store.py`
- **更新：读旧 meta 合并，不擦 tclaude 写入的字段（originSessionId 等）。** (1 connections) — `server/memory_store.py`

## Relationships

- [REST API Routes (Todos/Memos/Queue)](REST_API_Routes_%28Todos-Memos-Queue%29.md) (6 shared connections)
- [Skill Frontmatter & Safe Path Utils](Skill_Frontmatter_%26_Safe_Path_Utils.md) (4 shared connections)
- [YAML Frontmatter Utils](YAML_Frontmatter_Utils.md) (3 shared connections)
- [Codex Oneshot Helper & Config](Codex_Oneshot_Helper_%26_Config.md) (2 shared connections)

## Source Files

- `server/main.py`
- `server/memory_store.py`

## Audit Trail

- EXTRACTED: 89 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*