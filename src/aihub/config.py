"""Configuration loading for the ai-hub server.

Precedence is environment variables first, then the JSON config file, then
built-in defaults. TOML is deliberately not used because ``tomllib`` only
exists on Python 3.11+ and the deployment host (ds30) runs Python 3.10.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict

DEFAULT_PORT = 16001
DEFAULT_HOST = "0.0.0.0"
DEFAULT_MAX_BODY_BYTES = 1 * 1024 * 1024
DEFAULT_MAX_FILE_BYTES = 25 * 1024 * 1024
DEFAULT_MAX_REQUEST_BYTES = 64 * 1024 * 1024
MIN_FREE_BYTES = 500 * 1024 * 1024


def _default_config_path() -> Path:
    override = os.environ.get("AIHUB_CONFIG")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base).expanduser() / "ai-hub" / "server.json"


def _default_home() -> Path:
    override = os.environ.get("AIHUB_HOME")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base).expanduser() / "ai-hub"


@dataclass
class ClassifyConfig:
    enabled: bool = True
    concurrency: int = 1
    timeout_sec: int = 90
    max_attempts: int = 3
    claude_bin: str = "claude"
    model: str = "claude-haiku-4-5-20251001"
    batch_size: int = 4
    batch_wait_sec: float = 5.0
    max_topics_in_prompt: int = 40
    max_calls_per_day: int = 200

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "ClassifyConfig":
        base = cls()
        for key in (
            "enabled",
            "concurrency",
            "timeout_sec",
            "max_attempts",
            "claude_bin",
            "model",
            "batch_size",
            "batch_wait_sec",
            "max_topics_in_prompt",
            "max_calls_per_day",
        ):
            if key in raw and raw[key] is not None:
                setattr(base, key, raw[key])
        return base


@dataclass
class Config:
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    token: str = ""
    # Accepted alongside `token` until it expires, so a rotation does not have to
    # reach every client in the same instant.
    token_previous: str = ""
    token_previous_until_ms: int = 0
    auth_enabled: bool = True
    home: Path = field(default_factory=_default_home)
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    max_file_bytes: int = DEFAULT_MAX_FILE_BYTES
    max_request_bytes: int = DEFAULT_MAX_REQUEST_BYTES
    log_level: str = "INFO"
    classify: ClassifyConfig = field(default_factory=ClassifyConfig)
    config_path: Path = field(default_factory=_default_config_path)
    retention_days: int = 0  # 0 disables age-based pruning

    # Derived paths -------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.home / "db" / "aihub.sqlite3"

    @property
    def blobs_dir(self) -> Path:
        return self.home / "blobs"

    @property
    def blobs_tmp_dir(self) -> Path:
        return self.home / "blobs" / "tmp"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def pid_file(self) -> Path:
        return self.home / "server.pid"

    def ensure_dirs(self) -> None:
        for path in (
            self.home,
            self.db_path.parent,
            self.blobs_dir,
            self.blobs_tmp_dir,
            self.logs_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_config(path: Path | None = None, *, create_if_missing: bool = True) -> Config:
    """Build a Config from the config file and environment overrides.

    When the config file is absent and ``create_if_missing`` is true, a fresh
    file is written with a newly generated token and mode 0600.
    """
    cfg_path = Path(path).expanduser() if path else _default_config_path()
    raw: Dict[str, Any] = {}
    if cfg_path.is_file():
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))

    cfg = Config(config_path=cfg_path)
    if "host" in raw:
        cfg.host = str(raw["host"])
    if "port" in raw:
        cfg.port = int(raw["port"])
    if "token" in raw:
        cfg.token = str(raw["token"])
    if "token_previous" in raw:
        cfg.token_previous = str(raw["token_previous"] or "")
    if "token_previous_until_ms" in raw:
        cfg.token_previous_until_ms = int(raw["token_previous_until_ms"] or 0)
    if "auth_enabled" in raw:
        cfg.auth_enabled = bool(raw["auth_enabled"])
    if raw.get("home"):
        cfg.home = Path(str(raw["home"])).expanduser()
    for key in ("max_body_bytes", "max_file_bytes", "max_request_bytes", "retention_days"):
        if key in raw and raw[key] is not None:
            setattr(cfg, key, int(raw[key]))
    if "log_level" in raw:
        cfg.log_level = str(raw["log_level"]).upper()
    if isinstance(raw.get("classify"), dict):
        cfg.classify = ClassifyConfig.from_dict(raw["classify"])

    # Environment overrides take precedence over the file.
    if os.environ.get("AIHUB_HOST"):
        cfg.host = os.environ["AIHUB_HOST"]
    if os.environ.get("AIHUB_PORT"):
        cfg.port = int(os.environ["AIHUB_PORT"])
    if os.environ.get("AIHUB_TOKEN"):
        cfg.token = os.environ["AIHUB_TOKEN"]
    if os.environ.get("AIHUB_HOME"):
        cfg.home = Path(os.environ["AIHUB_HOME"]).expanduser()
    if os.environ.get("AIHUB_LOG_LEVEL"):
        cfg.log_level = os.environ["AIHUB_LOG_LEVEL"].upper()
    if os.environ.get("AIHUB_AUTH_DISABLED"):
        cfg.auth_enabled = not _as_bool(os.environ["AIHUB_AUTH_DISABLED"])
    if os.environ.get("AIHUB_CLAUDE_BIN"):
        cfg.classify.claude_bin = os.environ["AIHUB_CLAUDE_BIN"]
    if os.environ.get("AIHUB_CLASSIFY_DISABLED"):
        cfg.classify.enabled = not _as_bool(os.environ["AIHUB_CLASSIFY_DISABLED"])
    if os.environ.get("AIHUB_CLASSIFY_MODEL"):
        cfg.classify.model = os.environ["AIHUB_CLASSIFY_MODEL"]

    # A token is minted and persisted regardless of auth_enabled: the file always
    # records auth as enabled, so a run with auth off must not leave it tokenless.
    if not cfg.token:
        if not create_if_missing:
            # Minting a throwaway token here would hand every caller a different
            # secret and turn a missing config into a silent wall of 401s.
            raise RuntimeError(
                "no auth token in %s. Run scripts/install.sh (or "
                "python -m aihub.admin init) to create one." % cfg_path
            )
        cfg.token = secrets.token_urlsafe(32)
        write_config(cfg)

    if create_if_missing and not cfg_path.is_file():
        write_config(cfg)

    return cfg


def accepted_tokens(cfg: "Config") -> list:
    """Tokens that authenticate right now, newest first."""
    import time as _time

    out = [cfg.token]
    if cfg.token_previous and _time.time() * 1000 < cfg.token_previous_until_ms:
        out.append(cfg.token_previous)
    return [t for t in out if t]


def assert_safe_to_serve(cfg: "Config") -> None:
    """Refuse to listen on a non-loopback address without authentication.

    AIHUB_AUTH_DISABLED is a volatile debugging switch; it is never written to
    the config file, so it cannot silently latch a public port open.
    """
    if cfg.auth_enabled:
        return
    loopback = cfg.host in ("127.0.0.1", "::1", "localhost")
    if not loopback:
        raise RuntimeError(
            "auth is disabled but the server would bind %s. Refusing to start. "
            "Bind 127.0.0.1 for local debugging, or leave auth enabled." % cfg.host
        )


def write_config(cfg: Config) -> None:
    """Persist the config file with mode 0600, creating parents as needed."""
    cfg.config_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "host": cfg.host,
        "port": cfg.port,
        "token": cfg.token,
        "token_previous": cfg.token_previous,
        "token_previous_until_ms": cfg.token_previous_until_ms,
        # Always persist true: disabling auth is a volatile env-only override so
        # a one-off debugging run cannot leave the server permanently open.
        "auth_enabled": True,
        "home": str(cfg.home),
        "max_body_bytes": cfg.max_body_bytes,
        "max_file_bytes": cfg.max_file_bytes,
        "max_request_bytes": cfg.max_request_bytes,
        "retention_days": cfg.retention_days,
        "log_level": cfg.log_level,
        "classify": {
            "enabled": cfg.classify.enabled,
            "concurrency": cfg.classify.concurrency,
            "timeout_sec": cfg.classify.timeout_sec,
            "max_attempts": cfg.classify.max_attempts,
            "claude_bin": cfg.classify.claude_bin,
            "model": cfg.classify.model,
            "batch_size": cfg.classify.batch_size,
            "batch_wait_sec": cfg.classify.batch_wait_sec,
            "max_topics_in_prompt": cfg.classify.max_topics_in_prompt,
            "max_calls_per_day": cfg.classify.max_calls_per_day,
        },
    }
    tmp = cfg.config_path.with_suffix(".json.tmp")
    # Create with 0600 rather than chmod after writing: otherwise the token
    # exists world-readable for the moment between the two calls.
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, cfg.config_path)
