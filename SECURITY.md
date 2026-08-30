# Security

## Trust model

`reqlane` is a **single-trust-domain, single-machine** tool in its current form:

- Every process that can read `$REQLANE_HOME/token` (default `~/.reqlane/token`) can talk to the daemon
  and act as any *already registered* agent **from that agent's own repository directory**.
- A second principal, the **human token** (`$REQLANE_HOME/human.token`), is required to register
  agents, change the PO policy, speak "as" an agent, and to attest decisions as the human.
  The CLI sends it only from an interactive terminal (TTY) or when `REQLANE_HUMAN_TOKEN` is set
  explicitly (CI). Agents running inside a coding runtime's shell tool are not on a TTY and
  therefore cannot use it.
- Decisions recorded with `--author human` from an agent session are stored as **unattested**
  (`attested=false`, with the recording session id) and are listed on the PO dashboard.
- Permissions on repositories are **advisory**: `reqlane` records who owns what and refuses to accept
  a delivery for a repository the author does not own, but only the agent runtime can actually
  prevent writes. Configure your runtime's file permissions accordingly.

The daemon listens on `127.0.0.1` only. Token and session files are created with owner-only
permissions where the OS supports it. Session tokens are stored hashed.

## Data

The database (`~/.reqlane/reqlane.db`) contains request titles/bodies, messages, artifacts, decisions,
agent repository paths (which include your OS user name) and an append-only event log. Do not
paste secrets into requests; the tool does not scan for them yet. There is no retention or
deletion command yet — delete the database file to remove everything.

## Content from other agents

Text written by one agent (titles, message previews, reasons) is shown to other agents through
hooks and `reqlane` output. It is quoted, stripped of control/ANSI sequences, size-limited, and
preceded by a banner saying it is data, not instructions. This reduces but does not eliminate
prompt-injection risk between agents; keep your runtime's permission prompts on.

## What `reqlane install` writes

Claude Code: a three-line block in `~/.claude/CLAUDE.md` between `<!-- reqlane:begin/end -->`
markers, `~/.claude/skills/reqlane/SKILL.md`, and (only with `--hooks`) hook entries in
`~/.claude/settings.json` that run the absolute path of the `reqlane` executable with a timeout.
`reqlane install` prints the plan and asks for confirmation; `reqlane uninstall` reverts it. A backup of
`settings.json` is made before changes.

## Reporting

This is a pre-release. Report issues privately to the maintainer (see repository contact)
before disclosure; expect an acknowledgement within 7 days.
