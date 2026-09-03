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


def test_auth_enabled_without_token_generates_one(tmp_path, monkeypatch):
    monkeypatch.delenv("AIHUB_TOKEN", raising=False)
    path = tmp_path / "server.json"
    path.write_text(json.dumps({"auth_enabled": True, "token": ""}))
    cfg = load_config(path, create_if_missing=False)
    assert cfg.token, "auth must never end up enabled with an empty token"
