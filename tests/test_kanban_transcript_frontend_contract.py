"""看板卡片 → 只读会话内容预览（showSessionTranscript）的前后端契约。

这次改动的核心约束是"只读"：点开卡片是看一眼会话在做什么，不能顺手变成编辑/发消息
入口。前端断言直接对着源码文本做，防止后续把预览接到 peek 输入框那条路上（那条路
会把预览变成写会话的通道）而没人发现。后端断言覆盖 /meta 端点的字段白名单——
sessions 表里有 claude_session_id / pending_compact_summary 等内部字段，一旦有人把
它改成 `return sess` 就会整行外泄。
"""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server import config
from server import main as main_module


ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "web" / "style.css").read_text(encoding="utf-8")

META_FIELDS = {
    "id", "title", "status", "summary", "archived", "workdir", "engine", "updated_at",
}


def _function_body(name: str) -> str:
    """截取 app.js 里某个顶层函数的函数体（缩进 2 空格的函数，以其闭合行收尾）。"""
    m = re.search(
        rf"^  (?:async )?function {re.escape(name)}\(.*?\n  \}}\n",
        APP_JS,
        re.DOTALL | re.MULTILINE,
    )
    assert m, f"app.js 里找不到函数 {name}"
    return m.group(0)


def test_transcript_renders_messages_with_the_shared_renderer():
    body = _function_body("showSessionTranscript")
    assert "appendMsgWithPreview(" in body


def test_transcript_is_read_only():
    """只读硬护栏：预览函数体内不得出现任何写主输入框 / 切会话 / 全量拉取的调用。"""
    body = _function_body("showSessionTranscript")
    for forbidden in ("peekReply", "peek-input", "user_message", "switchSession(", "limit=0"):
        assert forbidden not in body, f"只读预览里不该出现 {forbidden}"


def test_transcript_strips_buttons_that_write_to_the_composer():
    assert '".msg-resend, .qr-btn"' in APP_JS
    # 摘按钮的 helper 必须真的在预览正文上被调用（渲染完 + 翻页各一次）
    assert APP_JS.count("stripInteractiveBtns(") >= 3


def test_jump_falls_back_to_loading_archived_sessions():
    """归档会话列表默认没加载，跳转必须补拉一次再判存在性，否则又回到"会话不存在"死路。"""
    body = _function_body("doJumpToSessionEnsured")
    assert "await loadArchivedSessions()" in body
    # 定义 + 两个调用点（看板行 ↗ / 多会话选择器）之外，预览底部「打开完整会话」也走它
    assert APP_JS.count("doJumpToSessionEnsured") >= 3
    assert "doJumpToSession(" not in APP_JS.replace("doJumpToSessionEnsured(", "")


def test_kanban_row_main_is_focusable_as_preview_opener():
    """预览的 opener 是 .kanban-row-main（纯 div），不带 tabindex 时 focus() 是静默
    no-op，Esc 关闭预览后焦点掉到 body —— 键盘/读屏用户丢失位置。这里钉住 tabindex
    只在构建处出现一次，防止将来重构把属性丢掉。"""
    m = re.search(r'<div class="kanban-row-main"([^>]*)>', APP_JS)
    assert m, "app.js 里找不到 .kanban-row-main 的元素构建处"
    assert 'tabindex="-1"' in m.group(1)


def test_transcript_accepts_explicit_sessions_and_goal_schedule_id():
    """向收件箱行泛化：opts.sessions 是权威会话列表（那一侧没有 todo 对象可解析），
    opts.goalScheduleId 给 🎯 入口。两个入参都缺省时必须回落旧路径（看板卡片行为不变）。"""
    body = _function_body("showSessionTranscript")
    assert "opts.sessions" in body
    assert "todoSessionIds(opts.todo)" in body
    assert "opts.goalScheduleId" in body
    # goal 入口的旧来源仍在：两个来源走同一条判断，别把看板那条删了
    assert "opts.todo.dispatched_schedule_id" in body


def test_stall_row_main_opens_the_same_preview():
    """收件箱行主体 = 同一个只读预览弹层：三类 kind 的会话来源在后端 related 里
    （todo_idle -> session_ids，dispatch_* -> child_session_ids），goal 类用行自身的
    session_id + ref_id 当目标循环入口；无会话时只提示、不开空弹层。"""
    body = _function_body("renderStallRow")
    assert "showSessionTranscript(" in body
    assert "session_ids" in body and "child_session_ids" in body
    assert 'it.session_id' in body and "it.ref_id" in body
    assert "该事项无关联会话" in body
    # 只有 main 一个点击入口，动作按钮区照旧各有各的 onclick
    assert body.count("showSessionTranscript(") == 1


def test_stall_row_main_is_focusable_as_preview_opener():
    """与 .kanban-row-main 同款教训：非可聚焦元素上 opener.focus() 是静默 no-op。"""
    m = re.search(r'<div class="stall-row-main"([^>]*)>', APP_JS)
    assert m, "app.js 里找不到 .stall-row-main 的元素构建处"
    assert 'tabindex="-1"' in m.group(1)
    assert "点击查看会话内容" in m.group(1)


def test_transcript_card_overrides_modal_narrow_width():
    """会话预览要覆盖 .modal-card 的 max-width: 340px，否则正文被挤成一条窄缝。"""
    m = re.search(r"\.transcript-card \{(?P<body>.*?)\n\}", STYLE_CSS, re.DOTALL)
    assert m, "style.css 里找不到 .transcript-card 规则块"
    body = m.group("body")
    assert "min(" in body
    assert "max-width: none" in body


def test_message_styles_are_not_scoped_under_chat_id():
    """预览正文复用 .msg/.bubble 的全局样式；若有人把它们改成 #chat 后代选择器，
    预览正文会全裸——这条断言是那种改动的预警。"""
    for sel in (".msg ", ".msg{", ".msg .bubble", ".tool-group "):
        assert f"#chat {sel}" not in STYLE_CSS


@pytest.fixture
def client(monkeypatch, temp_db):
    monkeypatch.setattr(config, "AUTH_TOKEN", "unit-token")
    return TestClient(main_module.app)


def _headers():
    return {"Authorization": "Bearer unit-token"}


def test_meta_of_archived_session_returns_whitelisted_fields(client, temp_db):
    sess = temp_db.create_session("归档会话", "/tmp", engine="codex")
    temp_db.update_session(
        sess["id"], archived=1, summary="一句话摘要", claude_session_id="internal-csid",
    )

    r = client.get(f"/api/sessions/{sess['id']}/meta", headers=_headers())

    assert r.status_code == 200
    body = r.json()
    assert set(body) == META_FIELDS
    assert body["id"] == sess["id"]
    assert body["title"] == "归档会话"
    assert body["archived"] == 1
    assert body["summary"] == "一句话摘要"
    assert body["engine"] == "codex"
    assert "claude_session_id" not in body
    assert "pending_compact_summary" not in body


def test_meta_404_for_unknown_session(client):
    r = client.get("/api/sessions/does-not-exist/meta", headers=_headers())
    assert r.status_code == 404


def test_meta_requires_auth(monkeypatch, temp_db):
    monkeypatch.setattr(config, "AUTH_TOKEN", "unit-token")
    r = TestClient(main_module.app).get("/api/sessions/whatever/meta")
    assert r.status_code == 401
