"""日志依赖注入：各模块不再直连 stdlib logging 全局单例，改为经统一 Provider 获取 logger。

设计要点（改造前后行为等价）：
- `Logger`：日志器接口（结构类型 Protocol），只列出本项目实际用到的方法；stdlib
  `logging.Logger` 天然满足，无需包装。
- `LoggerProvider`：logger 来源的抽象。默认实现 `StdLoggerProvider` 直接透传
  `logging.getLogger(name)`，返回的就是改造前那个完全相同的 logger 对象——名称、
  级别、handler、输出格式一律不变，因此行为、日志格式与级别严格等价。
- `get_logger(name)`：各模块的注入获取入口（替代直接调用 logging.getLogger）。
- `configure(provider)`：初始化装配点。默认在本模块导入时安装 `StdLoggerProvider`；
  应用启动（main.lifespan）或测试可在此显式注入不同的 Provider（如假 logger、
  或将来切换日志后端），而无需改动任何业务模块。
"""
from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable


@runtime_checkable
class Logger(Protocol):
    """日志器接口：仅列出本项目实际使用的方法（stdlib Logger 已满足此结构）。"""

    def debug(self, msg: object, *args: object, **kwargs: object) -> None: ...
    def info(self, msg: object, *args: object, **kwargs: object) -> None: ...
    def warning(self, msg: object, *args: object, **kwargs: object) -> None: ...
    def error(self, msg: object, *args: object, **kwargs: object) -> None: ...
    def exception(self, msg: object, *args: object, **kwargs: object) -> None: ...


class LoggerProvider(Protocol):
    """logger 来源抽象：给定名称返回一个 Logger。"""

    def get_logger(self, name: str) -> Logger: ...


class StdLoggerProvider:
    """默认实现：直接透传 stdlib logging.getLogger，与改造前行为完全一致。"""

    def get_logger(self, name: str) -> Logger:
        return logging.getLogger(name)


# 装配点：默认安装 stdlib 透传 Provider（导入即生效，保证行为等价）。
_provider: LoggerProvider = StdLoggerProvider()


def configure(provider: LoggerProvider | None = None) -> None:
    """初始化装配点：安装全局 LoggerProvider。

    传 None 时安装默认的 `StdLoggerProvider`（幂等，与改造前一致）；传入自定义
    Provider 则全局改由其提供 logger，供测试注入或切换日志后端时使用。
    """
    global _provider
    _provider = provider if provider is not None else StdLoggerProvider()


def get_logger(name: str) -> Logger:
    """各模块经此注入获取 logger，而非直接调用 logging.getLogger。"""
    return _provider.get_logger(name)
