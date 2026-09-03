#!/usr/bin/env python3
"""ai-hub client. Standard library only, so it runs wherever python3 does.

Exit codes carry meaning so the calling skill can react without parsing text:
0 success, 1 configuration or usage error, 2 server unreachable or 5xx,
3 authentication rejected, 4 the requested item does not exist.
"""

from __future__ import annotations

import argparse
import errno
import json
import mimetypes
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

VERSION = "0.2.0"
DEFAULT_URL = "http://192.168.49.48:16001"
USER_CONFIG = Path(
    os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
) / "ai-hub" / "client.json"
PROJECT_CONFIG_NAME = ".ai-hub.json"

EXIT_OK = 0
EXIT_CONFIG = 1
EXIT_SERVER = 2
EXIT_AUTH = 3
EXIT_NOTFOUND = 4

RETRY_STATUSES = (429, 500, 502, 503, 504)


#: Set when this process is already a fallback re-exec, to stop a loop.
REEXEC_GUARD = "AIHUB_NO_REEXEC"
FALLBACK_INTERPRETERS = ("/usr/bin/python3",)


class HubError(Exception):
    def __init__(self, message: str, code: int = EXIT_SERVER) -> None:
        super().__init__(message)
        self.code = code


class LocalNetworkBlocked(HubError):
    """macOS refused local-network access to this interpreter."""


# --------------------------------------------------------------------- config
def _read_json(path: Path) -> Dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _find_project_config(start: Optional[Path] = None) -> Tuple[Dict[str, Any], Optional[Path]]:
    """Walk up from cwd looking for .ai-hub.json."""
    here = (start or Path.cwd()).resolve()
    for directory in [here] + list(here.parents):
        candidate = directory / PROJECT_CONFIG_NAME
        if candidate.is_file():
            return _read_json(candidate), candidate
        if (directory / ".git").exists():
            break
    return {}, None


def _git_repo_name() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=3,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).name
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def _sanitize_label(value: str) -> str:
    out = "".join(ch if (ch.isalnum() or ch in "._-") else "-" for ch in value.lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-.") [:64]


def derive_label() -> str:
    base = _git_repo_name() or Path.cwd().name or "session"
    host = socket.gethostname().split(".")[0]
    return _sanitize_label("%s-%s" % (base, host)) or "session"


class Config:
    def __init__(self, args: argparse.Namespace) -> None:
        user = _read_json(USER_CONFIG)
        project, project_path = _find_project_config()
        self.project_path = project_path
        self.warnings: List[str] = []
        if "token" in project:
            self.warnings.append(
                "%s contains a 'token' key; it is ignored because project files may be committed"
                % project_path
            )

        self.url = (
            getattr(args, "server", None)
            or os.environ.get("AIHUB_URL")
            or project.get("server")
            or user.get("server")
            or DEFAULT_URL
        ).rstrip("/")
        self.token = (
            getattr(args, "token", None)
            or os.environ.get("AIHUB_TOKEN")
            or user.get("token")
            or ""
        )
        self.label = _sanitize_label(
            getattr(args, "label", None)
            or os.environ.get("AIHUB_LABEL")
            or project.get("label")
            or user.get("label")
            or derive_label()
        )
        self.default_topic = project.get("defaultTopic") or user.get("defaultTopic") or None
        self.auto_inbox = bool(user.get("autoInbox", False))
        self.timeout = float(os.environ.get("AIHUB_TIMEOUT", "15"))


# --------------------------------------------------------------------- HTTP
def _request(
    cfg: Config,
    method: str,
    path: str,
    *,
    params: Optional[Dict[str, Any]] = None,
    body: Optional[bytes] = None,
    content_type: Optional[str] = None,
    raw: bool = False,
    retries: int = 3,
) -> Any:
    url = cfg.url + path
    if params:
        clean = {k: v for k, v in params.items() if v is not None and v != ""}
        if clean:
            url += "?" + urllib.parse.urlencode(clean, doseq=True)

    last_error: Optional[str] = None
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, method=method)
        if cfg.token:
            req.add_header("X-AIHub-Token", cfg.token)
        if content_type:
            req.add_header("Content-Type", content_type)
        req.add_header("User-Agent", "ai-hub-client/%s" % VERSION)
        try:
            with urllib.request.urlopen(req, timeout=cfg.timeout) as resp:
                payload = resp.read()
                if raw:
                    return payload, dict(resp.headers)
                return json.loads(payload.decode("utf-8")) if payload else {}
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = json.loads(exc.read().decode("utf-8"))["error"]["message"]
            except Exception:
                pass
            if exc.code in (401, 403):
                raise HubError(
                    "authentication rejected by %s%s"
                    % (cfg.url, (": " + detail) if detail else ""),
                    EXIT_AUTH,
                )
            if exc.code == 404:
                raise HubError(detail or "not found", EXIT_NOTFOUND)
            if exc.code in RETRY_STATUSES and attempt < retries - 1:
                last_error = "HTTP %d %s" % (exc.code, detail)
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise HubError(
                "server returned HTTP %d%s" % (exc.code, (": " + detail) if detail else ""),
                EXIT_SERVER if exc.code >= 500 else EXIT_CONFIG,
            )
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            last_error = str(getattr(exc, "reason", exc))
            if _looks_like_local_network_block(exc):
                raise LocalNetworkBlocked(_local_network_hint(cfg, last_error), EXIT_SERVER)
            if attempt < retries - 1:
                time.sleep(0.5 * (2 ** attempt))
                continue
            raise HubError("cannot reach %s (%s)" % (cfg.url, last_error), EXIT_SERVER)
    raise HubError("cannot reach %s (%s)" % (cfg.url, last_error), EXIT_SERVER)


