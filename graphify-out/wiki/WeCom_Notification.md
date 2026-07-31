# WeCom Notification

> 12 nodes

## Key Concepts

- **wecom_notify.py** (13 connections) — `server/wecom_notify.py`
- **notify()** (6 connections) — `server/wecom_notify.py`
- **notify_memo()** (5 connections) — `server/wecom_notify.py`
- **_clip()** (4 connections) — `server/wecom_notify.py`
- **build_markdown()** (4 connections) — `server/wecom_notify.py`
- **_post_sync()** (4 connections) — `server/wecom_notify.py`
- **企业微信「消息推送」（原群机器人）通知。  回合完成后给指定群/单聊发一条 markdown 摘要卡片。  关键约束：本服务器在内网，**直连出不去公网**，而** (1 connections) — `server/wecom_notify.py`
- **按 utf-8 字节截断，超出加省略号。企业微信 content 上限是字节而非字符数。** (1 connections) — `server/wecom_notify.py`
- **拼一张 markdown 摘要卡片。status: success / error / cancelled。** (1 connections) — `server/wecom_notify.py`
- **同步发送（放到线程里跑，避免阻塞事件循环）。返回 (ok, detail)。** (1 connections) — `server/wecom_notify.py`
- **异步发送企业微信通知。永不抛异常——失败只返回 (False, 原因) 供调用方记日志。** (1 connections) — `server/wecom_notify.py`
- **备忘提醒专用推送，用橙色警示格式，与 agent 任务完成通知视觉区分。永不抛异常。** (1 connections) — `server/wecom_notify.py`

## Relationships

- [Codex Oneshot Helper & Config](Codex_Oneshot_Helper_%26_Config.md) (3 shared connections)
- [Goal Loop Iteration Queries](Goal_Loop_Iteration_Queries.md) (2 shared connections)
- [Backlog Import & Goal Prompt Building](Backlog_Import_%26_Goal_Prompt_Building.md) (2 shared connections)
- [REST API Routes (Todos/Memos/Queue)](REST_API_Routes_%28Todos-Memos-Queue%29.md) (1 shared connections)
- [AgentProvider Abstract Base](AgentProvider_Abstract_Base.md) (1 shared connections)
- [DB Write/Insert Helpers](DB_Write-Insert_Helpers.md) (1 shared connections)

## Source Files

- `server/wecom_notify.py`

## Audit Trail

- EXTRACTED: 38 (90%)
- INFERRED: 4 (10%)
- AMBIGUOUS: 0 (0%)

---

*Part of the graphify knowledge wiki. See [index](index.md) to navigate.*