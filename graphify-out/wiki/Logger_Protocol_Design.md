# Logger Protocol Design

> 16 nodes

## Key Concepts

- **Logger** (11 connections) — `server/logging_util.py`
- **LoggerProvider** (5 connections) — `server/logging_util.py`
- **configure()** (5 connections) — `server/logging_util.py`
- **StdLoggerProvider** (4 connections) — `server/logging_util.py`
- **Protocol** (2 connections)
- **.get_logger()** (2 connections) — `server/logging_util.py`
- **.get_logger()** (2 connections) — `server/logging_util.py`
- **.debug()** (1 connections) — `server/logging_util.py`
- **.info()** (1 connections) — `server/logging_util.py`
- **.warning()** (1 connections) — `server/logging_util.py`
- **.error()** (1 connections) — `server/logging_util.py`
- **.exception()** (1 connections) — `server/logging_util.py`
- **日志器接口：仅列出本项目实际使用的方法（stdlib Logger 已满足此结构）。** (1 connections) — `server/logging_util.py`
- **logger 来源抽象：给定名称返回一个 Logger。** (1 connections) — `server/logging_util.py`
- **默认实现：直接透传 stdlib logging.getLogger，与改造前行为完全一致。** (1 connections) — `server/logging_util.py`
- **初始化装配点：安装全局 LoggerProvider。      传 None 时安装默认的 `StdLoggerProvider`（幂等，与改造前一致）；传入** (1 connections) — `server/logging_util.py`

## Relationships

- [AgentProvider Abstract Base](AgentProvider_Abstract_Base.md) (5 shared connections)
- [REST API Routes (Todos/Memos/Queue)](REST_API_Routes_%28Todos-Memos-Queue%29.md) (1 shared connections)

## Source Files

- `server/logging_util.py`

## Audit Trail

- EXTRACTED: 40 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*