"""`reqlane` command line: the primary client of the workspace for agents, humans and hooks.

Layout: verbs an agent uses most are top-level (connect, inbox, reply, propose, deliver, evaluate,
ask-po, decide, handoff, wait); `reqlane req ...` holds request management; `reqlane po ...` only PO operations.
"""
from __future__ import annotations

import functools
import json
import os
import sys
from pathlib import Path
from typing import List, Optional

import typer

from .. import PROTOCOL_VERSION, __version__, config
from ..adapters.claude_code import install as inst
from ..client.http import Client, ClientError, daemon_alive, drop_session, find_session, human_token_if_human, save_session, update_session
from ..core import cards
from . import fmt

app = typer.Typer(help="Reqlane CLI. Rules for agents: `reqlane protocol`.", no_args_is_help=True, add_completion=False,
                  context_settings={"help_option_names": ["-h", "--help"]})
req_app = typer.Typer(help="Request management: new, list, show, claim, decline, withdraw, close, reassign, accept, escalate, ack.", no_args_is_help=True)
art_app = typer.Typer(help="Artifacts (investigation, note; proposal/delivery/evaluation have top-level verbs).", no_args_is_help=True)
agr_app = typer.Typer(help="Cross-project agreements.", no_args_is_help=True)
po_app = typer.Typer(help="Product Owner operations: dashboard, policy, task, delegate.", no_args_is_help=True)
perm_app = typer.Typer(help="Permissions (advisory; the runtime enforces).", no_args_is_help=True)
events_app = typer.Typer(help="Event log of this session.", invoke_without_command=True)
hook_app = typer.Typer(help="Runtime hooks (Claude Code). Print to stdout; never fail.", no_args_is_help=True)
agents_app = typer.Typer(help="Registered agents.", invoke_without_command=True)
for name, sub in [("req", req_app), ("artifact", art_app), ("agreement", agr_app), ("po", po_app), ("perm", perm_app),
                  ("events", events_app), ("hook", hook_app), ("agents", agents_app)]:
    app.add_typer(sub, name=name)

STATE = {"json": False}


def _version_cb(value: bool):
    if value:
        typer.echo(f"reqlane {__version__} protocol {PROTOCOL_VERSION}")
        raise typer.Exit()


@app.callback()
def _root(json_out: bool = typer.Option(False, "--json", help="Machine-readable output."),
          version: bool = typer.Option(False, "--version", help="Print version and protocol.", callback=_version_cb, is_eager=True)):
    STATE["json"] = json_out


# ---------------------------------------------------------------- plumbing
def fail(e: ClientError) -> None:
    if STATE["json"]:
        typer.echo(json.dumps({"error": str(e), "code": e.code, "hint": e.hint, "protocol": PROTOCOL_VERSION}))
    else:
        typer.echo(f"error: {e} [{e.code}]" + (f"\nhint: {e.hint}" if e.hint else ""), err=True)
    raise typer.Exit(e.exit_code)


def emit(data: dict, text: str | None) -> None:
    if STATE["json"]:
        typer.echo(json.dumps({**data, "protocol": PROTOCOL_VERSION}, ensure_ascii=False, indent=1))
    elif text:
        typer.echo(text)


def client(need_session: bool = True, human: bool = False) -> tuple[Client, dict | None]:
    ses = find_session()
    if need_session and not ses:
        hint = "reqlane connect <agent>"
        try:
            sug = Client(autostart=True).get("/agent-for-cwd", cwd=str(Path.cwd())).get("agent")
            if sug:
                hint = f"reqlane connect {sug}"
        except ClientError:
            pass
        fail(ClientError("this directory has no connected session", "not_connected", hint, 401))
    return Client(session_token=ses["token"] if ses else None, human=human_token_if_human() if human else None), ses


def me_of(ses: dict | None) -> str | None:
    return ses.get("agent") if ses else None


def read_body(body: Optional[str], body_file: Optional[Path]) -> str:
    if body_file:
        return body_file.read_text(encoding="utf-8")
    if body == "-":
        return sys.stdin.read()
    return body or ""


def csv(s: Optional[str]) -> list[str]:
    return [x.strip() for x in s.split(",") if x.strip()] if s else []


def opts_of(options: Optional[List[str]]) -> list[dict]:
    out = []
    for part in options or []:
        if ":" in part:
            i, s = part.split(":", 1)
            out.append({"id": i.strip(), "summary": s.strip()})
        else:
            out.append({"id": part.strip(), "summary": part.strip()})
    return out


def notify_line(res: dict) -> str:
    n = res.get("notify") or []
    if not n:
        return "notify: none online (they will see it on their next `reqlane inbox`)"
    return "notify: " + ", ".join(dict.fromkeys(f"{t['agent']}[{t.get('runtime') or '?'}:{t['ref']}]" for t in n))


def guard(fn):
    @functools.wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except ClientError as e:
            fail(e)
    return wrapper


# ---------------------------------------------------------------- daemon
@app.command()
def serve():
    """Start the daemon in the foreground (idempotent). Clients also start it on demand."""
    url = config.base_url()
    if daemon_alive(url):
        typer.echo(f"daemon already running at {url}")
        raise typer.Exit()
    from ..server.run import main
    typer.echo(f"reqlane daemon {__version__} at {url}  (db: {config.db_path()})")
    main()


@app.command()
@guard
def status():
    """Daemon health, agents and online sessions."""
    c = Client(autostart=False)
    if not daemon_alive(c.base_url):
        fail(ClientError(f"daemon not running at {c.base_url}", "daemon_unavailable", "reqlane serve", 503))
    h = c.get("/health")
    ag = c.get("/agents")
    lines = [f"daemon ok v{h['version']} protocol {h['protocol']}  po: {'present' if h['po_present'] else 'absent'}"]
    for a in ag["agents"]:
        on = ", ".join(s.get("name") or s["id"] for s in a["sessions"]) or "offline"
        lines.append(f"  {a['id']:<14} {a['kind']:<14} open→{a['open_requests_to']:<3} {on}")
    emit({"health": h, **ag}, "\n".join(lines))


@app.command()
def ui(no_browser: bool = typer.Option(False, "--no-browser", help="Only print the URL.")):
    """Open the live request tree in the browser."""
    import webbrowser
    c = Client()
    c.get("/health")
    url = f"{c.base_url}/ui?token={config.token()}"
    typer.echo(url)
    if not no_browser:
        webbrowser.open(url)


