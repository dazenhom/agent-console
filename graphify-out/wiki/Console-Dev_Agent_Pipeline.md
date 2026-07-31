# Console-Dev Agent Pipeline

> 35 nodes

## Key Concepts

- **IMPLEMENTATION.md：Agent Console MVP 设计与实现** (8 connections) — `IMPLEMENTATION.md`
- **README.md：Agent Console 项目总览** (7 connections) — `README.md`
- **Developer Agent (调度 tcodex 实现代码)** (6 connections) — `.claude/agents/developer.md`
- **Ops Agent (只读部署验证/不改代码)** (6 connections) — `.claude/agents/ops.md`
- **console-dev skill (agent-console 自身开发四角流水线)** (6 connections) — `.claude/skills/console-dev/SKILL.md`
- **devops-team skill (通用四角流水线)** (6 connections) — `.claude/skills/devops-team/SKILL.md`
- **quant-dev skill (quant-recommender 四角流水线)** (6 connections) — `.claude/skills/quant-dev/SKILL.md`
- **Goal Loop 现状说明与两大痛点根因** (6 connections) — `docs/GOAL_LOOP_现状与痛点.md`
- **Logger 依赖注入改造方案** (6 connections) — `docs/LOGGER_DI_DESIGN.md`
- **Analyst Agent (需求拆解/根因定位)** (5 connections) — `.claude/agents/analyst.md`
- **Reviewer Agent (审查改动/不改代码)** (5 connections) — `.claude/agents/reviewer.md`
- **ClaudeRunner (server/claude_runner.py 封装)** (4 connections) — `IMPLEMENTATION.md`
- **server/arbiter.py（背对背仲裁 /api/arbitrate）** (4 connections) — `docs/GOAL_LOOP_现状与痛点.md`
- **痛点2：loop 智能化不够（单会话重复，未接分派/仲裁/流水线）** (4 connections) — `docs/GOAL_LOOP_现状与痛点.md`
- **server/scheduler.py（goal 状态机 / _tick_goal）** (3 connections) — `docs/GOAL_LOOP_现状与痛点.md`
- **server/goal_verifier.py（验收器 checker）** (3 connections) — `docs/GOAL_LOOP_现状与痛点.md`
- **server/dispatcher.py（智能分派 /api/dispatch）** (3 connections) — `docs/GOAL_LOOP_现状与痛点.md`
- **server/logging_setup.py（新增：Logger Protocol/ContextLogger/contextvar）** (3 connections) — `docs/LOGGER_DI_DESIGN.md`
- **智能分派 / 背对背仲裁 独立面板（#dispatch-view / #arb-view）** (3 connections) — `web/index.html`
- **server/main.py（REST + WebSocket 编排）** (2 connections) — `IMPLEMENTATION.md`
- **server/db.py（SQLite 持久化：sessions/messages/tasks）** (2 connections) — `IMPLEMENTATION.md`
- **痛点1：目标循环入口埋得深/命名错位** (2 connections) — `docs/GOAL_LOOP_现状与痛点.md`
- **fastapi>=0.110 依赖** (2 connections) — `requirements.txt`
- **uvicorn[standard]>=0.27 依赖** (2 connections) — `requirements.txt`
- **pytest>=8.0（开发/测试依赖）** (2 connections) — `requirements-dev.txt`
- *... and 10 more nodes in this community*

## Relationships

- No strong cross-community connections detected

## Source Files

- `.claude/agents/analyst.md`
- `.claude/agents/developer.md`
- `.claude/agents/ops.md`
- `.claude/agents/reviewer.md`
- `.claude/skills/console-dev/SKILL.md`
- `.claude/skills/devops-team/SKILL.md`
- `.claude/skills/quant-dev/SKILL.md`
- `IMPLEMENTATION.md`
- `README.md`
- `docs/GOAL_LOOP_现状与痛点.md`
- `docs/LOGGER_DI_DESIGN.md`
- `requirements-dev.txt`
- `requirements.txt`
- `web/icon.svg`
- `web/index.html`

## Audit Trail

- EXTRACTED: 104 (88%)
- INFERRED: 12 (10%)
- AMBIGUOUS: 2 (2%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*