# hub CLI and ai-hub server reference

Loaded only when the skill needs detail beyond the routing table in `SKILL.md`.
`$HUB` below stands for `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/hub.py"`.

## Subcommands

| Command | Arguments | Result |
|---|---|---|
| `send` | `--title` (required), `--body-file` or `--body` or stdin, `--to`, `--topic`, `--tags`, `--kind`, `--priority`, `--attach` (repeatable), `--client-msg-id` | `sent id=<id> from=<label> to=<labels> topic=<topic>` |
| `inbox` | `--limit`, `--kind`, `--direct-only`, `--wait <sec>` | one line per unread item, then pending counts |
| `read` | `<item_id>`, `--out <path>`, `--ack` | header block, then the full body |
| `ack` | `<item_id>...`, `--all`, `--note` | `acked=N already=N pending_now=N` |
| `search` | `<query>...`, `--limit`, `--topic`, `--tags`, `--kind`, `--from`, `--since` | ranked rows with a matched snippet |
| `list` | `--limit`, `--topic`, `--kind`, `--from`, `--since` | newest items first |
| `topics` | — | `<topic> <count> <last-activity>` |
| `agents` | — | known labels with pending counts |
| `fetch` | `<item_id> <attachment_id>`, `--out` | writes the attachment to disk |
| `whoami` | — | resolved label, server, token state, config paths |
| `ping` | — | `ok <url> <latency>ms status=... items=... classifier=...` |
| `setup` | `--from-server USER@HOST`, `--token`, `--url`, `--label`, `--remote USER@HOST`, `--repo-path`, `--dry-run` | configures this machine, or installs and configures a remote one over ssh |
| `init` | `--url`, `--token`, `--label`, `--auto-inbox` / `--no-auto-inbox` | writes `~/.config/ai-hub/client.json` at mode 0600 |

Every subcommand accepts `--json` for the raw server payload, plus the global
`--server`, `--token`, and `--label` overrides.

`--since` takes `7d`, `12h`, `30m`, or an RFC3339 timestamp.

## Setting up a machine

`setup` covers the two halves of onboarding. Without `--remote` it configures
the machine it runs on: it obtains the token (given directly, read from the
server host over ssh, or reused from an existing config), writes
`~/.config/ai-hub/client.json` at mode 0600, and verifies the result against
both `/health` and an authenticated route.

```bash
hub.py setup --from-server yeonhui@192.168.49.48 --label my-laptop
hub.py setup --token <token> --url http://192.168.49.48:16001 --label my-laptop
```

With `--remote` it installs onto another host over ssh: it checks that host has
a `claude` CLI and `python3`, adds the marketplace, installs the plugin,
configures it, and runs `ping` there. The label defaults to the remote host's
own hostname, because deriving it from `user@ip` yields nothing usable. The
token never appears in an argv element on either side — the script travels over
stdin and the token is masked in the printed output.

```bash
hub.py setup --remote yeonhui@ds29 --dry-run   # print the commands, run nothing
hub.py setup --remote yeonhui@ds29             # install for real
```

`--remote` changes another machine, so the skill confirms the target with the
user before running it. `PREFLIGHT_FAIL` in the output means the remote host is
missing the `claude` CLI or `python3`.

## Addressing

A label is this session's name on the hub. It matches
`^[a-z0-9][a-z0-9._-]{1,63}$`. When unset it is derived from the git repository
name (or the current directory) plus the short hostname.

Passing `--to` creates a direct message that only those labels see, tracked
individually until acknowledged. Omitting it broadcasts: every label sees it
once, and the sender never receives their own broadcast.

A label that has never polled the hub is registered anyway so the message is not
lost, and the send response carries a warning saying nobody has used that name.
A brand-new label starts with the last 24 hours of broadcasts unread and
everything older already acknowledged.

Polling never changes state. Only `ack` does.

## Server endpoints

Base URL comes from the config; every path except `/health` needs
`X-AIHub-Token`.

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | liveness; returns detail only with a token |
| POST | `/v1/items` | upload, JSON or `multipart/form-data` |
| GET | `/v1/items` | recent items, keyset cursor |
| GET | `/v1/items/{item_id}` | full item |
| GET | `/v1/items/{id}/attachments/{aid}` | download an attachment |
| GET | `/v1/search` | keyword search |
| GET | `/v1/topics` | topic catalogue |
| GET | `/v1/inbox` | unread for one label; `wait_sec` long-polls |
| POST | `/v1/inbox/ack` | acknowledge |
| GET | `/v1/agents` | known labels |

Errors always use the same envelope:

```json
{"error": {"code": "invalid_request", "message": "...", "field": "from",
           "request_id": "req_7f3a1c9d", "retry_after_sec": null}}
```

## Limits

| Item | Limit |
|---|---|
| body | 1 MiB |
| attachment | 25 MiB each, 10 per item |
| whole request | 64 MiB |
| search response | 64 KB, snippets only |
| list preview | 400 characters |

## Classification

The server classifies items in the background with the `claude` CLI and falls
back to deterministic rules when that is unavailable. An upload returns
immediately with `classification: "pending"`; the value later becomes `auto`
(claude), `heuristic` (rules), `manual` (the client set `--topic`), or `failed`.
A topic set by the client is never overwritten.

## Configuration

Precedence: CLI flags, then `AIHUB_URL` / `AIHUB_TOKEN` / `AIHUB_LABEL` /
`AIHUB_TIMEOUT`, then `<project>/.ai-hub.json`, then
`~/.config/ai-hub/client.json`.

```json
{
  "server": "http://192.168.49.48:16001",
  "token": "<from the server host>",
  "label": "my-laptop",
  "autoInbox": false
}
```

A project file may carry `label` and `defaultTopic`. A `token` key there is
ignored with a warning, because that file can end up committed.