@app.command()
@guard
def tick():
    """Run the timers now (session expiry, local→PO promotion)."""
    res = Client().post("/admin/tick")
    emit(res, f"sessions expired: {res['sessions_expired']}, decisions promoted: {res['decisions_promoted']}")


# ---------------------------------------------------------------- identity
@app.command()
@guard
def connect(agent: Optional[str] = typer.Argument(None, help="Agent id. Omit to use the agent registered for this directory."),
            kind: str = typer.Option("project", help="project | po  (registration only; needs the human: TTY or REQLANE_HUMAN_TOKEN)"),
            name: Optional[str] = typer.Option(None, "--session", "-s", help="Session name (several sessions in one directory)."),
            depends_on: Optional[str] = typer.Option(None, help="Comma-separated agents this project depends on (human only)."),
            description: Optional[str] = typer.Option(None, help="(human only)"),
            no_card: bool = typer.Option(False, "--no-card", help="Do not print the protocol card.")):
    """Connect this directory's session to an agent. First-time registration (repo = cwd) is done by the human."""
    cwd = Path.cwd()
    name = name or os.environ.get("REQLANE_SESSION") or None
    runtime = os.environ.get("REQLANE_RUNTIME") or ("claude-code" if os.environ.get("CLAUDECODE") else "cli")
    existing = find_session(cwd, name, exact=True)
    c = Client(human=human_token_if_human())
    res = c.post("/sessions/connect", agent=agent, kind=kind, cwd=str(cwd), name=name, runtime=runtime,
                 runtime_ref=os.environ.get("REQLANE_RUNTIME_REF") or claude_session_name() or name, pid=os.getppid(), depends_on=csv(depends_on), description=description)
    ses = res["session"]
    save_session(cwd, name, {"token": res["token"], "agent": ses["agent_id"], "session_id": ses["id"], "hook_cursor": res["cursor"]})
    who = res["who"]
    head = f"connected: {who['agent']} ({who['kind']}) session {ses['id']}" + (f" '{name}'" if name else "") + ("  (replaced previous session)" if existing else "")
    if who["kind"] != "product_owner":
        head += f"\nrepos: {', '.join(who['repos']) or '-'}   depends_on: {', '.join(who['depends_on']) or '-'}   consumers: {', '.join(who['consumers']) or '-'}"
    parts = [head, fmt.inbox_view(res["inbox"], who["agent"])]
    if not no_card:
        parts += ["", "=== protocol card (rules for this session) ===", res["card"].rstrip()]
    emit(res, "\n".join(parts))


@app.command()
@guard
def address(text: str = typer.Argument(..., help="Your cross-session address, e.g. 'gridlib-44 [e07008]' — the 'This session is …' line of ListAgents.")):
    """Record this session's address so other agents can wake it with a cross-session message."""
    import re as _re
    m = _re.search(r"([^\s\[]+(?:\s[^\s\[]+)*)\s*(\[[0-9a-f]{4,}\])?", text.replace("This session is", "").strip())
    ref = (m.group(1).strip() + (" " + m.group(2) if m.group(2) else "")) if m else text.strip()
    c, _ = client()
    res = c.post("/sessions/me/address", runtime_ref=ref)
    emit(res, f"address recorded: {res['runtime_ref']}  (others will SendMessage you at this name)")


@app.command()
@guard
def disconnect():
    """End this directory's session."""
    c, ses = client()
    try:
        c.post("/sessions/disconnect")
    finally:
        drop_session(ses)
    emit({"ok": True}, "disconnected")


@app.command()
@guard
def whoami():
    """Which agent this session is (exit 3 if not connected)."""
    c, ses = client()
    w = c.get("/whoami")
    emit(w, f"{w['agent']} ({w['kind']}) session {w['session']}  po: {'present' if w['po_present'] else 'absent'}\n"
            f"repos: {', '.join(w['repos']) or '-'}  depends_on: {', '.join(w['depends_on']) or '-'}  consumers: {', '.join(w['consumers']) or '-'}")


@agents_app.callback()
@guard
def agents_list(ctx: typer.Context):
    """Registered agents and who is online."""
    if ctx.invoked_subcommand:
        return
    res = Client().get("/agents")
    lines = []
    for a in res["agents"]:
        on = ", ".join(s.get("name") or s["id"] for s in a["sessions"]) or "offline"
        deps = f" depends_on={','.join(a['depends_on'])}" if a["depends_on"] else ""
        lines.append(f"{a['id']:<14} {a['kind']:<14} {on:<20} open→{a['open_requests_to']}{deps}")
    emit(res, "\n".join(lines) or "no agents; the human registers one with `reqlane connect <name>` inside its repository")


@agents_app.command("set")
@guard
def agents_set(agent: str, depends_on: Optional[str] = typer.Option(None), description: Optional[str] = None,
               add_repo: Optional[Path] = typer.Option(None, help="Additional repository directory.")):
    """Update an agent (human only)."""
    c = Client(human=human_token_if_human())
    res = c.post(f"/agents/{agent}", depends_on=csv(depends_on), description=description, add_repo=str(add_repo) if add_repo else None)
    emit(res, f"{agent}: repos={', '.join(res['repos'])} depends_on={', '.join(res['depends_on']) or '-'}")


# ---------------------------------------------------------------- inbox / events
@app.command()
@guard
def inbox():
    """What needs your attention: blocking / awaiting you / in progress / waiting / fyi."""
    c, ses = client()
    ib = c.get("/inbox")
    emit(ib, fmt.inbox_view(ib, me_of(ses)))


@app.command()
@guard
def wait(req: Optional[str] = typer.Option(None, "--req", help="Wait for events on this request only."),
         timeout: int = typer.Option(600, help="Seconds (max 600).")):
    """Block until an event arrives for you (long-poll). Exit 7 on timeout. Nothing wakes you afterwards — tell the user."""
    c, _ = client()
    res = c.get("/events", request_id=req, wait=min(timeout, 600))
    if res["timeout"]:
        emit(res, f"timeout after {timeout}s; nothing new" + (f" on {req}" if req else "") + ". Tell the user what you are waiting for.")
        raise typer.Exit(7)
    emit(res, "\n".join(fmt.event_line(e) for e in res["events"]))


