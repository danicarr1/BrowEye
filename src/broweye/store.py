"""SQLite persistence for a project (one database file per project).

All access happens from the asyncio event-loop thread; SQLite calls are short
and the database runs in WAL mode, so this is simpler and safer than a pool.
Bodies are stored as BLOBs (not base64) so they can be searched.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .models import (
    Execution,
    Flow,
    Hop,
    RequestDraft,
    RequestSpec,
    ResponseSpec,
    header_get,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS flows (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    session_id TEXT NOT NULL,
    started_at REAL NOT NULL,
    method TEXT NOT NULL,
    url TEXT NOT NULL,
    host TEXT NOT NULL,
    path TEXT NOT NULL,
    status INTEGER,
    mime TEXT,
    resource_type TEXT,
    duration_ms REAL,
    error TEXT,
    req_headers TEXT NOT NULL,
    req_body BLOB,
    req_note TEXT,
    resp_reason TEXT,
    resp_http_version TEXT,
    resp_headers TEXT,
    resp_body BLOB,
    resp_note TEXT
);
CREATE INDEX IF NOT EXISTS flows_host ON flows(host);
CREATE INDEX IF NOT EXISTS flows_session ON flows(session_id, seq);
CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS executions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    draft_id TEXT NOT NULL,
    started_at REAL NOT NULL,
    duration_ms REAL NOT NULL,
    status INTEGER,
    error TEXT,
    data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS executions_draft ON executions(draft_id, seq);
"""


