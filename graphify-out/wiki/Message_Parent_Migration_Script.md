# Message Parent Migration Script

> 2 nodes

## Key Concepts

- **migrate_parent.py** (1 connections) — `migrate_parent.py`
- **迁移脚本：为历史消息补全 parent 字段。  规则：按 created_at 顺序扫描每个会话的消息， 遇到 tool_use Agent 时入栈，入栈期间** (1 connections) — `migrate_parent.py`

## Relationships

- No strong cross-community connections detected

## Source Files

- `migrate_parent.py`

## Audit Trail

- EXTRACTED: 2 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*