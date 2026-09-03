"""Console entry point: ``python -m aihub`` or ``aihub-server``."""

from __future__ import annotations

import argparse
import os
import sys

from .config import load_config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aihub", description="ai-hub server")
    parser.add_argument("--host", default=None, help="bind address override")
    parser.add_argument("--port", type=int, default=None, help="port override")
    parser.add_argument("--config", default=None, help="path to server.json")
    parser.add_argument(
        "--print-token", action="store_true", help="print the auth token and exit"
    )
    parser.add_argument(
        "--reload", action="store_true", help="enable uvicorn autoreload (development)"
    )
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    if args.host:
        cfg.host = args.host
    if args.port:
        cfg.port = args.port

    if args.print_token:
        sys.stdout.write(cfg.token + "\n")
        return 0

    cfg.ensure_dirs()

    # Written by the server itself. `$!` in the start script captures the uv
    # wrapper's pid; SIGKILLing that leaves the real server holding the port.
    try:
        cfg.pid_file.write_text("%d\n" % os.getpid(), encoding="utf-8")
    except OSError:
        pass

    import uvicorn

    from .app import create_app

    try:
        uvicorn.run(
            create_app(cfg),
            host=cfg.host,
            port=cfg.port,
            workers=1,
            access_log=False,
            timeout_graceful_shutdown=20,
            # Leave the JSON-line handlers alone; uvicorn's dictConfig would
            # disable existing loggers and replace the formatter.
            log_config=None,
        )
    finally:
        try:
            if cfg.pid_file.is_file() and cfg.pid_file.read_text().strip() == str(os.getpid()):
                cfg.pid_file.unlink()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
