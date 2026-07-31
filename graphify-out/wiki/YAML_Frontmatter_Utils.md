# YAML Frontmatter Utils

> 12 nodes

## Key Concepts

- **dump_frontmatter()** (12 connections) — `server/fs_util.py`
- **fs_util.py** (9 connections) — `server/fs_util.py`
- **safe_path_under()** (6 connections) — `server/fs_util.py`
- **_parse_simple_yaml()** (4 connections) — `server/fs_util.py`
- **_fmt_val()** (3 connections) — `server/fs_util.py`
- **Path** (2 connections)
- **_unquote()** (2 connections) — `server/fs_util.py`
- **_needs_quote()** (2 connections) — `server/fs_util.py`
- **文件系统工具：路径安全 + 轻量 frontmatter 解析/序列化。  memory_store / agent_store 共用。刻意不引入 PyYAML** (1 connections) — `server/fs_util.py`
- **把 relpath（可含多级子目录）安全地解析到 base 之下，防 ../ 越界。      与 safe_join 区别：允许目录分隔符和多级路径（产物预览** (1 connections) — `server/fs_util.py`
- **解析 frontmatter 子集：顶层标量 + 一层缩进的嵌套 map。      例：         name: foo         descript** (1 connections) — `server/fs_util.py`
- **把 (meta, body) 序列化回带 frontmatter 的文本。      保留传入 meta 的全部字段（含 tclaude 写入的 originS** (1 connections) — `server/fs_util.py`

## Relationships

- [Skill Frontmatter & Safe Path Utils](Skill_Frontmatter_%26_Safe_Path_Utils.md) (7 shared connections)
- [Agent Store CRUD](Agent_Store_CRUD.md) (3 shared connections)
- [Memory Store CRUD](Memory_Store_CRUD.md) (3 shared connections)
- [REST API Routes (Todos/Memos/Queue)](REST_API_Routes_%28Todos-Memos-Queue%29.md) (2 shared connections)
- [Session-Todo Linking](Session-Todo_Linking.md) (1 shared connections)

## Source Files

- `server/fs_util.py`

## Audit Trail

- EXTRACTED: 44 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*