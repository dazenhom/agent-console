import re
from pathlib import Path


APP_JS = (
    Path(__file__).resolve().parents[1] / "web" / "app.js"
).read_text(encoding="utf-8")


def test_401_logout_waits_for_cookie_cleanup_contract():
    assert 'if (res.status === 401) { await logout();' in APP_JS
    logout_body = re.search(
        r"async function logout\(\) \{(?P<body>.*?)\n  \}\n"
        r"  \$\(\"logout-btn\"\)",
        APP_JS,
        re.DOTALL,
    )
    assert logout_body
    assert "resetSwanlabFrame();" in logout_body.group("body")
    assert "await clearSwanlabSessionCookie(1000);" in logout_body.group("body")


def test_close_swanlab_resets_iframe_and_revokes_cookie_contract():
    close_body = re.search(
        r"function closeSwanlab\(\) \{(?P<body>.*?)\n  \}",
        APP_JS,
        re.DOTALL,
    )
    assert close_body
    assert "resetSwanlabFrame();" in close_body.group("body")
    assert "clearSwanlabSessionCookie(1000);" in close_body.group("body")


def test_cookie_cleanup_does_not_depend_on_bearer_contract():
    cleanup_body = re.search(
        r"async function clearSwanlabSessionCookie\(.*?\) \{(?P<body>.*?)\n  \}",
        APP_JS,
        re.DOTALL,
    )
    assert cleanup_body
    body = cleanup_body.group("body")
    assert '"/api/swanlab/session/cookie"' in body
    assert "Authorization" not in body
