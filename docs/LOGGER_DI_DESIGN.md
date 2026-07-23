# Logger 依赖注入改造方案（可交付开发）

> 目标：把散落在 backend 的两套日志机制（`logging.getLogger` + 裸 `print`）
> 收敛为**单一可注入的 Logger 抽象**，让每条日志自动携带 session/job 上下文，
> 并能路由到既有 sink（WS monitor、`job_logs/<jid>.log`），同时可测、可静默。

## 0. 现状与耦合根因（结论）

| 现象 | 位置 | 耦合问题 |
|---|---|---|
| `logger = logging.getLogger(__name__)` | claude_runner / codex_runner / worktree | 直接依赖 stdlib 全局单例，无法在构造/测试期替换 |
| `print(f"[module] ...")` | triage / dispatcher / scheduler / kanban / goal_verifier / arbiter | 硬编码 sink=stdout，无级别、无格式、不可关 |
| `logger.warning("session %s: ...", session_id)` | claude_runner | 上下文被塞进消息文本，无法按 session 路由/过滤 |
| 无 sink 收敛 | 全局 | 已有 `hub.broadcast_monitor` / `job_logs/<jid>.log` 两个 sink，但诊断日志到不了它们 |

**根因一句话**：模块直接依赖具体日志实现（stdlib 单例 / stdout），且上下文靠字符串拼接，
导致「统一格式 / 上下文路由 / 可测 / 可静默」四件事都做不到。

## 1. 注入方式：混合策略（关键决策）

代码里既有长生命周期的**类**（`SessionHub`、`ClaudeRunner`、`CodexRunner`、`LoopDetector`），
也有大量**模块级 async 函数**（scheduler tick、triage、dispatcher…）。纯构造函数注入对后者不现实
（要改 40+ 函数签名）。因此采用两条车道：

- **构造函数注入 —— 用于类**：`__init__(..., logger: Logger | None = None)`，
  内部 `self.log = logger or get_logger(__name__)`。向后兼容（不传即用模块 logger）。
  `SessionHub` 持有 per-session 绑定的 logger，每回合把 `self.log.bind(session_id=sid)`
  下传给它构造的 `ClaudeRunner`。
- **ContextVar 环境注入 —— 用于模块级 async 函数**：一个 `ContextVar[Logger]` 存「当前上下文 logger」，
  在 async 入口处（`_tick_goal` / `run_triage` / `dispatch` / `run_arbitration` 等）用
  `with_logger(bound)` 设置，函数内部 `log = ctx_logger()` 读取（读不到则回落模块 logger）。
  这是本方案的核心选择：**避免改签名，且能跨 `await` / `asyncio.ensure_future` 传播**
  （`ensure_future` 会拷贝当前 contextvar 上下文）。

> 不采用「容器注入 / DI 框架」（如 dependency-injector）：项目是 FastAPI + 模块函数，
> 引入容器成本高、收益低。用「单一 bootstrap + 工厂 + contextvar」即可覆盖。

## 2. 接口抽象

结构化 `Protocol`（不用继承，鸭子类型即可，方便测试塞 Fake）：

```python
# server/logging_setup.py
from typing import Protocol, runtime_checkable

@runtime_checkable
class Logger(Protocol):
    def debug(self, msg: str, *args, **kw) -> None: ...
    def info(self, msg: str, *args, **kw) -> None: ...
    def warning(self, msg: str, *args, **kw) -> None: ...
    def error(self, msg: str, *args, **kw) -> None: ...
    def exception(self, msg: str, *args, **kw) -> None: ...
    def bind(self, **fields) -> "Logger": ...   # 返回带附加上下文字段的新 logger
```

实现用 stdlib `LoggerAdapter` 包一层，**复用** handler/level/format，不重造日志框架：

```python
class ContextLogger(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        ctx = {**self.extra}                       # session_id / jid / schedule_id ...
        kwargs.setdefault("extra", {})["ctx"] = ctx
        return msg, kwargs
    def bind(self, **fields):
        return ContextLogger(self.logger, {**self.extra, **fields})
```

上下文字段随 `LogRecord.ctx` 传递，由 Formatter/Handler 消费（既能进文本行，也能被路由 Handler 读取）。

## 3. Bootstrap 与生命周期

单一入口 `server/logging_setup.py`：

- `configure()`：**只在 app 启动时调用一次**（`main.py` 的 lifespan startup），
  设置 root handler（console `StreamHandler` + 统一 `Formatter`，格式含 `%(ctx)s`），
  级别从 `config.LOG_LEVEL` 读（默认 INFO；开发 DEBUG）。禁止其它文件再 `basicConfig`。
- `get_logger(name) -> Logger`：工厂，返回 `ContextLogger(logging.getLogger(name), {})`。
- `with_logger(bound: Logger)`：contextmanager，`token = _current.set(bound)` / `finally reset`。
- `ctx_logger(name=None) -> Logger`：读 contextvar；无则 `get_logger(name or "app")`。

