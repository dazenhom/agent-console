import stat

import pytest
from fastapi.testclient import TestClient

from server import config
from server import main as main_module


@pytest.fixture
def notify_env(monkeypatch, tmp_path, temp_db):
    secret_path = tmp_path / "data" / "notify_secret"
    calls = []

    monkeypatch.setattr(config, "AUTH_TOKEN", "unit-token")
    monkeypatch.delenv("LOCAL_NOTIFY_SECRET", raising=False)
    monkeypatch.setattr(config, "LOCAL_NOTIFY_SECRET_PATH", str(secret_path))
    monkeypatch.setattr(config, "_local_notify_secret_cache", None)
    monkeypatch.setattr(config, "WECOM_ENABLED", False)

    async def fake_notify(**kwargs):
        calls.append(kwargs)
        return True, "ok"

    monkeypatch.setattr(main_module.wecom_notify, "notify", fake_notify)

    async def fake_broadcast_monitor(_payload):
        return None

    monkeypatch.setattr(main_module.hub, "broadcast_monitor", fake_broadcast_monitor)
    return {"calls": calls, "secret_path": secret_path}


def _post_notify(client, headers=None):
    return client.post(
        "/api/notify",
        headers=headers or {},
        json={"title": "测试标题", "text": "测试正文", "level": "warning"},
    )


def test_correct_local_secret_bypasses_bearer(notify_env):
    secret = config.get_local_notify_secret()

    response = _post_notify(
        TestClient(main_module.app),
        {"X-Local-Secret": secret},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True, "wecom_ok": True, "wecom_detail": ""}
    assert len(notify_env["calls"]) == 1
    assert notify_env["calls"][0] == {
        "title": "测试标题",
        "user_text": "",
        "reply_text": "测试正文",
        "status": "success",
    }
    assert stat.S_IMODE(notify_env["secret_path"].stat().st_mode) == 0o600


def test_wrong_local_secret_without_bearer_is_401(notify_env):
    response = _post_notify(
        TestClient(main_module.app),
        {"X-Local-Secret": "wrong-secret"},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "未授权"
    assert notify_env["calls"] == []


def test_no_local_secret_with_valid_bearer_is_200(notify_env):
    response = _post_notify(
        TestClient(main_module.app),
        {"Authorization": "Bearer unit-token"},
    )

    assert response.status_code == 200
    assert response.json()["wecom_ok"] is True
    assert len(notify_env["calls"]) == 1


def test_wrong_local_secret_with_valid_bearer_is_200(notify_env):
    response = _post_notify(
        TestClient(main_module.app),
        {
            "X-Local-Secret": "wrong-secret",
            "Authorization": "Bearer unit-token",
        },
    )

    assert response.status_code == 200
    assert response.json()["wecom_ok"] is True
    assert len(notify_env["calls"]) == 1


def test_empty_local_secret_header_never_bypasses(notify_env):
    response = _post_notify(
        TestClient(main_module.app),
        {"X-Local-Secret": ""},
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "未授权"
    assert notify_env["calls"] == []


def test_secret_generation_failure_falls_back_to_bearer(
    monkeypatch, tmp_path, notify_env
):
    blocker = tmp_path / "blocker_file"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(
        config,
        "LOCAL_NOTIFY_SECRET_PATH",
        str(blocker / "notify_secret"),
    )
    monkeypatch.setattr(config, "_local_notify_secret_cache", None)

    assert config.get_local_notify_secret() == ""
    assert config._local_notify_secret_cache is None

    client = TestClient(main_module.app)
    unauthorized = _post_notify(client)
    authorized = _post_notify(
        client,
        {"Authorization": "Bearer unit-token"},
    )

    assert unauthorized.status_code == 401
    assert unauthorized.json()["detail"] == "未授权"
    assert authorized.status_code == 200
    assert authorized.json()["wecom_ok"] is True
    assert len(notify_env["calls"]) == 1


def test_non_ascii_local_secret_header_is_401_not_500(notify_env):
    response = _post_notify(
        TestClient(main_module.app),
        [(b"x-local-secret", b"\xff" * 8)],
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "未授权"
    assert notify_env["calls"] == []


def test_env_secret_takes_priority_without_touching_files(
    monkeypatch, notify_env
):
    monkeypatch.setenv("LOCAL_NOTIFY_SECRET", "e" * 40)
    monkeypatch.setattr(config, "_local_notify_secret_cache", None)

    response = _post_notify(
        TestClient(main_module.app),
        {"X-Local-Secret": "e" * 40},
    )

    assert response.status_code == 200
    assert response.json()["wecom_ok"] is True
    assert len(notify_env["calls"]) == 1
    assert not notify_env["secret_path"].exists()
