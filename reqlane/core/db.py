"""SQLite storage: schema, migrations, connection, small helpers."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

USER_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents(
  id TEXT PRIMARY KEY, kind TEXT NOT NULL, display_name TEXT, repos TEXT NOT NULL,
  depends_on TEXT NOT NULL DEFAULT '[]', description TEXT, metadata TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
  id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, agent_id TEXT NOT NULL REFERENCES agents(id),
  name TEXT, runtime TEXT, runtime_ref TEXT, cwd TEXT, pid INTEGER, status TEXT NOT NULL,
  last_seen_at TEXT NOT NULL, started_at TEXT NOT NULL, ended_at TEXT, event_cursor INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS sessions_agent ON sessions(agent_id, status);
CREATE TABLE IF NOT EXISTS requests(
  id TEXT PRIMARY KEY, type TEXT NOT NULL, from_agent TEXT NOT NULL, to_agent TEXT NOT NULL,
  cc TEXT NOT NULL DEFAULT '[]', parent_id TEXT, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
  goal TEXT NOT NULL DEFAULT '{}', priority TEXT NOT NULL DEFAULT 'normal', blocking INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL, routed_to TEXT, resume_status TEXT, claimed_by TEXT, iteration INTEGER NOT NULL DEFAULT 1,
  labels TEXT NOT NULL DEFAULT '[]', kind TEXT, options TEXT NOT NULL DEFAULT '[]', accepted_option TEXT, idem TEXT,
  due_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS requests_idem ON requests(from_agent, idem) WHERE idem IS NOT NULL;
CREATE INDEX IF NOT EXISTS requests_to ON requests(to_agent, status);
CREATE INDEX IF NOT EXISTS requests_from ON requests(from_agent, status);
CREATE TABLE IF NOT EXISTS request_links(
  request_id TEXT NOT NULL, related_id TEXT NOT NULL, relation TEXT NOT NULL,
  PRIMARY KEY(request_id, related_id, relation));
CREATE TABLE IF NOT EXISTS messages(
  id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES requests(id), from_agent TEXT NOT NULL,
  from_session TEXT, type TEXT NOT NULL, body TEXT NOT NULL, refs TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS messages_req ON messages(request_id);
CREATE TABLE IF NOT EXISTS artifacts(
  id TEXT PRIMARY KEY, request_id TEXT, type TEXT NOT NULL, title TEXT NOT NULL, content TEXT NOT NULL DEFAULT '',
  data TEXT NOT NULL DEFAULT '{}', author_agent TEXT NOT NULL, author_session TEXT, supersedes TEXT,
  version INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS artifacts_req ON artifacts(request_id);
CREATE TABLE IF NOT EXISTS agreements(
  id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL, title TEXT NOT NULL, parties TEXT NOT NULL,
  status TEXT NOT NULL, acknowledged TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, superseded_by TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
  request_id TEXT, agent_id TEXT, session_id TEXT, payload TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS events_request ON events(request_id);
CREATE TABLE IF NOT EXISTS event_audience(event_id INTEGER NOT NULL, agent_id TEXT NOT NULL, PRIMARY KEY(event_id, agent_id));
CREATE INDEX IF NOT EXISTS event_audience_agent ON event_audience(agent_id, event_id);
CREATE TABLE IF NOT EXISTS acks(session_id TEXT NOT NULL, event_id INTEGER NOT NULL, PRIMARY KEY(session_id, event_id));
CREATE TABLE IF NOT EXISTS permissions(
  agent_id TEXT NOT NULL, repo TEXT NOT NULL, read INTEGER NOT NULL, write INTEGER NOT NULL,
  source TEXT NOT NULL DEFAULT 'owner', PRIMARY KEY(agent_id, repo));
CREATE TABLE IF NOT EXISTS policies(agent_id TEXT PRIMARY KEY, policy TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY, value INTEGER NOT NULL);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(entity_type, entity_id, title, body);
"""

JSON_COLUMNS = {"repos", "depends_on", "metadata", "cc", "goal", "labels", "options", "refs",
                "data", "parties", "acknowledged", "payload", "policy"}


def now() -> str:
    """UTC ISO-8601 with microseconds (trace-grade ordering)."""
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level="DEFERRED")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version and version < USER_VERSION:
        raise RuntimeError(f"database schema v{version} is older than v{USER_VERSION}; remove {path} (pre-release, no migrations yet)")
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version={USER_VERSION}")
    conn.commit()
    return conn


def j(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for k, v in list(d.items()):
        if isinstance(v, str) and k in JSON_COLUMNS and v:
            try:
                d[k] = json.loads(v)
            except ValueError:
                pass
    if "blocking" in d:
        d["blocking"] = bool(d["blocking"])
    for k in ("read", "write"):
        if k in d:
            d[k] = bool(d[k])
    d.pop("token_hash", None)
    return d


def next_id(conn: sqlite3.Connection, prefix: str) -> str:
    conn.execute("INSERT INTO counters(name, value) VALUES(?, 0) ON CONFLICT(name) DO NOTHING", (prefix,))
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = ?", (prefix,))
    n = conn.execute("SELECT value FROM counters WHERE name = ?", (prefix,)).fetchone()[0]
    return f"{prefix}_{n:04d}"