@events_app.callback()
@guard
def events_list(ctx: typer.Context, since: Optional[int] = typer.Option(None, help="Cursor; default: this session's cursor."),
                req: Optional[str] = typer.Option(None, "--req"), limit: int = 50, own: bool = typer.Option(False, help="Include your own actions.")):
    """Events since the session cursor (non-blocking); advances the cursor."""
    if ctx.invoked_subcommand:
        return
    c, _ = client()
    res = c.get("/events", since=since, request_id=req, limit=limit, own=own)
    emit(res, "\n".join(fmt.event_line(e) for e in res["events"]) or "no new events")


@events_app.command("ack")
@guard
def events_ack(req: Optional[str] = typer.Option(None, "--req", help="Acknowledge all events of a request."),
               all_: bool = typer.Option(False, "--all"), event_ids: Optional[str] = typer.Argument(None, help="Comma-separated event ids.")):
    """Mark events as read."""
    c, _ = client()
    res = c.post("/ack", event_ids=[int(x) for x in csv(event_ids)], request_id=req, all=all_)
    emit(res, f"acked {res['acked']}")


# ---------------------------------------------------------------- requests
@req_app.command("new")
@guard
def req_new(to: str = typer.Option(..., help="Recipient agent."),
            type_: str = typer.Option("question", "--type", help="question|capability|bug|change|review|task"),
            title: str = typer.Option(..., help="One line."),
            body: Optional[str] = typer.Option(None, help="Text, or '-' for stdin."), body_file: Optional[Path] = typer.Option(None),
            goal: Optional[str] = typer.Option(None, help="Measurable goal."),
            constraint: Optional[List[str]] = typer.Option(None, help="Repeatable."), acceptance: Optional[List[str]] = typer.Option(None, help="Repeatable."),
            ref: Optional[List[str]] = typer.Option(None, help="@refs (repeatable; also parsed from body)."),
            priority: str = typer.Option("normal", help="low|normal|high"), blocking: bool = typer.Option(False, help="You cannot continue without the answer."),
            cc: Optional[List[str]] = typer.Option(None), parent: Optional[str] = typer.Option(None, help="Parent request id (sub-request)."),
            label: Optional[List[str]] = typer.Option(None), due: Optional[str] = typer.Option(None, help="ISO date."),
            idem: Optional[str] = typer.Option(None, help="Idempotency key: repeating the command will not create a duplicate."),
            as_agent: Optional[str] = typer.Option(None, "--as", help="Human only: speak as this agent.")):
    """Create a request to another agent."""
    c, ses = client(human=bool(as_agent))
    g = {k: v for k, v in (("description", goal), ("constraints", constraint), ("acceptance", acceptance)) if v}
    text = read_body(body, body_file)
    if ref:
        text = text.rstrip() + "\n\nrefs: " + " ".join(ref) + "\n"
    res = c.post("/requests", to=to, type=type_, title=title, body=text, goal=g, priority=priority, blocking=blocking,
                 cc=cc or [], parent_id=parent, labels=label or [], due_at=due, idem=idem, as_agent=as_agent)
    r = res["request"]
    dup = " (duplicate of an earlier call; nothing created)" if res.get("duplicate") else ""
    emit(res, f"{r['id']} created ({r['type']}{', blocking' if r['blocking'] else ''}) → {r['to_agent']}{dup}\n{notify_line(res)}"
              + (f"\nnext: reqlane wait --req {r['id']}" if r["blocking"] else ""))


@req_app.command("list")
@guard
def req_list(box: str = typer.Option("all", help="inbox|outbox|all"), status: Optional[str] = None, type_: Optional[str] = typer.Option(None, "--type"),
             agent: Optional[str] = typer.Option(None, help="Only requests involving this agent."), all_: bool = typer.Option(False, "--all", help="Include closed."),
             limit: int = 50):
    """List requests you are involved in."""
    c, ses = client()
    res = c.get("/requests", box=box, status=status, type=type_, other=agent, limit=limit, all=all_)
    emit(res, "\n".join(fmt.req_line(r, me_of(ses)) for r in res["requests"]) or "none")


@req_app.command("show")
@guard
def req_show(rid: str, since: Optional[int] = typer.Option(None, help="Only messages after this message rowid."),
             events_: bool = typer.Option(False, "--events", help="Include the event log.")):
    """Full thread: request, messages, artifacts, next actions."""
    c, ses = client()
    res = c.get(f"/requests/{rid}", include="messages,artifacts" + (",events" if events_ else ""), since=since)
    emit(res, fmt.request_view(res, me_of(ses)))


@req_app.command("claim")
@guard
def req_claim(rid: str):
    """Take a request addressed to you into work (a reply does this implicitly)."""
    c, _ = client()
    res = c.post(f"/requests/{rid}/claim")
    emit(res, f"{rid} → {res['request']['status']} (claimed)")


def _action(rid: str, action: str, reason: Optional[str] = None, to: Optional[str] = None):
    c, _ = client()
    res = c.post(f"/requests/{rid}/action", action=action, reason=reason, to=to)
    emit(res, f"{rid} → {res['request']['status']}\n{notify_line(res)}")


@req_app.command("decline")
@guard
def req_decline(rid: str, reason: str = typer.Option(..., help="Why. The initiator may escalate to the PO.")):
    """Decline a request addressed to you."""
    _action(rid, "decline", reason)


@req_app.command("withdraw")
@guard
def req_withdraw(rid: str, reason: Optional[str] = None):
    """Withdraw your own request (open sub-requests are closed as wont_do)."""
    _action(rid, "withdraw", reason)


@req_app.command("close")
@guard
def req_close(rid: str, reason: Optional[str] = None):
    """Close your own request (e.g. after an answer)."""
    _action(rid, "close", reason)


@req_app.command("ack")
@guard
def req_ack(rid: str):
    """Acknowledge a notice addressed to you."""
    _action(rid, "ack")


@req_app.command("reassign")
@guard
def req_reassign(rid: str, to: str = typer.Option(...), reason: Optional[str] = None):
    """Hand a request addressed to you to the right owner (thread is kept)."""
    _action(rid, "reassign", reason, to)


@req_app.command("accept")
@guard
def req_accept(rid: str, option: Optional[str] = typer.Option(None, help="Option id from the proposal."), notes: Optional[str] = None):
    """Initiator accepts the pending proposal; the recipient may implement."""
    c, _ = client()
    res = c.post(f"/requests/{rid}/accept_proposal", option=option, notes=notes)
    emit(res, f"{rid} → implementation (option {res['request'].get('accepted_option') or '-'})\n{notify_line(res)}")


