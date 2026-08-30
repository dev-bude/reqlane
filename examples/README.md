# Example: two project agents and a Product Owner

Three tiny "projects", each meant to be opened in its own agent session:

- `gridlib/` — a text-grid rendering library. Agent **gridlib**, the provider. `Grid.set()` re-renders
  the whole grid on every call; the 1.x public API is frozen.
- `dashboard/` — an app that refreshes a 200×50 metrics table every tick using gridlib. Agent
  **dashboard**, the consumer. `python -m dashboard.bench` prints the time per tick and the number of
  renders (baseline: ~12 s per tick, 10 000 renders).
- `product/` — product notes the Product Owner reads (`PRODUCT.md`: refresh must be under 50 ms;
  no breaking changes before 2.0).

Requirements: Python 3.12; `pytest` for the tests. No other dependencies.

## 1. Open the sessions (all from the chat)

Three VS Code windows or terminals, each with Claude Code in one of the example folders:

| window | directory | in the chat, the first time only |
|---|---|---|
| T1 | `examples/dashboard` | `/reqlane connect` |
| T2 | `examples/gridlib` | `/reqlane connect` |
| T3 | `examples/product` | `/reqlane po` — the Product Owner; open it when you like |

`/reqlane connect` registers the folder's agent (name = folder name) with your authority; the
agent only relays the result. Afterwards every new session in these folders reconnects on its
own and starts with the list of agents and the card. Watch the flow in the browser:
`/reqlane ui` in any window.

Then tell the dashboard agent one thing:

> "A tick must fit in 50 ms. Figure it out."

## 2. What should happen

1. **dashboard** profiles, reads `@gridlib/gridlib/__init__.py`, cannot write there
   (`reqlane perm check gridlib` → denied) and files a request:
   `reqlane req new --to gridlib --type capability --blocking --title "Batch cell updates" --goal "tick < 50 ms" --constraint "1.x API compatible" --acceptance "renders per tick == 1" --body -`,
   then waits: `reqlane wait --req req_0001`.
2. **gridlib** sees it in `reqlane inbox` (BLOCKING), asks a clarifying question with `reqlane reply`,
   then publishes a proposal with two options — A: additive `Grid.batch()` context manager;
   B: lazy `set()` + explicit `render()` (breaking) — marked `--requires-po` because B breaks the API.
3. No PO session is present, so the decision lands with the **initiator** (dashboard) as a *local*
   decision. The dashboard agent shows the choice to you in its chat. You either answer there
   (`reqlane decide req_0002 --author human --option A --reason "..."`, recorded as unattested) or say
   "hand it to the PO" (`reqlane handoff req_0002`).
4. Open T3, `reqlane connect po`; `reqlane po dashboard` lists the handed-over decision first. The PO
   agent reads `PRODUCT.md`, proposes A to you (mode `hybrid`), and records
   `reqlane decide req_0002 --option A --reason "1.x frozen" --affected gridlib,dashboard`; optionally
   `reqlane agreement publish --parties gridlib,dashboard --title "gridlib 1.x compatibility" --body -`.
5. dashboard accepts (`reqlane req accept req_0001 --option A`); gridlib implements `batch()`, runs the
   tests and delivers: `reqlane deliver req_0001 --repo gridlib --commit <hash> --tests-passed --tests-cmd "pytest -q"`.
6. dashboard integrates (`with self.grid.batch(): ...`), re-runs the benchmark (expect `renders=1`,
   tens of milliseconds) and closes the loop: `reqlane evaluate req_0001 --verdict accepted --body -`.

`reqlane req show req_0001` (or the browser page) then shows the whole chain: proposal → decision →
delivery → evaluation. You intervened exactly twice: the initial instruction and the PO decision.

If an agent goes quiet before the answer arrived, type `/reqlane inbox` in its window — see
"When something looks stuck" in the main README. To run the example again from scratch:
`git checkout -- examples/` (the agents commit into this repository) and delete `~/.reqlane/`.

## Variations

- Let gridlib **decline** ("eager rendering is part of the contract"); dashboard escalates with
  `reqlane req escalate`; the PO resolves it.
- Run the dashboard agent with a runtime that has no hooks (`reqlane install --runtime codex --dir examples/dashboard`
  writes the card into `AGENTS.md`) and see whether it still checks `reqlane inbox` on its own.
