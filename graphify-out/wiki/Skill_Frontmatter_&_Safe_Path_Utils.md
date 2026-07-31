# Skill Frontmatter & Safe Path Utils

> 28 nodes

## Key Concepts

- **skill_store.py** (21 connections) — `server/skill_store.py`
- **parse_frontmatter()** (12 connections) — `server/fs_util.py`
- **safe_join()** (10 connections) — `server/fs_util.py`
- **_skill_dir()** (9 connections) — `server/skill_store.py`
- **expand()** (9 connections) — `server/skill_store.py`
- **list_skills_meta()** (7 connections) — `server/skill_store.py`
- **_skills_dir()** (6 connections) — `server/skill_store.py`
- **_skill_body()** (6 connections) — `server/skill_store.py`
- **list_skills()** (6 connections) — `server/skill_store.py`
- **get_skill()** (5 connections) — `server/skill_store.py`
- **update_skill()** (5 connections) — `server/skill_store.py`
- **post_arbitrate()** (4 connections) — `server/main.py`
- **create_skill()** (4 connections) — `server/skill_store.py`
- **delete_skill()** (3 connections) — `server/skill_store.py`
- **skills_list()** (2 connections) — `server/main.py`
- **skills_create()** (2 connections) — `server/main.py`
- **skills_get()** (2 connections) — `server/main.py`
- **skills_update()** (2 connections) — `server/main.py`
- **skills_delete()** (2 connections) — `server/main.py`
- **Path** (2 connections)
- **把 name 安全地拼到 base 下，防止 ../ 越界。      双重防御：① 字符白名单拒绝任何分隔符/点路径；② resolve() 后用     i** (1 connections) — `server/fs_util.py`
- **拆出 (frontmatter dict, body)。无 frontmatter 时返回 ({}, 原文)。** (1 connections) — `server/fs_util.py`
- **Skill 展开：把网页对话里输入的 `/<skill名> [附加内容]` 展开成 SKILL.md 正文。  claude-code 的 skill（.cla** (1 connections) — `server/skill_store.py`
- **读 .claude/skills/<name>/SKILL.md，返回去掉 frontmatter 和 <command-args> 占位的正文。** (1 connections) — `server/skill_store.py`
- **.claude/skills/<name> 目录，name 走 safe_join 防路径穿越。** (1 connections) — `server/skill_store.py`
- *... and 3 more nodes in this community*

## Relationships

- [YAML Frontmatter Utils](YAML_Frontmatter_Utils.md) (7 shared connections)
- [REST API Routes (Todos/Memos/Queue)](REST_API_Routes_%28Todos-Memos-Queue%29.md) (7 shared connections)
- [Agent Store CRUD](Agent_Store_CRUD.md) (4 shared connections)
- [Memory Store CRUD](Memory_Store_CRUD.md) (4 shared connections)
- [Codex Oneshot Helper & Config](Codex_Oneshot_Helper_%26_Config.md) (3 shared connections)
- [Goal Loop Iteration Queries](Goal_Loop_Iteration_Queries.md) (3 shared connections)
- [Dispatcher Deterministic Routing](Dispatcher_Deterministic_Routing.md) (2 shared connections)
- [LLMs.txt Skill Index Builder](LLMs.txt_Skill_Index_Builder.md) (2 shared connections)
- [Dispatch Plan Work Item Tracking](Dispatch_Plan_Work_Item_Tracking.md) (2 shared connections)
- [Arbiter Back-to-Back Arbitration](Arbiter_Back-to-Back_Arbitration.md) (1 shared connections)
- [DB Write/Insert Helpers](DB_Write-Insert_Helpers.md) (1 shared connections)
- [AgentProvider Abstract Base](AgentProvider_Abstract_Base.md) (1 shared connections)

## Source Files

- `server/fs_util.py`
- `server/main.py`
- `server/skill_store.py`

## Audit Trail

- EXTRACTED: 127 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*