"""Business logic. One instance per daemon; every public method is short and transactional.

Principals:
- shared daemon token: any local client (agents included) — can act as an already registered
  agent from that agent's own directory;
- human token: registers agents, changes policy, attests decisions, speaks "as" an agent.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import threading
import unicodedata
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Callable

from . import lifecycle as lc
from .db import connect, j, next_id, now, parse_ts, row_to_dict
from .. import config

REF_RE = re.compile(r"@([\w.-]+)(?:@([0-9a-fA-F]{6,40}))?(?:[/!]([^\s:,)]+))?(?::(\d+)(?:-(\d+))?)?")
AGENT_ID_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{0,40}")
LIMITS = {"title": 200, "body": 64_000, "content": 256_000, "reason": 2_000, "options": 20, "cc": 10}


class ServiceError(Exception):
    def __init__(self, msg: str, code: str = "bad_request", status: int = 400, hint: str | None = None):
        super().__init__(msg)
        if code in ("bad_transition", "conflict") and status == 400:
            status = 409
        self.code, self.status, self.hint = code, status, hint


def clean(s: str | None, limit: int, field: str) -> str:
    """Strip control/invisible characters, enforce a size limit."""
    s = s or ""
    s = "".join(ch for ch in s if ch in "\n\t" or (unicodedata.category(ch)[0] != "C" and unicodedata.category(ch) != "Cf"))
    if len(s) > limit:
        raise ServiceError(f"{field} too long ({len(s)} > {limit})", "bad_request")
    return s


def _norm(p: str) -> str:
    return str(Path(p).resolve()).replace("\\", "/").lower()


def _hash(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


class Service:
    def __init__(self, db_path: Path):
        self.conn = connect(db_path)
        self.lock = threading.RLock()
        self._depth = 0
        self.on_event: Callable[[], None] | None = None  # set by the server to wake long-pollers

    # ------------------------------------------------------------------ plumbing
    @contextmanager
    def tx(self):
        with self.lock:
            self._depth += 1
            try:
                yield
                if self._depth == 1:
                    self.conn.commit()
            except BaseException:
                if self._depth == 1:
                    self.conn.rollback()
                raise
            finally:
                self._depth -= 1
                if self._depth == 0 and self.on_event:
                    self.on_event()

    def _one(self, sql: str, *args) -> dict | None:
        with self.lock:
            return row_to_dict(self.conn.execute(sql, args).fetchone())

    def _all(self, sql: str, *args) -> list[dict]:
        with self.lock:
            return [row_to_dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def _emit(self, type_: str, entity_type: str, entity_id: str, audience: list[str], payload: dict | None = None,
              request_id: str | None = None, agent_id: str | None = None, session_id: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO events(type, entity_type, entity_id, request_id, agent_id, session_id, payload, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (type_, entity_type, entity_id, request_id, agent_id, session_id, j(payload or {}), now()))
        eid = cur.lastrowid
        for a in sorted({a for a in audience if a}):
            self.conn.execute("INSERT OR IGNORE INTO event_audience(event_id, agent_id) VALUES(?,?)", (eid, a))
        return eid

    def _fts(self, entity_type: str, entity_id: str, title: str, body: str) -> None:
        self.conn.execute("INSERT INTO fts(entity_type, entity_id, title, body) VALUES(?,?,?,?)", (entity_type, entity_id, title or "", body or ""))

    def _notify_targets(self, agents: list[str], exclude_session: str | None = None) -> list[dict]:
        out = []
        for a in sorted(set(agents)):
            for s in self.active_sessions(a):
                if s["id"] != exclude_session:
                    out.append({"agent": a, "runtime": s["runtime"], "ref": s["runtime_ref"] or s["name"] or s["id"]})
        return out

    def _req(self, rid: str) -> dict:
        r = self._one("SELECT * FROM requests WHERE id=?", rid)
        if not r:
            raise ServiceError(f"{rid} not found", "not_found", 404)
        return r

    def _agent(self, aid: str) -> dict:
        a = self._one("SELECT * FROM agents WHERE id=?", aid)
        if not a:
            raise ServiceError(f"agent '{aid}' not found", "not_found", 404, hint="reqlane agents")
        return a

    def _is_po(self, agent_id: str) -> bool:
        a = self._one("SELECT kind FROM agents WHERE id=?", agent_id)
        return bool(a and a["kind"] == "product_owner")

    def _set_status(self, req: dict, status: str, session: dict | None, extra: dict | None = None, reason: str | None = None) -> dict:
        old = req["status"]
        fields = {"status": status, "updated_at": now()}
        if status in lc.TERMINAL:
            fields["closed_at"] = now()
        if extra:
            fields.update(extra)
        sets = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE requests SET {sets} WHERE id=?", (*[j(v) if isinstance(v, (list, dict)) else v for v in fields.values()], req["id"]))
        self._emit("status", "request", req["id"], self._audience(req), {"from": old, "to": status, "reason": reason},
                   request_id=req["id"], agent_id=session["agent_id"] if session else None, session_id=session["id"] if session else None)
        return self._req(req["id"])

    def _audience(self, req: dict) -> list[str]:
        aud = [req["from_agent"], req["to_agent"], *(req.get("cc") or [])]
        if req.get("routed_to"):
            aud.append(req["routed_to"])
        return aud

    def _last_message_from(self, rid: str) -> str | None:
        m = self._one("SELECT from_agent FROM messages WHERE request_id=? AND type!='system' ORDER BY id DESC LIMIT 1", rid)
        return m["from_agent"] if m else None

    def _po_id(self) -> str | None:
        a = self._one("SELECT id FROM agents WHERE kind='product_owner' ORDER BY created_at LIMIT 1")
        return a["id"] if a else None

    def po_present(self) -> bool:
        po = self._po_id()
        if not po:
            return False
        cutoff = (parse_ts(now()) - timedelta(minutes=config.PO_PRESENT_MINUTES)).isoformat().replace("+00:00", "Z")
        return bool(self._one("SELECT id FROM sessions WHERE agent_id=? AND status!='gone' AND last_seen_at>=? LIMIT 1", po, cutoff))

    # ------------------------------------------------------------------ agents & sessions
    def list_agents(self) -> list[dict]:
        out = []
        for a in self._all("SELECT * FROM agents ORDER BY kind DESC, id"):
            a["sessions"] = [{"id": s["id"], "name": s["name"], "runtime": s["runtime"], "status": s["status"]} for s in self.active_sessions(a["id"])]
            a["open_requests_to"] = self.conn.execute(
                "SELECT COUNT(*) FROM requests WHERE to_agent=? AND status NOT IN ('closed','declined','withdrawn','acknowledged','wont_do')", (a["id"],)).fetchone()[0]
            out.append(a)
        return out

    def register_agent(self, agent_id: str, kind: str, repo: str, depends_on: list[str] | None, description: str | None) -> dict:
        """Human-only. Creates the agent with repo = the given directory."""
        with self.tx():
            if not AGENT_ID_RE.fullmatch(agent_id or ""):
                raise ServiceError("agent id: lowercase letters, digits, '-', '_', '.'", "bad_request")
            kind = {"po": "product_owner", "product_owner": "product_owner", "project": "project", "observer": "observer"}.get(kind)
            if kind is None:
                raise ServiceError("kind must be project|po|observer")
            if self._one("SELECT id FROM agents WHERE id=?", agent_id):
                raise ServiceError(f"agent '{agent_id}' already exists", "conflict", 409)
            if kind == "product_owner" and self._po_id():
                raise ServiceError(f"a product owner already exists: {self._po_id()}", "conflict", 409)
            for d in depends_on or []:
                self._agent(d)
            repos = [_norm(repo)]
            owner = self.agent_for_cwd(repo)
            if owner and kind != "product_owner" and _norm(self._agent(owner)["repos"][0]) == repos[0]:
                raise ServiceError(f"directory already belongs to agent '{owner}'", "conflict", 409)
            self.conn.execute("INSERT INTO agents(id, kind, display_name, repos, depends_on, description, created_at) VALUES(?,?,?,?,?,?,?)",
                              (agent_id, kind, agent_id, j(repos), j(depends_on or []), description, now()))
            if kind == "project":
                for r in repos:
                    self.conn.execute("INSERT OR REPLACE INTO permissions(agent_id, repo, read, write, source) VALUES(?,?,1,1,'owner')", (agent_id, r))
            if kind == "product_owner":
                self.conn.execute("UPDATE requests SET to_agent=? WHERE type='decision' AND to_agent='po'", (agent_id,))
                self.conn.execute("UPDATE requests SET routed_to=? WHERE type='decision' AND routed_to='po'", (agent_id,))
            everyone = [a["id"] for a in self._all("SELECT id FROM agents")]
            self._emit("agent.created", "agent", agent_id, everyone, {"kind": kind, "repos": repos, "agent": agent_id})
            return self._agent(agent_id)

    def update_agent(self, agent_id: str, depends_on: list[str] | None, description: str | None, add_repo: str | None) -> dict:
        """Human-only."""
        with self.tx():
            a = self._agent(agent_id)
            deps = list(a["depends_on"])
            for d in depends_on or []:
                self._agent(d)
                if d not in deps:
                    deps.append(d)
            repos = list(a["repos"])
            if add_repo and _norm(add_repo) not in repos:
                repos.append(_norm(add_repo))
                if a["kind"] == "project":
                    self.conn.execute("INSERT OR REPLACE INTO permissions(agent_id, repo, read, write, source) VALUES(?,?,1,1,'owner')", (agent_id, _norm(add_repo)))
            self.conn.execute("UPDATE agents SET depends_on=?, repos=?, description=COALESCE(?, description) WHERE id=?", (j(deps), j(repos), description, agent_id))
            return self._agent(agent_id)

    def agent_for_cwd(self, cwd: str) -> str | None:
        """Longest repository prefix wins; on a tie a project agent beats the PO (the PO is chosen explicitly)."""
        p = _norm(cwd)
        best, best_len, best_kind = None, -1, ""
        for a in self._all("SELECT id, kind, repos FROM agents"):
            for r in a["repos"]:
                rp = _norm(r)
                if not (p == rp or p.startswith(rp.rstrip("/") + "/")):
                    continue
                better = len(rp) > best_len or (len(rp) == best_len and best_kind == "product_owner" and a["kind"] != "product_owner")
                if better:
                    best, best_len, best_kind = a["id"], len(rp), a["kind"]
        return best

    def connect(self, agent_id: str | None, kind: str, cwd: str, name: str | None, runtime: str | None, runtime_ref: str | None,
                pid: int | None, depends_on: list[str] | None, description: str | None, human: bool) -> dict:
        with self.tx():
            suggested = self.agent_for_cwd(cwd)
            if not agent_id:
                agent_id = suggested
                if not agent_id:
                    raise ServiceError("no agent registered for this directory", "not_connected", 409,
                                       hint="the user registers it by typing `/reqlane connect [name]` or `/reqlane po` in the chat")
            agent = self._one("SELECT * FROM agents WHERE id=?", agent_id)
            if not agent:
                if not human:
                    raise ServiceError(f"agent '{agent_id}' is not registered", "forbidden", 403,
                                       hint="registration is the user's action: they type `/reqlane connect [name]` in the chat (or run it in their terminal)")
                agent = self.register_agent(agent_id, kind, cwd, depends_on, description)
            else:
                if suggested != agent_id and not human:
                    raise ServiceError(f"this directory is not a repository of '{agent_id}'" + (f" (it belongs to '{suggested}')" if suggested else ""),
                                       "forbidden", 403, hint="connect from the agent's own repository; the user can override with `/reqlane connect <name>`")
                if human and (depends_on or description or (suggested != agent_id)):
                    agent = self.update_agent(agent_id, depends_on, description, cwd if suggested != agent_id else None)
            # One live session per (agent, cwd, name): close the previous one.
            for old in self._all("SELECT id FROM sessions WHERE agent_id=? AND cwd=? AND name IS ? AND status!='gone'", agent_id, cwd, name):
                self.conn.execute("UPDATE sessions SET status='gone', ended_at=? WHERE id=?", (now(), old["id"]))
                self.conn.execute("UPDATE requests SET claimed_by=NULL WHERE claimed_by=?", (old["id"],))
            sid = next_id(self.conn, "ses")
            tok = secrets.token_urlsafe(24)
            cursor = self.last_event_id()
            self.conn.execute(
                "INSERT INTO sessions(id, token_hash, agent_id, name, runtime, runtime_ref, cwd, pid, status, last_seen_at, started_at, event_cursor)"
                " VALUES(?,?,?,?,?,?,?,?, 'active', ?, ?, ?)", (sid, _hash(tok), agent_id, name, runtime, runtime_ref, cwd, pid, now(), now(), cursor))
            self._emit("session.connected", "session", sid, [agent_id], {"name": name, "runtime": runtime, "human": human}, agent_id=agent_id, session_id=sid)
            ses = self._one("SELECT * FROM sessions WHERE id=?", sid)
            who = self.whoami(ses)
            return {"session": ses, "token": tok, "agent": agent, "who": who, "inbox": self.inbox(ses), "po_present": self.po_present(), "cursor": cursor}

    def set_session_ref(self, session: dict, runtime_ref: str) -> dict:
        """The session records its own cross-session address (e.g. Claude's 'name [ref]')."""
        with self.tx():
            ref = clean(runtime_ref, 200, "runtime_ref").strip()
            if not ref:
                raise ServiceError("address is empty")
            self.conn.execute("UPDATE sessions SET runtime_ref=? WHERE id=?", (ref, session["id"]))
            return self._one("SELECT * FROM sessions WHERE id=?", session["id"])

    def disconnect(self, session: dict) -> None:
        with self.tx():
            self.conn.execute("UPDATE sessions SET status='gone', ended_at=? WHERE id=?", (now(), session["id"]))
            self.conn.execute("UPDATE requests SET claimed_by=NULL WHERE claimed_by=?", (session["id"],))
            self._emit("session.disconnected", "session", session["id"], [session["agent_id"]], agent_id=session["agent_id"], session_id=session["id"])

    def auth(self, token: str | None) -> dict:
        if not token:
            raise ServiceError("no session", "not_connected", 401, hint="reqlane connect <agent>")
        with self.tx():
            s = self._one("SELECT * FROM sessions WHERE token_hash=?", _hash(token))
            if not s or s["status"] == "gone":
                raise ServiceError("session expired or unknown", "not_connected", 401, hint="reqlane connect <agent>")
            if (parse_ts(now()) - parse_ts(s["last_seen_at"])).total_seconds() > 10:
                self.conn.execute("UPDATE sessions SET last_seen_at=?, status='active' WHERE id=?", (now(), s["id"]))
            return s

    def active_sessions(self, agent_id: str) -> list[dict]:
        cutoff = (parse_ts(now()) - timedelta(minutes=config.SESSION_IDLE_MINUTES)).isoformat().replace("+00:00", "Z")
        return self._all("SELECT * FROM sessions WHERE agent_id=? AND status!='gone' AND last_seen_at>=? ORDER BY last_seen_at DESC", agent_id, cutoff)

    def whoami(self, session: dict) -> dict:
        a = self._agent(session["agent_id"])
        return {"agent": a["id"], "kind": a["kind"], "session": session["id"], "name": session["name"], "runtime": session["runtime"],
                "repos": a["repos"], "depends_on": a["depends_on"], "consumers": self.consumers_of(a["id"]), "po_present": self.po_present()}

    def consumers_of(self, agent_id: str) -> list[str]:
        return [a["id"] for a in self._all("SELECT id, depends_on FROM agents") if agent_id in a["depends_on"]]

    # ------------------------------------------------------------------ requests
    def create_request(self, session: dict, to: str, type_: str, title: str, body: str = "", goal: dict | None = None,
                       priority: str = "normal", blocking: bool = False, cc: list[str] | None = None, parent_id: str | None = None,
                       labels: list[str] | None = None, due_at: str | None = None, idem: str | None = None, kind: str | None = None,
                       options: list | None = None, as_agent: str | None = None) -> dict:
        with self.tx():
            me = as_agent or session["agent_id"]
            if as_agent:
                self._agent(as_agent)
            title = clean(title, LIMITS["title"], "title").strip()
            body = clean(body, LIMITS["body"], "body")
            if type_ not in lc.TYPES:
                raise ServiceError(f"type must be one of {', '.join(lc.TYPES)}")
            if not title:
                raise ServiceError("title is required")
            if priority not in lc.PRIORITIES:
                raise ServiceError(f"priority must be one of {', '.join(lc.PRIORITIES)}")
            if len(cc or []) > LIMITS["cc"]:
                raise ServiceError("too many cc")
            if idem:
                dup = self._one("SELECT * FROM requests WHERE from_agent=? AND idem=?", me, idem)
                if dup:
                    return {"request": dup, "notify": [], "duplicate": True}
            if type_ == "task" and not self._is_po(me) and to != me:
                raise ServiceError("only the product owner assigns tasks to other agents", "forbidden", 403)
            if type_ == "decision":
                return self.ask_po(session, title, body, kind or "other", options or [], parent_id, blocking)
            if type_ == "notice" and to not in self.consumers_of(me) and not self._is_po(me):
                raise ServiceError(f"notices go to your consumers only ({', '.join(self.consumers_of(me)) or 'none registered'})", "forbidden", 403)
            self._agent(to)
            if to == me:
                raise ServiceError("cannot address a request to yourself")
            for c in cc or []:
                self._agent(c)
            if parent_id:
                self._req(parent_id)
            rid = next_id(self.conn, "req")
            refs = self.extract_refs(body + " " + json.dumps(goal or {}))
            self.conn.execute(
                "INSERT INTO requests(id, type, from_agent, to_agent, cc, parent_id, title, body, goal, priority, blocking, status, labels, idem, due_at, kind, options, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, type_, me, to, j(cc or []), parent_id, title, body, j(goal or {}), priority, int(blocking), "open",
                 j(labels or []), idem, due_at, kind, j(options or []), now(), now()))
            self._fts("request", rid, title, body)
            req = self._req(rid)
            self._emit("request.created", "request", rid, self._audience(req), {"type": type_, "title": title, "from": me, "to": to, "blocking": blocking},
                       request_id=rid, agent_id=me, session_id=session["id"])
            if type_ in ("question", "capability", "bug", "change", "review"):
                # Asking another agent for something is the dependency; no need to declare it up front.
                a_from = self._agent(me)
                if a_from["kind"] == "project" and to not in a_from["depends_on"] and not self._is_po(to):
                    self.conn.execute("UPDATE agents SET depends_on=? WHERE id=?", (j(a_from["depends_on"] + [to]), me))
            if type_ == "notice" and "breaking" in (labels or []) and self._po_id():
                self._emit("notice.breaking", "request", rid, [self._po_id()], {"title": title, "from": me}, request_id=rid, agent_id=me)
            return {"request": req, "notify": self._notify_targets([to, *(cc or [])], session["id"]), "refs": refs}

    def list_requests(self, agent_id: str, box: str = "all", status: str | None = None, type_: str | None = None,
                      other: str | None = None, since: str | None = None, limit: int = 50, open_only: bool = True) -> list[dict]:
        where, args = [], []
        cc_has = "EXISTS(SELECT 1 FROM json_each(requests.cc) WHERE value=?)"
        if box == "inbox":
            where.append(f"(to_agent=? OR routed_to=? OR {cc_has})"); args += [agent_id] * 3
        elif box == "outbox":
            where.append("from_agent=?"); args.append(agent_id)
        elif agent_id != "*":
            where.append(f"(from_agent=? OR to_agent=? OR routed_to=? OR {cc_has})"); args += [agent_id] * 4
        if status:
            where.append("status=?"); args.append(status)
        elif open_only:
            where.append("status NOT IN ('closed','declined','withdrawn','acknowledged','wont_do')")
        if type_:
            where.append("type=?"); args.append(type_)
        if other:
            where.append("(from_agent=? OR to_agent=?)"); args += [other, other]
        if since:
            where.append("updated_at>=?"); args.append(since)
        sql = "SELECT * FROM requests" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY blocking DESC, updated_at DESC LIMIT ?"
        rows = self._all(sql, *args, min(limit, 500))
        for r in rows:
            r["actor"] = lc.actor_for(r, self._last_message_from(r["id"]))
        return rows

    def get_request(self, rid: str, me: str | None, include: tuple[str, ...] = ("messages", "artifacts"), since: int | None = None) -> dict:
        req = self._req(rid)
        out = {"request": req, "actor": lc.actor_for(req, self._last_message_from(rid))}
        if me:
            out["next"] = lc.next_actions(req, me)
        if "messages" in include:
            out["messages"] = self._all("SELECT * FROM messages WHERE request_id=? AND (? IS NULL OR rowid>?) ORDER BY id", rid, since, since)
        if "artifacts" in include:
            out["artifacts"] = self._all("SELECT * FROM artifacts WHERE request_id=? ORDER BY id", rid)
        if "events" in include:
            out["events"] = self._all("SELECT * FROM events WHERE request_id=? ORDER BY id", rid)
        out["children"] = self._all("SELECT id, type, status, to_agent, title FROM requests WHERE parent_id=? ORDER BY id", rid)
        out["links"] = self._all("SELECT * FROM request_links WHERE request_id=? OR related_id=?", rid, rid)
        if req["parent_id"]:
            out["parent"] = self._one("SELECT id, type, status, from_agent, to_agent, title FROM requests WHERE id=?", req["parent_id"])
        if req.get("claimed_by"):
            out["claimed_by_session"] = self._one("SELECT id, name, status, last_seen_at FROM sessions WHERE id=?", req["claimed_by"])
        return out

    def _claim_free(self, req: dict, session: dict) -> bool:
        if not req["claimed_by"] or req["claimed_by"] == session["id"]:
            return True
        return not any(s["id"] == req["claimed_by"] for s in self.active_sessions(req["to_agent"]))

    def claim(self, session: dict, rid: str) -> dict:
        with self.tx():
            req = self._req(rid)
            if req["to_agent"] != session["agent_id"]:
                raise ServiceError("only the recipient can claim", "forbidden", 403)
            if req["status"] in lc.TERMINAL or req["status"] == "blocked":
                raise ServiceError(f"cannot claim in status {req['status']}", "bad_transition")
            if not self._claim_free(req, session):
                holder = self._one("SELECT name, id FROM sessions WHERE id=?", req["claimed_by"])
                raise ServiceError(f"already claimed by live session {holder['name'] or holder['id']}", "conflict", 409)
            if req["status"] == "open":
                target = lc.CLAIM_STATUS.get(req["type"])
                if target is None:
                    raise ServiceError(f"{req['type']} requests are not claimed; use `reqlane req ack`", "bad_transition")
                return {"request": self._set_status(req, target, session, {"claimed_by": session["id"]})}
            self.conn.execute("UPDATE requests SET claimed_by=? WHERE id=?", (session["id"], rid))
            return {"request": self._req(rid)}

    def reply(self, session: dict, rid: str, body: str, type_: str = "comment", as_agent: str | None = None) -> dict:
        with self.tx():
            req = self._req(rid)
            me = as_agent or session["agent_id"]
            if as_agent:
                self._agent(as_agent)
            if type_ not in {"comment", "clarification", "answer"}:
                raise ServiceError("type must be comment|clarification|answer")
            body = clean(body, LIMITS["body"], "body")
            if not body.strip():
                raise ServiceError("empty message")
            if me not in self._audience(req) and not self._is_po(me):
                raise ServiceError("you are not a party of this request", "forbidden", 403)
            if req["status"] in lc.TERMINAL:
                raise ServiceError(f"request is {req['status']}", "bad_transition")
            if req["to_agent"] == me and not as_agent and not self._claim_free(req, session):
                holder = self._one("SELECT name, id FROM sessions WHERE id=?", req["claimed_by"])
                raise ServiceError(f"claimed by live session {holder['name'] or holder['id']}", "conflict", 409)
            mid = next_id(self.conn, "msg")
            refs = self.extract_refs(body)
            self.conn.execute("INSERT INTO messages(id, request_id, from_agent, from_session, type, body, refs, created_at) VALUES(?,?,?,?,?,?,?,?)",
                              (mid, rid, me, None if as_agent else session["id"], type_, body, j(refs), now()))
            self._fts("message", mid, req["title"], body)
            status_note = None
            if req["status"] == "open" and me == req["to_agent"] and req["type"] in lc.CLAIM_STATUS:
                req = self._set_status(req, lc.CLAIM_STATUS[req["type"]], session, {"claimed_by": None if as_agent else session["id"]})
                status_note = req["status"]
            if type_ == "answer" and me == req["to_agent"] and req["type"] in {"question", "review"}:
                req = self._set_status(req, "answered", session)
                status_note = "answered"
            self.conn.execute("UPDATE requests SET updated_at=? WHERE id=?", (now(), rid))
            self._emit("message", "message", mid, self._audience(req), {"type": type_, "from": me, "preview": body.strip().splitlines()[0][:120], "as": bool(as_agent)},
                       request_id=rid, agent_id=me, session_id=session["id"])
            return {"message": self._one("SELECT * FROM messages WHERE id=?", mid), "request": self._req(rid), "status": status_note,
                    "notify": self._notify_targets([a for a in self._audience(req) if a != me], session["id"])}

    def _cascade_children(self, parent: dict, session: dict | None, reason: str) -> None:
        for c in self._all("SELECT * FROM requests WHERE parent_id=? AND status NOT IN ('closed','declined','withdrawn','acknowledged','wont_do')", parent["id"]):
            self._set_status(c, "wont_do", session, reason=reason)

    def set_status(self, session: dict, rid: str, action: str, reason: str | None = None, to: str | None = None) -> dict:
        with self.tx():
            req = self._req(rid)
            me = session["agent_id"]
            reason = clean(reason, LIMITS["reason"], "reason") or None
            is_init, is_rcpt = req["from_agent"] == me, req["to_agent"] == me
            if action == "decline":
                if not is_rcpt:
                    raise ServiceError("only the recipient declines", "forbidden", 403)
                if req["status"] not in lc.DECLINE_FROM:
                    raise ServiceError(f"cannot decline in status {req['status']}", "bad_transition")
                if not reason:
                    raise ServiceError("--reason is required for decline")
                req = self._set_status(req, "declined", session, reason=reason)
            elif action == "withdraw":
                if not is_init:
                    raise ServiceError("only the initiator withdraws", "forbidden", 403)
                if req["status"] in lc.TERMINAL:
                    raise ServiceError(f"cannot withdraw in status {req['status']}", "bad_transition")
                req = self._set_status(req, "withdrawn", session, reason=reason)
                self._cascade_children(req, session, f"{rid} withdrawn")
                self._unblock_parent(req, session)
            elif action == "close":
                if not is_init and not self._is_po(me):
                    raise ServiceError("only the initiator or PO closes", "forbidden", 403)
                if req["status"] in lc.TERMINAL:
                    raise ServiceError("already closed", "bad_transition")
                req = self._set_status(req, "closed", session, reason=reason)
                self._cascade_children(req, session, f"{rid} closed")
                self._unblock_parent(req, session)
            elif action == "ack":
                if req["type"] != "notice":
                    raise ServiceError("ack is for notices; use `reqlane events ack` for events", "bad_transition")
                if not is_rcpt:
                    raise ServiceError("only the recipient acknowledges", "forbidden", 403)
                if req["status"] != "open":
                    raise ServiceError("already acknowledged", "bad_transition")
                req = self._set_status(req, "acknowledged", session)
            elif action == "reassign":
                if not is_rcpt:
                    raise ServiceError("only the recipient reassigns", "forbidden", 403)
                if req["status"] not in lc.REASSIGN_FROM:
                    raise ServiceError(f"cannot reassign in status {req['status']}", "bad_transition")
                if not to:
                    raise ServiceError("--to is required")
                self._agent(to)
                if to in (me, req["from_agent"]):
                    raise ServiceError("reassign to a third agent")
                old = req["to_agent"]
                self.conn.execute("UPDATE requests SET to_agent=?, claimed_by=NULL, status='open', updated_at=? WHERE id=?", (to, now(), rid))
                req = self._req(rid)
                self._emit("request.reassigned", "request", rid, self._audience(req) + [old], {"from": old, "to": to, "reason": reason},
                           request_id=rid, agent_id=me, session_id=session["id"])
            elif action == "wont_do":
                if not self._is_po(me):
                    raise ServiceError("only the PO marks wont_do", "forbidden", 403)
                req = self._set_status(req, "wont_do", session, reason=reason)
                self._cascade_children(req, session, f"{rid} wont_do")
                self._unblock_parent(req, session)
            else:
                raise ServiceError(f"unknown action {action}")
            return {"request": req, "notify": self._notify_targets([a for a in self._audience(req) if a != me], session["id"])}

    def accept_proposal(self, session: dict, rid: str, option: str | None, notes: str | None) -> dict:
        with self.tx():
            req = self._req(rid)
            if req["from_agent"] != session["agent_id"]:
                raise ServiceError("only the initiator accepts a proposal", "forbidden", 403)
            if req["status"] != "proposal":
                raise ServiceError(f"no proposal pending (status {req['status']})", "bad_transition")
            prop = self._one("SELECT * FROM artifacts WHERE request_id=? AND type='proposal' ORDER BY id DESC LIMIT 1", rid)
            opts = (prop or {}).get("data", {}).get("options") or []
            ids = [o.get("id") for o in opts]
            if opts and option and option not in ids:
                raise ServiceError(f"unknown option {option}; available: {ids}")
            if opts and not option:
                if len(opts) > 1:
                    raise ServiceError("proposal has several options; pass --option")
                option = ids[0]
            req = self._set_status(req, "implementation", session, {"accepted_option": option}, reason=notes)
            mid = next_id(self.conn, "msg")
            self.conn.execute("INSERT INTO messages(id, request_id, from_agent, from_session, type, body, created_at) VALUES(?,?,?,?,?,?,?)",
                              (mid, rid, session["agent_id"], session["id"], "system", f"Proposal accepted: option {option or '-'}. {notes or ''}".strip(), now()))
            return {"request": req, "notify": self._notify_targets([req["to_agent"]], session["id"])}

    # ------------------------------------------------------------------ PO
    def ask_po(self, session: dict, title: str, body: str, kind: str, options: list, parent_id: str | None, blocking: bool = False,
               route_to: str | None = None) -> dict:
        with self.tx():
            me = session["agent_id"]
            title = clean(title, LIMITS["title"], "title").strip()
            body = clean(body, LIMITS["body"], "body")
            if kind not in lc.KINDS:
                raise ServiceError(f"kind must be one of {', '.join(lc.KINDS)}")
            if len(options) > LIMITS["options"]:
                raise ServiceError("too many options")
            if self._is_po(me):
                raise ServiceError("the PO does not ask itself; decide or delegate", "bad_request")
            po = self._po_id()
            present = self.po_present()
            rid = next_id(self.conn, "req")
            status = "open" if present else "local"
            routed = po if present else (route_to or me)
            self.conn.execute(
                "INSERT INTO requests(id, type, from_agent, to_agent, parent_id, title, body, priority, blocking, status, routed_to, kind, options, created_at, updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (rid, "decision", me, po or "po", parent_id, title, body, "high" if blocking else "normal", int(blocking), status, routed, kind, j(options), now(), now()))
            self._fts("request", rid, title, body)
            req = self._req(rid)
            self._emit("request.created", "request", rid, self._audience(req), {"type": "decision", "title": title, "from": me, "kind": kind, "mode": status, "routed_to": routed},
                       request_id=rid, agent_id=me, session_id=session["id"])
            if parent_id:
                parent = self._req(parent_id)
                if parent["status"] == "declined":
                    self._set_status(parent, "blocked", session, {"resume_status": "discussion"}, reason=f"escalated as {rid}")
                elif parent["status"] not in lc.TERMINAL and parent["status"] != "blocked":
                    self._set_status(parent, "blocked", session, {"resume_status": parent["status"]}, reason=f"waiting for {rid}")
                self.conn.execute("INSERT OR IGNORE INTO request_links(request_id, related_id, relation) VALUES(?,?,'depends_on')", (parent_id, rid))
            targets = [po] if (present and po) else ([routed] if routed != me else [])
            return {"request": req, "po_present": present, "mode": status, "routed_to": routed, "notify": self._notify_targets(targets, session["id"])}

    def handoff(self, session: dict, rid: str) -> dict:
        with self.tx():
            req = self._req(rid)
            if req["type"] != "decision" or req["status"] != "local":
                raise ServiceError("only local decisions can be handed over", "bad_transition")
            if session["agent_id"] not in (req["routed_to"], req["from_agent"]):
                raise ServiceError("not routed to you", "forbidden", 403)
            po = self._po_id() or "po"
            req = self._set_status(req, "open", session, {"routed_to": po, "to_agent": po}, reason="handed over to PO")
            self._emit("decision.handoff", "request", rid, [po, req["from_agent"]], {"title": req["title"]}, request_id=rid, agent_id=session["agent_id"])
            return {"request": req, "po_present": self.po_present(), "notify": self._notify_targets([po], session["id"])}

    def decide(self, session: dict, rid: str, decision: str, option: str | None, reason: str, affected: list[str],
               author: str = "po", human: bool = False) -> dict:
        with self.tx():
            req = self._req(rid)
            me = session["agent_id"]
            reason = clean(reason, LIMITS["reason"], "reason").strip()
            decision = clean(decision, LIMITS["body"], "decision")
            if req["type"] != "decision":
                raise ServiceError("not a decision request", "bad_transition")
            if req["status"] in lc.TERMINAL:
                raise ServiceError("already decided", "bad_transition")
            if not reason:
                raise ServiceError("--reason is required")
            is_po = self._is_po(me)
            if author == "human":
                if req["status"] != "local" and not (is_po or human):
                    raise ServiceError("--author human is allowed only for local decisions, from the PO session, or with the human token", "forbidden", 403)
                if req["status"] == "local" and req["routed_to"] != me and not (is_po or human):
                    raise ServiceError(f"this local decision is with {req['routed_to']}", "forbidden", 403)
                parent = self._one("SELECT * FROM requests WHERE id=?", req["parent_id"]) if req["parent_id"] else None
                if parent and parent["to_agent"] == me and not human:
                    raise ServiceError("the recipient of the parent request cannot record the human's decision on it; the initiator does", "forbidden", 403)
                attested = human
            elif author == "po":
                if not is_po:
                    raise ServiceError("only the PO decides; use --author human for a local decision", "forbidden", 403)
                attested = True
            else:
                raise ServiceError("author must be po|human")
            opts = req.get("options") or []
            if option and opts and option not in [o.get("id") for o in opts]:
                raise ServiceError(f"unknown option {option}; available: {[o.get('id') for o in opts]}")
            aid = next_id(self.conn, "art")
            title = f"Decision on {rid}: {option or decision[:60]}"
            data = {"decision": decision, "option": option, "reason": reason, "affected": affected, "author": author,
                    "attested": attested, "via_session": session["id"], "via_agent": me, "request": rid, "kind": req.get("kind")}
            self.conn.execute("INSERT INTO artifacts(id, request_id, type, title, content, data, author_agent, author_session, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                              (aid, rid, "decision", title, f"{decision}\n\nReason: {reason}", j(data), "human" if author == "human" else me, session["id"], now()))
            self._fts("artifact", aid, title, decision + " " + reason)
            req = self._set_status(req, "closed", session, reason=f"decided by {author}{'' if attested else ' (unattested)'}: {option or decision[:40]}")
            aud = self._audience(req) + list(affected)
            self._emit("decision", "artifact", aid, aud, {"request": rid, "option": option, "author": author, "attested": attested, "reason": reason},
                       request_id=rid, agent_id=me, session_id=session["id"])
            self._unblock_parent(req, session)
            return {"artifact": self._one("SELECT * FROM artifacts WHERE id=?", aid), "request": req, "attested": attested,
                    "notify": self._notify_targets([a for a in aud if a != me], session["id"])}

    def delegate(self, session: dict, rid: str, reason: str) -> dict:
        with self.tx():
            req = self._req(rid)
            if not self._is_po(session["agent_id"]):
                raise ServiceError("only the PO delegates back", "forbidden", 403)
            if req["type"] != "decision" or req["status"] in lc.TERMINAL:
                raise ServiceError("not an open decision", "bad_transition")
            reason = clean(reason, LIMITS["reason"], "reason")
            req = self._set_status(req, "closed", session, reason=f"delegated back: {reason}")
            self._unblock_parent(req, session)
            return {"request": req, "notify": self._notify_targets([req["from_agent"]], session["id"])}

    def _unblock_parent(self, child: dict, session: dict | None) -> None:
        if not child.get("parent_id"):
            return
        parent = self._one("SELECT * FROM requests WHERE id=?", child["parent_id"])
        if not parent or parent["status"] != "blocked":
            return
        if self._all("SELECT id FROM requests WHERE parent_id=? AND status NOT IN ('closed','declined','withdrawn','acknowledged','wont_do')", parent["id"]):
            return
        self._set_status(parent, parent.get("resume_status") or "discussion", session, {"resume_status": None}, reason=f"{child['id']} resolved")

    def escalate(self, session: dict, rid: str, question: str, kind: str, options: list) -> dict:
        req = self._req(rid)
        if session["agent_id"] not in (req["from_agent"], req["to_agent"]):
            raise ServiceError("not a party of this request", "forbidden", 403)
        if req["status"] in lc.TERMINAL - {"declined"}:
            raise ServiceError(f"cannot escalate a {req['status']} request", "bad_transition")
        return self.ask_po(session, f"Escalation of {rid}: {req['title']}", question, kind, options, rid, blocking=req["blocking"])

    def create_task(self, session: dict, to: str, title: str, body: str, depends_on: list[str], due_at: str | None) -> dict:
        with self.tx():
            res = self.create_request(session, to, "task", title, body, due_at=due_at)
            for d in depends_on:
                self._req(d)
                self.conn.execute("INSERT OR IGNORE INTO request_links(request_id, related_id, relation) VALUES(?,?,'depends_on')", (res["request"]["id"], d))
            return res

    def dashboard(self, po: str) -> dict:
        open_ = self.list_requests("*", "all", limit=500)
        decisions = [r for r in open_ if r["type"] == "decision" and r["status"] != "local"]
        for r in decisions:
            if self._one("SELECT id FROM events WHERE request_id=? AND type='decision.handoff'", r["id"]):
                r["origin"] = "handed_over"
            elif r["title"].startswith("Approve proposal"):
                r["origin"] = "requires_po"
            else:
                r["origin"] = "escalation" if r["parent_id"] else "question"
        local = [r for r in open_ if r["type"] == "decision" and r["status"] == "local"]
        blocking = [r for r in open_ if r["blocking"] and r["type"] != "decision"]
        stale_cut = (parse_ts(now()) - timedelta(hours=config.STALE_HOURS)).isoformat().replace("+00:00", "Z")
        stale = [r for r in open_ if r["updated_at"] < stale_cut and r["status"] != "blocked"]
        notices = [r for r in open_ if r["type"] == "notice"]
        agreements = self._all("SELECT * FROM agreements WHERE status='active'")
        pending_ack = [a for a in agreements if set(a["parties"]) - set(a["acknowledged"])]
        unattested = self._all("SELECT id, request_id, data, created_at FROM artifacts WHERE type='decision' AND json_extract(data,'$.attested')=0 ORDER BY id DESC LIMIT 20")
        return {"decisions": decisions, "local_decisions": local, "blocking": blocking, "stale": stale, "notices_unacked": notices,
                "open": [r for r in open_ if r["type"] not in ("decision", "notice") and not r["blocking"]], "agreements_pending_ack": pending_ack,
                "unattested_decisions": unattested, "agents": self.list_agents(), "policy": self.get_policy(po)}

    def overview(self, limit: int = 200) -> dict:
        """Everything the UI needs in one call: agents, requests (incl. closed) with artifact and message summaries."""
        reqs = self.list_requests("*", "all", limit=limit, open_only=False)
        for r in reqs:
            r["artifacts"] = self._all("SELECT id, type, title, author_agent, data, created_at FROM artifacts WHERE request_id=? ORDER BY id", r["id"])
            row = self.conn.execute("SELECT COUNT(*), MAX(id) FROM messages WHERE request_id=? AND type!='system'", (r["id"],)).fetchone()
            r["messages_count"] = row[0]
            r["last_message_from"] = self._last_message_from(r["id"])
        events = self._all("SELECT id, type, request_id, agent_id, created_at FROM events ORDER BY id DESC LIMIT 10")[::-1]
        return {"agents": self.list_agents(), "requests": reqs, "recent_events": events, "po_present": self.po_present()}

    def get_policy(self, po: str) -> dict:
        p = self._one("SELECT policy FROM policies WHERE agent_id=?", po)
        return p["policy"] if p else {"mode": "hybrid", "auto_decide": ["priority", "clarification"],
                                     "always_ask_human": ["breaking_change", "scope_change", "conflict", "budget"], "default": "ask_human"}

    def set_policy(self, session: dict, policy: dict, human: bool) -> dict:
        with self.tx():
            if not human:
                raise ServiceError("policy changes need the human token (run from your terminal)", "forbidden", 403)
            if not self._is_po(session["agent_id"]):
                raise ServiceError("policy belongs to the PO agent", "forbidden", 403)
            self.conn.execute("INSERT OR REPLACE INTO policies(agent_id, policy, updated_at) VALUES(?,?,?)", (session["agent_id"], j(policy), now()))
            return policy

    # ------------------------------------------------------------------ artifacts & agreements
    def publish_artifact(self, session: dict, type_: str, title: str, content: str, data: dict | None, request_id: str | None,
                         supersedes: str | None, verdict: str | None, requires_po: bool, po_question: str | None) -> dict:
        with self.tx():
            me = session["agent_id"]
            if type_ not in lc.ARTIFACT_TYPES:
                raise ServiceError(f"artifact type must be one of {', '.join(lc.ARTIFACT_TYPES)}")
            title = clean(title, LIMITS["title"], "title").strip()
            content = clean(content, LIMITS["content"], "content")
            if not title:
                raise ServiceError("title is required")
            data = dict(data or {})
            req = self._req(request_id) if request_id else None
            if type_ in {"proposal", "delivery", "evaluation"} and not req:
                raise ServiceError(f"{type_} needs --request")
            if req:
                if type_ in {"proposal", "delivery"} and me != req["to_agent"]:
                    raise ServiceError(f"only the recipient ({req['to_agent']}) publishes a {type_}", "forbidden", 403)
                if type_ == "evaluation" and me != req["from_agent"] and self._agent(me)["kind"] != "observer":
                    raise ServiceError(f"only the initiator ({req['from_agent']}) or an observer publishes an evaluation", "forbidden", 403)
                if type_ == "proposal":
                    if req["status"] not in lc.PROPOSAL_FROM:
                        raise ServiceError(f"cannot propose in status {req['status']}", "bad_transition")
                    if len(data.get("options") or []) > LIMITS["options"]:
                        raise ServiceError("too many options")
                    if not self._claim_free(req, session):
                        raise ServiceError("request is claimed by another live session", "conflict", 409)
                if type_ == "delivery":
                    allowed = lc.DELIVERY_FROM.get(req["type"], set())
                    if req["status"] not in allowed:
                        raise ServiceError(f"cannot deliver a {req['type']} in status {req['status']} (allowed: {', '.join(sorted(allowed)) or 'none'})", "bad_transition")
                    repo = data.get("repo")
                    if not repo:
                        raise ServiceError("delivery data needs repo")
                    if not self.check_permission(me, repo, "write")["allowed"]:
                        raise ServiceError(f"{me} has no write permission on repo '{repo}'", "forbidden", 403)
                    if not data.get("commit") and not data.get("branch"):
                        raise ServiceError("delivery data needs commit or branch")
                    if "tests" not in data:
                        raise ServiceError("delivery data needs tests (what you ran and the result)")
                if type_ == "evaluation" and req["status"] != "evaluation":
                    raise ServiceError(f"nothing to evaluate in status {req['status']}", "bad_transition")
            if type_ == "evaluation":
                if verdict not in {"accepted", "rejected"}:
                    raise ServiceError("--verdict accepted|rejected is required")
                data["verdict"] = verdict
            aid = next_id(self.conn, "art")
            version = 1
            if supersedes:
                prev = self._one("SELECT version FROM artifacts WHERE id=?", supersedes)
                version = (prev["version"] + 1) if prev else 1
            self.conn.execute("INSERT INTO artifacts(id, request_id, type, title, content, data, author_agent, author_session, supersedes, version, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                              (aid, request_id, type_, title, content, j(data), me, session["id"], supersedes, version, now()))
            self._fts("artifact", aid, title, content)
            out: dict = {"artifact": self._one("SELECT * FROM artifacts WHERE id=?", aid), "notify": []}
            if req:
                new_status = lc.status_after_artifact(type_, verdict) if type_ in {"proposal", "delivery", "evaluation"} else None
                extra = {}
                if type_ == "evaluation" and verdict == "rejected":
                    extra["iteration"] = req["iteration"] + 1
                if new_status:
                    req = self._set_status(req, new_status, session, extra, reason=f"{type_} {aid}")
                self._emit("artifact", "artifact", aid, self._audience(req) + ([self._po_id()] if type_ == "evaluation" and self._po_id() else []),
                           {"type": type_, "title": title, "request": request_id, "verdict": verdict}, request_id=request_id, agent_id=me, session_id=session["id"])
                if type_ == "evaluation" and verdict == "rejected" and req["iteration"] >= 3 and self._po_id():
                    self._emit("request.repeated_reject", "request", req["id"], [self._po_id()], {"iteration": req["iteration"]}, request_id=req["id"])
                if type_ == "proposal" and requires_po:
                    opts = [{"id": o.get("id"), "summary": o.get("summary")} for o in data.get("options", [])]
                    kind = "breaking_change" if any("break" in str(o.get("compat", "")).lower() for o in data.get("options", [])) else "scope_change"
                    # Routed to the request's initiator (the consumer), never to the proposal's author.
                    dec = self.ask_po(session, f"Approve proposal for {req['id']}: {title}", po_question or content, kind, opts, req["id"],
                                      blocking=req["blocking"], route_to=req["from_agent"])
                    out.update({"decision": dec["request"], "po_present": dec["po_present"], "mode": dec["mode"], "routed_to": dec["routed_to"]})
                    out["notify"] += dec["notify"]
                out["request"] = self._req(req["id"])
                out["notify"] += self._notify_targets([a for a in self._audience(req) if a != me], session["id"])
            else:
                self._emit("artifact", "artifact", aid, [me], {"type": type_, "title": title}, agent_id=me, session_id=session["id"])
            return out

    def get_artifact(self, aid: str) -> dict:
        a = self._one("SELECT * FROM artifacts WHERE id=?", aid)
        if not a:
            raise ServiceError(f"{aid} not found", "not_found", 404)
        return a

    def list_artifacts(self, type_: str | None, request_id: str | None, agent: str | None, limit: int = 50) -> list[dict]:
        where, args = [], []
        if type_:
            where.append("type=?"); args.append(type_)
        if request_id:
            where.append("request_id=?"); args.append(request_id)
        if agent:
            where.append("author_agent=?"); args.append(agent)
        return self._all("SELECT id, request_id, type, title, author_agent, version, created_at FROM artifacts" + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY id DESC LIMIT ?", *args, min(limit, 500))

    def publish_agreement(self, session: dict, title: str, content: str, parties: list[str], supersedes: str | None) -> dict:
        with self.tx():
            me = session["agent_id"]
            title = clean(title, LIMITS["title"], "title").strip()
            content = clean(content, LIMITS["content"], "content")
            for p in parties:
                self._agent(p)
            if not parties:
                raise ServiceError("--parties is required")
            if not self._is_po(me) and me not in parties:
                raise ServiceError("an agreement is published by the PO or by one of its parties", "forbidden", 403)
            aid = next_id(self.conn, "art")
            gid = next_id(self.conn, "agr")
            self.conn.execute("INSERT INTO artifacts(id, type, title, content, data, author_agent, author_session, created_at) VALUES(?,?,?,?,?,?,?,?)",
                              (aid, "agreement", title, content, j({"parties": parties, "agreement": gid}), me, session["id"], now()))
            ack = [me] if me in parties else []
            self.conn.execute("INSERT INTO agreements(id, artifact_id, title, parties, status, acknowledged, created_at) VALUES(?,?,?,?,?,?,?)",
                              (gid, aid, title, j(parties), "active", j(ack), now()))
            if supersedes:
                self.conn.execute("UPDATE agreements SET status='superseded', superseded_by=? WHERE id=?", (gid, supersedes))
            self._fts("agreement", gid, title, content)
            self._emit("agreement", "agreement", gid, parties + [me], {"title": title, "parties": parties}, agent_id=me, session_id=session["id"])
            return {"agreement": self._one("SELECT * FROM agreements WHERE id=?", gid), "artifact_id": aid,
                    "notify": self._notify_targets([p for p in parties if p != me], session["id"])}

    def ack_agreement(self, session: dict, gid: str) -> dict:
        with self.tx():
            g = self._one("SELECT * FROM agreements WHERE id=?", gid)
            if not g:
                raise ServiceError(f"{gid} not found", "not_found", 404)
            me = session["agent_id"]
            if me not in g["parties"]:
                raise ServiceError("not a party", "forbidden", 403)
            ack = sorted(set(g["acknowledged"]) | {me})
            self.conn.execute("UPDATE agreements SET acknowledged=? WHERE id=?", (j(ack), gid))
            self._emit("agreement.ack", "agreement", gid, g["parties"], {"by": me}, agent_id=me)
            return self._one("SELECT * FROM agreements WHERE id=?", gid)

    def list_agreements(self, party: str | None = None, all_: bool = False) -> list[dict]:
        rows = self._all("SELECT a.*, r.content FROM agreements a JOIN artifacts r ON r.id=a.artifact_id" + ("" if all_ else " WHERE a.status='active'") + " ORDER BY a.id")
        return [r for r in rows if not party or party in r["parties"]]

    # ------------------------------------------------------------------ inbox & events
    def inbox(self, session: dict) -> dict:
        agent_id = session["agent_id"]
        rows = self.list_requests(agent_id, "all", limit=200)
        groups: dict = {"blocking": [], "awaiting_me": [], "in_progress": [], "waiting_on_others": [], "fyi": []}
        for r in rows:
            mine_to_act = r["actor"] == agent_id
            if r["type"] == "notice":
                (groups["awaiting_me"] if r["to_agent"] == agent_id and r["status"] == "open" else groups["fyi"]).append(r)
            elif r["status"] == "local" and r["routed_to"] == agent_id:
                groups["awaiting_me"].append(r)
            elif mine_to_act and r["blocking"]:
                groups["blocking"].append(r)
            elif mine_to_act:
                groups["awaiting_me"].append(r)
            elif r["to_agent"] == agent_id and r["status"] != "open":
                groups["in_progress"].append(r)
            elif r["from_agent"] == agent_id:
                groups["waiting_on_others"].append(r)
            else:
                groups["fyi"].append(r)
        groups["unread_events"] = self.conn.execute(
            "SELECT COUNT(*) FROM events e JOIN event_audience a ON a.event_id=e.id WHERE a.agent_id=? AND e.id>? AND (e.session_id IS NULL OR e.session_id!=?)"
            " AND NOT EXISTS(SELECT 1 FROM acks k WHERE k.session_id=? AND k.event_id=e.id)",
            (agent_id, session["event_cursor"], session["id"], session["id"])).fetchone()[0]
        groups["po_present"] = self.po_present()
        return groups

    def events(self, session: dict, since: int | None = None, request_id: str | None = None, limit: int = 100, include_own: bool = False,
               advance: bool = True) -> dict:
        """Events for this session's agent after `since` (default: the session cursor). Advances the cursor unless filtered."""
        cur = session["event_cursor"] if since is None else since
        sql = "SELECT e.* FROM events e JOIN event_audience a ON a.event_id=e.id WHERE a.agent_id=? AND e.id>?"
        args: list = [session["agent_id"], cur]
        if request_id:
            sql += " AND e.request_id=?"; args.append(request_id)
        if not include_own:
            sql += " AND (e.session_id IS NULL OR e.session_id!=?)"; args.append(session["id"])
        sql += " AND NOT EXISTS(SELECT 1 FROM acks k WHERE k.session_id=? AND k.event_id=e.id)"; args.append(session["id"])
        sql += " ORDER BY e.id LIMIT ?"; args.append(min(limit, 500))
        rows = self._all(sql, *args)
        new_cursor = rows[-1]["id"] if rows else cur
        if rows and advance and since is None:
            with self.tx():
                if request_id:
                    # A request-filtered read must not skip other requests' events: mark just these as read.
                    for r in rows:
                        self.conn.execute("INSERT OR IGNORE INTO acks(session_id, event_id) VALUES(?,?)", (session["id"], r["id"]))
                else:
                    self.conn.execute("UPDATE sessions SET event_cursor=MAX(event_cursor, ?) WHERE id=?", (new_cursor, session["id"]))
        return {"events": rows, "cursor": new_cursor}

    def last_event_id(self) -> int:
        return int(self.conn.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0])

    def ack_events(self, session: dict, event_ids: list[int] | None, request_id: str | None, all_: bool) -> int:
        with self.tx():
            if all_:
                self.conn.execute("UPDATE sessions SET event_cursor=? WHERE id=?", (self.last_event_id(), session["id"]))
                event_ids = [r[0] for r in self.conn.execute("SELECT e.id FROM events e JOIN event_audience a ON a.event_id=e.id WHERE a.agent_id=?", (session["agent_id"],)).fetchall()]
            elif request_id:
                event_ids = [r[0] for r in self.conn.execute("SELECT e.id FROM events e JOIN event_audience a ON a.event_id=e.id WHERE a.agent_id=? AND e.request_id=?",
                                                             (session["agent_id"], request_id)).fetchall()]
            n = 0
            for e in event_ids or []:
                self.conn.execute("INSERT OR IGNORE INTO acks(session_id, event_id) VALUES(?,?)", (session["id"], int(e)))
                n += 1
            return n

    # ------------------------------------------------------------------ misc
    def check_permission(self, agent_id: str, repo: str, op: str) -> dict:
        a = self._agent(agent_id)
        if a["kind"] == "product_owner":
            return {"allowed": op == "read", "source": "po (read-only)"}
        target = self._one("SELECT * FROM agents WHERE id=?", repo)
        candidates = [repo] + (target["repos"] if target else [])
        try:
            candidates.append(_norm(repo))
        except OSError:
            pass
        for c in candidates:
            p = self._one("SELECT * FROM permissions WHERE agent_id=? AND repo=?", agent_id, c)
            if p:
                return {"allowed": bool(p["write"] if op == "write" else p["read"]), "source": p["source"]}
        owner = target["id"] if target else self.agent_for_cwd(repo)
        if owner == agent_id:
            return {"allowed": True, "source": "owner"}
        return {"allowed": op == "read", "source": "default (read-only on other repos; advisory — the runtime enforces)"}

    def resolve(self, ref: str) -> dict:
        m = REF_RE.fullmatch(ref.strip())
        if not m:
            return {"ref": ref, "known": False}
        repo, commit, path, ls, le = m.groups()
        a = self._one("SELECT * FROM agents WHERE id=?", repo)
        out = {"ref": ref, "repo": repo, "commit": commit, "path": path, "line_start": int(ls) if ls else None, "line_end": int(le) if le else None, "known": bool(a)}
        if a and a["repos"]:
            root = Path(a["repos"][0])
            out["root"] = str(root)
            if path:
                fp = (root / path).resolve()
                if not str(fp).lower().startswith(str(root.resolve()).lower()):
                    return {**out, "error": "path escapes the repository"}
                out["abs_path"] = str(fp); out["exists"] = fp.exists()
        return out

    def extract_refs(self, text: str) -> list[dict]:
        known = {a["id"] for a in self._all("SELECT id FROM agents")}
        return [{"raw": m.group(0), "repo": m.group(1), "commit": m.group(2), "path": m.group(3),
                 "line_start": int(m.group(4)) if m.group(4) else None, "line_end": int(m.group(5)) if m.group(5) else None}
                for m in REF_RE.finditer(text or "") if m.group(1) in known]

    def search(self, query: str, scope: str = "all", limit: int = 20) -> list[dict]:
        sql = "SELECT entity_type, entity_id, title, snippet(fts, 3, '[', ']', '…', 12) AS snippet FROM fts WHERE fts MATCH ?"
        args: list = [query]
        if scope != "all":
            sql += " AND entity_type=?"; args.append(scope.rstrip("s"))
        sql += " ORDER BY rank LIMIT ?"; args.append(min(limit, 100))
        try:
            return self._all(sql, *args)
        except sqlite3.OperationalError as e:
            raise ServiceError(f"bad query: {e}", "bad_request")

    def tick(self) -> dict:
        """Timers: expire sessions, promote local decisions to the PO."""
        with self.tx():
            cutoff = (parse_ts(now()) - timedelta(minutes=config.SESSION_IDLE_MINUTES)).isoformat().replace("+00:00", "Z")
            gone = self.conn.execute("UPDATE sessions SET status='gone', ended_at=? WHERE status!='gone' AND last_seen_at<?", (now(), cutoff)).rowcount
            self.conn.execute("UPDATE requests SET claimed_by=NULL WHERE claimed_by IN (SELECT id FROM sessions WHERE status='gone')")
            promoted = 0
            lcut = (parse_ts(now()) - timedelta(minutes=config.local_timeout_minutes())).isoformat().replace("+00:00", "Z")
            po = self._po_id() or "po"
            for r in self._all("SELECT * FROM requests WHERE type='decision' AND status='local' AND updated_at<?", lcut):
                self._set_status(r, "open", None, {"routed_to": po, "to_agent": po}, reason="local timeout; routed to PO")
                promoted += 1
            return {"sessions_expired": gone, "decisions_promoted": promoted}
