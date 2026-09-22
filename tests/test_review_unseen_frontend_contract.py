"""Review Tab「未查看 / 已查看」两段 + seen 水位前后端契约。

口径只有一处：app.js isUnseen = max(服务端 last_seen_at, 本地 localStorage) 与 updated_at 比。
最容易复发的错是「标记已读走了 update_session」——那会刷 updated_at，把刚点开的会话顶到
列表最前并再次判成未读（2026-09-22 踩过），故这里有一条直接量 DB 的测试兜它。
"""
import re
from pathlib import Path

from fastapi.testclient import TestClient

from server import config
from server import main as main_module


ROOT = Path(__file__).resolve().parents[1]
APP_JS = (ROOT / "web" / "app.js").read_text(encoding="utf-8")
INDEX_HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")


def _function_body(name: str, next_marker: str) -> str:
    """取某个顶层函数的函数体（到下一个同级函数前的注释行为止）。

    与 test_session_pin_todo_frontend_contract 同款，差别只是对 marker 做了 re.escape——
    这里的 marker 带 '+'、'(' 等正则元字符，不转义会直接编译不过。
    """
    match = re.search(
        rf"function {name}\([^)]*\) \{{(?P<body>.*?)\n  \}}\n\n  {re.escape(next_marker)}",
        APP_JS,
        re.DOTALL,
    )
    assert match, f"找不到 {name} 的函数体（下一个 marker 也许已改）"
    return match.group("body")


def test_both_review_sections_exist_in_html_and_js():
    for sid in ("session-list-review-unseen", "session-list-review-seen"):
        assert INDEX_HTML.count(sid) == 1, f"{sid} 在 index.html 里应恰好出现一次"
        assert sid in APP_JS, f"{sid} 未被 app.js 引用"


def test_old_single_review_list_id_is_gone():
    """旧的 session-list-review 是单段列表，两段化后必须彻底移除（带引号的精确匹配，
    免得被 session-list-review-unseen 这类前缀相同的 id 蒙混过关）。"""
    assert 'session-list-review"' not in APP_JS
    assert 'session-list-review"' not in INDEX_HTML
    assert "<ul id=\"session-list-review\"" not in INDEX_HTML


def test_review_sections_are_wired_and_labels_come_from_js():
    # 两段容器与 label 都在 HTML 侧，label 文案（未查看 N）由 renderReviewList 填
    assert INDEX_HTML.index('id="review-unseen-section"') < INDEX_HTML.index('id="review-seen-section"')
    assert 'id="review-unseen-label"' in INDEX_HTML
    assert 'id="review-seen-label"' in INDEX_HTML
    assert 'id="review-sub"' in INDEX_HTML
    # 三段结构里不能再出现「待审视」旧抬头
    assert "待审视" not in INDEX_HTML


def test_render_review_list_splits_done_sessions_into_two_sections():
    body = _function_body("renderReviewList", "// Review 两段计数实时减")
    assert 'session-list-review-unseen' in body
    assert 'session-list-review-seen' in body
    assert 'isUnseen(' in body
    # 未查看段有专属空态，不能借 fillList 的「这里还没有会话」（语义是"全都看过了"）
    assert "review-allread" in body
    # 已查看段空时整段隐藏（否则空列表 + 空态文案是噪声）
    assert 'classList.toggle("hidden"' in body


def test_render_review_list_is_the_only_review_renderer():
    """两处旧调用（renderSessionLists / patchSessionRow）都要收敛到 renderReviewList。"""
    assert APP_JS.count("renderReviewList(") >= 3


def test_every_unseen_decision_goes_through_is_unseen():
    """口径唯一：行圆点 / 看板计数 / Review 分段 / 计数刷新共 4 处以上，全走 isUnseen。"""
    assert APP_JS.count("isUnseen(") >= 4
    row = _function_body("renderSessionRow", "// 路径缩写")
    assert "isUnseen(s)" in row
    dash = _function_body("renderDashboard", "// ---------------- 智能任务看板 ----------------")
    assert "isUnseen(s, seenMap)" in dash
    counts = _function_body("refreshReviewCounts", "// Overview 列表 + Agents 副标题")
    assert "isUnseen(s, map)" in counts


def test_mark_seen_skips_redundant_post():
    """loadSessions 每轮轮询都会对当前会话调 markSeen，水位没往前推就必须短路，否则 POST 风暴。"""
    body = _function_body("markSeen", "// 未查看判定唯一口径")
    assert "/seen" in body
    short_circuit = re.search(r"if \((?P<cond>[^\n]*)\) return;", body)
    assert short_circuit, "markSeen 缺少 no-op 短路"
    assert "prev" in short_circuit.group("cond")
    # 短路必须在 POST 之前
    assert body.index(short_circuit.group(0)) < body.index("/seen")


