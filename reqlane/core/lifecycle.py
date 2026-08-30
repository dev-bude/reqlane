"""Request types, statuses and allowed transitions. Pure functions; the service enforces them."""
from __future__ import annotations

TYPES = ("question", "capability", "bug", "change", "review", "decision", "notice", "task")
ARTIFACT_TYPES = ("investigation", "proposal", "delivery", "evaluation", "note")
KINDS = ("priority", "scope_change", "breaking_change", "conflict", "budget", "access", "clarification", "other")
PRIORITIES = ("low", "normal", "high")

TERMINAL = {"closed", "declined", "withdrawn", "acknowledged", "wont_do"}
WORKING = {"open", "discussion", "triage", "review", "in_review", "deliberation"}

CLAIM_STATUS = {
    "question": "discussion", "capability": "discussion", "bug": "triage", "change": "review",
    "review": "in_review", "decision": "deliberation", "task": "discussion",
}
INITIATOR_ACTS = {"proposal", "evaluation", "answered", "declined"}

# From which statuses a delivery may be published, per request type.
DELIVERY_FROM = {
    "capability": {"implementation"}, "task": {"implementation"},
    "bug": {"triage", "discussion", "implementation", "evaluation"}, "change": {"review", "discussion", "implementation", "evaluation"},
}
PROPOSAL_FROM = {"open", "discussion", "triage", "review", "proposal"}
REASSIGN_FROM = {"open", "discussion", "triage", "review", "in_review"}
DECLINE_FROM = {"open", "discussion", "triage", "review", "in_review", "proposal"}


class TransitionError(Exception):
    def __init__(self, msg: str, code: str = "bad_transition"):
        super().__init__(msg)
        self.code = code


def status_after_artifact(art_type: str, verdict: str | None) -> str | None:
    if art_type == "proposal":
        return "proposal"
    if art_type == "delivery":
        return "evaluation"
    if art_type == "evaluation":
        if verdict == "accepted":
            return "closed"
        if verdict == "rejected":
            return "discussion"
        raise TransitionError("evaluation needs verdict accepted|rejected", "bad_request")
    return None


def actor_for(req: dict, last_message_from: str | None) -> str | None:
    """Which agent is expected to act next on this request, or None."""
    s = req["status"]
    if s in TERMINAL or s == "blocked":
        return None
    if s == "local":
        return req.get("routed_to") or req["from_agent"]
    if req["type"] == "decision":
        return req["to_agent"]
    if s in INITIATOR_ACTS:
        return req["from_agent"]
    if s in {"open", "implementation", "triage", "in_review", "review"}:
        return req["to_agent"]
    if s == "discussion":
        if last_message_from and last_message_from == req["to_agent"]:
            return req["from_agent"]
        return req["to_agent"]
    return req["to_agent"]


def next_actions(req: dict, me: str) -> list[str]:
    """Full commands the current agent may run next (printed as `next:`)."""
    s, t, rid = req["status"], req["type"], req["id"]
    initiator, recipient = req["from_agent"] == me, req["to_agent"] == me
    out: list[str] = []
    if s in TERMINAL:
        return out
    if t == "notice":
        return [f"reqlane req ack {rid}"] if recipient and s == "open" else []
    if t == "decision":
        if s == "local" and req.get("routed_to") == me:
            out += [f"reqlane decide {rid} --author human --option <id> --reason \"<user's words>\"", f"reqlane handoff {rid}"]
        elif recipient and s in {"open", "deliberation"}:
            out += [f"reqlane decide {rid} --option <id> --reason ... --affected a,b", f"reqlane po delegate {rid} --reason ..."]
        if initiator and s not in {"local"}:
            out.append(f"reqlane req withdraw {rid}")
        return out
    if recipient and s == "open":
        out += [f"reqlane reply {rid} --body ...", f"reqlane req decline {rid} --reason ...", f"reqlane req reassign {rid} --to <agent>"]
    if recipient and s in {"discussion", "triage", "review", "in_review"}:
        out.append(f"reqlane reply {rid} --body ...")
        if t in {"capability", "task"}:
            out.append(f"reqlane propose {rid} --title ... --option \"A: ...\" --recommend A --body ...")
        if t in {"bug", "change"}:
            out.append(f"reqlane deliver {rid} --repo <you> --commit <hash> --tests-passed|--tests-failed --body ...")
        if t in {"question", "review"}:
            out.append(f"reqlane reply {rid} --type answer --body ...")
        if s in DECLINE_FROM:
            out.append(f"reqlane req decline {rid} --reason ...")
    if initiator and s == "proposal":
        out += [f"reqlane req accept {rid} --option <id>", f"reqlane reply {rid} --body ...", f"reqlane req escalate {rid} --question ... --kind ..."]
    if recipient and s == "implementation":
        out += [f"reqlane deliver {rid} --repo <you> --commit <hash> --tests-passed|--tests-failed --body ...", f"reqlane reply {rid} --body ..."]
    if initiator and s == "evaluation":
        out += [f"reqlane evaluate {rid} --verdict accepted|rejected --body ...", f"reqlane reply {rid} --body ..."]
    if initiator and s == "answered":
        out += [f"reqlane req close {rid}", f"reqlane reply {rid} --body ..."]
    if initiator and s == "declined":
        out += [f"reqlane req escalate {rid} --question ... --kind conflict", f"reqlane req withdraw {rid}"]
    if initiator and s not in TERMINAL and s != "declined":
        out.append(f"reqlane req withdraw {rid}")
    return out
