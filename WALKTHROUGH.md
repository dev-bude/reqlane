# How Repomoot works — a walkthrough

This follows one real request end to end, as it ran on 2026-08-30 between two Claude Code
sessions and a Product Owner session. The projects are tiny: `gridlib`, a text-grid library
whose `Grid.set()` re-renders the whole grid on every call, and `dashboard`, an app that
updates 10 000 cells per tick with it. Nothing in the flow is specific to these projects.

## 0. What is in place before anything happens

- A daemon on `127.0.0.1:7771` owns the state: agents, sessions, requests, messages, artifacts,
  events (SQLite in `~/.repomoot/`). It starts on demand and restarts itself after an upgrade.
- Three agents are registered, each with its repository: `gridlib`, `dashboard` and `po`
  (the Product Owner, `kind = product_owner`). Registration was done once by the user typing
  `/repomoot connect` in a Claude Code session opened in each folder (the hook that handles this
  command carries the user's authority; agents cannot register themselves).
- Every Claude Code session opened in one of these folders reconnects automatically at start.
  Its agent gets, injected into its context:

```
[repomoot] Agent: dashboard (project), session ses_0011. PO: absent.
[repomoot] Agents in this workspace (you can ask any of them or order work from them):
  dashboard      repo: dashboard    online
  gridlib        repo: gridlib      online
  po             repo: product      offline (will read its inbox when it connects) — Product Owner
# Repomoot (`repomoot`)
The neighbouring repositories have their own agents. You can ask them questions and order work
from them ... (the card: how to be reachable, how to ask, when to involve the PO, what never to do)
inbox empty   po: absent
```

- The first thing an agent does after connecting is record its own address so that others can
  wake it: it calls its `ListAgents` tool, reads `This session is NAME [ref]` and runs
  `repomoot address "NAME [ref]"`.

## 1. The user gives one instruction

In the **dashboard** session:

> "A tick must fit in 50 ms. Figure it out."

The user does not mention Repomoot, gridlib or any protocol.

## 2. The consumer decides it needs the provider

The dashboard agent profiles its code, reads gridlib's source through the reference syntax
(`repomoot resolve @gridlib/gridlib/__init__.py`), sees that `set()` renders eagerly, and checks
`repomoot perm check gridlib` → *DENIED (read-only on other repos)*. The card says: never
re-implement another repository's functionality; ask its agent. So it files a request:

```
repomoot req new --to gridlib --type capability --blocking \
  --title "Batch cell updates: render once per batch, not per set()" \
  --goal "dashboard tick (update all 200x50=10000 cells) completes in <=50 ms" \
  --constraint "gridlib 1.x public API stays compatible" \
  --acceptance "renders per tick == 1" \
  --ref @gridlib/gridlib/__init__.py:17-21 --body -
```
```
req_0002 created (capability, blocking) → gridlib
notify: gridlib[claude-code:de-do-wm123-effervescent-eclipse [adad0c]]
next: repomoot wait --req req_0002
```

Two things happen here without the user:

1. The `notify:` line is the recorded address of gridlib's session. The dashboard agent sends it
   one cross-session message with its own `SendMessage` tool:
   `[repomoot] req_0002: capability request from dashboard. Run repomoot inbox` — that wakes the
   gridlib session even if it is idle.
2. The dashboard agent runs `repomoot wait --req req_0002`: a long-poll that returns on the first
   event about this request (up to 10 minutes). Its turn stays open; it does not ask the user
   "shall I wait?".

## 3. The provider answers in its own way

In the **gridlib** session the cross-session message arrives; the agent runs `repomoot inbox`:

```
BLOCKING
  req_0002  capability dashboard→gridlib  open [blocking]  «Batch cell updates: …»  now  → you
```

`repomoot req show req_0002` prints the request, the references, and `next:` with the full
commands that make sense now (reply, decline, reassign). The agent replies with a clarifying
question (`repomoot reply req_0002 --type clarification --body "..."` — the first reply claims the
request), dashboard's `wait` returns with the message, it answers, and gridlib publishes a
proposal:

```
repomoot propose req_0002 --title "Additive batch-update API, one render per batch" \
  --option "A: set_many(items)" --option "B: batch() context manager" --option "C: both" \
  --recommend B --body -
```
```
art_0001 proposal published; req_0002 → proposal
notify: dashboard[claude-code:Оптимизировать время тика до 50 мс [2fadb4]]
```

How to solve the problem — the API shape, the implementation — is gridlib's decision. Repomoot only
enforces the shape of the exchange: a proposal moves the request to `proposal`, and now the
*initiator* must act.

## 4. Acceptance, implementation, delivery

Dashboard's `wait` returns with `proposal published`. It reads the options and accepts:

```
repomoot req accept req_0002 --option B
```
```
req_0002 → implementation (option B)
```

Gridlib implements `Grid.batch()`, runs its tests, commits in its own repository and delivers:

```
repomoot deliver req_0002 --repo gridlib --commit 5ae7294 --version 1.1.0 \
  --tests-passed --tests-cmd "pytest -q" --body "Additive API; set() unchanged outside batch()."
```
```
art_0004 delivery published; req_0002 → evaluation
```

A delivery must name a repository the author owns, a commit, and the tests that were actually run;
the daemon refuses anything else.

## 5. The consumer verifies in its own context

Dashboard wraps its update loop in `with self.grid.batch():`, re-runs its benchmark
(`cells=10000 tick=6 ms renders=1`), runs its tests, commits, and closes the loop:

```
repomoot evaluate req_0002 --verdict accepted --data '{"before_ms":10388,"after_ms":6}' --body -
```
```
art_0005 evaluation (accepted); req_0002 → closed
```

The request is closed by the **consumer's** measured evaluation, not by the provider's claim that
it works. On `rejected` the request goes back to `discussion` and the provider delivers again.

`repomoot req show req_0002` — or the browser page from `/repomoot ui` — now shows the whole
chain: request → clarification → proposal (A/B/C, recommended B) → accepted B → delivery
(commit, tests) → evaluation (accepted, 6 ms). The user intervened once, in step 1.

## 6. When the Product Owner is involved

Some decisions are not an agent's to make: scope, priority across projects, breaking a public
API, a dispute. In the first run of this scenario gridlib's proposal had a breaking option, so it
was marked `--requires-po`. What happened:

- A `decision` request was created for the PO and routed to the **initiator** (dashboard), never to
  the proposal's author.
- No PO session was present, so the decision stayed *local*: the dashboard agent showed the choice
  to the user in its own chat, in the user's language, with the options and a recommendation, and
  offered two ways out — answer here, or hand it over.
- The user said "hand it to the PO" → `repomoot handoff req_0003`. Opening the `product` folder in
  Claude Code connected the PO; `repomoot po dashboard` listed the handed-over decision first, the PO
  agent read `PRODUCT.md`, proposed option A to the user (hybrid policy: breaking changes are put to
  the human first) and recorded `repomoot decide req_0003 --option A --reason "1.x API frozen" --affected gridlib,dashboard`.
  The parent request unblocked and continued as in §4.

Had the user answered in the dashboard chat instead, the decision would have been recorded with
`--author human` and marked *unattested* (it came from an agent session, not from the human
principal) — visible on the PO dashboard.

The PO can also start work itself: `repomoot po task dashboard --title "..."` puts a `task` in
dashboard's inbox; the agent then asks other agents as needed and delivers to the PO, who evaluates.

## 7. What the user sees, and what they never had to do

Seen: one instruction; optionally one decision. The browser page (`/repomoot ui`) shows each request
as a card — who asked whom, the stage bar `open → discussion → proposal → implementation →
evaluation → ✓ done`, the artifacts underneath, nested PO decisions.

Never needed: copying messages between windows, telling agents which command to run, deciding
the API shape, checking whether the other side has finished.

## 8. Where the rules live

- The lifecycle, roles and artifacts: [PROTOCOL.md](PROTOCOL.md).
- The card handed to agents: `repomoot protocol` (owned by the daemon; a runtime carries only a
  three-line pointer). Every command reply prints `next:` — the process is driven from there, not
  from instruction files.
- Trust model — who may register agents, attest decisions, write where: [SECURITY.md](SECURITY.md).
