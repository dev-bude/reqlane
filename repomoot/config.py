"""Paths, config and token handling. All state lives under REPOMOOT_HOME (default ~/.repomoot)."""
from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

DEFAULT_PORT = 7771
SESSION_IDLE_MINUTES = 30      # session considered gone
PO_PRESENT_MINUTES = 10        # PO counts as present only if seen this recently
LOCAL_DECISION_TIMEOUT_MINUTES = 240
STALE_HOURS = 24


def _private_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(p, stat.S_IRWXU)
    except OSError:
        pass
    return p


def _private_file(f: Path, content: str) -> None:
    f.write_text(content, encoding="utf-8")
    try:
        os.chmod(f, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def home() -> Path:
    p = _private_dir(Path(os.environ.get("REPOMOOT_HOME") or Path.home() / ".repomoot"))
    _private_dir(p / "sessions")
    return p


def db_path() -> Path:
    return home() / "repomoot.db"


def port() -> int:
    return int(os.environ.get("REPOMOOT_PORT") or DEFAULT_PORT)


def base_url() -> str:
    return os.environ.get("REPOMOOT_URL") or f"http://127.0.0.1:{port()}"


def _token(name: str) -> str:
    f = home() / name
    if not f.exists():
        _private_file(f, secrets.token_urlsafe(32))
    return f.read_text(encoding="utf-8").strip()


def token() -> str:
    """Shared daemon token: every local client (agents included) has it."""
    return _token("token")


def human_token() -> str:
    """Human principal: registering agents, policy, attested decisions. Sent only from a TTY or via REPOMOOT_HUMAN_TOKEN."""
    return _token("human.token")


def local_timeout_minutes() -> int:
    return int(os.environ.get("REPOMOOT_LOCAL_TIMEOUT_MIN") or LOCAL_DECISION_TIMEOUT_MINUTES)


def write_private(f: Path, content: str) -> None:
    _private_file(f, content)
