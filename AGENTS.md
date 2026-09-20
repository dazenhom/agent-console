# Agent Console：Codex 项目约定

## 项目定位

- FastAPI + 原生前端的多会话 Agent 控制台。
- 后端入口：`server/main.py`。
- 前端：`web/index.html`、`web/app.js`、`web/style.css`。
- 本地状态：`data/console.db` 及 `data/` 下运行时文件。
- 正式双端口启动入口：`python3 start_dual.py`。

## 开始任务前

1. 先运行 `./dev status`，识别用户已有改动；不得覆盖或回退无关改动。
2. 只读取与任务有关的源码。`data/`、日志、上传文件和 `graphify-out/` 默认视为运行时或生成物，除非任务明确涉及。
3. README/IMPLEMENTATION 可能滞后；行为以源码、测试和本文件为准。

## 修改约束

- 禁止自动重启、停止或批量杀死服务进程。不得执行 `pkill`、`restart.sh` 或 `start_dual.py`；需要重启时只告知用户。
- 静态前端文件由当前服务直接读取，通常不需要重启；后端 Python 改动需要用户手动重启后生效。
- 不读取、打印或提交 `data/.secrets/`、`data/notify_secret`、`.env`、证书或 token。
- 不把数据库、日志、上传内容、worktree 或 Graphify 缓存加入 Git。
- 代码行为修改默认需要独立 Reviewer。纯文档或机械格式修改可说明理由后省略 Reviewer。
- Reviewer 应重点检查：API/前端契约、并发与 SQLite 状态、鉴权、会话恢复、进程生命周期，以及用户已有改动是否被误伤。
- `/fs`、`/api/file`、`/api/audio`、`/api/preview` 及其他文件服务路由属于强制安全审查范围。

## 标准验证

统一使用项目命令：

```bash
./dev status
./dev doctor
./dev check
./dev test-changed
./dev smoke
```

- `status`：只读显示 Git、运行进程和最近提交。
- `doctor`：检查 Python、Node、依赖和关键目录，不启动服务。
- `check`：执行 Python/JavaScript/Shell 语法检查及 `git diff --check`。
- `test-changed`：代码改动运行全量 pytest；只有文档/生成物变化时跳过 pytest。
- `smoke`：运行一组不启动、不重启服务的核心回归测试。

提交结论时列出实际执行的命令及结果；不要声称未执行的验证已经通过。