@dataclass
class FlowQuery:
    text: str = ""          # substring of URL
    method: str = ""
    host: str = ""
    status: str = ""        # "200", "4xx", ...
    body: str = ""          # substring of request or response body
    session_id: str = ""
    after_seq: int = 0
    limit: int = 500


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ---- sessions ---------------------------------------------------------
    def add_session(self, session_id: str, name: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO sessions(id, name, created_at) VALUES (?,?,?)",
            (session_id, name, time.time()),
        )
        self.db.commit()

    def list_sessions(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM sessions ORDER BY created_at")]

    # ---- flows ------------------------------------------------------------
    def add_flow(self, f: Flow) -> int:
        resp = f.response
        u = urlsplit(f.request.url)
        cur = self.db.execute(
            """INSERT INTO flows(id, session_id, started_at, method, url, host, path,
                status, mime, resource_type, duration_ms, error, req_headers, req_body,
                req_note, resp_reason, resp_http_version, resp_headers, resp_body, resp_note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                f.id, f.session_id, f.started_at, f.request.method, f.request.url,
                u.hostname or "", u.path or "/",
                resp.status if resp else None,
                (header_get(resp.headers, "content-type") or "").split(";")[0] if resp else None,
                f.resource_type, f.duration_ms, f.error,
                json.dumps(f.request.headers), f.request.body, f.request_note,
                resp.reason if resp else None,
                resp.http_version if resp else None,
                json.dumps(resp.headers) if resp else None,
                resp.body if resp else None,
                resp.body_note if resp else None,
            ),
        )
        self.db.commit()
        return cur.lastrowid

    def _row_to_flow(self, r: sqlite3.Row) -> Flow:
        req = RequestSpec(
            method=r["method"], url=r["url"],
            headers=[tuple(h) for h in json.loads(r["req_headers"])],
            body=r["req_body"],
        )
        resp = None
        if r["status"] is not None:
            resp = ResponseSpec(
                status=r["status"], reason=r["resp_reason"] or "",
                headers=[tuple(h) for h in json.loads(r["resp_headers"] or "[]")],
                body=r["resp_body"], http_version=r["resp_http_version"] or "",
                body_note=r["resp_note"],
            )
        return Flow(
            id=r["id"], session_id=r["session_id"], request=req, response=resp,
            started_at=r["started_at"], duration_ms=r["duration_ms"],
            resource_type=r["resource_type"] or "", error=r["error"],
            request_note=r["req_note"],
        )

    def get_flow(self, flow_id: str) -> Flow | None:
        r = self.db.execute("SELECT * FROM flows WHERE id=?", (flow_id,)).fetchone()
        return self._row_to_flow(r) if r else None

    def query_flows(self, q: FlowQuery) -> list[dict[str, Any]]:
        """Return summary rows (no bodies) matching the query, oldest first."""
        where, args = ["seq > ?"], [q.after_seq]
        if q.session_id:
            where.append("session_id = ?"); args.append(q.session_id)
        if q.text:
            where.append("instr(lower(url), lower(?)) > 0"); args.append(q.text)
        if q.method:
            where.append("method = ?"); args.append(q.method.upper())
        if q.host:
            where.append("host = ?"); args.append(q.host)
        if q.status:
            s = q.status.lower()
            if len(s) == 3 and s.endswith("xx") and s[0].isdigit():
                lo = int(s[0]) * 100
                where.append("status >= ? AND status < ?"); args += [lo, lo + 100]
            elif s.isdigit():
                where.append("status = ?"); args.append(int(s))
        if q.body:
            where.append(
                "(instr(CAST(req_body AS TEXT), ?) > 0 OR instr(CAST(resp_body AS TEXT), ?) > 0)"
            )
            args += [q.body, q.body]
        sql = (
            "SELECT seq, id, session_id, started_at, method, url, host, path, status, mime,"
            " resource_type, duration_ms, error, length(resp_body) AS resp_size"
            f" FROM flows WHERE {' AND '.join(where)} ORDER BY seq LIMIT ?"
        )
        args.append(q.limit)
        return [dict(r) for r in self.db.execute(sql, args)]

    def site_map(self, session_id: str = "") -> list[dict[str, Any]]:
        """Hosts with their distinct (method, path) endpoints."""
        sql = "SELECT host, method, path, COUNT(*) AS n, MAX(status) AS last_status FROM flows"
        args: list[Any] = []
        if session_id:
            sql += " WHERE session_id = ?"; args.append(session_id)
        sql += " GROUP BY host, method, path ORDER BY host, path, method"
        hosts: dict[str, list[dict[str, Any]]] = {}
        for r in self.db.execute(sql, args):
            hosts.setdefault(r["host"], []).append(
                {"method": r["method"], "path": r["path"], "count": r["n"], "status": r["last_status"]}
            )
        return [{"host": h, "endpoints": e} for h, e in hosts.items()]

    # ---- drafts -----------------------------------------------------------
    def save_draft(self, d: RequestDraft) -> None:
        d.updated_at = time.time()
        data = d.to_dict()
        data["auth_secret"] = ""  # secrets live only in memory (see repeater.py)
        self.db.execute(
            "INSERT OR REPLACE INTO drafts(id, data, updated_at) VALUES (?,?,?)",
            (d.id, json.dumps(data), d.updated_at),
        )
        self.db.commit()

    def get_draft(self, draft_id: str) -> RequestDraft | None:
        r = self.db.execute("SELECT data FROM drafts WHERE id=?", (draft_id,)).fetchone()
        return RequestDraft.from_dict(json.loads(r["data"])) if r else None

    def list_drafts(self) -> list[dict[str, Any]]:
        out = []
        for r in self.db.execute("SELECT data FROM drafts ORDER BY updated_at DESC"):
            d = json.loads(r["data"])
            out.append({k: d[k] for k in ("id", "name", "method", "url", "source_flow_id", "updated_at")})
        return out

    def delete_draft(self, draft_id: str) -> None:
        self.db.execute("DELETE FROM drafts WHERE id=?", (draft_id,))
        self.db.commit()

    # ---- executions -------------------------------------------------------
    def add_execution(self, e: Execution) -> None:
        final = e.final
        data = {"draft": e.draft, "hops": [h.to_dict() for h in e.hops]}
        self.db.execute(
            """INSERT INTO executions(id, draft_id, started_at, duration_ms, status, error, data)
               VALUES (?,?,?,?,?,?,?)""",
            (
                e.id, e.draft_id, e.started_at, e.duration_ms,
                final.response.status if final and final.response else None,
                e.error, json.dumps(data),
            ),
        )
        self.db.commit()

    def get_execution(self, exec_id: str) -> Execution | None:
        r = self.db.execute("SELECT * FROM executions WHERE id=?", (exec_id,)).fetchone()
        if not r:
            return None
        data = json.loads(r["data"])
        return Execution(
            id=r["id"], draft_id=r["draft_id"], draft=data["draft"],
            hops=tuple(Hop.from_dict(h) for h in data["hops"]),
            started_at=r["started_at"], duration_ms=r["duration_ms"], error=r["error"],
        )

    def list_executions(self, draft_id: str = "", limit: int = 200) -> list[dict[str, Any]]:
        sql = "SELECT seq, id, draft_id, started_at, duration_ms, status, error FROM executions"
        args: list[Any] = []
        if draft_id:
            sql += " WHERE draft_id = ?"; args.append(draft_id)
        sql += " ORDER BY seq DESC LIMIT ?"; args.append(limit)
        return [dict(r) for r in self.db.execute(sql, args)]