def test_switch_session_refreshes_counts_without_rebuilding_review_list():
    body = _function_body("switchSession", "// 详情页头摘要")
    assert body.count("refreshReviewCounts();") == 2
    # 只改计数，绝不重建 Review 列表：重建会让刚点开的行立刻跳到「已查看」段，滚动位置丢失
    assert "renderReviewList()" not in body
    assert body.count("renderDashboard();") == 2


def test_unseen_dashboard_card_jumps_to_review_tab():
    dash = _function_body("renderDashboard", "// ---------------- 智能任务看板 ----------------")
    assert 'jump: "review"' in dash
    assert "data-jump" in dash
    assert 'switchTab(n.dataset.jump)' in dash
    # 只有待查看那张卡可点：失败/空闲没有对应分区
    assert dash.count("jump:") == 1


def test_review_unseen_styles_are_scoped_to_the_section():
    css = (ROOT / "web" / "style.css").read_text(encoding="utf-8")
    # label 样式：漏配就是无样式裸文本
    assert "#review-unseen-section .section-label" in css
    assert "#review-seen-section .section-label" in css
    # 行强化用 ID 提权，必须补 li.active 兜回选中态
    assert "#session-list-review-unseen li.active" in css
    assert ".review-allread" in css
    assert ".mini-stat--jump" in css


def test_set_session_seen_does_not_touch_updated_at_or_order(temp_db):
    older = temp_db.create_session("older", "/tmp")
    newer = temp_db.create_session("newer", "/tmp")
    temp_db._exec("UPDATE sessions SET updated_at=? WHERE id=?", (100.0, older["id"]))
    temp_db._exec("UPDATE sessions SET updated_at=? WHERE id=?", (200.0, newer["id"]))
    before = temp_db.get_session(older["id"])["updated_at"]

    temp_db.set_session_seen(older["id"])

    row = temp_db.get_session(older["id"])
    assert row["updated_at"] == before, "标记已读不许刷 updated_at（会把会话顶到最前 + 再次标未读）"
    assert row["last_seen_at"] >= row["updated_at"], "水位要盖住 updated_at，否则同毫秒仍判未读"
    # 顺序不动：仍按 updated_at DESC
    rows = temp_db.list_sessions()
    assert [r["id"] for r in rows[:2]] == [newer["id"], older["id"]]
    # 新列随 SELECT * 出现在列表接口里（前端 isUnseen 依赖它）
    assert next(r for r in rows if r["id"] == older["id"])["last_seen_at"] > 0


def test_set_session_seen_floors_water_level_at_updated_at(temp_db):
    """水位口径 = max(传入 ts, updated_at)：调用方（后端接口）实际只传 None → 取当前时间。

    传入更小的 ts 会被 updated_at 兜住，绝不会低于 updated_at——这是「同毫秒仍判未读」的兜底。
    """
    sess = temp_db.create_session("s", "/tmp")
    temp_db._exec("UPDATE sessions SET updated_at=? WHERE id=?", (100.0, sess["id"]))

    temp_db.set_session_seen(sess["id"], 150.0)
    assert temp_db.get_session(sess["id"])["last_seen_at"] == 150.0
    # 传进来的水位比 updated_at 还早 → 抬到 updated_at，不留一个低于更新的水位
    temp_db.set_session_seen(sess["id"], 10.0)
    assert temp_db.get_session(sess["id"])["last_seen_at"] == 100.0
    # 接口默认路径（不传 ts）：取服务端当前时间，必 > updated_at
    temp_db.set_session_seen(sess["id"])
    assert temp_db.get_session(sess["id"])["last_seen_at"] > 100.0


def test_seen_endpoint_writes_watermark_without_touching_updated_at(monkeypatch, temp_db):
    """走真实路由：前端 markSeen 打的 POST 必须只动 last_seen_at。"""
    monkeypatch.setattr(config, "AUTH_TOKEN", "unit-token")
    sess = temp_db.create_session("跑完的会话", "/tmp")
    temp_db._exec("UPDATE sessions SET updated_at=? WHERE id=?", (100.0, sess["id"]))

    client = TestClient(main_module.app)
    resp = client.post(
        f"/api/sessions/{sess['id']}/seen",
        headers={"Authorization": "Bearer unit-token"},
    )

    assert resp.status_code == 200
    row = temp_db.get_session(sess["id"])
    assert row["updated_at"] == 100.0
    assert row["last_seen_at"] > 100.0


def test_seen_endpoint_404s_for_unknown_session(monkeypatch, temp_db):
    monkeypatch.setattr(config, "AUTH_TOKEN", "unit-token")
    client = TestClient(main_module.app)
    resp = client.post(
        "/api/sessions/nope/seen",
        headers={"Authorization": "Bearer unit-token"},
    )
    assert resp.status_code == 404
