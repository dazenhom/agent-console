"""Agent provider 基础契约：on_event 接收 type 为 assistant、携带 tool_result 的 user、result 或 status 的归一化 dict，所有 provider 必须先把原生事件翻译成这些形状再回调。"""
import asyncio
import os
import re
import signal
from abc import ABC, abstractmethod
from typing import Awaitable, Callable


EventCallback = Callable[[dict], Awaitable[None]]


def is_sleep_command(command) -> bool:
    """判断一条 shell 命令是否为「等待型」sleep 调用。

    这类调用（agent 轮询等待后台任务）不该被计入循环检测 / 超时预算。
    规则：命令按 && / || / ; 分段，任一段 strip 后以 sleep 开头（词边界）即视为
    sleep 命令。入参兼容字符串或 argv 数组（数组先 join 成字符串再判断）。
    """
    if isinstance(command, (list, tuple)):
        command = " ".join(str(x) for x in command)
    if not isinstance(command, str):
        return False
    for seg in re.split(r"&&|\|\||;", command):
        if re.match(r"^\s*sleep\b", seg):
            return True
    return False


# stream-json 单行可能很大（含命令输出），放大缓冲上限
_STREAM_LIMIT = 16 * 1024 * 1024

# 若本服务本身是被某个 claude-code / tclaude 会话拉起的（常见于在 Agent
# 终端里 `nohup bash run.sh`），进程会继承一批“编排态”环境变量：
#   ANTHROPIC_BASE_URL  -> 指向父会话的临时网关代理（127.0.0.1:<随机端口>）
#   ANTHROPIC_AUTH_TOKEN/ANTHROPIC_API_KEY -> 只对那个代理有效的临时令牌
#   CLAUDE_CODE_CHILD_SESSION / CLAUDE_CODE_SESSION_ID / ANTHROPIC_CUSTOM_HEADERS ...
# 这些值会让我们 spawn 出来的 tclaude 子进程去连父会话的代理，导致
# “403 API key not allowed for this path or method”——每个回合都鉴权失败。
# 解决：spawn 前把这些继承来的编排变量清掉，让 tclaude 用它自己的 IOA 登录凭证。
_STRIP_ENV_KEYS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_CUSTOM_HEADERS",
    "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_SESSION_ID",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "CLAUDE_CODE_TEAMMATE_COMMAND",
    "CLAUDE_CONFIG_DIR",      # 让 tclaude 回落到自己的默认配置目录（/root/.tclaude）
    "CLAUDE_SETTINGS_DIR",
    "CLAUDECODE",
)


def _child_env() -> dict:
    """构造给 tclaude 子进程的环境：复制当前环境，剔除继承来的编排态变量。"""
    env = {k: v for k, v in os.environ.items() if k not in _STRIP_ENV_KEYS}
    return env


def _kill_process_group(proc: asyncio.subprocess.Process, sig: int) -> bool:
    """优雅地把 proc 整个进程组发信号。返回 True 表示信号已发出。"""
    if proc.returncode is not None:
        return False
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        # 兜底：至少把直接子进程杀掉
        try:
            if sig == signal.SIGKILL:
                proc.kill()
            else:
                proc.terminate()
            return True
        except Exception:
            return False


class AgentProvider(ABC):
    @abstractmethod
    async def run_turn(
        self,
        session_id: str,
        message: str,
        workdir: str,
        resume_claude_session: str | None,
        on_event: EventCallback,
        model: str | None = None,
        on_permission=None,
        on_session_id=None,
        effort: str | None = None,
    ) -> dict:
        """跑一个 agent 回合。"""

    async def send_turn(self, *args, **kwargs):
        return await self.run_turn(*args, **kwargs)

    async def forget_session(self, *args, **kwargs):
        pass

    async def respond_permission(self, *args, **kwargs):
        pass

    async def ensure_warm(self, *args, **kwargs):
        pass

    async def cleanup_idle(self, *args, **kwargs):
        pass
