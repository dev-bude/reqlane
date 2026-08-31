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

Status: pre-release. Local mode works end to end — see [WALKTHROUGH.md](WALKTHROUGH.md) for one real
request followed from the user's instruction to the consumer's evaluation. No MCP surface, no
CI/ephemeral mode yet. Code: Apache-2.0.

## Install

```
git clone https://github.com/dev-bude/reqlane.git && cd reqlane
python -m venv .venv
.venv/bin/pip install -e .          # Windows: .venv\Scripts\python -m pip install -e .
.venv/bin/reqlane --version         # Windows: .venv\Scripts\reqlane --version
```

Then install the Claude Code adapter (run it from the venv; it shows what it will write and asks):

```
reqlane install --runtime claude-code --hooks
```

That adds a 3-line pointer to `~/.claude/CLAUDE.md`, a `/reqlane` skill, and four hooks that call
`reqlane` by its absolute path — so the agents do not need `reqlane` on PATH. Put `.venv/bin`
(Windows: `.venv\Scripts`) on your own PATH for typing commands yourself; `reqlane uninstall`
reverts the adapter.

Other runtimes: `reqlane install --runtime codex --dir <project>` writes the card into `AGENTS.md`
(`gemini` → `GEMINI.md`, `cursor` → `.cursorrules`); `reqlane protocol` prints it.

## First steps

Open one Claude Code session per repository (VS Code windows or terminals). In the chat:

```
/reqlane connect      # once per repository: registers this repo's agent (name = folder) and connects the session
/reqlane po           # once, in the folder with your product notes: the Product Owner (optional)
/reqlane ui           # opens the live request tree in the browser
```

These commands run in the hook with **your** authority before the agent sees them — agents cannot
register themselves. From then on it is automatic: every new session, `/clear` or resume in a
registered folder reconnects by itself, and the agent gets a short card: who else is in the
workspace (agents, repositories, online/offline), how to ask them or order work, how to wait and
how to wake the others. The first thing an agent does after connecting is record its own address
(`reqlane address "NAME [ref]"`, from its ListAgents tool) so that other agents can wake it with a
cross-session message; the daemon starts on demand and restarts itself after an upgrade.

Then just work with the agents as usual. The workspace shows up when an agent needs something from
another repository:

```
reqlane req new --to gridlib --type capability --blocking --title "..." --goal "..." --body -
reqlane wait --req req_0001          # the other agent is woken; this one waits for the answer
reqlane propose / deliver / evaluate / ask-po / decide / handoff   (each reply prints `next:`)
```

`/reqlane status`, `/reqlane disconnect` work the same way; dependencies between agents are
inferred from the requests they send. Everything is also available from your terminal
(`reqlane agents`, `reqlane req show req_0001`, ...). `reqlane --help`, `reqlane <cmd> --help`;
every command accepts `--json`. Exit codes: 0 ok, 2 bad arguments, 3 not connected,
4 forbidden or bad transition, 5 not found, 6 daemon unavailable, 7 wait timeout.

## When something looks stuck

- An agent "went quiet": its turn ended before the answer arrived and nothing woke it. Type
  `/reqlane inbox` in that session — the hook injects what is new and the agent continues. It
  happens when the other side could not reach it (no address recorded yet, or the sessions are on
  different machines); after `reqlane address` the wake-up is automatic.
- `/reqlane status` in any session shows who is online; `reqlane agents` from the terminal too.
- Two sessions in one folder: give the second one a name, `reqlane connect --session two`.
- State lives in `~/.reqlane/` (`REQLANE_HOME`), port 7771 (`REQLANE_PORT`); delete the folder to
  start from scratch (stop the daemon first: `reqlane status` shows it, `/admin/shutdown` or kill it).

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

## Reading on

- [WALKTHROUGH.md](WALKTHROUGH.md) — how a request flows, step by step, with real output.
- [PROTOCOL.md](PROTOCOL.md) — lifecycle, roles, artifacts, mapping to A2A / MCP Tasks.
- [SECURITY.md](SECURITY.md) — trust model, data, what the adapter writes.
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to set up, what is worth working on, house rules.

## Layout

```
reqlane/core      db, lifecycle, cards (instruction texts), service (all rules)
reqlane/server    FastAPI daemon: HTTP API, async long-poll and SSE events
reqlane/client    HTTP client, session files, human principal, daemon autostart
reqlane/cli       typer CLI + text rendering (quotes and sanitizes other agents' text)
reqlane/adapters  claude_code: install/uninstall, skill, hooks
tests/            the walkthrough's flow and the rules, through the API
```

Tests: `python -m pytest -q`.

## Licence

Code: Apache-2.0 ([LICENSE](LICENSE), [NOTICE](NOTICE)). The protocol description
([PROTOCOL.md](PROTOCOL.md)): CC BY 4.0.

Claude Code, Codex, Gemini CLI and Cursor are products of their respective owners; Reqlane is an
independent project and is not affiliated with or endorsed by any of them.
