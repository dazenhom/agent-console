import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import start_dual


def _write_private(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)


def test_session_secret_is_generated_once_with_private_permissions(tmp_path):
    secret_path = tmp_path / ".secrets" / "swanlab_session_secret"

    first = start_dual.load_or_create_swanlab_session_secret({}, secret_path)
    second = start_dual.load_or_create_swanlab_session_secret({}, secret_path)

    assert first == second
    assert len(first) >= 32
    assert stat.S_IMODE(secret_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(secret_path.parent.stat().st_mode) == 0o700


def test_environment_credentials_take_priority_without_touching_files(tmp_path):
    api_path = tmp_path / "missing-api-key"
    session_path = tmp_path / "missing-session-secret"
    environment = {
        "PATH": "/usr/bin",
        "SWANLAB_API_KEY": "api-key-from-environment",
        "SWANLAB_SESSION_SECRET": "x" * 48,
    }

    built = start_dual.build_base_env(environment, api_path, session_path)

    assert built["SWANLAB_API_KEY"] == environment["SWANLAB_API_KEY"]
    assert built["SWANLAB_SESSION_SECRET"] == environment["SWANLAB_SESSION_SECRET"]
    assert not api_path.exists()
    assert not session_path.exists()


def test_api_key_loads_from_private_file_and_rejects_unsafe_permissions(tmp_path):
    api_path = tmp_path / ".secrets" / "swanlab_api_key"
    _write_private(api_path, "local-api-key")

    assert start_dual.load_swanlab_api_key({}, api_path) == "local-api-key"

    api_path.chmod(0o644)
    with pytest.raises(RuntimeError, match="0600"):
        start_dual.load_swanlab_api_key({}, api_path)


def test_build_base_env_loads_same_private_credentials_for_both_launches(tmp_path):
    api_path = tmp_path / ".secrets" / "swanlab_api_key"
    session_path = tmp_path / ".secrets" / "swanlab_session_secret"
    _write_private(api_path, "local-api-key")
    _write_private(session_path, "y" * 48)

    built = start_dual.build_base_env({"PATH": "/usr/bin"}, api_path, session_path)

    assert built["SWANLAB_API_KEY"] == "local-api-key"
    assert built["SWANLAB_SESSION_SECRET"] == "y" * 48


def test_private_value_loader_rejects_symlinks(tmp_path):
    real_path = tmp_path / "real"
    link_path = tmp_path / "link"
    _write_private(real_path, "private-value")
    link_path.symlink_to(real_path)

    with pytest.raises(RuntimeError, match="安全打开"):
        start_dual.load_swanlab_api_key({}, link_path)


def test_secret_directory_symlink_is_rejected(tmp_path):
    real_directory = tmp_path / "real-secrets"
    real_directory.mkdir(mode=0o700)
    linked_directory = tmp_path / "linked-secrets"
    linked_directory.symlink_to(real_directory, target_is_directory=True)

    with pytest.raises(RuntimeError, match="secret 目录"):
        start_dual.load_swanlab_api_key({}, linked_directory / "swanlab_api_key")


def test_lock_symlink_is_rejected(tmp_path):
    secret_directory = tmp_path / ".secrets"
    secret_directory.mkdir(mode=0o700)
    lock_target = tmp_path / "lock-target"
    _write_private(lock_target, "not-a-lock")
    (secret_directory / ".lock").symlink_to(lock_target)

    with pytest.raises(RuntimeError, match="锁文件无法安全打开"):
        start_dual.load_swanlab_api_key(
            {}, secret_directory / "swanlab_api_key"
        )


def test_secret_directory_requires_exact_mode(tmp_path):
    secret_directory = tmp_path / ".secrets"
    secret_directory.mkdir(mode=0o755)

    with pytest.raises(RuntimeError, match="0700"):
        start_dual.load_swanlab_api_key(
            {}, secret_directory / "swanlab_api_key"
        )


def test_lock_and_secret_files_require_exact_mode(tmp_path):
    secret_directory = tmp_path / ".secrets"
    secret_directory.mkdir(mode=0o700)
    lock_path = secret_directory / ".lock"
    _write_private(lock_path, "lock")
    lock_path.chmod(0o640)

    with pytest.raises(RuntimeError, match="0600"):
        start_dual.load_swanlab_api_key(
            {}, secret_directory / "swanlab_api_key"
        )

    lock_path.chmod(0o600)
    api_path = secret_directory / "swanlab_api_key"
    _write_private(api_path, "local-api-key")
    api_path.chmod(0o400)

    with pytest.raises(RuntimeError, match="0600"):
        start_dual.load_swanlab_api_key({}, api_path)


def test_secret_storage_requires_current_euid_owner(tmp_path, monkeypatch):
    api_path = tmp_path / ".secrets" / "swanlab_api_key"
    _write_private(api_path, "local-api-key")
    actual_euid = start_dual.os.geteuid()
    monkeypatch.setattr(start_dual.os, "geteuid", lambda: actual_euid + 1)

    with pytest.raises(RuntimeError, match="owner"):
        start_dual.load_swanlab_api_key({}, api_path)


def test_owner_validator_rejects_non_euid_file_owner():
    metadata = SimpleNamespace(
        st_uid=start_dual.os.geteuid() + 1,
        st_mode=stat.S_IFREG | 0o600,
    )

    with pytest.raises(RuntimeError, match="owner"):
        start_dual._validate_owned_mode(
            metadata, "test secret file", "file", 0o600
        )


def test_missing_api_key_fails_closed(tmp_path):
    with pytest.raises(RuntimeError, match="未配置"):
        start_dual.load_swanlab_api_key({}, tmp_path / "missing")


def test_short_environment_session_secret_is_rejected(tmp_path):
    with pytest.raises(RuntimeError, match="至少需要 32"):
        start_dual.load_or_create_swanlab_session_secret(
            {"SWANLAB_SESSION_SECRET": "too-short"},
            tmp_path / "unused",
        )
