import re
from pathlib import Path


APP_JS = (
    Path(__file__).resolve().parents[1] / "web" / "app.js"
).read_text(encoding="utf-8")


def test_foreground_resync_uses_silent_history_validation():
    resync = re.search(
        r"function resync\(\) \{(?P<body>.*?)\n  \}\n\n  function closeWs",
        APP_JS,
        re.DOTALL,
    )
    assert resync
    body = resync.group("body")
    assert "loadHistory({ silent: true, preserveScroll: true })" in body
    assert "loadHistory().catch" not in body


def test_silent_history_validation_keeps_dom_when_snapshot_is_unchanged():
    load_history = re.search(
        r"async function loadHistory\(\{ silent = false, preserveScroll = false \} = \{\}\) "
        r"\{(?P<body>.*?)\n  \}\n\n  function renderLoadEarlierBtn",
        APP_JS,
        re.DOTALL,
    )
    assert load_history
    body = load_history.group("body")
    unchanged = body.index("sameHistorySnapshot(previous, msgs)")
    rerender = body.index("renderHistorySnapshot(msgs")
    assert unchanged < rerender
    assert "if (!silent) chat.innerHTML" in body
    assert "if (silent) return;" in body
    assert "msgs.length < previous.length && previous.some((m) => m._live)" in body


def test_reselecting_current_session_does_not_reload_history():
    switch_session = re.search(
        r"async function switchSession\(id\) \{(?P<body>.*?)\n  \}\n\n  // workdir",
        APP_JS,
        re.DOTALL,
    )
    assert switch_session
    body = switch_session.group("body")
    guard = body.index("prevId === id && state.historySessionId === id")
    load = body.index("await loadHistory();")
    assert guard < load


def test_ws_echo_of_locally_rendered_user_message_is_deduplicated():
    assert "function recordWsHistory(role, content)" in APP_JS
    assert "last._liveLocal && isSameLiveMessage(last, role, content)" in APP_JS
    assert 'recordLiveHistory("user", { text }, { _liveLocal: true });' in APP_JS
