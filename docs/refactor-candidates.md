# 低内聚社区拆分候选清单（未来重构参考，非当前任务）

> 从 `.claude/skills/console-dev/SKILL.md` 挪出来的参考清单。真要立项拆分 `web/app.js` / `server/main.py` / `server/db.py` 时再读，日常派单不需要看这份。

以下是 graphify 图谱按内聚度（cohesion）评出的**低分社区**——分数越低，说明"这堆函数被算法归到一组，但彼此调用关系稀疏、弱相关"，通常是历史上功能不断往同一个大文件里堆、缺乏模块边界的信号。**这份清单只是将来真要拆分这些大文件时的参考名单，不是现在就要动**：接普通需求时不要因为改到了这些区域就顺手重构，除非需求本身就是"拆分/重组某模块"。

数据取自 `graphify-out/GRAPH_REPORT.md`（**2026-07-24 生成的快照，图谱早于 2026-09-10 的多轮改动，动手前先重新生成一次 graphify 再核对，不要直接采信旧数字**），cohesion ≤ 0.07 的社区：

| Cohesion | 社区名 | 规模 | 拆分时大致落点（改前用 graphify 核实） |
| --- | --- | --- | --- |
| 0.04 | REST API Routes (Todos/Memos/Queue) | 57 节点 | `server/main.py` 路由层 |
| 0.04 | DB Query Helpers | 56 节点 | `server/db.py` 查询函数 |
| 0.06 | Frontend Chat UI | 56 节点 | `web/app.js` 聊天界面 |
| 0.06 | Env Config & run.sh | 31 节点 | `server/config.py` + 启动脚本 |
| 0.07 | Goal Loop Iteration Queries | 57 节点 | `server/db.py` 目标循环相关查询 |
| 0.07 | WebSocket Session Monitoring | 22 节点 | `server/main.py` WS + `session_hub.py` |
| 0.07 | Backlog Import & Goal Prompt Building | 41 节点 | `backlog_import.py` / `triage.py` |

其中 **Frontend Chat UI（0.06）和 REST API Routes（0.04）是图谱 Suggested Questions 里明确点名 "should be split into smaller, more focused modules" 的两个**，优先级最高。注意：低内聚 ≠ 有 bug（图谱报的"环/低内聚/异常连接"要先用 Grep/Read 核实再下结论——例如它曾报 scheduler.py/secretary.py/triage.py 三者互为 import 环，实际是函数体内延迟 import 的标准写法，不是 bug）；列进来只为将来重构立项时有据可依。"拆分时大致落点"一列是推断，动手前务必用 `graphify explain` 核实实际文件，且优先重新生成图谱确认数字仍然成立。
