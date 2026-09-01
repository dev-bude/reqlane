"""`repomoot install` / `repomoot uninstall` for runtimes. The workspace owns the instruction texts; the
runtime only gets a three-line pointer, a thin skill and hooks that ask the daemon for the card."""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from ...core import cards

BEGIN, END = "<!-- repomoot:begin -->", "<!-- repomoot:end -->"
REPOMOOT_HOOK_RE = re.compile(r"""repomoot(\.exe)?["']?\s+hook\s""", re.IGNORECASE)


def is_aw_hook(entry: dict) -> bool:
    return any(REPOMOOT_HOOK_RE.search(h.get("command", "")) for h in entry.get("hooks", []))

SKILL = """---
name: repomoot
description: Repomoot shortcuts — connect this session, read the inbox, create a request to another project's agent, ask the Product Owner, deliver, evaluate. Use when the user says "/repomoot", "ask <agent>", "hand over to PO", "what's in the inbox" — in any language.
---

# /repomoot — Repomoot

## Handled by the user's hook — do NOT run anything for these

`/repomoot start`, `/repomoot connect [name] [--depends-on a,b]`, `/repomoot po`, `/repomoot depends a,b`,
`/repomoot status`, `/repomoot ui`, `/repomoot disconnect` are executed by the `UserPromptSubmit` hook with the
user's own authority before you see them. Their result is already in your context as a
`[repomoot] …` block. Your only job: tell the user in one line what happened (e.g. "Connected as
agent gridlib; PO absent; inbox empty"), then — if a card was printed — call ListAgents and run
`repomoot address "NAME [ref]"` with its `This session is …` line, and follow the card from now on.
If no `[repomoot]` block appeared, say so — the hooks are probably not installed
(`repomoot install --runtime claude-code --hooks` in the user's terminal).

## Everything else — thin wrappers over the `repomoot` CLI (`repomoot --help`)

The rules are the protocol card (printed at connect / session start; `repomoot protocol` reprints it).
If `repomoot` is not on PATH, use the full path given in the `[repomoot]` line.

- `/repomoot inbox` → `repomoot inbox`. Blocking first.
- `/repomoot wait` → `repomoot wait` (blocks up to 10 min until something happens), then act on it.
- `/repomoot request <agent>` → collect title, goal, constraints, acceptance, code refs from the
  conversation (ask only for what is missing), then `repomoot req new --to <agent> --type ... --body -`.
- `/repomoot ask <agent> <text>` → `repomoot req new --to <agent> --type question --title "<short>" --body -`.
- `/repomoot po <text>` → `repomoot ask-po --title "<short>" --kind <kind> --option "A: ..." --body -`;
  if `po_present: false`, present the choice to the user in their language and wait.
- `/repomoot po handoff <req>` → `repomoot handoff <req>`.
- `/repomoot reply <req> <text>` → `repomoot reply <req> --body -`.
- `/repomoot deliver <req>` → run the project's verification gate, then `repomoot deliver <req> --repo <you> --commit $(git rev-parse HEAD) --tests-passed|--tests-failed --body -`.
- `/repomoot eval <req>` → integrate, measure, then `repomoot evaluate <req> --verdict accepted|rejected --body -`.
- `/repomoot status [req]` → `repomoot req list` or `repomoot req show <req>`.
"""


def aw_executable() -> str:
    exe = shutil.which("repomoot") or str(Path(sys.executable).with_name("repomoot.exe" if sys.platform == "win32" else "repomoot"))
    return exe.replace("\\", "/")


def hooks_settings() -> dict:
    exe = f'"{aw_executable()}"'
    return {"hooks": {
        "SessionStart": [{"matcher": "startup|clear|resume|compact", "hooks": [{"type": "command", "command": f"{exe} hook session-start", "timeout": 10}]}],
        "UserPromptSubmit": [{"hooks": [{"type": "command", "command": f"{exe} hook prompt", "timeout": 10}]}],
        "Stop": [{"hooks": [{"type": "command", "command": f"{exe} hook stop", "timeout": 10}]}],
        "SessionEnd": [{"hooks": [{"type": "command", "command": f"{exe} hook session-end", "timeout": 10}]}],
    }}


def _replace_block(path: Path, text: str | None) -> str:
    """Insert/replace (text) or remove (None) the repomoot block. Returns the action taken."""
    cur = path.read_text(encoding="utf-8") if path.exists() else ""
    if BEGIN in cur and END in cur:
        pre, rest = cur.split(BEGIN, 1)
        _, post = rest.split(END, 1)
        new = pre.rstrip("\n") + ("\n\n" + text.strip() + "\n" if text else "\n") + post.lstrip("\n")
        action = "updated" if text else "removed"
    elif text:
        new = (cur.rstrip() + "\n\n" if cur.strip() else "") + text.strip() + "\n"
        action = "appended" if cur else "created"
    else:
        return "absent"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
    return action


def claude_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")


