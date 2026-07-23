# Agent Console — 手机远程操控服务器 Agent

一个跑在服务器上的轻量 Web 控制台：通过 VPN，用**手机浏览器**指挥服务器上的
**Claude Code CLI** Agent 干活（运维、跑脚本、查状态等），并优雅地记录/回看整个工作流。

## 特性

- **移动端优先**：单页应用，自适应手机屏幕，无需装 App。
- **流式对话**：WebSocket 实时推送 Agent 的思考、工具调用、命令输出。
- **工作流记录**：每一步（消息 / 工具调用 / 结果）结构化落库，可随时回看。
- **任务看板**：侧边栏展示最近任务（状态 / 耗时 / 花费）。
- **鲁棒性**：断线自动重连、会话与状态持久化（服务器重启不丢）、回合超时保护。
- **多会话隔离**：不同任务开不同会话，互不干扰；底层用 Claude Code 的 `--resume` 维持上下文。
- **Token 鉴权**：所有接口与 WebSocket 校验口令。
- **易扩展**：Agent 能力来自 Claude Code CLI 自带工具，未来加功能无需改本服务核心。

## 目录结构

```
agent-console/
├── server/
│   ├── main.py           # FastAPI：REST + WebSocket 编排
│   ├── claude_runner.py  # 封装 Claude Code CLI（stream-json 解析）
│   ├── db.py             # SQLite 持久化
│   └── config.py         # 配置（环境变量）
├── web/
│   ├── index.html        # 移动端单页
│   ├── style.css
│   └── app.js
├── requirements.txt
├── run.sh
└── README.md
```

## 快速开始

### 1. 安装依赖

```bash
cd agent-console
pip install -r requirements.txt
```

并确保 **tclaude**（腾讯内部 claude-code wrapper）已安装且可用：

```bash
tclaude --version      # 能输出 wrapper + 上游 claude-code 版本即可
which tclaude          # 记下这个路径
```

### 2. 配置

编辑 `run.sh` 顶部，或用环境变量覆盖：

| 变量 | 说明 | 默认 |
|---|---|---|
| `AUTH_TOKEN` | 手机登录口令（**务必修改**） | `change-me-please` |
| `CLAUDE_BIN` | tclaude 可执行路径 | `tclaude`（走 PATH） |
| `CLAUDE_SKIP_PERMISSIONS` | 是否加 `--dangerously-skip-permissions`（root 下需保持 `false`） | `false` |
| `CLAUDE_MODEL_FAST/STRONG/SUPER` | 三档模型 ID | haiku / sonnet / opus-4.8[1m] |
| `AGENT_WORKDIR` | Agent 默认工作目录 | 上级目录 |
| `PORT` | 服务端口 | `8800` |

> 服务以子进程调用 tclaude，会自动继承本进程环境变量。
> 认证走 tclaude 自身的腾讯 IOA 登录（`tclaude login`），凭证落盘后 `-p` 模式直接复用。

### 3. 启动

```bash
bash run.sh
```

### 4. 手机访问

连上 VPN 后，手机浏览器打开：

```
http://<服务器IP>:8800
```

输入 `AUTH_TOKEN` 口令登录即可。

## 安全说明

- 服务默认开启 `--dangerously-skip-permissions`，这样 Agent 才能真正执行 Bash/文件操作。
  因为服务挂在 **VPN 内网 + Token 鉴权** 之后，风险可控。
- 若要更严格的权限控制（如危险操作二次确认），可在 `claude_runner.py` 接入
  Claude Code 的 `--permission-prompt-tool`（MCP 权限网关）——已为后续扩展预留。
- 请务必修改默认 `AUTH_TOKEN`，不要把端口直接暴露公网。

## 关于临时脚本归档

agent-console 的 git worktree 隔离（`sessions.is_worktree`）是**会话级**的临时工作区，会话删除时目录随之销毁，不适合长期归档。任何跟具体某个任务相关但不属于本项目代码库的临时脚本/一次性分析，请放到统一的 `worklog/`（见上级目录 `../CLAUDE.md` / `../AGENTS.md` 里的约定），不要指望靠 worktree 保留。

## 工作原理

每发一条消息，后端执行：

```
tclaude -- -p "<你的消息>" --output-format stream-json --verbose \
       [--model <档位模型>] [--resume <上次的claude会话id>]
```

> 注意 `tclaude` 是 wrapper，必须用 `--` 把参数原样透传给底层 claude-code。
> root 下默认权限模式即可执行 Bash/文件工具，无需 `--dangerously-skip-permissions`
> （该 flag 在 root 下会被直接拒绝）。

逐行解析 stream-json 事件 → 翻译成前端消息 → WebSocket 推送 + 落库。
Claude 返回的 `session_id` 被记录在会话上，下一回合用 `--resume` 续上下文。

## 团队 Agent 协作流水线

`.claude/agents/` 目录下定义了四个专职 Agent，形成完整的开发运维闭环：

| Agent | 职责 | 工具权限 |
|-------|------|----------|
| analyst | 拆解需求，产出实现方案，不写代码 | Read, Grep, Glob, Bash（只读） |
| developer | 按方案实现代码 | 全工具（含 Write/Edit） |
| reviewer | 审查改动，找 bug 和风险，不改代码 | Read, Grep, Glob, Bash（只读） |
| ops | 执行部署、运行测试、验证服务，不改代码 | Read, Grep, Glob, Bash（执行） |

**流转规则**：
- analyst → developer：analyst 输出方案后，developer 实现
- developer → reviewer：developer 完成后，reviewer 审查
- reviewer → developer：reviewer 输出【需修改】，developer 修复后重新提交审查
- reviewer → ops：reviewer 输出【可合并】，ops 开始执行部署和验证
- ops → developer：ops 发现运行时错误，整理错误反馈单交 developer 修复（最多 3 轮）
- ops → 汇报：全部验证通过，ops 输出执行报告，任务终态

## 后续可扩展（已预留）

- 长任务完成推送通知（微信 / Server酱 / 邮件）
- 快捷指令按钮（一键触发常用运维操作）
- 文件 / 产物预览、loss 曲线图
- 语音输入（浏览器语音转文字）
- 危险操作二次确认（权限网关）
# agent-console