@req_app.command("escalate")
@guard
def req_escalate(rid: str, question: str = typer.Option(..., help="What the PO must decide."),
                 kind: str = typer.Option("conflict", help="priority|scope_change|breaking_change|conflict|budget|access|other"),
                 option: Optional[List[str]] = typer.Option(None, help="'A: text' (repeatable)")):
    """Escalate a request to the PO (creates a child decision; parent becomes blocked)."""
    c, _ = client()
    res = c.post(f"/requests/{rid}/escalate", question=question, kind=kind, options=opts_of(option))
    emit(res, _po_created_text(res))


@app.command()
@guard
def reply(rid: str, body: Optional[str] = typer.Option(None, help="Text, or '-' for stdin."), body_file: Optional[Path] = None,
          type_: str = typer.Option("comment", "--type", help="comment|clarification|answer"),
          as_agent: Optional[str] = typer.Option(None, "--as", help="Human only: speak as this agent.")):
    """Post a message into a request thread (claims it if you are the recipient)."""
    c, _ = client(human=bool(as_agent))
    res = c.post(f"/requests/{rid}/messages", body=read_body(body, body_file), type=type_, as_agent=as_agent)
    extra = f" ({rid} → {res['status']})" if res.get("status") else ""
    emit(res, f"{res['message']['id']} posted{extra}\n{notify_line(res)}")


@app.command()
@guard
def notice(title: str = typer.Option(...), body: Optional[str] = typer.Option(None), body_file: Optional[Path] = None,
           to: Optional[List[str]] = typer.Option(None, help="Recipients (repeatable); default: all consumers of your repo."),
           label: Optional[List[str]] = typer.Option(None, help="e.g. breaking (also alerts the PO)")):
    """Inform dependent agents (release, breaking change). One notice per recipient."""
    c, ses = client()
    targets = to or c.get("/whoami")["consumers"]
    if not targets:
        fail(ClientError("no consumers registered and no --to given", "bad_request"))
    text = read_body(body, body_file)
    out = [c.post("/requests", to=t, type="notice", title=title, body=text, labels=label or []) for t in targets]
    emit({"notices": out}, "\n".join(f"{r['request']['id']} → {r['request']['to_agent']}  {notify_line(r)}" for r in out))


# ---------------------------------------------------------------- top-level verbs over artifacts
@app.command()
@guard
def propose(rid: str, title: str = typer.Option(...), option: Optional[List[str]] = typer.Option(None, help="'A: summary' (repeatable)"),
            recommend: Optional[str] = typer.Option(None, help="Recommended option id."),
            compat: Optional[List[str]] = typer.Option(None, help="'A: full|breaking' (repeatable)"),
            effort: Optional[List[str]] = typer.Option(None, help="'A: 1h' (repeatable)"),
            body: Optional[str] = typer.Option(None, help="Markdown, or '-' for stdin."), body_file: Optional[Path] = None,
            requires_po: bool = typer.Option(False, help="Needs a PO decision before acceptance (breaking change / scope)."),
            data: Optional[str] = typer.Option(None, help="Raw JSON instead of --option/--compat/--effort.")):
    """Publish a proposal on a request you received: options + recommendation."""
    c, _ = client()
    if data:
        d = json.loads(data)
    else:
        opts = opts_of(option)
        extra = {k: dict(o.split(":", 1) for o in (v or [])) for k, v in (("compat", compat), ("effort", effort))}
        for o in opts:
            for k in ("compat", "effort"):
                val = extra[k].get(o["id"])
                if val:
                    o[k] = val.strip()
        d = {"options": opts, "recommended": recommend}
    res = c.post("/artifacts", type="proposal", title=title, content=read_body(body, body_file), data=d, request_id=rid, requires_po=requires_po)
    lines = [f"{res['artifact']['id']} proposal published; {rid} → {res['request']['status']}"]
    if res.get("decision"):
        lines.append(_po_created_text(res | {"request": res["decision"]}))
    lines.append(notify_line(res))
    emit(res, "\n".join(lines))


@app.command()
@guard
def deliver(rid: str, repo: str = typer.Option(..., help="Your agent id or repo path."), commit: Optional[str] = typer.Option(None),
            branch: Optional[str] = typer.Option(None), title: Optional[str] = typer.Option(None),
            tests_passed: bool = typer.Option(False, "--tests-passed"), tests_failed: bool = typer.Option(False, "--tests-failed"),
            tests_cmd: Optional[str] = typer.Option(None, help="Command you ran."), version: Optional[str] = None,
            body: Optional[str] = typer.Option(None, help="How to integrate; '-' for stdin."), body_file: Optional[Path] = None,
            data: Optional[str] = typer.Option(None, help="Extra JSON merged into data.")):
    """Publish a delivery (repo + commit + tests you ran) on a request you received."""
    c, _ = client()
    if not (tests_passed or tests_failed):
        fail(ClientError("say what happened to the tests: --tests-passed or --tests-failed", "bad_request"))
    d = {"repo": repo, "commit": commit, "branch": branch, "version": version,
         "tests": {"cmd": tests_cmd, "result": "passed" if tests_passed else "failed"}}
    if data:
        d.update(json.loads(data))
    res = c.post("/artifacts", type="delivery", title=title or f"Delivery {commit or branch}", content=read_body(body, body_file), data=d, request_id=rid)
    emit(res, f"{res['artifact']['id']} delivery published; {rid} → {res['request']['status']}\n{notify_line(res)}")


@app.command()
@guard
def evaluate(rid: str, verdict: str = typer.Option(..., help="accepted|rejected"), title: Optional[str] = typer.Option(None),
             body: Optional[str] = typer.Option(None, help="Measured results and reason; '-' for stdin."), body_file: Optional[Path] = None,
             data: Optional[str] = typer.Option(None, help="JSON: bench, tests, consumer_commit ...")):
    """Publish your evaluation of a delivery (you are the initiator)."""
    c, _ = client()
    res = c.post("/artifacts", type="evaluation", title=title or f"Evaluation: {verdict}", content=read_body(body, body_file),
                 data=json.loads(data) if data else {}, request_id=rid, verdict=verdict)
    emit(res, f"{res['artifact']['id']} evaluation ({verdict}); {rid} → {res['request']['status']}\n{notify_line(res)}")


