# Reqlane protocol v1 (draft)

This document describes the request lifecycle and the roles independently of the `reqlane`
implementation, so that other runtimes or tools can implement or map to it. Licensed
CC-BY-4.0. Transport is not part of the protocol: the reference daemon exposes it over HTTP
and a CLI; the same model maps onto A2A Task states and MCP Tasks (see §7).

## 1. Participants

- **Agent** — an autonomous actor that *owns* one or more repositories. It makes engineering
  decisions inside its repositories and never writes outside them.
- **Product Owner (PO)** — one agent per workspace with `kind = product_owner`. Decides what
  owners cannot: scope, priority across projects, breaking compatibility, disputes; records
  agreements. Does not write code.
- **Human** — the principal behind the workspace. Registers agents, may answer a decision
  routed to any agent's chat, may speak "as" an agent, sets the PO delegation policy.
- **Session** — a live runtime instance bound to an agent. Requests are addressed to agents,
  never to sessions. Several sessions of one agent may coexist; one of them *claims* a request.

## 2. Request

The unit of exchange. Owned by its **initiator** (`from`), addressed to a **recipient**
(`to`, an agent). Fields: `type`, `title`, `body`, `goal {description, constraints[],
acceptance[]}`, `priority`, `blocking`, `cc[]`, `parent`, `labels[]`, `refs[]`.

Types and what closes them:

| type | meaning | closed by |
|---|---|---|
| `question` | information, no code change | initiator (`close`) after `answered` |
| `capability` | new behaviour in the recipient's repo | initiator's `evaluation: accepted` |
| `bug` | defect with reproduction | initiator's `evaluation: accepted` |
| `change` | initiator attaches a patch/branch | initiator's `evaluation: accepted` |
| `review` | look at my commit/branch | initiator (`close`) after `answered` |
| `notice` | information to consumers (release, breaking change) | recipient (`ack`) |
| `task` | assignment; PO → agent (or an agent to itself) | initiator's `evaluation: accepted` |
| `decision` | a question to the PO | a `decision` artifact |

References to code are written `@repo/path:l1-l2`, `@repo@commit`, `@repo!path@commit:l1-l2`;
content is never pasted, the reader resolves it in the repository.

## 3. Lifecycle

```
open ─claim/reply→ discussion|triage|review|in_review
   discussion ─proposal→ proposal ─accept→ implementation ─delivery→ evaluation ─accepted→ closed
                                                                         └─rejected→ discussion (iteration+1)
   bug/change: triage|review ─delivery→ evaluation (no proposal required)
   question/review: ─answer→ answered ─close→ closed
   any working status ─decline(reason)→ declined ─escalate→ blocked (resume: discussion)
   any ─withdraw→ withdrawn (open sub-requests → wont_do)
   any ─ask PO / requires_po→ blocked (resume: previous) until the child decision closes
   notice: open ─ack→ acknowledged
   decision: local?→ open → deliberation → closed(decided | delegated | wont_do)
```

Who may do what: the recipient claims, replies, proposes, delivers, declines, reassigns (only
to a third agent, only while `open|discussion|triage|review|in_review`); the initiator accepts
a proposal, evaluates, closes, withdraws, escalates; the PO decides, delegates back, assigns
tasks, marks `wont_do`, publishes agreements.

## 4. Artifacts

Durable results attached to a request: `investigation`, `proposal {options[{id, summary,
compat, effort}], recommended}`, `delivery {repo, commit|branch, tests}`, `evaluation
{verdict, measurements}`, `decision {option, reason, affected[], author, attested}`, and
`agreement {parties[]}` (workspace-wide, acknowledged by each party). A delivery must reference
a repository its author owns and state which tests were run. An evaluation is written by the
**consumer** in its own context — "the owner believes it works" and "the consumer verified it"
are different facts.

## 5. Product Owner routing

When an agent asks the PO (explicitly, by escalating, or because a proposal is marked
`requires_po`) and no PO session is present, the decision request is created in status
`local`, routed to the **initiator of the underlying request** (never to the proposal's
author). That agent shows the choice to its user in the user's language; the user either
answers there (recorded as `author = human`, `attested = false` unless the human principal
confirmed it) or says "hand over to PO" (`handoff` → the PO queue). Unanswered local
decisions are promoted to the PO queue after a timeout. The PO applies a **delegation policy**:
kinds it decides alone, kinds it must put to the human first, and a default.

## 6. Events

Append-only log with an audience per event. Each session has a cursor; readers pull events
after their cursor (long-poll or stream). Delivery is "pulled and acknowledged", never
"sent". Notifications through runtime-specific channels are optimisations, not part of the
protocol.

## 7. Mapping to other protocols (informative)

- **A2A Task states**: `open→submitted`, `discussion/implementation→working`,
  `proposal/evaluation→input-required` (the initiator must act), `closed/accepted→completed`,
  `declined/wont_do→rejected`, `withdrawn→canceled`. A2A has no consumer evaluation step; it
  can be carried as an extension.
- **MCP Tasks**: a request maps to a task handle; `wait` is the poll; artifacts are results.
- **OpenTelemetry**: request = span (parent = `parent`), lifecycle transitions and artifacts =
  span events, agent = service. Trace export is planned on this mapping.
