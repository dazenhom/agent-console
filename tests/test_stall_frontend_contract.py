"""Stall Watch 前端契约（正则读 web/app.js，先例 test_history_refresh_frontend_contract.py）。

盯两件事：
1) 「续跑问新成本上限」那段：成本上限是后端 env 配置（GOAL_MAX_COST_USD / 每目标
   max_cost_usd），前端一旦自己兜一个具体数字（曾写死 20），env 改了就会拿错数字诱导用户
   定预算——这类"看着像防御性默认值、实则硬编码另一套配置"的写法只能用源码断言守住——
   它不报错、不抛异常，只在特定配置下悄悄显示错值。
2) 「继续（带记忆续跑）」的行内表单：goal 三类（成本熔断/耗尽/暂停）必须展开表单并把
   用户写的 resume_note 一起提交，且表单得落在 .stall-row-main 之外（否则点表单会触发
   行主体的只读会话预览，把表单顶掉）。
"""
import re
from pathlib import Path

APP_JS = (
    Path(__file__).resolve().parents[1] / "web" / "app.js"
).read_text(encoding="utf-8")
STYLE_CSS = (
    Path(__file__).resolve().parents[1] / "web" / "style.css"
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


def _continue_form() -> str:
    """取行内续跑表单的构造体（buildStallContinueForm 函数体）：成本上限的预填口径与提交
    body 的字段组装都搬到这里了。函数名/签名变了要同步本测试。"""
    m = re.search(
        r"function buildStallContinueForm\(it, onSubmit, onCancel\) \{"
        r"(?P<body>.*?)\n  \}",
        APP_JS,
        re.DOTALL,
    )
    assert m, "未找到行内续跑表单的构造函数 buildStallContinueForm"
    return m.group("body")


def _goal_continue_form() -> str:
    """取目标列表卡片「继续」弹窗（openGoalContinueForm）的函数体。"""
    m = re.search(
        r"async function openGoalContinueForm\(it\) \{(?P<body>.*?)\n  \}",
        APP_JS,
        re.DOTALL,
    )
    assert m, "未找到 openGoalContinueForm"
    return m.group("body")


def test_cost_capped_limit_does_not_hardcode_value():
    body = _continue_form()
    assert 'it.kind === "goal_cost_capped"' in body
    assert "|| 20" not in body  # 曾经的硬编码兜底：env 一改就显示错值
    assert "上限未知" in body    # 上限缺失时如实说未知，不编数字


def test_cost_capped_limit_prefills_only_when_limit_known():
    body = _continue_form()
    # 预填输入框的 value 属性：有上限才预填具体值，缺失时传空串（留空 = 不改上限，后端沿用原值）
    assert 'hasLimit ? String(rel.cost_limit) : ""' in body


def test_goal_kinds_open_inline_form_instead_of_one_click():
    """goal 三类的续跑要带记忆（resume_note）：点「继续」先展开表单，再点收起；其余
    kind 仍是一键直发。"""
    body = _continue_handler()
    for kind in ('"goal_cost_capped"', '"goal_exhausted"', '"goal_paused"'):
        assert kind in body, f"行内表单分支没覆盖 {kind}"
    assert "openForm()" in body and "closeForm()" in body
    # 非 goal 分支保持原来的直发：直接 post 空 body
    assert 'await post("continue", {}, 120000)' in body


def test_continue_form_submits_resume_note():
    """表单提交必须把「这次换个跑法」的文本作为 resume_note 带给后端（留空则不传，
    退回不带记忆的续跑）。"""
    body = _continue_form()
    assert 'data-act="continue-note"' in body
    assert "body.resume_note = resumeNote" in body
    assert "续跑指示不要超过 2000 字" in body  # 与前端的 2000 字上限同口径


def test_goal_continue_modal_sends_resume_note():
    body = _goal_continue_form()
    assert 'id="gc-note"' in body
    assert "resume_note: resumeNote || undefined" in body


def test_continue_handler_does_not_reuse_modal_root():
    """行内表单绝不能借 modal-root 实现：Stall Watch 侧同时可能开着只读会话预览 sheet
    （也占 modal-root），续跑 handler 一旦去碰它会静默把 sheet 顶掉/被 sheet 顶掉。"""
    assert "modal-root" not in _continue_handler()
    assert "modal-root" not in _continue_form()


def test_stall_row_main_is_clickable():
    """行主体可点开只读会话预览，光标得跟着变（否则用户不知道这儿能点）。"""
    m = re.search(r"\.stall-row-main \{(?P<body>.*?)\n\}", STYLE_CSS, re.DOTALL)
    assert m, "style.css 里找不到 .stall-row-main 规则块"
    assert "cursor: pointer" in m.group("body")


def test_inline_form_is_outside_row_main():
    """表单要独占一行地挂在 .stall-row 上：CSS 里 flex-basis:100% + .stall-row 换行，
    app.js 里 append 到 row（不是 mainEl），两道一起守住"点表单不会弹预览"。"""
    assert "row.appendChild(formEl)" in APP_JS
    m = re.search(r"\.stall-continue-form \{(?P<body>.*?)\n\}", STYLE_CSS, re.DOTALL)
    assert m, "style.css 里找不到 .stall-continue-form 规则块"
    assert "flex-basis: 100%" in m.group("body")
    row = re.search(r"\.stall-row \{(?P<body>.*?)\n\}", STYLE_CSS, re.DOTALL)
    assert row and "flex-wrap: wrap" in row.group("body")