def _looks_like_local_network_block(exc: BaseException) -> bool:
    """Recognise macOS denying local-network access to this interpreter.

    macOS grants that permission per binary. A Python installed by pyenv or
    Homebrew is a different binary from /usr/bin/python3, so it gets its own
    (initially absent) grant and every LAN connection fails with EHOSTUNREACH
    even though curl on the same machine succeeds.
    """
    if sys.platform != "darwin":
        return False
    err = getattr(exc, "reason", exc)
    return getattr(err, "errno", None) in (errno.EHOSTUNREACH, errno.ENETUNREACH)


def _local_network_hint(cfg: "Config", detail: str) -> str:
    return (
        "cannot reach %s (%s)\n"
        "\n"
        "On macOS this usually means this Python is not allowed to use the local\n"
        "network. The permission is granted per binary, so %s\n"
        "may be blocked while /usr/bin/python3 and curl work.\n"
        "\n"
        "Check with:\n"
        "  curl -sS %s/health\n"
        "If curl succeeds, either allow this interpreter under\n"
        "System Settings > Privacy & Security > Local Network, or run the client\n"
        "with the system interpreter:\n"
        "  /usr/bin/python3 %s ping"
        % (cfg.url, detail, sys.executable, cfg.url, __file__)
    )


def _fallback_interpreter() -> Optional[str]:
    """An interpreter that may already hold the local-network grant."""
    if os.environ.get(REEXEC_GUARD):
        return None
    here = os.path.realpath(sys.executable)
    for candidate in FALLBACK_INTERPRETERS:
        if os.path.exists(candidate) and os.path.realpath(candidate) != here:
            return candidate
    return None


def _multipart(payload: Dict[str, Any], files: List[Path]) -> Tuple[bytes, str]:
    boundary = "----aihub%s" % uuid.uuid4().hex
    out = bytearray()

    def part(headers: str, data: bytes) -> None:
        out.extend(("--%s\r\n%s\r\n\r\n" % (boundary, headers)).encode("utf-8"))
        out.extend(data)
        out.extend(b"\r\n")

    part(
        'Content-Disposition: form-data; name="payload"\r\nContent-Type: application/json',
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    )
    for path in files:
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        part(
            'Content-Disposition: form-data; name="files"; filename="%s"\r\nContent-Type: %s'
            % (path.name.replace('"', ""), ctype),
            path.read_bytes(),
        )
    out.extend(("--%s--\r\n" % boundary).encode("utf-8"))
    return bytes(out), "multipart/form-data; boundary=%s" % boundary


