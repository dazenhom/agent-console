import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from server import config
from server import main as main_module


def test_javascript_rewrite_sets_router_base_and_is_idempotent():
    source = (
        'const router={history:createHistory("/"),routes:[{path:"/:pathMatch(.*)*"}]};'
        'function preload(e){return"/"+e}'
    )

    rewritten, changed = main_module._rewrite_swanlab_javascript(
        "assets/main.js", source, "/proxy/swanlab"
    )
    rewritten_twice, changed_twice = main_module._rewrite_swanlab_javascript(
        "assets/main.js", rewritten, "/proxy/swanlab"
    )

    assert changed is True
    assert 'history:createHistory("/proxy/swanlab/")' in rewritten
    assert 'return"/proxy/swanlab/"+e' in rewritten
    assert rewritten_twice == rewritten
    assert changed_twice is False
    assert "/proxy/swanlab/proxy/swanlab" not in rewritten


def test_javascript_rewrite_preserves_outer_public_base():
    source = 'const router={history:(0,makeHistory)("/")};'

    rewritten, changed = main_module._rewrite_swanlab_javascript(
        "assets/main.js", source, "/proxy/80/proxy/swanlab"
    )

    assert changed is True
    assert 'history:(0,makeHistory)("/proxy/80/proxy/swanlab/")' in rewritten


def test_html_rewrite_uses_public_base_posts_ready_and_is_idempotent():
    source = (
        '<html><head><link href="/assets/app.css"></head>'
        '<body><div id="app"></div><script src="/assets/app.js"></script></body></html>'
    )

    rewritten = main_module._rewrite_swanlab_html(
        source, "/proxy/80/proxy/swanlab"
    )
    rewritten_twice = main_module._rewrite_swanlab_html(
        rewritten, "/proxy/80/proxy/swanlab"
    )

    assert '<base data-agent-console-swanlab-proxy="2" href="/proxy/80/proxy/swanlab/">' in rewritten
    assert 'var _PROXY = "/proxy/80/proxy/swanlab"' in rewritten
    assert "window.parent.postMessage({type: 'swanlab-ready'}, location.origin)" in rewritten
    assert '/proxy/80/proxy/swanlab/assets/app.js?ac_swanlab_proxy=2' in rewritten
    assert '/proxy/80/proxy/swanlab/assets/app.css?ac_swanlab_proxy=2' in rewritten
    assert rewritten_twice == rewritten
    assert "/proxy/80/proxy/swanlab/proxy/swanlab" not in rewritten


@pytest.mark.parametrize(
    "path, expected_status",
    [
        ("../api/project", 400),
        ("assets/../secret", 400),
        ("assets\\main.js", 400),
        ("assets//main.js", 400),
        ("assets/%2e%2e/secret", 400),
        ("/assets/main.js", 400),
        ("internal/admin", 403),
    ],
)
def test_swanlab_path_rejects_unsafe_or_unknown_paths(path, expected_status):
    with pytest.raises(HTTPException) as exc:
        main_module._validate_swanlab_path(path)
    assert exc.value.status_code == expected_status


@pytest.mark.parametrize("path", ["icon.png", "apple-icon.png", "manifest.json"])
def test_swanlab_path_allows_required_root_assets(path):
    assert main_module._validate_swanlab_path(path) == path


def test_signed_session_token_binds_public_base_and_rejects_tampering(monkeypatch):
    monkeypatch.setattr(config, "SWANLAB_SESSION_SECRET", "s" * 48)
    monkeypatch.setattr(config, "SWANLAB_SESSION_TTL", 300)

    token, expires_at = main_module._make_swanlab_session_token("/proxy/80", now=1000)
    payload = main_module._verify_swanlab_session_token(token, now=1001)

    assert expires_at == 1300
    assert payload == {"exp": 1300, "public_base": "/proxy/80"}

    with pytest.raises(HTTPException) as tampered:
        main_module._verify_swanlab_session_token(token[:-1] + "x", now=1001)
    assert tampered.value.status_code == 401

    with pytest.raises(HTTPException) as expired:
        main_module._verify_swanlab_session_token(token, now=1300)
    assert expired.value.status_code == 401


def test_session_signing_fails_closed_without_independent_secret(monkeypatch):
    monkeypatch.setattr(config, "SWANLAB_SESSION_SECRET", "")

    with pytest.raises(HTTPException) as exc:
        main_module._make_swanlab_session_token("")

    assert exc.value.status_code == 503


def test_session_endpoint_sets_httponly_cookie_and_proxy_requires_it(monkeypatch):
    monkeypatch.setattr(config, "AUTH_TOKEN", "unit-token")
    monkeypatch.setattr(config, "SWANLAB_SESSION_SECRET", "s" * 48)
    monkeypatch.setattr(config, "SWANLAB_SESSION_TTL", 300)

    async def fake_login(force=False):
        return "server-only-sid"

    monkeypatch.setattr(main_module, "_login_swanlab", fake_login)

    client = TestClient(main_module.app)
    response = client.post(
        "/api/swanlab/session",
        headers={"Authorization": "Bearer unit-token"},
        json={"path": "@Speech_Model/demo", "public_base": ""},
    )

    assert response.status_code == 200
    assert response.json()["url"] == "/proxy/swanlab/@Speech_Model/demo"
    set_cookie = response.headers["set-cookie"]
    assert main_module.SWANLAB_SESSION_COOKIE in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Path=/proxy/swanlab" in set_cookie
    assert "swanlab_sid" not in set_cookie

    unauthenticated = TestClient(main_module.app).get(
        "/proxy/swanlab/assets/main.js"
    )
    assert unauthenticated.status_code == 401

    forbidden = client.get("/proxy/swanlab/internal/admin")
    assert forbidden.status_code == 403