# ---------------------------------------------------------------- PO interaction (any agent)
def _po_created_text(res: dict) -> str:
    r = res["request"]
    if res.get("po_present"):
        return f"{r['id']} decision → PO (present)\n{notify_line(res)}"
    routed = res.get("routed_to") or r.get("routed_to")
    holder = "Show" if routed in (None, r["from_agent"]) else f"{routed} shows"
    return (f"{r['id']} decision created; po_present: false → mode: local (with {routed})\n"
            f"{holder} the question to the user in the chat, in their language, with the options and a recommendation; then either\n"
            f"  reqlane decide {r['id']} --author human --option <id> --reason \"<user's words>\"   or   reqlane handoff {r['id']}")


@app.command("ask-po")
@guard
def ask_po(title: str = typer.Option(...), body: Optional[str] = typer.Option(None, help="Context, or '-' for stdin."), body_file: Optional[Path] = None,
           kind: str = typer.Option("other", help="priority|scope_change|breaking_change|conflict|budget|access|clarification|other"),
           option: Optional[List[str]] = typer.Option(None, help="'A: text' (repeatable)"), parent: Optional[str] = typer.Option(None, help="Request this blocks."),
           blocking: bool = False):
    """Ask the Product Owner. If the PO is absent the question stays in your chat (mode: local)."""
    c, _ = client()
    res = c.post("/po/ask", title=title, body=read_body(body, body_file), kind=kind, options=opts_of(option), parent_id=parent, blocking=blocking)
    emit(res, _po_created_text(res))


@app.command()
@guard
def handoff(rid: str):
    """Hand a local decision over to the PO queue (the user said "hand it to the PO")."""
    c, _ = client()
    res = c.post(f"/requests/{rid}/handoff")
    emit(res, f"{rid} handed over to PO ({'present' if res['po_present'] else 'absent — it waits in the PO inbox'})\n{notify_line(res)}")


@app.command()
@guard
def decide(rid: str, option: Optional[str] = typer.Option(None), decision: Optional[str] = typer.Option(None, help="Free-text decision if no option."),
           reason: str = typer.Option(..., help="For --author human: the user's own words."), affected: Optional[str] = typer.Option(None, help="Comma-separated agents."),
           author: str = typer.Option("po", help="po | human (human: the user answered in the chat)")):
    """Record a decision. PO session: --author po. Local question answered by the user in your chat: --author human."""
    c, _ = client(human=(author == "human"))
    if not option and not decision:
        fail(ClientError("--option or --decision is required", "bad_request"))
    res = c.post(f"/requests/{rid}/decide", decision=decision or f"option {option}", option=option, reason=reason, affected=csv(affected), author=author)
    att = "" if res.get("attested") else " — UNATTESTED: recorded from an agent session; the PO dashboard lists it"
    emit(res, f"{res['artifact']['id']} decision recorded ({author}{att}); {rid} closed\n{notify_line(res)}")


# ---------------------------------------------------------------- PO-only
@po_app.command("dashboard")
@guard
def po_dashboard():
    """Everything open across projects, ordered by urgency."""
    c, _ = client()
    res = c.get("/po/dashboard")
    emit(res, fmt.dashboard_view(res))


@po_app.command("policy")
@guard
def po_policy(set_json: Optional[str] = typer.Option(None, "--set", help="JSON policy (human only)."), set_file: Optional[Path] = None):
    """Show or set the delegation policy."""
    c, _ = client(human=bool(set_json or set_file))
    if set_json or set_file:
        res = c.put("/po/policy", json.loads(set_file.read_text(encoding="utf-8")) if set_file else json.loads(set_json))
    else:
        res = c.get("/po/policy")
    emit(res, f"mode: {res.get('mode', 'hybrid')}\ndecide alone: {', '.join(res.get('auto_decide', [])) or '-'}\n"
              f"ask the user first: {', '.join(res.get('always_ask_human', [])) or '-'}\notherwise: {res.get('default', 'ask_human')}")


@po_app.command("task")
@guard
def po_task(to: str = typer.Argument(...), title: str = typer.Option(...), body: Optional[str] = typer.Option(None), body_file: Optional[Path] = None,
            depends_on: Optional[str] = typer.Option(None, help="Comma-separated request ids."), due: Optional[str] = None):
    """Assign work to an agent (PO only)."""
    c, _ = client()
    res = c.post("/tasks", to=to, title=title, body=read_body(body, body_file), depends_on=csv(depends_on), due_at=due)
    emit(res, f"{res['request']['id']} task → {to}\n{notify_line(res)}")


@po_app.command("delegate")
@guard
def po_delegate(rid: str, reason: str = typer.Option(..., help="Why this is the owner's call.")):
    """PO sends a decision back: it is an engineering question for the owner."""
    c, _ = client()
    res = c.post(f"/requests/{rid}/delegate", reason=reason)
    emit(res, f"{rid} delegated back\n{notify_line(res)}")


# ---------------------------------------------------------------- artifacts & agreements
@art_app.command("publish")
@guard
def art_publish(type_: str = typer.Option(..., "--type", help="investigation|note|proposal|delivery|evaluation"),
                title: str = typer.Option(...), request: Optional[str] = typer.Option(None),
                body: Optional[str] = typer.Option(None), body_file: Optional[Path] = None,
                data: Optional[str] = typer.Option(None, help="JSON"), verdict: Optional[str] = None, requires_po: bool = False, supersedes: Optional[str] = None):
    """Publish any artifact (low-level; prefer propose/deliver/evaluate)."""
    c, _ = client()
    res = c.post("/artifacts", type=type_, title=title, content=read_body(body, body_file), data=json.loads(data) if data else {}, request_id=request,
                 supersedes=supersedes, verdict=verdict, requires_po=requires_po)
    emit(res, f"{res['artifact']['id']} {type_} published" + (f"; {request} → {res['request']['status']}" if res.get("request") else "") + f"\n{notify_line(res)}")


@art_app.command("show")
@guard
def art_show(aid: str):
    """Show an artifact."""
    c, _ = client()
    a = c.get(f"/artifacts/{aid}")
    emit(a, f"{a['id']} {a['type']} {fmt.q(a['title'])} by {a['author_agent']} v{a['version']} {a['created_at'][:19]}\n"
            f"request: {a.get('request_id') or '-'}\ndata: {json.dumps(a.get('data') or {}, ensure_ascii=False)}\n--- content (quoted) ---\n{a['content']}")


