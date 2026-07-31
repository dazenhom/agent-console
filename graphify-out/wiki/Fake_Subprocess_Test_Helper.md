# Fake Subprocess Test Helper

> 4 nodes

## Key Concepts

- **_FakeProc** (4 connections) — `tests/test_job_store_cwd.py`
- **.communicate()** (1 connections) — `tests/test_job_store_cwd.py`
- **.kill()** (1 connections) — `tests/test_job_store_cwd.py`
- **够用即可的假子进程：communicate 立即返回空 stdout/stderr。** (1 connections) — `tests/test_job_store_cwd.py`

## Relationships

- [Arbiter Back-to-Back Arbitration](Arbiter_Back-to-Back_Arbitration.md) (1 shared connections)

## Source Files

- `tests/test_job_store_cwd.py`

## Audit Trail

- EXTRACTED: 7 (100%)
- INFERRED: 0 (0%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*