def plan(runtime: str, target_dir: Path | None, hooks: bool) -> list[tuple[str, Path, str]]:
    """What install would write: (action, path, description)."""
    if runtime == "claude-code":
        home = claude_home()
        out = [("write-block", home / "CLAUDE.md", "3-line pointer between <!-- repomoot:begin/end --> markers"),
               ("write", home / "skills" / "repomoot" / "SKILL.md", "the /repomoot skill")]
        if hooks:
            out.append(("merge", home / "settings.json", "hooks SessionStart/UserPromptSubmit/Stop/SessionEnd → `repomoot hook ...` (backup made)"))
        return out
    files = {"codex": "AGENTS.md", "gemini": "GEMINI.md", "cursor": ".cursorrules"}
    if runtime in files and target_dir:
        return [("write-block", target_dir / files[runtime], "the protocol card between markers")]
    return []


def install(runtime: str, target_dir: Path | None, hooks: bool, who: dict | None) -> list[str]:
    log: list[str] = []
    if runtime == "claude-code":
        home = claude_home()
        log.append(f"{_replace_block(home / 'CLAUDE.md', cards.CLAUDE_CODE_POINTER)}: {home / 'CLAUDE.md'}")
        skill = home / "skills" / "repomoot" / "SKILL.md"
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_text(SKILL, encoding="utf-8")
        log.append(f"written: {skill}")
        settings = home / "settings.json"
        if hooks:
            cur = json.loads(settings.read_text(encoding="utf-8")) if settings.exists() else {}
            if settings.exists():
                bak = settings.with_suffix(f".json.bak-{datetime.now():%Y%m%d%H%M%S}")
                shutil.copy(settings, bak)
                log.append(f"backup: {bak}")
            hooks_cfg = cur.setdefault("hooks", {})
            for event, entries in hooks_settings()["hooks"].items():
                lst = [e for e in hooks_cfg.get(event, []) if not is_aw_hook(e)]
                hooks_cfg[event] = lst + entries
            settings.write_text(json.dumps(cur, indent=2), encoding="utf-8")
            log.append(f"hooks merged into {settings}")
        else:
            log.append("hooks NOT installed; rerun with --hooks, or merge this into ~/.claude/settings.json:")
            log.append(json.dumps(hooks_settings(), indent=2))
        return log
    files = {"codex": "AGENTS.md", "gemini": "GEMINI.md", "cursor": ".cursorrules"}
    text = cards.card(who, runtime)
    if runtime in files and target_dir:
        log.append(f"{_replace_block(target_dir / files[runtime], text)}: {target_dir / files[runtime]}")
    else:
        log.append(text)
    return log


def uninstall(runtime: str, target_dir: Path | None) -> list[str]:
    log: list[str] = []
    if runtime == "claude-code":
        home = claude_home()
        log.append(f"{_replace_block(home / 'CLAUDE.md', None)}: {home / 'CLAUDE.md'}")
        skill = home / "skills" / "repomoot"
        if skill.exists():
            shutil.rmtree(skill)
            log.append(f"removed: {skill}")
        settings = home / "settings.json"
        if settings.exists():
            cur = json.loads(settings.read_text(encoding="utf-8"))
            changed = False
            for event, lst in list(cur.get("hooks", {}).items()):
                kept = [e for e in lst if not is_aw_hook(e)]
                if len(kept) != len(lst):
                    changed = True
                    if kept:
                        cur["hooks"][event] = kept
                    else:
                        del cur["hooks"][event]
            if changed:
                shutil.copy(settings, settings.with_suffix(f".json.bak-{datetime.now():%Y%m%d%H%M%S}"))
                settings.write_text(json.dumps(cur, indent=2), encoding="utf-8")
                log.append(f"hooks removed from {settings}")
        return log
    files = {"codex": "AGENTS.md", "gemini": "GEMINI.md", "cursor": ".cursorrules"}
    if runtime in files and target_dir:
        log.append(f"{_replace_block(target_dir / files[runtime], None)}: {target_dir / files[runtime]}")
    return log


def handover_block(agent: str, rows: list[dict]) -> str:
    L = [f"<!-- repomoot:handover:begin -->", f"## Repomoot ({agent}) — {datetime.now():%Y-%m-%d %H:%M}"]
    if not rows:
        L.append("- no open requests")
    for r in rows:
        actor = r.get("actor")
        L.append(f"- {r['id']} {r['type']} {r['from_agent']}→{r['to_agent']} {r['status']}{' [blocking]' if r.get('blocking') else ''}: {r['title']}"
                 + (f" (next: {'me' if actor == agent else actor})" if actor else ""))
    L.append("- resume with: `repomoot inbox`, `repomoot req show <id>`")
    L.append("<!-- repomoot:handover:end -->")
    return "\n".join(L)


def write_handover(f: Path, block: str) -> None:
    cur = f.read_text(encoding="utf-8") if f.exists() else ""
    b, e = "<!-- repomoot:handover:begin -->", "<!-- repomoot:handover:end -->"
    if b in cur and e in cur:
        pre, rest = cur.split(b, 1)
        _, post = rest.split(e, 1)
        new = pre.rstrip("\n") + "\n\n" + block + post
    else:
        new = cur.rstrip() + "\n\n" + block + "\n"
    f.write_text(new, encoding="utf-8")


def find_handover(start: Path, repos: list[str]) -> Path | None:
    """docs/HANDOVER.md at or above cwd, but never above the agent's own repository roots."""
    roots = [Path(r).resolve() for r in repos]
    for p in [start.resolve(), *start.resolve().parents]:
        f = p / "docs" / "HANDOVER.md"
        if f.exists() and any(str(p).lower().startswith(str(r).lower()) for r in roots):
            return f
    return None
