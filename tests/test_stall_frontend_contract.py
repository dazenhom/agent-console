"""Stall Watch 前端契约（正则读 web/app.js，先例 test_history_refresh_frontend_contract.py）。

盯的是「续跑问新成本上限」那段：成本上限是后端 env 配置（GOAL_MAX_COST_USD / 每目标
max_cost_usd），前端一旦自己兜一个具体数字（曾写死 20），env 改了就会拿错数字诱导用户
定预算——这类"看着像防御性默认值、实则硬编码另一套配置"的写法只能用源码断言守住——
它不报错、不抛异常，只在特定配置下悄悄显示错值。
"""
import re
from pathlib import Path

APP_JS = (
    Path(__file__).resolve().parents[1] / "web" / "app.js"
).read_text(encoding="utf-8")


def _continue_handler() -> str:
    """取 renderStallRow 里继续按钮的 onclick 函数体（缩进/选择器变了要同步本测试）。"""
    m = re.search(
        r"row\.querySelector\(\"\[data-act='continue'\]\"\)\.onclick = async \(\) => \{"
        r"(?P<body>.*?)\n    \};",
        APP_JS,
        re.DOTALL,
    )
    assert m, "未找到 continue 按钮的处理函数"
    return m.group("body")


def test_cost_capped_prompt_does_not_hardcode_limit():
    body = _continue_handler()
    assert 'it.kind === "goal_cost_capped"' in body and "prompt(" in body
    assert "|| 20" not in body  # 曾经的硬编码兜底：env 一改就显示错值
    assert "上限未知" in body    # 上限缺失时如实说未知，不编数字


def test_cost_capped_prompt_prefills_only_when_limit_known():
    body = _continue_handler()
    # 预填第二参：有上限才预填具体值，缺失时传空串（留空 = 不改上限，由后端沿用原值）
    assert 'hasLimit ? String(rel.cost_limit) : ""' in body
