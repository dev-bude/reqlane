# Contributing

Thanks for looking at Reqlane. It is a pre-release: the protocol and the CLI surface still move,
so please open an issue before starting anything large.

## Getting set up

```
git clone https://github.com/dev-bude/reqlane.git && cd reqlane
python -m venv .venv
.venv/bin/pip install -e ".[dev]"    # Windows: .venv\Scripts\python -m pip install -e ".[dev]"
.venv/bin/python -m pytest -q        # Windows: .venv\Scripts\python -m pytest -q
```

Python 3.12+. The tests drive the daemon through its HTTP API and cover the walkthrough's flow
and the lifecycle rules.

## What to work on

- **Bugs and rough edges** — issues are welcome, especially around the Claude Code adapter,
  Windows behaviour, and sessions that go quiet.
- **Other runtimes.** `reqlane install --runtime codex|gemini|cursor` only writes the card today.
  A proper adapter (hooks or equivalent) for another runtime is the most useful contribution.
- **Protocol changes** ([PROTOCOL.md](PROTOCOL.md)) need an issue first — the document is
  licensed CC-BY-4.0 and versioned separately from the implementation.

## House rules for changes

- Keep the layers separate: `core` owns the rules, `server` only exposes them over HTTP, `cli`
  only renders. New rules belong in `core/service.py` with a test, not in a CLI command.
- Every state transition goes through `core/lifecycle.py`. Do not special-case a transition in a
  route handler.
- Text that came from another agent is untrusted: it must stay quoted and sanitised on the way
  out (`cli/fmt.py`). Do not interpolate it into anything an agent will read as instructions.
- Add or extend a test in `tests/` for behaviour you change.
- Do not commit anything from `~/.reqlane/` — the database contains local filesystem paths.

## Commits and pull requests

- One logical change per commit; imperative subject line, as in the existing history.
- Bump `__version__` in `reqlane/__init__.py` when behaviour visible to an agent changes.
- Say in the PR what you ran, and note it if you did not run the tests.

## Security

Do not open a public issue for a vulnerability — see [SECURITY.md](SECURITY.md) for private
reporting.

## Licence

By contributing you agree that your contribution is licensed under Apache-2.0 (code) and
CC-BY-4.0 (the protocol document), matching the project's licensing.
