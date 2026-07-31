# LLMs.txt Skill Index Builder

> 16 nodes

## Key Concepts

- **_build_index()** (7 connections) — `server/llms_doc.py`
- **build_llms_full_txt()** (7 connections) — `server/llms_doc.py`
- **build_llms_txt()** (5 connections) — `server/llms_doc.py`
- **_skill_files()** (4 connections) — `server/llms_doc.py`
- **_route_lines()** (3 connections) — `server/llms_doc.py`
- **_agent_files()** (3 connections) — `server/llms_doc.py`
- **llms_txt()** (3 connections) — `server/main.py`
- **llms_full_txt()** (3 connections) — `server/main.py`
- **内省 app.routes，列出 REST 接口（method + path + docstring 首行）。** (1 connections) — `server/llms_doc.py`
- **精选索引正文（llms.txt 与 llms-full.txt 共用的头部）。** (1 connections) — `server/llms_doc.py`
- **[(skill名, SKILL.md 全文)]，按名排序。** (1 connections) — `server/llms_doc.py`
- **[(subagent名, .md 全文)]，按名排序。** (1 connections) — `server/llms_doc.py`
- **精选文本索引，末尾指向 /llms-full.txt。** (1 connections) — `server/llms_doc.py`
- **索引 + 每个 skill / subagent 定义全文，拼成单一 markdown。** (1 connections) — `server/llms_doc.py`
- **项目文本索引（llms.txt 约定）；无鉴权供外部 agent 直接摄取。** (1 connections) — `server/main.py`
- **项目全文：索引 + 所有 skill / subagent 定义全文内联。** (1 connections) — `server/main.py`

## Relationships

- [Codex Oneshot Helper & Config](Codex_Oneshot_Helper_%26_Config.md) (6 shared connections)
- [REST API Routes (Todos/Memos/Queue)](REST_API_Routes_%28Todos-Memos-Queue%29.md) (4 shared connections)
- [Skill Frontmatter & Safe Path Utils](Skill_Frontmatter_%26_Safe_Path_Utils.md) (2 shared connections)
- [Agent Store CRUD](Agent_Store_CRUD.md) (1 shared connections)

## Source Files

- `server/llms_doc.py`
- `server/main.py`

## Audit Trail

- EXTRACTED: 43 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*