# --------------------------------------------------------------------- output
def out(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def emit_json(value: Any) -> None:
    out(json.dumps(value, ensure_ascii=False, indent=2))


def rel_time(created_ms: int) -> str:
    delta = max(0, int(time.time() * 1000) - int(created_ms)) // 1000
    if delta < 60:
        return "%ds ago" % delta
    if delta < 3600:
        return "%dm ago" % (delta // 60)
    if delta < 86400:
        return "%dh ago" % (delta // 3600)
    return "%dd ago" % (delta // 86400)


def format_row(item: Dict[str, Any]) -> str:
    marker = {"direct": "→you", "broadcast": "bcast"}.get(item.get("delivery_kind", ""), "")
    return "%s  %-6s %-16s %-14s %-8s %s  (%s)" % (
        item["item_id"],
        marker,
        item.get("sender", "")[:16],
        (item.get("topic") or "-")[:14],
        item.get("kind", "")[:8],
        (item.get("title") or item.get("summary") or "")[:70],
        rel_time(item.get("created_ms", 0)),
    )


def print_items(items: List[Dict[str, Any]], *, snippets: bool = False) -> None:
    if not items:
        out("(none)")
        return
    for item in items:
        out(format_row(item))
        if snippets and item.get("snippet"):
            out("      %s" % item["snippet"].replace("\n", " ")[:150])


# --------------------------------------------------------------------- commands
def cmd_init(cfg: Config, args: argparse.Namespace) -> int:
    USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_json(USER_CONFIG)
    existing.update(
        {
            "server": (args.url or cfg.url).rstrip("/"),
            "token": args.token if args.token is not None else existing.get("token", ""),
            "label": _sanitize_label(args.label or cfg.label),
            "autoInbox": bool(args.auto_inbox) if args.auto_inbox is not None
            else existing.get("autoInbox", False),
        }
    )
    tmp = USER_CONFIG.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, USER_CONFIG)
    out("wrote %s (mode 0600)" % USER_CONFIG)
    out("server=%s label=%s token=%s"
        % (existing["server"], existing["label"], "set" if existing["token"] else "MISSING"))
    if not existing["token"]:
        out("")
        out("No token stored. Get it from the server host with:")
        out("  ssh <host> 'cd <repo> && uv run python -m aihub.admin token'")
        out("then rerun: hub.py init --token <token>")
        return EXIT_CONFIG
    return EXIT_OK


def cmd_ping(cfg: Config, args: argparse.Namespace) -> int:
    started = time.perf_counter()
    data = _request(cfg, "GET", "/health", retries=1)
    latency = (time.perf_counter() - started) * 1000
    # /health is deliberately unauthenticated, so reaching it proves only that
    # the server is up. Touch one authenticated endpoint as well, otherwise a
    # wrong token reports "ok" and the real failure surfaces much later.
    _request(cfg, "GET", "/v1/agents", retries=1)
    if args.json:
        emit_json({"ok": True, "server": cfg.url, "latency_ms": round(latency, 1),
                   "auth": "ok", "health": data})
    else:
        out("ok %s %.0fms  auth=ok status=%s items=%s classifier=%s"
            % (cfg.url, latency, data.get("status"),
               (data.get("db") or {}).get("items"),
               (data.get("classifier") or {}).get("engine")))
    return EXIT_OK


def cmd_whoami(cfg: Config, args: argparse.Namespace) -> int:
    info = {
        "label": cfg.label,
        "server": cfg.url,
        "token": "set" if cfg.token else "MISSING",
        "user_config": str(USER_CONFIG) if USER_CONFIG.is_file() else None,
        "project_config": str(cfg.project_path) if cfg.project_path else None,
        "default_topic": cfg.default_topic,
    }
    if args.json:
        emit_json(info)
    else:
        out("label   = %s" % info["label"])
        out("server  = %s" % info["server"])
        out("token   = %s" % info["token"])
        out("config  = %s" % (info["user_config"] or "(none)"))
        if info["project_config"]:
            out("project = %s" % info["project_config"])
        out("")
        out("Override the label for this session with AIHUB_LABEL=<name> or --label <name>.")
    for warning in cfg.warnings:
        sys.stderr.write("warning: %s\n" % warning)
    return EXIT_OK if cfg.token else EXIT_CONFIG


def cmd_send(cfg: Config, args: argparse.Namespace) -> int:
    if args.body_file:
        body_path = Path(args.body_file).expanduser()
        if not body_path.is_file():
            raise HubError("body file not found: %s" % body_path, EXIT_CONFIG)
        body = body_path.read_text(encoding="utf-8")
    elif args.body is not None:
        body = args.body
    elif not sys.stdin.isatty():
        body = sys.stdin.read()
    else:
        raise HubError("provide --body-file, --body, or pipe the body on stdin", EXIT_CONFIG)

    payload: Dict[str, Any] = {
        "from": cfg.label,
        "title": args.title,
        "body": body,
        "kind": args.kind,
        "priority": args.priority,
        "client_msg_id": args.client_msg_id or str(uuid.uuid4()),
    }
    if args.to:
        payload["to"] = [t.strip() for t in args.to.split(",") if t.strip()]
    topic = args.topic or cfg.default_topic
    if topic:
        payload["topic"] = topic
    if args.tags:
        payload["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]
    repo = _git_repo_name()
    if repo:
        payload["repo"] = repo
    payload["host"] = socket.gethostname().split(".")[0]

    files = [Path(p).expanduser() for p in (args.attach or [])]
    for path in files:
        if not path.is_file():
            raise HubError("attachment not found: %s" % path, EXIT_CONFIG)

    if files:
        data, ctype = _multipart(payload, files)
        result = _request(cfg, "POST", "/v1/items", body=data, content_type=ctype)
    else:
        result = _request(
            cfg,
            "POST",
            "/v1/items",
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            content_type="application/json",
        )
    if args.json:
        emit_json(result)
    else:
        out("sent id=%s from=%s to=%s topic=%s%s"
            % (result["item_id"], result["from"],
               ",".join(result.get("to") or []) or "broadcast",
               result.get("topic") or "pending",
               "  (duplicate, not resent)" if result.get("deduplicated") else ""))
        for att in result.get("attachments") or []:
            out("  attached %s (%d bytes)" % (att["filename"], att["size_bytes"]))
    return EXIT_OK


def cmd_inbox(cfg: Config, args: argparse.Namespace) -> int:
    if getattr(args, "unread_banner", False):
        # Session-start path: a short timeout and no retries, so an unreachable
        # hub costs the session a moment rather than the full retry budget.
        cfg.timeout = min(cfg.timeout, 3.0)
    data = _request(
        cfg, "GET", "/v1/inbox",
        retries=1 if getattr(args, "unread_banner", False) else 3,
        params={
            "as": cfg.label,
            "limit": args.limit,
            "include_broadcast": "false" if args.direct_only else "true",
            "kind": args.kind,
            "wait_sec": args.wait,
        },
    )
    if args.unread_banner:
        # Quiet unless the user opted in and something is actually waiting.
        if not cfg.auto_inbox or not data["items"]:
            return EXIT_OK
        out("ai-hub: %d unread message(s) for '%s'. Run /hub:inbox to read them."
            % (len(data["items"]), cfg.label))
        for item in data["items"][:5]:
            out("  " + format_row(item))
        return EXIT_OK
    if args.json:
        emit_json(data)
        return EXIT_OK
    out("inbox for '%s' at %s" % (cfg.label, cfg.url))
    print_items(data["items"])
    out("")
    out("pending direct: %d, unseen broadcast: %d"
        % (data["pending_direct_count"], data["unseen_broadcast_count"]))
    if data["items"]:
        out("Read one with: hub.py read <id>   Then acknowledge: hub.py ack <id>")
    return EXIT_OK


def cmd_read(cfg: Config, args: argparse.Namespace) -> int:
    data = _request(cfg, "GET", "/v1/items/%s" % args.item_id)
    if args.json:
        emit_json(data)
        return EXIT_OK
    if args.out:
        Path(args.out).expanduser().write_text(data["body"], encoding="utf-8")
    out("id      : %s" % data["item_id"])
    out("from    : %s -> %s" % (data["sender"], ",".join(data["to"]) or "broadcast"))
    out("topic   : %s   kind: %s   tags: %s   classification: %s"
        % (data.get("topic") or "-", data["kind"],
           ",".join(data["tags"]) or "-", data["classification"]))
    out("created : %s (%s)" % (data["created_at"], rel_time(data["created_ms"])))
    out("title   : %s" % data["title"])
    if data.get("repo") or data.get("host"):
        out("origin  : repo=%s host=%s ref=%s"
            % (data.get("repo") or "-", data.get("host") or "-", data.get("ref") or "-"))
    for att in data.get("attachments") or []:
        out("attach  : %s  %s  %d bytes  (hub.py fetch %s %s)"
            % (att["attachment_id"], att["filename"], att["size_bytes"],
               data["item_id"], att["attachment_id"]))
    out("-" * 60)
    if args.out:
        out("body written to %s (%d bytes)" % (args.out, len(data["body"].encode("utf-8"))))
    else:
        out(data["body"])
    if args.ack:
        _request(
            cfg, "POST", "/v1/inbox/ack",
            body=json.dumps({"as": cfg.label, "item_ids": [args.item_id]}).encode("utf-8"),
            content_type="application/json",
        )
        out("-" * 60)
        out("acknowledged")
    return EXIT_OK


def cmd_ack(cfg: Config, args: argparse.Namespace) -> int:
    payload: Dict[str, Any] = {"as": cfg.label, "note": args.note or ""}
    if args.all:
        payload["all"] = True
    if args.item_ids:
        payload["item_ids"] = args.item_ids
    if not args.all and not args.item_ids:
        raise HubError("pass item ids or --all", EXIT_CONFIG)
    data = _request(
        cfg, "POST", "/v1/inbox/ack",
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        content_type="application/json",
    )
    if args.json:
        emit_json(data)
    else:
        out("acked=%d already=%d not_found=%s pending_now=%d"
            % (data["acked"], data["already_acked"],
               ",".join(data["not_found"]) or "-", data["pending_direct_count"]))
    return EXIT_OK


def _since_to_iso(value: Optional[str]) -> Optional[str]:
    """Accept 7d / 12h / 30m as well as an RFC3339 timestamp."""
    if not value:
        return None
    units = {"d": 86400, "h": 3600, "m": 60}
    if len(value) > 1 and value[-1] in units and value[:-1].isdigit():
        seconds = int(value[:-1]) * units[value[-1]]
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds))
    return value


def cmd_search(cfg: Config, args: argparse.Namespace) -> int:
    data = _request(
        cfg, "GET", "/v1/search",
        params={
            "q": " ".join(args.query),
            "limit": args.limit,
            "topic": args.topic,
            "tags": args.tags,
            "kind": args.kind,
            "from": args.sender,
            "since": _since_to_iso(args.since),
        },
    )
    if args.json:
        emit_json(data)
        return EXIT_OK
    out("%d result(s) in %sms" % (len(data["items"]), data["took_ms"]))
    print_items(data["items"], snippets=True)
    if not data["items"]:
        out("Nothing matched. Try fewer words, or 'hub.py topics' to see what exists.")
    return EXIT_OK


def cmd_list(cfg: Config, args: argparse.Namespace) -> int:
    data = _request(
        cfg, "GET", "/v1/items",
        params={
            "limit": args.limit,
            "topic": args.topic,
            "kind": args.kind,
            "from": args.sender,
            "since": _since_to_iso(args.since),
        },
    )
    if args.json:
        emit_json(data)
        return EXIT_OK
    print_items(data["items"])
    return EXIT_OK


def cmd_topics(cfg: Config, args: argparse.Namespace) -> int:
    data = _request(cfg, "GET", "/v1/topics")
    if args.json:
        emit_json(data)
        return EXIT_OK
    for topic in data["topics"]:
        out("%-20s %5d  %s" % (topic["topic"], topic["count"], topic["last_activity"] or "-"))
    out("")
    out("total items: %d" % data["total_items"])
    return EXIT_OK


def cmd_agents(cfg: Config, args: argparse.Namespace) -> int:
    data = _request(cfg, "GET", "/v1/agents")
    if args.json:
        emit_json(data)
        return EXIT_OK
    for agent in data["agents"]:
        out("%-24s sent=%-4d pending=%-3d last_seen=%s"
            % (agent["label"], agent["sent"], agent["pending_inbox"], agent["last_seen"]))
    return EXIT_OK


def cmd_fetch(cfg: Config, args: argparse.Namespace) -> int:
    payload, headers = _request(
        cfg, "GET",
        "/v1/items/%s/attachments/%s" % (args.item_id, args.attachment_id),
        raw=True,
    )
    target = Path(args.out).expanduser() if args.out else Path.cwd() / (
        headers.get("Content-Disposition", "").split("filename=")[-1].strip('"; ')
        or args.attachment_id
    )
    target.write_bytes(payload)
    out("saved %d bytes to %s" % (len(payload), target))
    return EXIT_OK



# ------------------------------------------------------------------- setup
MARKETPLACE_SOURCE = "vincenthanna/ai-hub"
MARKETPLACE_NAME = "ai-hub"
PLUGIN_REF = "hub@ai-hub"


def _run(cmd: List[str], *, timeout: int = 180, check: bool = False) -> Tuple[int, str]:
    """Run a command, returning (exit code, combined output)."""
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        return 127, "%s: not found" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "timed out after %ds" % timeout
    out = (proc.stdout or "") + (proc.stderr or "")
    if check and proc.returncode != 0:
        raise HubError("%s failed: %s" % (" ".join(cmd[:3]), out.strip()[:300]), EXIT_CONFIG)
    return proc.returncode, out


def _run_stdin(cmd: List[str], stdin_text: str, *, timeout: int = 240) -> Tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd, input=stdin_text, capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        return 127, "%s: not found" % cmd[0]
    except subprocess.TimeoutExpired:
        return 124, "timed out after %ds" % timeout
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def _ssh(host: str, script: str, *, timeout: int = 240) -> Tuple[int, str]:
    """Run a shell snippet on a remote host, feeding it over stdin.

    Passing the script on stdin instead of as an argv element keeps quoting out
    of the picture entirely, which matters because the script carries a token.
    """
    return _run_stdin(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, "bash -s"],
        script,
        timeout=timeout,
    )


def _fetch_token_over_ssh(host: str, repo_path: str) -> str:
    """Read the shared token from the server host's checkout."""
    # A quoted "~/..." never expands, so resolve it to $HOME on the remote side.
    path = repo_path
    if path.startswith("~/"):
        path = '"$HOME"/' + path[2:]
    elif path == "~":
        path = '"$HOME"'
    else:
        path = '"%s"' % path
    code, out = _ssh(
        host,
        'set -eu\ncd %s\nbash scripts/show-token.sh' % path,
        timeout=120,
    )
    token = ""
    for line in reversed(out.strip().splitlines()):
        line = line.strip()
        if line and " " not in line and len(line) >= 20:
            token = line
            break
    if code != 0 or not token:
        raise HubError(
            "could not read the token from %s:%s\n%s\n"
            "Pass it directly with --token instead." % (host, repo_path, out.strip()[:300]),
            EXIT_CONFIG,
        )
    return token


def cmd_setup(cfg: Config, args: argparse.Namespace) -> int:
    if args.remote:
        return _setup_remote(cfg, args)
    return _setup_local(cfg, args)


def _setup_local(cfg: Config, args: argparse.Namespace) -> int:
    out("configuring this machine")
    url = (args.url or cfg.url).rstrip("/")
    label = _sanitize_label(args.label or cfg.label)

    token = args.token or ""
    if not token and args.from_server:
        out("  reading the token from %s:%s" % (args.from_server, args.repo_path))
        token = _fetch_token_over_ssh(args.from_server, args.repo_path)
    if not token:
        token = cfg.token
    if not token:
        raise HubError(
            "no token available.\n"
            "Either pass --token <token>, or let this fetch it from the server host:\n"
            "  hub.py setup --from-server <user@host>\n"
            "On the server host the token comes from: bash scripts/show-token.sh",
            EXIT_CONFIG,
        )

    USER_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    existing = _read_json(USER_CONFIG)
    existing.update({"server": url, "token": token, "label": label})
    if args.auto_inbox is not None:
        existing["autoInbox"] = bool(args.auto_inbox)
    existing.setdefault("autoInbox", False)
    tmp = USER_CONFIG.with_suffix(".json.tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(existing, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, USER_CONFIG)
    out("  wrote %s (mode 0600)" % USER_CONFIG)

    cfg.url, cfg.token, cfg.label = url, token, label
    started = time.perf_counter()
    health = _request(cfg, "GET", "/health", retries=1)
    _request(cfg, "GET", "/v1/agents", retries=1)
    out("  verified %s in %.0fms (auth ok, classifier=%s)"
        % (url, (time.perf_counter() - started) * 1000,
           (health.get("classifier") or {}).get("engine", "?")))
    out("")
    out("ready. this session is '%s' on the hub." % label)
    out("Try: hub.py inbox     or just ask, e.g. \"허브에 나한테 온 거 있나 확인해줘\"")
    return EXIT_OK


REMOTE_INSTALL_SCRIPT = r"""set -eu
CB=""
for c in "$HOME/.local/bin/claude" "$HOME/.claude/local/claude" /usr/local/bin/claude /opt/homebrew/bin/claude; do
  [ -x "$c" ] && CB="$c" && break
done
[ -n "$CB" ] || CB="$(command -v claude 2>/dev/null || true)"
if [ -z "$CB" ]; then echo "PREFLIGHT_FAIL: claude CLI not found on this host"; exit 10; fi
command -v python3 >/dev/null 2>&1 || { echo "PREFLIGHT_FAIL: python3 not found"; exit 11; }
echo "claude: $CB"
echo "python3: $(command -v python3) ($(python3 -V 2>&1))"
"$CB" plugin marketplace add __SOURCE__ --sparse .claude-plugin plugins 2>&1 | tail -3 || \
  "$CB" plugin marketplace update __MARKETPLACE__ 2>&1 | tail -2
"$CB" plugin install __PLUGIN__ --yes 2>&1 | tail -3
HUBPY="$(ls -d "$HOME"/.claude/plugins/cache/__MARKETPLACE__/hub/*/scripts/hub.py 2>/dev/null | tail -1)"
[ -n "$HUBPY" ] || { echo "PREFLIGHT_FAIL: installed hub.py not found"; exit 12; }
echo "client: $HUBPY"
LABEL='__LABEL__'
# Fall back to the remote host's own name: deriving it locally from user@host
# turns an IP address into a useless label like "192".
if [ -z "$LABEL" ]; then
  LABEL="$(hostname -s 2>/dev/null || hostname)"
  LABEL="$(printf '%s' "$LABEL" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9._-' '-' | sed 's/-\{2,\}/-/g; s/^-//; s/-$//')"
fi
echo "label: $LABEL"
python3 "$HUBPY" init --url '__URL__' --token '__TOKEN__' --label "$LABEL" 2>&1 | tail -3
python3 "$HUBPY" ping 2>&1 | tail -2
"""


def _setup_remote(cfg: Config, args: argparse.Namespace) -> int:
    host = args.remote
    url = (args.url or cfg.url).rstrip("/")
    token = args.token or cfg.token
    if not token and args.from_server:
        token = _fetch_token_over_ssh(args.from_server, args.repo_path)
    if not token:
        raise HubError(
            "no token to install on %s. Pass --token, or --from-server <user@host>." % host,
            EXIT_CONFIG,
        )
    # Left empty when unset: the remote side derives it from its own hostname,
    # since user@ip gives nothing usable.
    label = _sanitize_label(args.label) if args.label else ""

    script = (
        REMOTE_INSTALL_SCRIPT
        .replace("__SOURCE__", MARKETPLACE_SOURCE)
        .replace("__MARKETPLACE__", MARKETPLACE_NAME)
        .replace("__PLUGIN__", PLUGIN_REF)
        .replace("__URL__", url)
        .replace("__LABEL__", label)
    )

    if args.dry_run:
        out("would run on %s:" % host)
        out("")
        for line in script.replace("__TOKEN__", "<token>").splitlines():
            out("  " + line)
        return EXIT_OK

    out("installing ai-hub client on %s" % host)
    out("  hub url : %s" % url)
    out("  label   : %s" % (label or "(derived from the remote hostname)"))
    code, output = _ssh(host, script.replace("__TOKEN__", token), timeout=420)
    for line in output.strip().splitlines():
        if token and token in line:
            line = line.replace(token, "<token>")
        out("  " + line)
    if code != 0 or "PREFLIGHT_FAIL" in output:
        raise HubError("remote setup failed on %s (exit %d)" % (host, code), EXIT_SERVER)
    out("")
    remote_label = label
    for line in output.splitlines():
        if line.startswith("label: "):
            remote_label = line[7:].strip()
    out("%s is set up as '%s'. Messages addressed to that label will reach it."
        % (host, remote_label))
    return EXIT_OK


# --------------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hub.py",
        description="ai-hub client: share messages and work context between AI sessions.",
    )
    # Shared by the top level and every subcommand, so `hub.py list --json` and
    # `hub.py --json list` both work. SUPPRESS keeps an absent flag from
    # overwriting a value that was given at the other level.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--server", default=argparse.SUPPRESS,
                        help="hub base URL (overrides config)")
    common.add_argument("--token", default=argparse.SUPPRESS,
                        help="auth token (overrides config)")
    common.add_argument("--label", default=argparse.SUPPRESS,
                        help="this session's name (overrides config)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="emit raw JSON")
    for action in common._actions:
        parser._add_action(action)
    parser.add_argument("--version", action="version", version="ai-hub client %s" % VERSION)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("send", help="upload a note, message, or handoff", parents=[common])
    p.add_argument("--title", required=True)
    p.add_argument("--body-file", help="path to the markdown body (preferred)")
    p.add_argument("--body", help="inline body text")
    p.add_argument("--to", help="comma-separated recipient labels; omit to broadcast")
    p.add_argument("--topic")
    p.add_argument("--tags", help="comma-separated")
    p.add_argument("--kind", default="note",
                   choices=["note", "message", "handoff", "issue", "decision", "artifact"])
    p.add_argument("--priority", default="normal", choices=["low", "normal", "high"])
    p.add_argument("--attach", action="append", help="file to attach; repeatable")
    p.add_argument("--client-msg-id", help="idempotency key; generated when omitted")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("inbox", help="list messages waiting for this session", parents=[common])
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--kind")
    p.add_argument("--direct-only", action="store_true")
    p.add_argument("--wait", type=float, default=0.0, help="long-poll seconds, max 25")
    p.add_argument("--unread-banner", action="store_true", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_inbox)

    p = sub.add_parser("read", help="print one item in full", parents=[common])
    p.add_argument("item_id")
    p.add_argument("--out", help="write the body to this file instead of stdout")
    p.add_argument("--ack", action="store_true", help="acknowledge after reading")
    p.set_defaults(func=cmd_read)

    p = sub.add_parser("ack", help="mark messages handled", parents=[common])
    p.add_argument("item_ids", nargs="*")
    p.add_argument("--all", action="store_true")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_ack)

    p = sub.add_parser("search", help="keyword search across everything stored", parents=[common])
    p.add_argument("query", nargs="+")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--topic")
    p.add_argument("--tags")
    p.add_argument("--kind")
    p.add_argument("--from", dest="sender")
    p.add_argument("--since", help="7d, 12h, 30m, or an RFC3339 timestamp")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("list", help="recent items, newest first", parents=[common])
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--topic")
    p.add_argument("--kind")
    p.add_argument("--from", dest="sender")
    p.add_argument("--since")
    p.set_defaults(func=cmd_list)

    sub.add_parser("topics", help="topic catalogue with item counts", parents=[common]).set_defaults(func=cmd_topics)
    sub.add_parser("agents", help="labels the hub has seen", parents=[common]).set_defaults(func=cmd_agents)

    p = sub.add_parser("fetch", help="download an attachment", parents=[common])
    p.add_argument("item_id")
    p.add_argument("attachment_id")
    p.add_argument("--out")
    p.set_defaults(func=cmd_fetch)

    sub.add_parser("whoami", help="show the resolved label, server, and token state", parents=[common]).set_defaults(
        func=cmd_whoami
    )
    sub.add_parser("ping", help="check the server is reachable", parents=[common]).set_defaults(func=cmd_ping)

    p = sub.add_parser(
        "setup",
        help="configure this machine, or install and configure a remote one",
        parents=[common],
    )
    p.add_argument("--remote", metavar="USER@HOST",
                   help="install the plugin on this host over ssh instead of configuring locally")
    p.add_argument("--url", help="hub base URL (default: current config)")
    p.add_argument("--from-server", metavar="USER@HOST",
                   help="read the shared token from the hub server host over ssh")
    p.add_argument("--repo-path", default="~/workspace/ai-hub",
                   help="server checkout path used with --from-server")
    p.add_argument("--auto-inbox", dest="auto_inbox", action="store_true", default=None)
    p.add_argument("--no-auto-inbox", dest="auto_inbox", action="store_false")
    p.add_argument("--dry-run", action="store_true",
                   help="with --remote, print the commands without running them")
    p.set_defaults(func=cmd_setup)

    p = sub.add_parser("init", help="write ~/.config/ai-hub/client.json")
    p.add_argument("--url")
    p.add_argument("--token")
    p.add_argument("--label")
    p.add_argument("--auto-inbox", dest="auto_inbox", action="store_true", default=None)
    p.add_argument("--no-auto-inbox", dest="auto_inbox", action="store_false")
    p.add_argument("--server", default=argparse.SUPPRESS)
    p.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    p.set_defaults(func=cmd_init)
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    # The shared flags use SUPPRESS so either level can set them; fill in the
    # defaults for whichever the caller left out.
    for name, fallback in (("server", None), ("token", None), ("label", None), ("json", False)):
        if not hasattr(args, name):
            setattr(args, name, fallback)
    try:
        cfg = Config(args)
    except Exception as exc:
        sys.stderr.write("configuration error: %s\n" % exc)
        return EXIT_CONFIG

    if args.command not in ("init", "whoami", "setup") and not cfg.token:
        sys.stderr.write(
            "no auth token configured.\n"
            "Run: %s init --url %s --token <token> --label <name>\n"
            % (Path(__file__).name, cfg.url)
        )
        return EXIT_CONFIG
    try:
        return args.func(cfg, args)
    except LocalNetworkBlocked as exc:
        # The permission is per binary, so another interpreter on the same
        # machine may well be allowed. Try one before giving up.
        alt = _fallback_interpreter()
        if alt is not None:
            env = dict(os.environ, **{REEXEC_GUARD: "1"})
            try:
                return subprocess.call([alt, os.path.abspath(__file__)] + list(argv or sys.argv[1:]), env=env)
            except OSError:
                pass
        sys.stderr.write("%s\n" % exc)
        return exc.code
    except HubError as exc:
        sys.stderr.write("%s\n" % exc)
        return exc.code
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # keep stack traces out of the model's context
        sys.stderr.write("unexpected client error: %s: %s\n" % (type(exc).__name__, exc))
        return EXIT_SERVER


if __name__ == "__main__":
    sys.exit(main())
