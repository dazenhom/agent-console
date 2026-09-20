# Agent Console 当前上下文

最后核对：2026-08-03

## 当前部署事实

- HTTP：端口 80。
- HTTPS：端口 8800，使用自签证书。
- 正式启动实现：`start_dual.py`。
- 人工重启包装：`restart.sh`。
- 两个 Uvicorn 进程共享 `data/console.db`。
- `run.sh` 是旧的单端口入口，不适用于当前双端口部署。
- Codex 默认不得启动、重启或停止服务。

## 当前验证入口

```bash
./dev status
./dev doctor
./dev check
./dev test-changed
./dev smoke
```

`smoke` 只运行本地、非破坏性的代码回归测试；它不会重启服务，也不能证明尚未重启的后端改动已经在线生效。

## 已知文档偏差

- `README.md` 和 `IMPLEMENTATION.md` 的部分启动说明仍停留在旧单端口模式。
- 部分旧说明把 8800 当作 HTTP；当前 8800 是 HTTPS。
- 启动脚本和 `server/config.py` 中的若干默认值并不完全一致，排障时应核对当前进程环境。

## 默认忽略的路径

除非任务明确涉及，不递归读取或修改：

- `data/`
- `.console_uploads/`
- `*.log`
- `graphify-out/cache/`

`graphify-out/` 包含受版本控制的生成快照，可能独立于源码改动而处于 dirty 状态。

