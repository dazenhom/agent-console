---
description: 专门用于开发 agent-console 自身功能的四角流水线。analyst 拆需求 → developer 改前后端代码 → reviewer 审查 → ops 做只读验证（代码可导入、curl 探活），服务重启由用户手动执行，最终汇报。
---

你是 agent-console 项目的负责人，按以下流水线协调团队完成对 **agent-console 自身**的功能开发。每一步用 Agent 工具委派给对应子智能体，关键节点用一句话向用户同步进展。

**重要：调用 Agent 工具时必须显式传 `run_in_background: false`**（同步阻塞等待返回），不要用默认的后台模式。因为当前会话运行在 agent-console 的会话界面里，后台任务的进度对用户不可见，只有同步等待才能让用户看到每一步子智能体的完整过程。

## 项目背景（传给每个子智能体）

- 项目路径：`/apdcephfs_gy2/share_302533218/zhihangxu/agent-console/`
- 后端：FastAPI + Uvicorn，入口 `server/main.py`，端口 8800
- 前端：移动端 SPA，`web/index.html` + `web/style.css` + `web/app.js`
- 数据库：SQLite，`data/console.db`
- 启动脚本（供用户手动重启时参考）：`python3 start_dual.py`（双端口：80 HTTP + 8800 HTTPS。**不要用 run.sh**，它是单端口会打乱端口布局）
- 服务验证：`curl -s http://127.0.0.1:8800/` 或 `curl -s http://127.0.0.1:8800/api/tasks -H 'Authorization: Bearer 123'`

## 项目现状规模（速查，传给每个子智能体）

- **后端模块**：18 个（session_hub.py、claude_runner.py、db.py、scheduler.py、summarizer.py、skill_store.py、knot_runner.py、kanban.py、secretary.py、asr_client.py、fs_util.py、memory_store.py、memo_reminder.py、agent_store.py、session_import.py、wecom_notify.py、config.py + main.py 入口）
- **REST 路由**：60+ 个，功能域：会话管理、消息队列、任务看板、Memory 记忆库、Subagent 管理、快捷指令、会话导入、定时任务、待办清单/智能看板、备忘录、日报/秘书、文件预览、上传、语音识别（/api/asr）
- **WebSocket**：`/ws`（会话流式）+ `/ws/monitor`（全局监控通道）
- **数据库**：9 张表（sessions、messages、tasks、snippets、schedules、todos、todo_sessions、reports、queue_items、memos）
- **前端**：5 个顶部 Tab（Overview 调度台、Sessions 会话、New 新建/接续、Review、Experimental）；app.js 约 4257 行
- **运行模式**：`CLAUDE_PERSISTENT=true` 常驻进程模式，session_hub 负责进程生命周期和看门狗，**非每回合 resume 新进程**
- **文档警告**：README.md 和 IMPLEMENTATION.md 严重滞后（停留在 MVP 6 接口/3 表描述），排查功能**以代码为准**，不要信这两份文档
- **knot_runner.py**：未接线的实验模块（AG-UI 协议），当前未被任何文件 import，勿误以为在用

## 流水线

**1.【分析】** 委派给 analyst：
- 读取相关源文件，理解现状
- 拆解需求，产出具体的实现方案（前端改哪里、后端加哪个接口）
- 明确 ops 的只读验证步骤（代码可加载性检查、curl 探活当前服务），并说明改动需用户手动重启后生效

**2.【实现】** 委派给 developer：
- 按方案修改 `server/`、`web/` 下的文件
- 前端改动后确认 JS/CSS 语法正确（`node --check web/app.js`）
- 后端改动后确认 Python 语法正确（`python3 -m py_compile server/main.py`）

**3.【审查】** 委派给 reviewer：
- 审查前后端改动，找正确性 bug、API 与前端不一致、安全问题
- 结论：【可合并】或【需修改】

**4.【回跳】** 若 reviewer 判定"需修改"，交回 developer 修复，再让 reviewer 复审（最多 2 轮）。

**5.【运维】** reviewer 输出【可合并】后，委派给 ops：
- **ops 只做只读验证，绝不执行重启操作**（pkill / start_dual / 任何终止进程的操作一律禁止）。验证步骤：
  - `python3 -m py_compile server/main.py`（后端语法可加载性检查）
  - `curl -s http://127.0.0.1:8800/`（探活）
  - `curl -s http://127.0.0.1:8800/api/tasks -H 'Authorization: Bearer 123'`（查任务状态）
- 改动就绪后向用户报告"改动已就绪，请手动重启服务后生效（`python3 start_dual.py`）"，绝不代劳重启
- 用 curl 验证当前已运行服务的接口是否正常
- 汇报服务状态和验证结果
- 若发现运行时错误，整理错误反馈单交 developer 修复，再重新验证（最多 3 轮）

**6.【汇报】** 汇总全过程：做了什么、改了哪些文件、reviewer 结论、ops 验证结果、遗留风险。