@art_app.command("list")
@guard
def art_list(type_: Optional[str] = typer.Option(None, "--type"), request: Optional[str] = None, agent: Optional[str] = None, limit: int = 50):
    """List artifacts."""
    c, _ = client()
    res = c.get("/artifacts", type=type_, request_id=request, agent=agent, limit=limit)
    emit(res, "\n".join(f"{a['id']} {a['type']:<13} {a.get('request_id') or '-':<9} {a['author_agent']:<12} {fmt.q(a['title'], 60)}" for a in res["artifacts"]) or "none")


@agr_app.command("publish")
@guard
def agr_publish(title: str = typer.Option(...), parties: str = typer.Option(..., help="Comma-separated agents."),
                body: Optional[str] = typer.Option(None), body_file: Optional[Path] = None, supersedes: Optional[str] = None):
    """Record a cross-project rule. Parties acknowledge with `reqlane agreement ack`."""
    c, _ = client()
    res = c.post("/agreements", title=title, content=read_body(body, body_file), parties=csv(parties), supersedes=supersedes)
    g = res["agreement"]
    emit(res, f"{g['id']} published; pending ack: {', '.join(set(g['parties']) - set(g['acknowledged'])) or 'none'}\n{notify_line(res)}")


@agr_app.command("ack")
@guard
def agr_ack(gid: str):
    """Acknowledge an agreement you are a party of."""
    c, _ = client()
    g = c.post(f"/agreements/{gid}/ack")
    emit(g, f"{gid} acknowledged by {', '.join(g['acknowledged'])} / {', '.join(g['parties'])}")


@agr_app.command("list")
@guard
def agr_list(all_: bool = typer.Option(False, "--all", help="Include superseded.")):
    """Active agreements (constraints for proposals)."""
    c, _ = client()
    res = c.get("/agreements", all=all_)
    lines = []
    for g in res["agreements"]:
        lines.append(f"{g['id']} [{g['status']}] {fmt.q(g['title'])} parties: {', '.join(g['parties'])}  acked: {', '.join(g['acknowledged']) or '-'}")
        lines.extend("    " + x for x in g["content"].strip().splitlines()[:6])
    emit(res, "\n".join(lines) or "no agreements")


# ---------------------------------------------------------------- misc
@perm_app.command("check")
@guard
def perm_check(repo: str, op: str = typer.Option("write", help="read|write")):
    """May this agent read/write that repo (agent id or path)? Advisory — the runtime enforces."""
    c, _ = client()
    res = c.get("/perm", repo=repo, op=op)
    emit(res, f"{op} {repo}: {'allowed' if res['allowed'] else 'DENIED'} ({res['source']})")
    if not res["allowed"]:
        raise typer.Exit(4)


@app.command()
@guard
def resolve(ref: str):
    """Resolve an @ref to an absolute path."""
    res = Client().get("/resolve", ref=ref)
    if not res.get("known") or res.get("error"):
        emit(res, res.get("error") or f"unknown repo in {ref}")
        raise typer.Exit(5)
    loc = res.get("abs_path") or res.get("root")
    if res.get("line_start"):
        loc += f":{res['line_start']}" + (f"-{res['line_end']}" if res.get("line_end") else "")
    emit(res, loc + ("" if res.get("exists", True) else "  (missing)"))


@app.command()
@guard
def search(query: str, scope: str = typer.Option("all", help="all|requests|artifacts|messages|agreements"), limit: int = 20):
    """Full-text search over requests, messages, artifacts, agreements."""
    res = Client().get("/search", q=query, scope=scope, limit=limit)
    emit(res, "\n".join(f"{r['entity_id']:<9} {r['entity_type']:<10} {fmt.q(r['title'], 50)}  {fmt.one_line(r['snippet'], 80)}" for r in res["results"]) or "no matches")


@app.command()
@guard
def export(handover: bool = typer.Option(False, help="Block for docs/HANDOVER.md."), req: Optional[str] = typer.Option(None, help="Export one thread as markdown.")):
    """Export for handover documents or archives."""
    c, ses = client()
    if req:
        d = c.get(f"/requests/{req}", include="messages,artifacts")
        typer.echo("# " + fmt.one_line(d["request"]["title"]) + "\n\n```\n" + fmt.request_view(d, None) + "\n```")
        return
    typer.echo(inst.handover_block(me_of(ses), c.get("/requests", box="all", limit=100)["requests"]))


@app.command()
def protocol(runtime: Optional[str] = typer.Option(None, help="generic|claude-code|codex|gemini|cursor"),
             fmt_: str = typer.Option("text", "--format", help="text|json")):
    """Print the protocol card (the rules handed to agents at connect)."""
    ses = find_session()
    who = None
    if ses:
        try:
            who = Client(session_token=ses["token"], autostart=False).get("/whoami")
        except ClientError:
            who = None
    if fmt_ == "json":
        cmds = []

        def walk(t, prefix=""):
            for cmd in t.registered_commands:
                cmds.append({"command": (prefix + (cmd.name or cmd.callback.__name__)).strip(), "help": (cmd.callback.__doc__ or "").strip()})
            for g in t.registered_groups:
                walk(g.typer_instance, prefix + g.name + " ")
        walk(app)
        typer.echo(json.dumps({"protocol": PROTOCOL_VERSION, "core": cards.CORE, "commands": cmds}, ensure_ascii=False, indent=1))
    else:
        typer.echo(cards.card(who, runtime))