def test_revoke_session_clears_cookie_at_requested_public_base(monkeypatch):
    monkeypatch.setattr(config, "AUTH_TOKEN", "unit-token")

    response = TestClient(main_module.app).request(
        "DELETE",
        "/api/swanlab/session",
        headers={"Authorization": "Bearer unit-token"},
        json={"public_base": "/proxy/80"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    set_cookie = response.headers["set-cookie"]
    assert f"{main_module.SWANLAB_SESSION_COOKIE}=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Max-Age=0" in set_cookie
    assert "Path=/proxy/80/proxy/swanlab" in set_cookie


def test_unauthenticated_cookie_clear_path_is_safe_for_401_logout(monkeypatch):
    async def unexpected_login(force=False):
        raise AssertionError("cookie-only cleanup must not contact SwanLab")

    monkeypatch.setattr(main_module, "_login_swanlab", unexpected_login)
    client = TestClient(main_module.app)

    response = client.request(
        "DELETE",
        "/api/swanlab/session/cookie",
        json={"public_base": "/proxy/80"},
    )
    protected_response = client.request(
        "DELETE",
        "/api/swanlab/session",
        json={"public_base": "/proxy/80"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    set_cookie = response.headers["set-cookie"]
    assert f"{main_module.SWANLAB_SESSION_COOKIE}=" in set_cookie
    assert "Max-Age=0" in set_cookie
    assert "Path=/proxy/80/proxy/swanlab" in set_cookie
    assert protected_response.status_code == 401


def test_authorized_proxy_rewrites_javascript_without_exposing_sid(monkeypatch):
    monkeypatch.setattr(config, "AUTH_TOKEN", "unit-token")
    monkeypatch.setattr(config, "SWANLAB_SESSION_SECRET", "s" * 48)
    monkeypatch.setattr(config, "SWANLAB_SESSION_TTL", 300)

    async def fake_login(force=False):
        return "server-only-sid"

    class FakeUpstreamResponse:
        status_code = 200
        headers = {
            "content-type": "application/javascript",
            "cache-control": "public, max-age=31536000",
            "set-cookie": "sid=must-not-leak",
        }
        content = b'const r={history:P("/")};function p(e){return"/"+e}'

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def request(self, **kwargs):
            assert kwargs["headers"]["cookie"] == "sid=server-only-sid"
            return FakeUpstreamResponse()

    monkeypatch.setattr(main_module, "_login_swanlab", fake_login)
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    client = TestClient(main_module.app)
    session_response = client.post(
        "/api/swanlab/session",
        headers={"Authorization": "Bearer unit-token"},
        json={"path": "@Speech_Model/demo", "public_base": ""},
    )
    assert session_response.status_code == 200

    response = client.get("/proxy/swanlab/assets/main.js")

    assert response.status_code == 200
    assert 'history:P("/proxy/swanlab/")' in response.text
    assert 'return"/proxy/swanlab/"+e' in response.text
    assert response.headers["cache-control"] == "no-store"
    assert "set-cookie" not in response.headers
    assert "server-only-sid" not in response.text
    assert "must-not-leak" not in response.text


def test_status_endpoint_returns_selected_health_fields_without_secrets(monkeypatch):
    monkeypatch.setattr(config, "AUTH_TOKEN", "unit-token")
    monkeypatch.setattr(config, "SWANLAB_API_KEY", "configured-for-test")

    async def fake_login(force=False):
        assert force is True
        return "server-only-sid"

    class FakeStatusResponse:
        status_code = 200

        @staticmethod
        def json():
            return {
                "version": "2.8.1",
                "expired": False,
                "expiredAt": "2026-07-29T16:00:00Z",
                "sid": "must-not-leak",
                "api_key": "must-not-leak",
            }

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url, headers):
            assert headers["cookie"] == "sid=server-only-sid"
            return FakeStatusResponse()

    monkeypatch.setattr(main_module, "_login_swanlab", fake_login)
    monkeypatch.setattr(httpx, "AsyncClient", FakeAsyncClient)

    response = TestClient(main_module.app).get(
        "/api/swanlab/status",
        headers={"Authorization": "Bearer unit-token"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["version"] == "2.8.1"
    assert payload["license_expired"] is False
    assert payload["license_expires_at"] == "2026-07-29T16:00:00Z"
    assert "sid" not in payload
    assert "api_key" not in payload


def test_public_base_validation_accepts_proxy_prefix_and_rejects_urls():
    assert main_module._normalize_swanlab_public_base("/proxy/80/") == "/proxy/80"
    assert main_module._swanlab_proxy_base("/proxy/80") == "/proxy/80/proxy/swanlab"

    for value in ("https://example.com", "//example.com", "/proxy/../80", "/proxy/%38%30"):
        with pytest.raises(HTTPException):
            main_module._normalize_swanlab_public_base(value)
