# Healthcheck Script

> 6 nodes

## Key Concepts

- **healthcheck.py** (4 connections) — `server/healthcheck.py`
- **check_process()** (3 connections) — `server/healthcheck.py`
- **check_http()** (3 connections) — `server/healthcheck.py`
- **main()** (3 connections) — `server/healthcheck.py`
- **本机是否有 uvicorn server.main:app 进程在跑。** (1 connections) — `server/healthcheck.py`
- **探活一个已存在的鉴权接口，返回 (是否正常, 说明文字)。** (1 connections) — `server/healthcheck.py`

## Relationships

- [Codex Oneshot Helper & Config](Codex_Oneshot_Helper_%26_Config.md) (1 shared connections)

## Source Files

- `server/healthcheck.py`

## Audit Trail

- EXTRACTED: 15 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*