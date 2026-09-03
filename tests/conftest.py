"""Shared fixtures: every test gets an isolated AIHUB_HOME and database."""

from __future__ import annotations

import os
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))


@pytest.fixture()
def home(tmp_path: pathlib.Path) -> pathlib.Path:
    root = tmp_path / "aihub-home"
    root.mkdir()
    return root


@pytest.fixture()
def config(home: pathlib.Path):
    from aihub.config import Config

    cfg = Config(
        host="127.0.0.1",
        port=0,
        token="test-token",
        auth_enabled=True,
        home=home,
        config_path=home / "server.json",
    )
    cfg.classify.enabled = False
    cfg.ensure_dirs()
    return cfg


@pytest.fixture()
def client(config):
    from fastapi.testclient import TestClient

    from aihub.app import create_app

    app = create_app(config)
    with TestClient(app) as c:
        c.headers.update({"X-AIHub-Token": "test-token"})
        yield c


@pytest.fixture()
def repo(config):
    """Storage layer without the HTTP stack."""
    from aihub.storage.blobs import BlobStore
    from aihub.storage.db import Database
    from aihub.storage.migrate import migrate
    from aihub.storage.repo import Repo

    db = Database(config.db_path)
    migrate(db.writer)
    blobs = BlobStore(config.blobs_dir)
    yield Repo(db, blobs)
    db.close()
