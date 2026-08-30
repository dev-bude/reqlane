# Reqlane

**Requests between AI coding agents that own different repositories — with consumer acceptance
and owner escalation.**

When several coding agents (Claude Code, Codex, Gemini CLI, your own) work on related
repositories, there is no working channel between them: you become the courier, the product
owner and the referee, and the history disappears with every `/clear`. Reqlane gives the agents a
shared lane for *requests* — "I need X from your repository" — with a lifecycle that ends when the
**consumer** has verified the result in its own context, a **Product Owner** role for the decisions
owners must not make alone, and a durable, searchable history.

It is not shared memory, not an orchestrator and not a task board. Agents keep their own runtime,
context and tools. Reqlane owns requests, messages, artifacts, decisions and events.

## How it works

- A local daemon holds the state (SQLite) and serves an HTTP API. The CLI `reqlane` is a thin
  client and starts the daemon on demand.
- Agents talk to it through `reqlane` from their own shell tool — nothing else is required from a
  runtime. The rules an agent follows (the *protocol card*) are owned by Reqlane and handed to the
  agent at `reqlane connect`; the Claude Code adapter adds a thin skill and hooks that fetch it.
- The lifecycle: `open → discussion → proposal → implementation → delivery → evaluation → closed`,
  with `question`, `bug`, `change`, `review`, `notice`, `task` and `decision` variants. See
  [PROTOCOL.md](PROTOCOL.md) — the protocol is documented separately from the code (CC-BY-4.0) and
  maps onto A2A task states and MCP Tasks.
- If no Product Owner session is present, a product question stays in the chat of the agent
  that needs it, with the option to hand it over to the PO later.

Status: pre-release. Local mode works end to end (see [examples/](examples/)); no MCP surface,
no CI/ephemeral mode, no UI yet. Code: Apache-2.0.

## Install

```
python -m venv .venv
.venv/bin/pip install -e .[dev]        # Windows: .venv\Scripts\python -m pip install -e .[dev]
reqlane --version
```

Claude Code adapter — a 3-line pointer in `~/.claude/CLAUDE.md`, a `/reqlane` skill and hooks
(`SessionStart`, `UserPromptSubmit`, `Stop`, `SessionEnd`). It shows what it will write and asks
first; `reqlane uninstall` reverts it.

```
reqlane install --runtime claude-code --hooks
```

Other runtimes: `reqlane install --runtime codex --dir <project>` writes the card into `AGENTS.md`
(`gemini` → `GEMINI.md`, `cursor` → `.cursorrules`); `reqlane protocol` prints it.

## First steps

You, in your own terminal (registration needs the human principal, see below):

```
cd <repo-A>;  reqlane connect a                   # registers agent "a" with repo = cwd
cd <repo-B>;  reqlane connect b --depends-on a
cd <docs>;    reqlane connect po --kind po        # product owner; optional, can be added later
```

Each agent session then runs `reqlane connect` in its repository, receives the card and works:

```
reqlane inbox
reqlane req new --to a --type capability --blocking --title "..." --goal "..." --body -
reqlane wait --req req_0001
reqlane reply / propose / deliver / evaluate / ask-po / decide / handoff
```

`reqlane --help`, `reqlane <cmd> --help`; every command accepts `--json`. Exit codes: 0 ok,
2 bad arguments, 3 not connected, 4 forbidden or bad transition, 5 not found, 6 daemon
unavailable, 7 wait timeout.

## Principals

- **Daemon token** (`~/.reqlane/token`): every local client. Lets a session act as an *already
  registered* agent from that agent's own repository directory.
- **Human token** (`~/.reqlane/human.token`): registering and editing agents, setting the PO policy,
  speaking as an agent (`--as`), attesting human decisions. The CLI sends it only from an
  interactive terminal or via `REQLANE_HUMAN_TOKEN` (CI). Agents inside a runtime's shell tool
  have no TTY and therefore cannot register agents or forge human decisions; a human answer they
  record is stored as *unattested* and listed for the PO.

State lives in `~/.reqlane/` (`REQLANE_HOME`); port 7771 (`REQLANE_PORT`). Trust model and data
notes: [SECURITY.md](SECURITY.md).

## Layout

```
reqlane/core      db, lifecycle, cards (instruction texts), service (all rules)
reqlane/server    FastAPI daemon: HTTP API, async long-poll and SSE events
reqlane/client    HTTP client, session files, human principal, daemon autostart
reqlane/cli       typer CLI + text rendering (quotes and sanitizes other agents' text)
reqlane/adapters  claude_code: install/uninstall, skill, hooks
examples/         two tiny projects + product notes: a complete walkthrough
tests/            the walkthrough and the rules, through the API
```

Tests: `python -m pytest -q`.
