"""HTTP API of the daemon. Wire protocol for CLI, MCP and any other client.

Auth headers: `Authorization: Bearer <daemon token>` (every local client), `X-Reqlane-Session: <session token>`
(after connect), `X-Reqlane-Human: <human token>` (only the human principal: registration, policy, attestation).
"""
from __future__ import annotations

import asyncio
import hmac
import json
import threading
import time
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from pathlib import Path

from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .. import PROTOCOL_VERSION, __version__, config
from ..core import cards
from ..core.lifecycle import TransitionError
from ..core.service import Service, ServiceError


def create_app(service: Service | None = None, api_token: str | None = None, human_token: str | None = None) -> FastAPI:
    svc = service or Service(config.db_path())
    api_token = api_token if api_token is not None else config.token()
    human_token = human_token if human_token is not None else config.human_token()
    app = FastAPI(title="Reqlane", version=__version__)
    app.state.service = svc
    started_at = time.time()
    cond = asyncio.Condition()
    loop_ref: dict[str, asyncio.AbstractEventLoop | None] = {"loop": None}

    async def _wake():
        async with cond:
            cond.notify_all()

    def on_event():
        loop = loop_ref["loop"]
        if loop and loop.is_running():
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(_wake()))

    svc.on_event = on_event

    @app.on_event("startup")
    async def _startup():
        loop_ref["loop"] = asyncio.get_running_loop()

    def _err(e: ServiceError) -> HTTPException:
        return HTTPException(e.status, {"error": str(e), "code": e.code, "hint": e.hint})

    def bearer(authorization: str | None = Header(default=None)) -> None:
        if not authorization or not hmac.compare_digest(authorization, f"Bearer {api_token}"):
            raise HTTPException(401, {"error": "bad token", "code": "unauthorized", "hint": "token lives in $REQLANE_HOME/token"})

    def human(x_reqlane_human: str | None = Header(default=None), _=Depends(bearer)) -> bool:
        return bool(x_reqlane_human) and hmac.compare_digest(x_reqlane_human, human_token)

    def session(x_reqlane_session: str | None = Header(default=None), _=Depends(bearer)) -> dict:
        try:
            return svc.auth(x_reqlane_session)
        except ServiceError as e:
            raise _err(e)

    @app.exception_handler(ServiceError)
    async def _se(_: Request, e: ServiceError):
        return JSONResponse({"error": str(e), "code": e.code, "hint": e.hint, "protocol": PROTOCOL_VERSION}, status_code=e.status)

    @app.exception_handler(TransitionError)
    async def _te(_: Request, e: TransitionError):
        return JSONResponse({"error": str(e), "code": e.code, "protocol": PROTOCOL_VERSION}, status_code=409)

    @app.middleware("http")
    async def _proto(request: Request, call_next):
        resp = await call_next(request)
        resp.headers["X-Reqlane-Protocol"] = str(PROTOCOL_VERSION)
        resp.headers["X-Reqlane-Version"] = __version__
        return resp

    async def body_of(request: Request) -> dict[str, Any]:
        raw = await request.body()
        if len(raw) > 1_000_000:
            raise HTTPException(413, {"error": "body too large", "code": "bad_request"})
        if not raw:
            return {}
        try:
            d = json.loads(raw)
        except ValueError:
            raise HTTPException(400, {"error": "invalid JSON body", "code": "bad_request"})
        if not isinstance(d, dict):
            raise HTTPException(400, {"error": "JSON object expected", "code": "bad_request"})
        return d

    # ---- public / identity
    @app.get("/health")
    def health():
        return {"ok": True, "version": __version__, "protocol": PROTOCOL_VERSION, "po_present": svc.po_present()}

    @app.get("/agents", dependencies=[Depends(bearer)])
    def agents():
        return {"agents": svc.list_agents(), "po_present": svc.po_present()}

    @app.get("/agent-for-cwd", dependencies=[Depends(bearer)])
    def agent_for_cwd(cwd: str):
        return {"agent": svc.agent_for_cwd(cwd)}

    @app.get("/protocol", dependencies=[Depends(bearer)])
    def protocol(runtime: str | None = None, x_reqlane_session: str | None = Header(default=None)):
        who = None
        if x_reqlane_session:
            try:
                who = svc.whoami(svc.auth(x_reqlane_session))
            except ServiceError:
                who = None
        return {"card": cards.card(who, runtime), "core": cards.CORE, "role": cards.role_text(who) if who else None}

    @app.post("/sessions/connect")
    async def connect(request: Request, is_human: bool = Depends(human)):
        b = await body_of(request)
        res = svc.connect(b.get("agent"), b.get("kind") or "project", b.get("cwd") or ".", b.get("name"), b.get("runtime"),
                          b.get("runtime_ref"), b.get("pid"), b.get("depends_on") or [], b.get("description"), is_human)
        res["card"] = cards.card(res["who"], b.get("runtime"))
        return res

    @app.post("/sessions/disconnect")
    def disconnect(s: dict = Depends(session)):
        svc.disconnect(s)
        return {"ok": True}

    @app.get("/whoami")
    def whoami(s: dict = Depends(session)):
        return svc.whoami(s)

    @app.post("/sessions/me/address")
    async def set_address(request: Request, s: dict = Depends(session)):
        b = await body_of(request)
        return svc.set_session_ref(s, b.get("runtime_ref", ""))

    @app.post("/agents/{aid}")
    async def update_agent(aid: str, request: Request, is_human: bool = Depends(human)):
        if not is_human:
            raise HTTPException(403, {"error": "human token required", "code": "forbidden", "hint": "run from your terminal or set REQLANE_HUMAN_TOKEN"})
        b = await body_of(request)
        return svc.update_agent(aid, b.get("depends_on"), b.get("description"), b.get("add_repo"))

    # ---- requests
    @app.post("/requests")
    async def create_request(request: Request, s: dict = Depends(session), is_human: bool = Depends(human)):
        b = await body_of(request)
        as_agent = b.get("as_agent")
        if as_agent and not is_human:
            raise HTTPException(403, {"error": "speaking as another agent needs the human token", "code": "forbidden"})
        return svc.create_request(s, b.get("to", ""), b.get("type", "question"), b.get("title", ""), b.get("body", ""), b.get("goal"),
                                  b.get("priority", "normal"), bool(b.get("blocking")), b.get("cc"), b.get("parent_id"), b.get("labels"),
                                  b.get("due_at"), b.get("idem"), b.get("kind"), b.get("options"), as_agent)

    @app.get("/requests")
    def list_requests(box: str = "all", status: str | None = None, type: str | None = None, other: str | None = None,
                      since: str | None = None, limit: int = 50, all: bool = False, s: dict = Depends(session)):
        return {"requests": svc.list_requests(s["agent_id"], box, status, type, other, since, limit, open_only=not all)}

    @app.get("/requests/{rid}")
    def get_request(rid: str, include: str = "messages,artifacts", since: int | None = None, s: dict = Depends(session)):
        return svc.get_request(rid, s["agent_id"], tuple(x for x in include.split(",") if x), since)

    @app.post("/requests/{rid}/claim")
    def claim(rid: str, s: dict = Depends(session)):
        return svc.claim(s, rid)

    @app.post("/requests/{rid}/messages")
    async def reply(rid: str, request: Request, s: dict = Depends(session), is_human: bool = Depends(human)):
        b = await body_of(request)
        as_agent = b.get("as_agent")
        if as_agent and not is_human:
            raise HTTPException(403, {"error": "speaking as another agent needs the human token", "code": "forbidden"})
        return svc.reply(s, rid, b.get("body", ""), b.get("type", "comment"), as_agent)

    @app.post("/requests/{rid}/action")
    async def action(rid: str, request: Request, s: dict = Depends(session)):
        b = await body_of(request)
        return svc.set_status(s, rid, b.get("action", ""), b.get("reason"), b.get("to"))

    @app.post("/requests/{rid}/accept_proposal")
    async def accept_proposal(rid: str, request: Request, s: dict = Depends(session)):
        b = await body_of(request)
        return svc.accept_proposal(s, rid, b.get("option"), b.get("notes"))

    @app.post("/requests/{rid}/escalate")
    async def escalate(rid: str, request: Request, s: dict = Depends(session)):
        b = await body_of(request)
        return svc.escalate(s, rid, b.get("question", ""), b.get("kind", "conflict"), b.get("options") or [])

    @app.post("/requests/{rid}/handoff")
    def handoff(rid: str, s: dict = Depends(session)):
        return svc.handoff(s, rid)

    @app.post("/requests/{rid}/decide")
    async def decide(rid: str, request: Request, s: dict = Depends(session), is_human: bool = Depends(human)):
        b = await body_of(request)
        return svc.decide(s, rid, b.get("decision", ""), b.get("option"), b.get("reason", ""), b.get("affected") or [], b.get("author") or "po", is_human)

    @app.post("/requests/{rid}/delegate")
    async def delegate(rid: str, request: Request, s: dict = Depends(session)):
        b = await body_of(request)
        return svc.delegate(s, rid, b.get("reason", ""))

    # ---- PO
    @app.post("/po/ask")
    async def ask_po(request: Request, s: dict = Depends(session)):
        b = await body_of(request)
        return svc.ask_po(s, b.get("title", ""), b.get("body", ""), b.get("kind", "other"), b.get("options") or [], b.get("parent_id"), bool(b.get("blocking")))

    @app.post("/tasks")
    async def create_task(request: Request, s: dict = Depends(session)):
        b = await body_of(request)
        return svc.create_task(s, b.get("to", ""), b.get("title", ""), b.get("body", ""), b.get("depends_on") or [], b.get("due_at"))

    @app.get("/po/dashboard")
    def dashboard(s: dict = Depends(session)):
        return svc.dashboard(s["agent_id"])

    @app.get("/po/policy")
    def get_policy(s: dict = Depends(session)):
        return svc.get_policy(s["agent_id"])

    @app.put("/po/policy")
    async def set_policy(request: Request, s: dict = Depends(session), is_human: bool = Depends(human)):
        return svc.set_policy(s, await body_of(request), is_human)

    # ---- artifacts & agreements
    @app.post("/artifacts")
    async def publish_artifact(request: Request, s: dict = Depends(session)):
        b = await body_of(request)
        return svc.publish_artifact(s, b.get("type", ""), b.get("title", ""), b.get("content", ""), b.get("data"), b.get("request_id"),
                                    b.get("supersedes"), b.get("verdict"), bool(b.get("requires_po")), b.get("po_question"))

    @app.get("/artifacts")
    def list_artifacts(type: str | None = None, request_id: str | None = None, agent: str | None = None, limit: int = 50, s: dict = Depends(session)):
        return {"artifacts": svc.list_artifacts(type, request_id, agent, limit)}

    @app.get("/artifacts/{aid}")
    def get_artifact(aid: str, s: dict = Depends(session)):
        return svc.get_artifact(aid)

    @app.post("/agreements")
    async def publish_agreement(request: Request, s: dict = Depends(session)):
        b = await body_of(request)
        return svc.publish_agreement(s, b.get("title", ""), b.get("content", ""), b.get("parties") or [], b.get("supersedes"))

    @app.post("/agreements/{gid}/ack")
    def ack_agreement(gid: str, s: dict = Depends(session)):
        return svc.ack_agreement(s, gid)

    @app.get("/agreements")
    def list_agreements(party: str | None = None, all: bool = False, s: dict = Depends(session)):
        return {"agreements": svc.list_agreements(party, all)}

    # ---- inbox & events (async: waiting costs no thread)
    @app.get("/inbox")
    def inbox(s: dict = Depends(session)):
        return svc.inbox(s)

    @app.get("/events")
    async def events(since: int | None = None, request_id: str | None = None, wait: int = 0, limit: int = 100, own: bool = False,
                     s: dict = Depends(session)):
        deadline = time.monotonic() + min(max(wait, 0), 600)
        while True:
            res = await run_in_threadpool(svc.events, s, since, request_id, limit, own)
            if res["events"] or time.monotonic() >= deadline:
                return {**res, "timeout": not res["events"]}
            remaining = deadline - time.monotonic()
            async with cond:
                try:
                    await asyncio.wait_for(cond.wait(), timeout=min(remaining, 15))
                except asyncio.TimeoutError:
                    pass

    @app.get("/events/stream")
    async def stream(since: int | None = None, s: dict = Depends(session), last_event_id: int | None = Header(default=None)):
        start = last_event_id if last_event_id is not None else since

        async def gen():
            cursor = start
            while True:
                res = await run_in_threadpool(svc.events, s, cursor, None, 100, False, False)
                for r in res["events"]:
                    cursor = r["id"]
                    yield f"id: {r['id']}\nevent: {r['type']}\ndata: {json.dumps(r, ensure_ascii=False)}\n\n"
                if not res["events"]:
                    yield ": keepalive\n\n"
                    async with cond:
                        try:
                            await asyncio.wait_for(cond.wait(), timeout=15)
                        except asyncio.TimeoutError:
                            pass
        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.post("/ack")
    async def ack(request: Request, s: dict = Depends(session)):
        b = await body_of(request)
        return {"acked": svc.ack_events(s, b.get("event_ids"), b.get("request_id"), bool(b.get("all")))}

    # ---- misc
    @app.get("/perm")
    def perm(repo: str, op: str = "write", s: dict = Depends(session)):
        return svc.check_permission(s["agent_id"], repo, op)

    @app.get("/resolve", dependencies=[Depends(bearer)])
    def resolve(ref: str):
        return svc.resolve(ref)

    @app.get("/search", dependencies=[Depends(bearer)])
    def search(q: str, scope: str = "all", limit: int = 20):
        return {"results": svc.search(q, scope, limit)}

    # ---- web UI (localhost; the page carries the daemon token as a query parameter)
    ui_html = (Path(__file__).with_name("ui.html")).read_text(encoding="utf-8")

    @app.get("/ui", response_class=HTMLResponse)
    def ui():
        return ui_html

    @app.get("/ui/data")
    def ui_data(token: str = ""):
        if not hmac.compare_digest(token, api_token):
            raise HTTPException(401, {"error": "bad token", "code": "unauthorized", "hint": "open the UI with `reqlane ui`"})
        import os
        data = svc.overview()
        reqs = data["requests"]
        open_ = [r for r in reqs if r["status"] not in ("closed", "declined", "withdrawn", "acknowledged", "wont_do")]
        data["workspace"] = {
            "version": __version__, "protocol": PROTOCOL_VERSION, "port": config.port(), "pid": os.getpid(),
            "uptime_s": int(time.time() - started_at), "db": str(config.db_path()), "home": str(config.home()),
            "sessions_online": sum(len(a["sessions"]) for a in data["agents"]), "agents": len(data["agents"]),
            "open": len(open_), "blocking": sum(1 for r in open_ if r["blocking"]),
            "awaiting_decision": sum(1 for r in open_ if r["type"] == "decision"),
            "last_event_at": data["recent_events"][-1]["created_at"] if data["recent_events"] else None,
        }
        return data

    @app.post("/admin/shutdown", dependencies=[Depends(bearer)])
    def shutdown():
        """Exit the daemon (clients call this when their version differs; the next call restarts it)."""
        import os
        threading.Timer(0.3, lambda: os._exit(0)).start()
        return {"ok": True, "version": __version__}

    @app.post("/admin/tick", dependencies=[Depends(bearer)])
    def tick():
        return svc.tick()

    def ticker():
        while True:
            time.sleep(30)
            try:
                svc.tick()
            except Exception:  # noqa: BLE001 - keep the daemon alive
                pass

    threading.Thread(target=ticker, daemon=True).start()
    return app