生命周期：
- 全局 handler：进程级，`configure()` 建一次。
- per-context 绑定：`bind()` 产生轻量对象，随请求/回合/tick 结束自然 GC，无需清理。
- per-job FileHandler（见 §4）：`try/finally` 内 add/remove，严格配对。

## 4. 路由到既有 sink（DI 带来的增益）

上下文字段就位后，加两个 Handler，无需改调用点：

- **`MonitorFanoutHandler`**：`emit()` 里若 `record.ctx` 含 `session_id` 且 `level>=WARNING`，
  投递到 `hub.broadcast_monitor({...})`（用 `asyncio.get_running_loop().call_soon_threadsafe`
  或 `run_coroutine_threadsafe`，避免在 handler 里直接 await）。让「循环检测/看门狗超时」等
  警告实时出现在前端监控通道。
- **per-job FileHandler**：`job_store.run_logged_oneshot` 里，围绕子进程运行段
  `with_logger(get_logger("job").bind(jid=jid, session_id=session_id))` +
  临时加一个写 `job_logs/<jid>.log` 的 FileHandler（`try/finally` 移除），
  使 triage/goal_verifier/kanban 的诊断日志与 stdout 落在同一份 job 日志里。

## 5. 迁移策略与兼容过渡

**兼容原则**：全程保持 `get_logger(__name__)` 回落可用；任何未迁移的调用点仍能工作，
迁移可按文件小步推进、随时可重启验证。

- 分阶段（每阶段独立可上线、可回滚）：
  1. **Phase 0 —— 打地基**：新增 `logging_setup.py`；`main.py` lifespan 调 `configure()`；
     `config.py` 加 `LOG_LEVEL` / `LOG_FORMAT`。此阶段不改任何调用点，行为不变。
  2. **Phase 1 —— 灭 print**：6 个 print-文件（triage/dispatcher/scheduler/kanban/
     goal_verifier/arbiter）入口处 `with_logger(...)`，函数内 `log = ctx_logger()`，
     把 `print(f"[x] ...")` → `log.info/warning/error(...)`。模块 tag 由 logger name 自然带出。
  3. **Phase 2 —— 类构造注入**：`SessionHub`/`ClaudeRunner`/`CodexRunner`/`LoopDetector`
     加 `logger` 参数并默认回落；`SessionHub.start_turn` 里
     `bound = self.log.bind(session_id=sid)`，`with_logger(bound)` 包住回合，
     并把 `bound` 传给新建的 runner。移除 `"session %s"` 手工拼接（改由 ctx 带出）。
  4. **Phase 3 —— 接 sink**：注册 `MonitorFanoutHandler`；`job_store` 加 per-job FileHandler。
  5. **Phase 4 —— 固化**：加单测（`FakeLogger` 断言日志）；加 pre-commit/CI grep 守卫
     禁止新代码出现裸 `print(` 与 `logging.getLogger(`（除 `logging_setup.py`）。
- **过渡兜底**：Phase 1/2 期间若某处暂不便注入，直接 `ctx_logger(__name__)` 即可，无需改签名。

## 6. 关键文件清单

**新增**
- `server/logging_setup.py` —— Logger Protocol、ContextLogger、contextvar、`configure/get_logger/with_logger/ctx_logger`、`MonitorFanoutHandler`。

**修改**
- `server/config.py` —— `LOG_LEVEL`、`LOG_FORMAT`（env 读取）。
- `server/main.py` —— lifespan startup 调 `configure()`；注册 `MonitorFanoutHandler`。
- `server/session_hub.py` —— `SessionHub.__init__(logger=None)`；`start_turn` 绑定 session_id + `with_logger`；下传 bound logger 给 runner。
- `server/claude_runner.py` / `server/codex_runner.py` —— 类构造注入；去掉 `"session %s"` 手工拼接。
- `server/worktree.py` —— `log = get_logger(__name__)` 替换 `logging.getLogger`。
- `server/scheduler.py` —— tick/loop 入口 `with_logger`；`print` → log。
- `server/triage.py` —— `run_triage/dispatch_triage/_auto_dispatch_one` 入口 `with_logger`；`_log_triage` 与各 `print` → log。
- `server/dispatcher.py` —— `run_planner/dispatch` 入口 `with_logger`；`print` → log。
- `server/goal_verifier.py` / `server/kanban.py` / `server/arbiter.py` —— `print` → log。
- `server/job_store.py` —— `run_logged_oneshot` 内 per-job FileHandler + `with_logger(bind(jid,session_id))`。

**测试/守卫**
- `server/tests/test_logging.py` —— FakeLogger 捕获 + 上下文字段断言。
- pre-commit / CI grep 守卫（禁裸 print / getLogger）。

## 7. 验收标准

- 全后端仅 `logging_setup.py` 出现 `getLogger`；无业务文件裸 `print`。
- 一条来自子进程回合的 WARNING 日志：控制台带 `session_id` 字段、且出现在 `/ws/monitor`。
- triage 一次运行的诊断日志与 stdout 同现于 `job_logs/<jid>.log`。
- 单测能在不改被测函数签名的前提下捕获并断言日志。
- 重启后行为与迁移前一致（无级别/格式回归）。
