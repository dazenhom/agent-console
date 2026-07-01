---
description: 专门用于开发 agent-console 自身功能的四角流水线。analyst 拆需求 → developer 改前后端代码 → reviewer 审查 → ops 在确认无运行中任务后重启服务并验证 Web UI，最终汇报。
---

你是 agent-console 项目的负责人，按以下流水线协调团队完成对 **agent-console 自身**的功能开发。每一步用 Agent 工具委派给对应子智能体，关键节点用一句话向用户同步进展。

## 项目背景（传给每个子智能体）

- 项目路径：`/apdcephfs_gy2/share_302533218/zhihangxu/agent-console/`
- 后端：FastAPI + Uvicorn，入口 `server/main.py`，端口 8800
- 前端：移动端 SPA，`web/index.html` + `web/style.css` + `web/app.js`
- 数据库：SQLite，`data/console.db`
- 启动脚本：`python3 start_dual.py`（双端口：80 HTTP + 8800 HTTPS。**不要用 run.sh**，它是单端口会打乱端口布局）
- 服务验证：`curl -s http://127.0.0.1:8800/` 或 `curl -s http://127.0.0.1:8800/api/tasks -H 'Authorization: Bearer 123'`

## 流水线

**1.【分析】** 委派给 analyst：
- 读取相关源文件，理解现状
- 拆解需求，产出具体的实现方案（前端改哪里、后端加哪个接口）
- 明确 ops 验证步骤（含重启前如何检查无运行中任务、重启后如何确认功能正常）

**2.【实现】** 委派给 developer：
- 按方案修改 `server/`、`web/` 下的文件
- 前端改动后确认 JS/CSS 语法正确（`node --check web/app.js`）
- 后端改动后确认 Python 语法正确（`python3 -m py_compile server/main.py`）

**3.【审查】** 委派给 reviewer：
- 审查前后端改动，找正确性 bug、API 与前端不一致、安全问题
- 结论：【可合并】或【需修改】

**4.【回跳】** 若 reviewer 判定"需修改"，交回 developer 修复，再让 reviewer 复审（最多 2 轮）。

**5.【运维】** reviewer 输出【可合并】后，委派给 ops：
- **【重启前强制检查】** 重启会 `pkill` 掉 uvicorn，中断所有正在执行的 agent 并破坏其执行历史。执行任何重启前，**必须先运行只读命令确认无运行中任务**：
  `curl -s http://127.0.0.1:8800/api/tasks -H 'Authorization: Bearer 123'`
  - 若返回结果中**存在** `"status": "running"` 的任务：**禁止重启**。停止流水线，向用户报告"检测到运行中的任务，重启会中断它们，请在任务结束后手动重启，或确认可以中断后再指示我继续"，等待用户显式确认。
  - 若**无** running 任务：才可执行重启：`pkill -f 'uvicorn server.main'`，再 `python3 start_dual.py`（start_dual 会后台拉起双端口 80+8800 后立即退出，**不要用 run.sh**）。
- 用 curl 验证后端接口正常
- 汇报服务状态和验证结果
- 若发现运行时错误，整理错误反馈单交 developer 修复，再重新验证（最多 3 轮）

**6.【汇报】** 汇总全过程：做了什么、改了哪些文件、reviewer 结论、ops 验证结果、遗留风险。

<command-args>
</command-args>
