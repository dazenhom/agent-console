# Summarizer (Title Gen)

> 13 nodes

## Key Concepts

- **summarizer.py** (12 connections) — `server/summarizer.py`
- **_haiku()** (5 connections) — `server/summarizer.py`
- **summarize()** (4 connections) — `server/summarizer.py`
- **_gen_title_haiku()** (4 connections) — `server/summarizer.py`
- **_heuristic()** (3 connections) — `server/summarizer.py`
- **gen_title()** (3 connections) — `server/summarizer.py`
- **_build_prompt()** (2 connections) — `server/summarizer.py`
- **行摘要：给会话列表生成一句话「这会话刚做了什么」。  主路径用便宜档 codex 模型（CHEAP_MODEL）一次性概括（漂亮、像 agent view 的行** (1 connections) — `server/summarizer.py`
- **启发式兜底：优先取 Agent 回复首句/前 N 字，没有则取用户指令。** (1 connections) — `server/summarizer.py`
- **一次性便宜档概括。返回干净摘要字符串；失败/超时/无输出返回空串。** (1 connections) — `server/summarizer.py`
- **生成一句话摘要：Haiku 优先，失败回退启发式。永不抛异常。** (1 connections) — `server/summarizer.py`
- **一次性便宜档起标题：给一段对话内容取一个不超过 10 字的中文标题。失败/超时/无输出返回空串。      current_title 非空时改判「是否需要换标** (1 connections) — `server/summarizer.py`
- **给一段对话内容生成语义标题。关闭/失败返回空串（上层保留截取标题）。永不抛异常。      current_title 非空时交给模型判断是否需要换标题，无需换** (1 connections) — `server/summarizer.py`

## Relationships

- [Codex Oneshot Helper & Config](Codex_Oneshot_Helper_%26_Config.md) (6 shared connections)
- [AgentProvider Abstract Base](AgentProvider_Abstract_Base.md) (1 shared connections)

## Source Files

- `server/summarizer.py`

## Audit Trail

- EXTRACTED: 39 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*