@app.command()
def install(runtime: str = typer.Option("claude-code", help="claude-code|codex|gemini|cursor|generic"),
            dir_: Optional[Path] = typer.Option(None, "--dir", help="codex/gemini/cursor: project directory for the instruction file."),
            hooks: bool = typer.Option(False, help="claude-code: merge hooks into ~/.claude/settings.json (backup is made)."),
            yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation.")):
    """Install the runtime adapter: 3-line pointer, thin skill, hooks. Shows what it writes first."""
    plan = inst.plan(runtime, dir_, hooks)
    if plan:
        typer.echo("reqlane install will write:")
        for action, path, desc in plan:
            typer.echo(f"  {action:<12} {path}  — {desc}")
        if not yes and not typer.confirm("Proceed?", default=False):
            raise typer.Exit(1)
    who = None
    ses = find_session()
    if ses:
        try:
            who = Client(session_token=ses["token"], autostart=False).get("/whoami")
        except ClientError:
            pass
    for line in inst.install(runtime, dir_, hooks, who):
        typer.echo(line)
    typer.echo("undo: reqlane uninstall --runtime " + runtime)


@app.command()
def uninstall(runtime: str = typer.Option("claude-code"), dir_: Optional[Path] = typer.Option(None, "--dir")):
    """Remove what `reqlane install` wrote (pointer block, skill, hooks)."""
    for line in inst.uninstall(runtime, dir_):
        typer.echo(line)


# ---------------------------------------------------------------- hooks (never fail, print little)
def _quiet(fn):
    @functools.wraps(fn)
    def w(*a, **k):
        try:
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
            fn(*a, **k)
        except Exception:  # noqa: BLE001
            pass
    return w


@hook_app.command("session-start")
@_quiet
def hook_session_start():
    """SessionStart: identity, protocol card, inbox summary — only in a connected directory."""
    data = _hook_input()
    if data.get("cwd"):
        try:
            os.chdir(data["cwd"])
        except OSError:
            pass
    ses = find_session(exact=True)
    if not ses:
        # Auto-connect: a directory that was registered before reconnects on its own (new session, /clear, resume).
        if not config.db_path().exists():
            return
        c = Client(autostart=True)
        sug = c.get("/agent-for-cwd", cwd=str(Path.cwd())).get("agent")
        if not sug:
            typer.echo("[reqlane] This repository is not registered in Reqlane. The user can type `/reqlane connect` to register it (or `/reqlane po` for the product owner).")
            return
        res = c.post("/sessions/connect", agent=sug, kind="project", cwd=str(Path.cwd()), name=None, runtime="claude-code",
                     runtime_ref=claude_session_name(), pid=os.getppid(), depends_on=[], description=None)
        s_ = res["session"]
        save_session(Path.cwd(), None, {"token": res["token"], "agent": s_["agent_id"], "session_id": s_["id"], "hook_cursor": res["cursor"]})
        ses = find_session(exact=True)
        typer.echo(f"[reqlane] Reconnected automatically as agent {s_['agent_id']} (session {s_['id']}). "
                   f"Call ListAgents and run `reqlane address \"NAME [ref]\"` with its `This session is …` line so others can wake you.")
    c = Client(session_token=ses["token"], autostart=False)
    who = c.get("/whoami")
    ib = c.get("/inbox")
    card = c.get("/protocol", runtime=who.get("runtime"))["card"]
    typer.echo(f"[reqlane] Agent: {who['agent']} ({who['kind']}), session {who['session']}. PO: {'present' if who['po_present'] else 'absent'}.")
    hint = _exe_hint()
    if hint:
        typer.echo(hint)
    typer.echo(agents_block(c))
    typer.echo(card.rstrip())
    typer.echo(cards.UNTRUSTED_BANNER)
    typer.echo("\n".join(fmt.inbox_view(ib, who["agent"]).splitlines()[:15]))


def agents_block(c: Client) -> str:
    """Who is in the workspace: agent, repositories, online/offline — so the agent knows whom it can ask."""
    try:
        ag = c.get("/agents")["agents"]
    except ClientError:
        return ""
    if not ag:
        return "[reqlane] no other agents registered yet."
    lines = ["[reqlane] Agents in this workspace (you can ask any of them or order work from them):"]
    for a in ag:
        repos = ", ".join(Path(r).name for r in a.get("repos") or []) or "-"
        state = "online" if a["sessions"] else "offline (will read its inbox when it connects)"
        kind = " — Product Owner" if a["kind"] == "product_owner" else ""
        lines.append(f"  {a['id']:<14} repo: {repos:<24} {state}{kind}")
    return "\n".join(lines)


def _exe_hint() -> str:
    import shutil
    if shutil.which("reqlane"):
        return ""
    return f'[reqlane] `reqlane` is not on PATH in this shell; call it as "{inst.aw_executable()}" (same arguments).'


def claude_session_name() -> str | None:
    """Name of the current Claude Code session (the address for cross-session messages), if any."""
    import shutil
    import subprocess
    sid = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not sid:
        return None
    exe = os.environ.get("CLAUDE_CODE_EXECPATH") or shutil.which("claude")
    if not exe:
        return None
    try:
        from ..client.http import no_window
        out = subprocess.run([exe, "agents", "--json"], capture_output=True, text=True, timeout=8, stdin=subprocess.DEVNULL, **no_window()).stdout
        for a in json.loads(out or "[]"):
            if a.get("sessionId") == sid:
                return a.get("name")
    except Exception:  # noqa: BLE001
        return None
    return None


def _slug(name: str) -> str:
    import re as _re
    s = _re.sub(r"[^a-z0-9_.-]+", "-", name.lower()).strip("-.")
    return s[:40] or "agent"


def _hook_input() -> dict:
    """Claude Code passes hook input as JSON on stdin (session_id, cwd, prompt, ...)."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001
        return {}


def _human_command(argv: list[str]) -> bool:
    """`/reqlane start|connect|po|depends|status|disconnect` typed by the user in chat.

    The prompt hook fires only on human input, so this is the human principal: the hook performs
    the action with the human token and prints the result for the agent to relay."""
    if not argv:
        return False
    cmd, args = argv[0], argv[1:]
    cwd = Path.cwd()
    human = config.human_token()
    out: list[str] = []
    try:
        if cmd == "start":
            c = Client(human=human)
            h = c.get("/health")
            ag = c.get("/agents")["agents"]
            out.append(f"[reqlane] workspace running at {c.base_url} (v{h['version']}); agents: " + (", ".join(a['id'] + ('*' if a['sessions'] else '') for a in ag) or "none yet") + ".")
            out.append("Next: `/reqlane connect` in each project's session, `/reqlane po` in the product owner's session.")
        elif cmd in ("connect", "po"):
            name = None
            deps: list[str] = []
            kind = "po" if cmd == "po" else "project"
            it = iter(args)
            for a in it:
                if a in ("--depends-on", "--depends", "-d"):
                    deps = csv(next(it, ""))
                elif a == "--po":
                    kind = "po"
                elif not a.startswith("-") and name is None:
                    name = a
            name = _slug(name or ("po" if kind == "po" else cwd.name))
            runtime = "claude-code"
            c = Client(human=human)
            res = c.post("/sessions/connect", agent=name, kind=kind, cwd=str(cwd), name=None, runtime=runtime, runtime_ref=claude_session_name(),
                         pid=os.getppid(), depends_on=deps, description=None)
            ses = res["session"]
            save_session(cwd, None, {"token": res["token"], "agent": ses["agent_id"], "session_id": ses["id"], "hook_cursor": res["cursor"]})
            who = res["who"]
            out.append(f"[reqlane] connected by the user: this session is agent **{who['agent']}** ({who['kind']}), session {ses['id']}"
                       f"{(' (Claude session ' + ses['runtime_ref'] + ')') if ses.get('runtime_ref') else ''}. PO: {'present' if res['po_present'] else 'absent'}.")
            if who["kind"] != "product_owner":
                out.append(f"repos: {', '.join(who['repos'])}  depends_on: {', '.join(who['depends_on']) or '-'}  consumers: {', '.join(who['consumers']) or '-'}")
            out.append("Do NOT run `reqlane connect` yourself — it is done. Tell the user in one line, then follow the card below.")
            out.append("FIRST: call your ListAgents tool, take its line `This session is NAME [ref]` and run "
                       "`reqlane address \"NAME [ref]\"` — that is how other agents will wake you.")
            out.append(agents_block(c))
            out.append("")
            out.append(res["card"].rstrip())
            out.append(cards.UNTRUSTED_BANNER)
            out.append(fmt.inbox_view(res["inbox"], who["agent"]))
        elif cmd == "depends":
            ses = find_session()
            if not ses:
                out.append("[reqlane] not connected here; `/reqlane connect` first.")
            else:
                res = Client(human=human).post(f"/agents/{ses['agent']}", depends_on=csv(" ".join(args).replace(" ", ",")))
                out.append(f"[reqlane] {ses['agent']} now depends on: {', '.join(res['depends_on']) or '-'}. Tell the user; nothing else to do.")
        elif cmd == "ui":
            import webbrowser
            c = Client(human=human)
            c.get("/health")
            url = f"{c.base_url}/ui?token={config.token()}"
            webbrowser.open(url)
            out.append(f"[reqlane] UI opened in the browser: {url} . Tell the user; nothing else to do.")
        elif cmd == "status":
            c = Client(autostart=False)
            if not daemon_alive(c.base_url):
                out.append("[reqlane] workspace is not running; `/reqlane start`.")
            else:
                ag = c.get("/agents")["agents"]
                out.append("[reqlane] agents: " + (", ".join(f"{a['id']} ({', '.join(s.get('name') or s['id'] for s in a['sessions']) or 'offline'}, open→{a['open_requests_to']})" for a in ag) or "none") + ". Relay this to the user.")
        elif cmd in ("disconnect", "stop"):
            ses = find_session(exact=True)
            if ses:
                Client(session_token=ses["token"], autostart=False).post("/sessions/disconnect")
                drop_session(ses)
                out.append(f"[reqlane] session of {ses['agent']} disconnected. Tell the user; nothing else to do.")
            else:
                out.append("[reqlane] nothing connected in this directory.")
        else:
            return False
    except ClientError as e:
        out.append(f"[reqlane] {cmd} failed: {e} [{e.code}]" + (f" — {e.hint}" if e.hint else "") + ". Tell the user; do not retry yourself.")
    hint = _exe_hint()
    if hint:
        out.append(hint)
    typer.echo("\n".join(out))
    return True


@hook_app.command("prompt")
@_quiet
def hook_prompt():
    """UserPromptSubmit: handle `/reqlane start|connect|po|...` typed by the user; else one line per new event."""
    data = _hook_input()
    if data.get("cwd"):
        try:
            os.chdir(data["cwd"])
        except OSError:
            pass
    prompt = (data.get("prompt") or "").strip()
    if prompt.startswith("/reqlane"):
        argv = prompt.split()[1:]
        if argv and argv[0] in ("start", "connect", "po", "depends", "status", "disconnect", "stop", "ui") and _human_command(argv):
            return
    ses = find_session()
    if not ses:
        return
    c = Client(session_token=ses["token"], autostart=False)
    res = c.get("/events", since=int(ses.get("hook_cursor") or 0), limit=20)
    if res["events"]:
        update_session(ses, hook_cursor=res["cursor"])
        typer.echo(f"[reqlane] {len(res['events'])} new — run `reqlane inbox`. " + cards.UNTRUSTED_BANNER)
        for e in res["events"][:5]:
            typer.echo("  " + fmt.event_line(e))


@hook_app.command("stop")
@_quiet
def hook_stop():
    """Stop: heartbeat + one line for the human about what this agent is waiting on / must handle."""
    ses = find_session()
    if not ses:
        return
    ib = Client(session_token=ses["token"], autostart=False).get("/inbox")
    if ib.get("blocking"):
        typer.echo(f"[reqlane] {len(ib['blocking'])} blocking request(s) await you: " + ", ".join(r["id"] for r in ib["blocking"]))
    waiting = [r for r in ib.get("waiting_on_others", []) if r.get("blocking")]
    if waiting:
        typer.echo("[reqlane] waiting on: " + ", ".join(f"{r['id']} ({r['to_agent']})" for r in waiting))


@hook_app.command("session-end")
@_quiet
def hook_session_end():
    """SessionEnd: mark the session gone so PO presence and claims are accurate."""
    ses = find_session(exact=True)
    if not ses:
        return
    Client(session_token=ses["token"], autostart=False).post("/sessions/disconnect")
    drop_session(ses)


@hook_app.command("pre-compact")
@_quiet
def hook_pre_compact():
    """PreCompact: refresh the Reqlane block in docs/HANDOVER.md (inside your repo only)."""
    ses = find_session()
    if not ses:
        return
    c = Client(session_token=ses["token"], autostart=False)
    who = c.get("/whoami")
    block = inst.handover_block(ses["agent"], c.get("/requests", box="all", limit=100)["requests"])
    f = inst.find_handover(Path.cwd(), who.get("repos") or [])
    if f:
        inst.write_handover(f, block)
        typer.echo(f"[reqlane] open requests written to {f}")
    else:
        typer.echo(block)


if __name__ == "__main__":
    app()
