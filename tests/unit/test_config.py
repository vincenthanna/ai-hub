from __future__ import annotations

import json
import os
import stat

from aihub.config import load_config, write_config


def test_creates_config_with_token(tmp_path, monkeypatch):
    monkeypatch.delenv("AIHUB_TOKEN", raising=False)
    path = tmp_path / "server.json"
    cfg = load_config(path)
    assert path.is_file()
    assert len(cfg.token) >= 32
    assert cfg.port == 16001
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, "config file must not be world readable"


def test_env_overrides_file(tmp_path, monkeypatch):
    path = tmp_path / "server.json"
    cfg = load_config(path)
    write_config(cfg)
    monkeypatch.setenv("AIHUB_PORT", "17777")
    monkeypatch.setenv("AIHUB_TOKEN", "env-token")
    monkeypatch.setenv("AIHUB_AUTH_DISABLED", "1")
    reread = load_config(path, create_if_missing=False)
    assert reread.port == 17777
    assert reread.token == "env-token"
    assert reread.auth_enabled is False


def test_missing_token_without_create_is_an_error(tmp_path, monkeypatch):
    """A throwaway token per caller would look fine and 401 everything."""
    import pytest

    monkeypatch.delenv("AIHUB_TOKEN", raising=False)
    path = tmp_path / "server.json"
    path.write_text(json.dumps({"auth_enabled": True, "token": ""}))
    with pytest.raises(RuntimeError):
        load_config(path, create_if_missing=False)


def test_token_is_stable_across_reads(tmp_path, monkeypatch):
    monkeypatch.delenv("AIHUB_TOKEN", raising=False)
    path = tmp_path / "server.json"
    first = load_config(path).token
    second = load_config(path, create_if_missing=False).token
    assert first == second and first


def test_auth_disabled_is_never_persisted(tmp_path, monkeypatch):
    """AIHUB_AUTH_DISABLED must not latch a public port open."""
    monkeypatch.delenv("AIHUB_TOKEN", raising=False)
    monkeypatch.setenv("AIHUB_AUTH_DISABLED", "1")
    path = tmp_path / "server.json"
    cfg = load_config(path)
    assert cfg.auth_enabled is False
    assert json.loads(path.read_text())["auth_enabled"] is True
    monkeypatch.delenv("AIHUB_AUTH_DISABLED")
    assert load_config(path, create_if_missing=False).auth_enabled is True


def test_refuses_public_bind_without_auth(tmp_path):
    import pytest

    from aihub.config import Config, assert_safe_to_serve

    cfg = Config(host="0.0.0.0", auth_enabled=False, token="")
    with pytest.raises(RuntimeError):
        assert_safe_to_serve(cfg)
    assert_safe_to_serve(Config(host="127.0.0.1", auth_enabled=False, token=""))
