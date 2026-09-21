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

## 项目现状规模（速查）

具体数字（模块数/路由数/WebSocket地址/表数/前端Tab数）已挪到 `docs/project-snapshot.md`，会过时，现查优先（`ls server/*.py`、`grep -c '@app\.\(get\|post\|put\|delete\|patch\)(' server/main.py`）。

- **运行模式**：`CLAUDE_PERSISTENT=true` 常驻进程模式，session_hub 负责进程生命周期和看门狗，**非每回合 resume 新进程**
- **文档警告**：README.md 和 IMPLEMENTATION.md 严重滞后（停留在 MVP 6 接口/3 表描述），排查功能**以代码为准**，不要信这两份文档
- **knot_runner.py**：未接线的实验模块（AG-UI 协议），当前无 import 依赖它（仅 codex_runner.py 注释里提到），勿误以为在用
- **AgentProvider 抽象**：`claude_runner.ClaudeRunner`/`codex_runner.CodexRunner` 现在都继承 `agent_provider.AgentProvider`（ABC），统一了 run_turn/send_turn/ensure_warm 等契约，`session_hub._runner_for` 用字典查表分发。加新 engine 时参照这个基类实现即可，不要再手写平行 runner。

**重构参考清单**（低内聚社区拆分候选，非当前任务）已挪到 `docs/refactor-candidates.md`，真要立项拆分 `web/app.js`/`server/main.py`/`server/db.py` 时再读，日常派单不需要。

## 流水线

**1.【分析】** 委派给 analyst：
- 读取相关源文件，理解现状
- 拆解需求，产出具体的实现方案（前端改哪里、后端加哪个接口）
- 明确 ops 的只读验证步骤（代码可加载性检查、curl 探活当前服务），并说明改动需用户手动重启后生效

**2.【实现】** 委派给 developer：
- developer 自己用 Edit/Write 落盘实现，不再派单给 tcodex
- 按方案修改 `server/`、`web/` 下的文件
- 前端改动后确认 JS/CSS 语法正确（`node --check web/app.js`）
- 后端改动后确认 Python 语法正确（`python3 -m py_compile server/main.py`）

**3.【审查】** 委派给 reviewer：
- 审查前后端改动，找正确性 bug、API 与前端不一致、安全问题
- **高连接度函数（God Nodes）改动需升级审查强度**：全项目最多依赖的函数——`_query()`、`api()`、`toast()`、`escapeHtml()`、`el()`、`_exec()`、`_now()`、`SessionHub`、`get_session()`、`new_id()`（按连接数从高到低）。改动涉及这些函数本身（不只是调用它们）时，reviewer 要额外确认：有没有遍历过所有调用点、有没有破坏既有调用方的假设（参数顺序/返回值语义/异常行为），倾向于多走一轮回跳复审而不是一次放行。
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

## 主动提醒用户（重要）

流水线里任何一个子智能体（analyst/developer/reviewer/ops）需要提醒用户"某件事已完成/需要关注"时，不要只在正文里说"我会通知你"，必须实际执行：

```bash
curl -s -X POST http://127.0.0.1/api/notify \
  -H "X-Local-Secret: $(cat /apdcephfs_gy2/share_302533218/zhihangxu/agent-console/data/notify_secret)" \
  -H "Content-Type: application/json" \
  -d "{\"title\":\"<一句话标题>\",\"text\":\"<简要说明>\"}"
```

这个接口靠 `X-Local-Secret` 共享密钥免鉴权（读本机文件比对，不是靠"来源 IP 是 127.0.0.1"——本项目对外访问经 ssh -L 本地转发，公网流量到服务端看到的对端地址同样是 127.0.0.1，不能拿这个当安全边界），会同时推一条页面内通知和企业微信消息，是唯一能真正送达用户的提醒方式。
