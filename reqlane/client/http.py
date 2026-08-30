"""HTTP client for the daemon + session files + human principal. Used by the CLI, hooks and the MCP proxy."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

from .. import config

EXIT = {"ok": 0, "bad_request": 2, "not_connected": 3, "forbidden": 4, "bad_transition": 4, "conflict": 4,
        "not_found": 5, "daemon_unavailable": 6, "unauthorized": 6, "timeout": 7}


class ClientError(Exception):
    def __init__(self, message: str, code: str = "bad_request", hint: str | None = None, status: int = 400):
        super().__init__(message)
        self.code, self.hint, self.status = code, hint, status

    @property
    def exit_code(self) -> int:
        return EXIT.get(self.code, 1)


# ---------------------------------------------------------------- human principal
def human_token_if_human() -> str | None:
    """The human token is sent only when a person is plausibly at the keyboard (TTY) or when CI passes it explicitly."""
    env = os.environ.get("REQLANE_HUMAN_TOKEN")
    if env:
        return env
    try:
        if sys.stdin.isatty() and sys.stdout.isatty():
            return config.human_token()
    except (ValueError, OSError):
        pass
    return None


# ---------------------------------------------------------------- session files
def _key(cwd: Path, name: str | None) -> str:
    h = hashlib.sha1(str(cwd.resolve()).lower().encode()).hexdigest()[:16]
    return f"{h}.{name}" if name else h


def session_file(cwd: Path, name: str | None) -> Path:
    return config.home() / "sessions" / (_key(cwd, name) + ".json")


def save_session(cwd: Path, name: str | None, data: dict) -> Path:
    f = session_file(cwd, name)
    config.write_private(f, json.dumps({**data, "cwd": str(cwd.resolve()), "name": name}, indent=1))
    return f


def find_session(cwd: Path | None = None, name: str | None = None, exact: bool = False) -> dict | None:
    """Session for cwd (walking up to parents unless exact); prefer REQLANE_SESSION name."""
    cwd = (cwd or Path.cwd()).resolve()
    name = name or os.environ.get("REQLANE_SESSION") or None
    for p in [cwd] if exact else [cwd, *cwd.parents]:
        for cand in ([session_file(p, name)] if name else []) + [session_file(p, None)]:
            if cand.exists():
                try:
                    return json.loads(cand.read_text(encoding="utf-8")) | {"_file": str(cand)}
                except ValueError:
                    continue
    return None


def update_session(data: dict, **changes) -> None:
    f = Path(data["_file"])
    cur = json.loads(f.read_text(encoding="utf-8"))
    cur.update(changes)
    config.write_private(f, json.dumps(cur, indent=1))


def drop_session(data: dict) -> None:
    Path(data["_file"]).unlink(missing_ok=True)


# ---------------------------------------------------------------- daemon
def daemon_alive(url: str, timeout: float = 0.5) -> bool:
    try:
        return httpx.get(url + "/health", timeout=timeout).status_code == 200
    except httpx.HTTPError:
        return False


def start_daemon(url: str, wait: float = 8.0) -> bool:
    if daemon_alive(url):
        return True
    log = config.home() / "daemon.log"
    env = {k: v for k, v in os.environ.items() if not k.startswith("AW_HUMAN")}
    with open(log, "ab") as logf:
        kw: dict = {"stdout": logf, "stderr": subprocess.STDOUT, "stdin": subprocess.DEVNULL, "env": env, "close_fds": True}
        if sys.platform == "win32":
            kw["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0x8)
        else:
            kw["start_new_session"] = True
        subprocess.Popen([sys.executable, "-m", "reqlane.server.run"], **kw)
    t0 = time.monotonic()
    while time.monotonic() - t0 < wait:
        if daemon_alive(url):
            return True
        time.sleep(0.2)
    return False


# ---------------------------------------------------------------- client
class Client:
    def __init__(self, base_url: str | None = None, token: str | None = None, session_token: str | None = None,
                 autostart: bool = True, transport: httpx.BaseTransport | None = None, human: str | None = None):
        self.base_url = base_url or config.base_url()
        self.token = token or config.token()
        self.session_token = session_token
        self.human = human
        self.autostart = autostart
        self._http = httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(30.0, read=660.0), transport=transport)

    def headers(self) -> dict:
        h = {"Authorization": f"Bearer {self.token}"}
        if self.session_token:
            h["X-Reqlane-Session"] = self.session_token
        if self.human:
            h["X-Reqlane-Human"] = self.human
        return h

    def call(self, method: str, path: str, *, json_body: dict | None = None, params: dict | None = None) -> dict:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            r = self._http.request(method, path, json=json_body, params=params, headers=self.headers())
        except httpx.ConnectError:
            if self.autostart and start_daemon(self.base_url):
                try:
                    r = self._http.request(method, path, json=json_body, params=params, headers=self.headers())
                except httpx.HTTPError as e:
                    raise ClientError(f"daemon unreachable: {e}", "daemon_unavailable", "reqlane serve", 503)
            else:
                raise ClientError(f"daemon not running at {self.base_url}", "daemon_unavailable", "reqlane serve", 503)
        except httpx.HTTPError as e:
            raise ClientError(f"transport error: {e}", "daemon_unavailable", None, 503)
        if r.status_code >= 400:
            try:
                d = r.json()
            except ValueError:
                d = {"error": r.text}
            if isinstance(d.get("detail"), dict):
                d = d["detail"]
            raise ClientError(d.get("error") or str(d.get("detail") or r.text), d.get("code") or ("not_found" if r.status_code == 404 else "bad_request"),
                              d.get("hint"), r.status_code)
        return r.json() if r.content else {}

    def get(self, path: str, **params) -> dict:
        return self.call("GET", path, params=params)

    def post(self, path: str, **body) -> dict:
        return self.call("POST", path, json_body=body)

    def put(self, path: str, body: dict) -> dict:
        return self.call("PUT", path, json_body=body)
