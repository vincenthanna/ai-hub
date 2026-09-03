# hub — ai-hub client for Claude Code

This plugin lets one Claude Code session hand work to another through an ai-hub
server. It ships a single skill (`hub`) and one CLI (`scripts/hub.py`) that uses
nothing outside the Python standard library, so it runs wherever `python3` does.

## Install

```shell
/plugin marketplace add vincenthanna/ai-hub
/plugin install hub@ai-hub
```

Cloning only the plugin keeps the server sources off client machines:

```bash
claude plugin marketplace add vincenthanna/ai-hub --sparse .claude-plugin plugins
claude plugin install hub@ai-hub --yes
```

## Configure

Point the client at a hub and give this machine a name. The token comes from the
server host (`bash scripts/show-token.sh` in the server checkout).

```bash
python3 ~/.claude/plugins/cache/ai-hub/hub/*/scripts/hub.py init \
  --url http://192.168.49.48:16001 \
  --token <token> \
  --label my-laptop
```

Verify:

```bash
python3 .../scripts/hub.py ping
python3 .../scripts/hub.py whoami
```

`claude plugin details hub@ai-hub` prints the exact install path.

## Optional: announce unread messages at session start

Off by default. Turn it on per machine:

```bash
python3 .../scripts/hub.py init --auto-inbox
```

With it off the SessionStart hook exits immediately and prints nothing, so a hub
that is down or unreachable never delays a session.

## Settings precedence

`--flag` beats `AIHUB_URL` / `AIHUB_TOKEN` / `AIHUB_LABEL`, which beat a
project-level `.ai-hub.json`, which beats `~/.config/ai-hub/client.json`.
A project file may set `label` and `defaultTopic` only; a `token` there is
ignored with a warning because that file can be committed.

## Troubleshooting

**The skill loads but Claude does not pick it up from a plain request.**
Claude Code fits every installed skill's description into an 8,000-character
listing budget and drops the descriptions of the least-used skills when it
overflows — a freshly installed plugin goes first. Confirm with
`claude -p "hi" --debug-file /tmp/s.log` and `grep -i "over budget" /tmp/s.log`,
then raise `skillListingBudgetFraction` (default 0.01) in
`~/.claude/settings.json`, or disable unused skills with `/skills`. Invoking
`/hub:hub` explicitly always works regardless of the budget.

**`[Errno 65] No route to host` from python3 while curl works.**
macOS grants local-network access per binary, so a pyenv or Homebrew python3
has its own (initially missing) grant. The client detects this and retries once
under `/usr/bin/python3`. To fix it properly, allow the interpreter under
System Settings > Privacy & Security > Local Network.
