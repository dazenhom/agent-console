# Context Compactor

> 9 nodes

## Key Concepts

- **compactor.py** (11 connections) — `server/compactor.py`
- **summarize_session()** (7 connections) — `server/compactor.py`
- **_build_transcript()** (4 connections) — `server/compactor.py`
- **_fallback()** (3 connections) — `server/compactor.py`
- **_build_prompt()** (2 connections) — `server/compactor.py`
- **/compact 上下文压缩：把整段对话概括成一份摘要。  会话上下文越滚越长会推高每回合 token 与成本，tclaude/tcodex 又没有原生的"截断** (1 connections) — `server/compactor.py`
- **拼整段对话转录（仅 user/assistant 文本）。超过上限时保留首尾两段、中间省略，     兼顾"概括整段"（不只取尾部）与 prompt 体积。** (1 connections) — `server/compactor.py`
- **兜底摘要：概括失败时直接用转录文本尾部截断。** (1 connections) — `server/compactor.py`
- **把会话整段对话概括成摘要文本。永不抛异常：子进程失败/超时/无输出时回退转录尾部截断。** (1 connections) — `server/compactor.py`

## Relationships

- [Arbiter Back-to-Back Arbitration](Arbiter_Back-to-Back_Arbitration.md) (4 shared connections)
- [Codex Oneshot Helper & Config](Codex_Oneshot_Helper_%26_Config.md) (2 shared connections)
- [DB Write/Insert Helpers](DB_Write-Insert_Helpers.md) (1 shared connections)
- [AgentProvider Abstract Base](AgentProvider_Abstract_Base.md) (1 shared connections)
- [Session-Todo Linking](Session-Todo_Linking.md) (1 shared connections)

## Source Files

- `server/compactor.py`

## Audit Trail

- EXTRACTED: 31 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*