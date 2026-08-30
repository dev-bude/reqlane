"""The walkthrough flow (WALKTHROUGH.md) and the rules, driven through the HTTP API."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reqlane.core.service import Service
from reqlane.server.app import create_app

TOKEN, HUMAN = "t", "h"


@pytest.fixture()
def api(tmp_path: Path):
    os.environ["REQLANE_HOME"] = str(tmp_path / "home")
    svc = Service(tmp_path / "reqlane.db")
    with TestClient(create_app(svc, api_token=TOKEN, human_token=HUMAN)) as c:
        c.svc = svc
        yield c


def H(session: str | None = None, human: bool = False) -> dict:
    h = {"Authorization": f"Bearer {TOKEN}"}
    if session:
        h["X-Reqlane-Session"] = session
    if human:
        h["X-Reqlane-Human"] = HUMAN
    return h


def ok(r):
    assert r.status_code == 200, r.text
    return r.json()


def connect(api, agent, cwd, kind="project", depends_on=None, name=None, human=True):
    return ok(api.post("/sessions/connect", json={"agent": agent, "kind": kind, "cwd": str(cwd), "depends_on": depends_on or [], "name": name, "runtime": "test"},
                       headers=H(human=human)))["token"]


def mkdirs(tmp_path, *names):
    out = []
    for n in names:
        d = tmp_path / n
        d.mkdir()
        out.append(d)
    return out


def test_registration_needs_human_and_own_directory(api, tmp_path):
    a_dir, b_dir = mkdirs(tmp_path, "a", "b")
    r = api.post("/sessions/connect", json={"agent": "a", "cwd": str(a_dir), "runtime": "test"}, headers=H())
    assert r.status_code == 403  # agent unknown, no human token
    a = connect(api, "a", a_dir)  # human registers
    # an agent (no human token) reconnects from its own dir
    a2 = connect(api, "a", a_dir, human=False)
    assert a2
    # ... but cannot connect as 'a' from another directory, nor become the PO
    assert api.post("/sessions/connect", json={"agent": "a", "cwd": str(b_dir), "runtime": "test"}, headers=H()).status_code == 403
    assert api.post("/sessions/connect", json={"agent": "po", "kind": "po", "cwd": str(b_dir), "runtime": "test"}, headers=H()).status_code == 403
    # repos are not extended by connecting from elsewhere without the human
    assert ok(api.get("/whoami", headers=H(a2)))["repos"] == [str(a_dir.resolve()).replace("\\", "/").lower()]
    # same (agent, cwd, name) reconnect closes the previous session
    assert api.get("/whoami", headers=H(a)).status_code == 401
    # kind is fixed at registration: connecting an existing project agent with kind=po changes nothing
    ok(api.post("/sessions/connect", json={"agent": "a", "kind": "po", "cwd": str(a_dir), "runtime": "test"}, headers=H(human=True)))
    assert ok(api.get("/agents", headers=H()))["agents"][0]["kind"] == "project"


def test_demo_flow(api, tmp_path):
    gl_dir, db_dir, po_dir = mkdirs(tmp_path, "gridlib", "dashboard", "po")
    gl = connect(api, "gridlib", gl_dir)
    db = connect(api, "dashboard", db_dir, depends_on=["gridlib"])
    assert ok(api.get("/whoami", headers=H(gl)))["consumers"] == ["dashboard"]
    assert ok(api.get("/agent-for-cwd", params={"cwd": str(db_dir / "sub")}, headers=H()))["agent"] == "dashboard"
    assert ok(api.get("/perm", params={"repo": "gridlib", "op": "write"}, headers=H(db)))["allowed"] is False

    # 2. request
    res = ok(api.post("/requests", headers=H(db), json={
        "to": "gridlib", "type": "capability", "blocking": True, "title": "Batch cell updates",
        "goal": {"description": "tick < 50 ms", "constraints": ["1.x compatible"], "acceptance": ["renders == 1"]},
        "body": "See @gridlib/gridlib/__init__.py:17-21 and @dashboard/dashboard/__init__.py:18-23."}))
    rid = res["request"]["id"]
    assert rid == "req_0001" and [n["agent"] for n in res["notify"]] == ["gridlib"]

    # 3. gridlib: inbox, reply claims, dashboard waits via long-poll and its cursor advances
    ib = ok(api.get("/inbox", headers=H(gl)))
    assert [r["id"] for r in ib["blocking"]] == [rid]
    res = ok(api.post(f"/requests/{rid}/messages", headers=H(gl), json={"body": "Final text only?", "type": "clarification"}))
    assert res["request"]["status"] == "discussion" and res["status"] == "discussion"
    ev = ok(api.get("/events", params={"request_id": rid, "wait": 1}, headers=H(db)))
    assert any(e["type"] == "message" for e in ev["events"])
    assert ok(api.get("/events", params={"request_id": rid, "wait": 0}, headers=H(db)))["events"] == []  # seen once, not replayed
    ev2 = ok(api.get("/events", params={"wait": 0}, headers=H(db)))  # unfiltered read still returns events of other requests
    assert all(e["request_id"] == rid for e in ev2["events"]) or ev2["events"] == []
    assert ok(api.get("/events", headers=H(db)))["events"] == []
    ok(api.post(f"/requests/{rid}/messages", headers=H(db), json={"body": "Yes.", "type": "answer"}))
    d = ok(api.get(f"/requests/{rid}", headers=H(gl)))
    assert d["actor"] == "gridlib" and any(x.startswith("reqlane propose") for x in d["next"])
    # delivery before proposal acceptance is refused for a capability
    r = api.post("/artifacts", headers=H(gl), json={"type": "delivery", "request_id": rid, "title": "x", "data": {"repo": "gridlib", "commit": "a", "tests": {}}})
    assert r.status_code == 409

    # 4. proposal requiring the PO; PO absent -> local decision routed to the INITIATOR (dashboard)
    res = ok(api.post("/artifacts", headers=H(gl), json={
        "type": "proposal", "request_id": rid, "title": "Batched updates", "requires_po": True, "content": "A or B",
        "data": {"options": [{"id": "A", "summary": "batch()", "compat": "full"}, {"id": "B", "summary": "lazy set()", "compat": "breaking"}], "recommended": "A"}}))
    assert res["request"]["status"] == "blocked" and res["mode"] == "local"
    dec = res["decision"]["id"]
    assert res["decision"]["routed_to"] == "dashboard" and res["decision"]["kind"] == "breaking_change"
    assert [r["id"] for r in ok(api.get("/inbox", headers=H(db)))["awaiting_me"]] == [dec]
    # the proposal's author cannot record "the human's decision" on it
    assert api.post(f"/requests/{dec}/decide", headers=H(gl), json={"option": "A", "reason": "x", "author": "human"}).status_code == 403

    # 5b. dashboard hands over to the (absent) PO
    res = ok(api.post(f"/requests/{dec}/handoff", headers=H(db)))
    assert res["request"]["status"] == "open" and res["request"]["to_agent"] == "po"

    # 6. PO registered by the human; decision requests are re-addressed to it
    po = connect(api, "po", po_dir, kind="po")
    assert ok(api.get("/health"))["po_present"] is True
    dash = ok(api.get("/po/dashboard", headers=H(po)))
    assert [(r["id"], r["origin"]) for r in dash["decisions"]] == [(dec, "handed_over")]
    assert dash["decisions"][0]["to_agent"] == "po"
    assert api.post(f"/requests/{dec}/decide", headers=H(gl), json={"option": "A", "reason": "x"}).status_code == 403
    res = ok(api.post(f"/requests/{dec}/decide", headers=H(po), json={"option": "A", "reason": "1.x frozen", "affected": ["gridlib", "dashboard"]}))
    assert res["request"]["status"] == "closed" and res["attested"] is True
    parent = ok(api.get(f"/requests/{rid}", headers=H(db)))
    assert parent["request"]["status"] == "proposal" and any(x.startswith("reqlane req accept") for x in parent["next"])
    gid = ok(api.post("/agreements", headers=H(po), json={"title": "1.x compat", "content": "No breaking changes before 2.0", "parties": ["gridlib", "dashboard"]}))["agreement"]["id"]
    # policy needs the human token even from the PO session
    assert api.put("/po/policy", headers=H(po), json={"mode": "agent"}).status_code == 403
    assert ok(api.put("/po/policy", headers=H(po, human=True), json={"mode": "agent", "auto_decide": ["priority"]}))["mode"] == "agent"

    # 7. accept, deliver (needs tests), 8. evaluate
    ok(api.post(f"/requests/{rid}/accept_proposal", headers=H(db), json={"option": "A"}))
    assert ok(api.get(f"/requests/{rid}", headers=H(db)))["request"]["accepted_option"] == "A"
    assert api.post(f"/requests/{rid}/accept_proposal", headers=H(db), json={"option": "A"}).status_code == 409
    r = api.post("/artifacts", headers=H(gl), json={"type": "delivery", "request_id": rid, "title": "x", "data": {"repo": "gridlib", "commit": "abc123"}})
    assert r.status_code == 400  # tests missing
    res = ok(api.post("/artifacts", headers=H(gl), json={"type": "delivery", "request_id": rid, "title": "Grid.batch()",
                                                          "data": {"repo": "gridlib", "commit": "abc123", "tests": {"result": "passed"}}}))
    assert res["request"]["status"] == "evaluation"
    ok(api.post(f"/agreements/{gid}/ack", headers=H(gl)))
    res = ok(api.post("/artifacts", headers=H(db), json={"type": "evaluation", "request_id": rid, "title": "bench", "verdict": "accepted", "data": {"after_ms": 20}}))
    assert res["request"]["status"] == "closed"
    assert sorted(ok(api.post(f"/agreements/{gid}/ack", headers=H(db)))["acknowledged"]) == ["dashboard", "gridlib"]
    full = ok(api.get(f"/requests/{rid}", params={"include": "messages,artifacts,events"}, headers=H(po)))
    assert [a["type"] for a in full["artifacts"]] == ["proposal", "delivery", "evaluation"]
    assert any(h["entity_id"] == rid for h in ok(api.get("/search", params={"q": "batch"}, headers=H()))["results"])


def test_local_decision_unattested_vs_attested_and_timeout(api, tmp_path, monkeypatch):
    (a_dir,) = mkdirs(tmp_path, "a")
    a = connect(api, "alpha", a_dir)
    res = ok(api.post("/po/ask", headers=H(a), json={"title": "Scope?", "kind": "scope_change", "options": [{"id": "X", "summary": "x"}, {"id": "Y", "summary": "y"}]}))
    dec = res["request"]["id"]
    assert res["mode"] == "local" and res["routed_to"] == "alpha"
    assert api.post(f"/requests/{dec}/decide", headers=H(a), json={"option": "Z", "reason": "r", "author": "human"}).status_code == 400
    res = ok(api.post(f"/requests/{dec}/decide", headers=H(a), json={"option": "X", "reason": "r", "author": "human"}))
    assert res["attested"] is False and res["artifact"]["data"]["via_agent"] == "alpha"
    res = ok(api.post("/po/ask", headers=H(a), json={"title": "Another?", "kind": "priority"}))
    res2 = ok(api.post(f"/requests/{res['request']['id']}/decide", headers=H(a, human=True), json={"decision": "do it", "reason": "r", "author": "human"}))
    assert res2["attested"] is True
    # timeout promotes a local decision to the PO queue
    res = ok(api.post("/po/ask", headers=H(a), json={"title": "Priority?", "kind": "priority"}))
    dec3 = res["request"]["id"]
    monkeypatch.setenv("REQLANE_LOCAL_TIMEOUT_MIN", "0")
    with api.svc.tx():
        api.svc.conn.execute("UPDATE requests SET updated_at='2000-01-01T00:00:00Z' WHERE id=?", (dec3,))
    assert ok(api.post("/admin/tick", headers=H()))["decisions_promoted"] == 1
    assert ok(api.get(f"/requests/{dec3}", headers=H(a)))["request"]["status"] == "open"
    # PO registered later adopts the queue
    (p_dir,) = mkdirs(tmp_path, "p")
    po = connect(api, "owner", p_dir, kind="po")
    assert [r["id"] for r in ok(api.get("/po/dashboard", headers=H(po)))["decisions"]] == [dec3]
    assert ok(api.get(f"/requests/{dec3}", headers=H(po)))["request"]["to_agent"] == "owner"
    # unattested decision is listed for the PO
    assert [u["request_id"] for u in ok(api.get("/po/dashboard", headers=H(po)))["unattested_decisions"]] == [dec]


def test_state_machine_rules(api, tmp_path):
    x_dir, y_dir, z_dir = mkdirs(tmp_path, "x", "y", "z")
    x, y, z = connect(api, "x", x_dir), connect(api, "y", y_dir), connect(api, "z", z_dir)
    assert ok(api.get("/whoami", headers=H(x)))["depends_on"] == []
    # question: answer -> answered -> close by initiator; idempotency
    q = ok(api.post("/requests", headers=H(x), json={"to": "y", "type": "question", "title": "Does it?", "idem": "k1"}))
    assert ok(api.post("/requests", headers=H(x), json={"to": "y", "type": "question", "title": "Does it?", "idem": "k1"}))["duplicate"]
    qid = q["request"]["id"]
    assert ok(api.get("/whoami", headers=H(x)))["depends_on"] == ["y"]  # inferred from the request
    assert ok(api.post(f"/requests/{qid}/messages", headers=H(y), json={"body": "Yes", "type": "answer"}))["request"]["status"] == "answered"
    assert api.post(f"/requests/{qid}/action", headers=H(y), json={"action": "reassign", "to": "z"}).status_code == 409  # not in open/discussion
    ok(api.post(f"/requests/{qid}/action", headers=H(x), json={"action": "close"}))
    assert api.post(f"/requests/{qid}/claim", headers=H(y)).status_code == 409  # closed
    # decline -> escalate from declined blocks the parent with resume=discussion; PO absent -> local at initiator
    c = ok(api.post("/requests", headers=H(x), json={"to": "y", "type": "capability", "title": "Do X"}))["request"]["id"]
    assert api.post(f"/requests/{c}/action", headers=H(y), json={"action": "decline"}).status_code == 400
    ok(api.post(f"/requests/{c}/action", headers=H(y), json={"action": "decline", "reason": "out of scope"}))
    res = ok(api.post(f"/requests/{c}/escalate", headers=H(x), json={"question": "Who is right?", "kind": "conflict"}))
    assert res["mode"] == "local" and ok(api.get(f"/requests/{c}", headers=H(x)))["request"]["status"] == "blocked"
    # withdrawing the parent cascades to the child decision
    ok(api.post(f"/requests/{c}/action", headers=H(x), json={"action": "withdraw"}))
    assert ok(api.get(f"/requests/{res['request']['id']}", headers=H(x)))["request"]["status"] == "wont_do"
    # reassign only to a third party, only while open; notice only to consumers
    b = ok(api.post("/requests", headers=H(x), json={"to": "y", "type": "bug", "title": "crash"}))["request"]["id"]
    assert api.post(f"/requests/{b}/action", headers=H(y), json={"action": "reassign", "to": "x"}).status_code == 400
    assert ok(api.post(f"/requests/{b}/action", headers=H(y), json={"action": "reassign", "to": "z", "reason": "theirs"}))["request"]["to_agent"] == "z"
    assert api.post("/requests", headers=H(z), json={"to": "x", "type": "notice", "title": "v2"}).status_code == 403  # x is not z's consumer
    n = ok(api.post("/requests", headers=H(y), json={"to": "x", "type": "notice", "title": "v2 soon", "labels": ["breaking"]}))["request"]["id"]  # x asked y earlier -> consumer
    assert ok(api.post(f"/requests/{n}/action", headers=H(x), json={"action": "ack"}))["request"]["status"] == "acknowledged"
    # bug delivered directly from triage (no proposal needed)
    b2 = ok(api.post("/requests", headers=H(x), json={"to": "y", "type": "bug", "title": "crash2"}))["request"]["id"]
    ok(api.post(f"/requests/{b2}/claim", headers=H(y)))
    assert ok(api.post("/artifacts", headers=H(y), json={"type": "delivery", "request_id": b2, "title": "fix", "data": {"repo": "y", "commit": "c1", "tests": {"result": "passed"}}}))["request"]["status"] == "evaluation"
    # human speaks as an agent only with the human token; input is sanitized
    assert api.post(f"/requests/{b2}/messages", headers=H(x), json={"body": "hi", "as_agent": "y"}).status_code == 403
    m = ok(api.post(f"/requests/{b2}/messages", headers=H(x, human=True), json={"body": "hi\x1b[31m there​", "as_agent": "y"}))["message"]
    assert m["from_agent"] == "y" and m["from_session"] is None and m["body"] == "hi[31m there"
    assert api.post("/requests", headers=H(x), json={"to": "y", "type": "question", "title": "t" * 201}).status_code == 400
    # auth
    assert api.get("/inbox", headers=H()).status_code == 401
    assert api.get("/inbox", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_two_sessions_same_agent(api, tmp_path):
    x_dir, y_dir = mkdirs(tmp_path, "x", "y")
    x = connect(api, "x", x_dir)
    y1 = connect(api, "y", y_dir, name="one")
    y2 = connect(api, "y", y_dir, name="two", human=False)
    rid = ok(api.post("/requests", headers=H(x), json={"to": "y", "type": "capability", "title": "T"}))["request"]["id"]
    ok(api.post(f"/requests/{rid}/claim", headers=H(y1)))
    assert api.post(f"/requests/{rid}/claim", headers=H(y2)).status_code == 409
    assert api.post(f"/requests/{rid}/messages", headers=H(y2), json={"body": "me too"}).status_code == 409
    # both sessions see the event; cursors are independent
    assert any(e["type"] == "request.created" for e in ok(api.get("/events", headers=H(y2)))["events"])
    assert any(e["type"] == "request.created" for e in ok(api.get("/events", headers=H(y1)))["events"])
    assert ok(api.get("/events", headers=H(y1)))["events"] == []
    # session one leaves -> claim released
    ok(api.post("/sessions/disconnect", headers=H(y1)))
    assert ok(api.post(f"/requests/{rid}/claim", headers=H(y2)))["request"]["claimed_by"]
