"""Application service: the one place where store, browser and repeater meet.

The HTTP API (server.py) and the tests both drive this class, so the UI is a
thin layer and every behaviour can be tested without a browser UI.
"""

from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path
from typing import Any

from . import analysis, diff, interop
from .capture import BrowserManager
from .models import COOKIE_MODES, Execution, Flow, RequestDraft, ResponseSpec, new_id
from .repeater import Repeater
from .store import FlowQuery, Store

EDITABLE = ("name", "method", "url", "cookie_mode", "session_id", "follow_redirects",
            "max_redirects", "timeout_s", "verify_tls", "auth_type", "auth_user")


class NotFound(LookupError):
    pass


class InvalidInput(ValueError):
    pass


class Events:
    """Fan-out of small notifications to UI subscribers. Never blocks producers."""

    def __init__(self) -> None:
        self._subs: set[asyncio.Queue] = set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=1000)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        for q in list(self._subs):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # A slow client loses notifications, never data: it re-syncs
                # with GET /api/flows?after_seq=...
                pass


class BrowEye:
    def __init__(self, project_dir: str | Path, *, headless: bool = True, with_browser: bool = True):
        self.project_dir = Path(project_dir)
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.project_dir / "broweye.sqlite3")
        self.events = Events()
        self.browser: BrowserManager | None = None
        if with_browser:
            self.browser = BrowserManager(
                self.store, headless=headless, on_flow=self._on_flow,
                state_dir=self.project_dir / "sessions",
            )
        self.repeater = Repeater(cookie_provider=self.browser.cookies_for if self.browser else None)
        # Draft secrets are kept in memory only, never written to SQLite.
        self._secrets: dict[str, str] = {}

    async def start(self) -> None:
        if self.browser:
            await self.browser.start()

    async def stop(self) -> None:
        if self.browser:
            await self.browser.stop()
        self.store.close()

    def _on_flow(self, flow: Flow, seq: int) -> None:
        self.events.publish({"type": "flow", "seq": seq})

    # ---- sessions ---------------------------------------------------------
    def _require_browser(self) -> BrowserManager:
        if not self.browser:
            raise InvalidInput("browser is disabled in this instance")
        return self.browser

    async def open_session(self, name: str, reuse_state: bool = False, ignore_https_errors: bool = False) -> dict:
        s = await self._require_browser().open_session(name, reuse_state=reuse_state,
                                                       ignore_https_errors=ignore_https_errors)
        return {"id": s.id, "name": s.name}

    def sessions(self) -> list[dict]:
        live = set(self.browser.sessions) if self.browser else set()
        return [dict(s, live=s["id"] in live) for s in self.store.list_sessions()]

    async def navigate(self, session_id: str, url: str) -> dict:
        b = self._require_browser()
        if session_id not in b.sessions:
            raise NotFound(f"session {session_id} is not open")
        if not url.startswith(("http://", "https://")):
            raise InvalidInput("only http:// and https:// URLs can be opened")
        status = await b.navigate(session_id, url)
        return {"status": status}

    # ---- flows ------------------------------------------------------------
    def flow(self, flow_id: str) -> Flow:
        f = self.store.get_flow(flow_id)
        if not f:
            raise NotFound(f"flow {flow_id} not found")
        return f

    def flow_detail(self, flow_id: str) -> dict:
        f = self.flow(flow_id)
        return {
            "id": f.id, "session_id": f.session_id, "started_at": f.started_at,
            "duration_ms": f.duration_ms, "resource_type": f.resource_type, "error": f.error,
            "request": f.request.to_dict(), "request_note": f.request_note,
            "response": f.response.to_dict() if f.response else None,
            "findings": analysis.analyze(f),
            "curl": interop.to_curl(f.request),
        }

    def query_flows(self, **kw) -> list[dict]:
        return self.store.query_flows(FlowQuery(**kw))

    # ---- drafts -----------------------------------------------------------
    def _draft(self, draft_id: str) -> RequestDraft:
        d = self.store.get_draft(draft_id)
        if not d:
            raise NotFound(f"draft {draft_id} not found")
        d.auth_secret = self._secrets.get(draft_id, "")
        return d

    def draft(self, draft_id: str) -> dict:
        return self._draft(draft_id).to_dict()

    def create_draft(self, *, from_flow: str | None = None, from_execution: str | None = None,
                     curl: str | None = None, fields: dict | None = None) -> dict:
        if from_flow:
            d = RequestDraft.from_flow(self.flow(from_flow))
        elif from_execution:
            e = self._execution(from_execution)
            d = RequestDraft.from_dict(dict(e.draft, id=new_id(), name=e.draft["name"] + " (copy)"))
            d.auth_secret = ""
        elif curl:
            try:
                d = interop.from_curl(curl)
            except interop.CurlParseError as e:
                raise InvalidInput(str(e)) from e
        else:
            d = RequestDraft(id=new_id(), name="New request")
        self._apply(d, fields or {})
        self.store.save_draft(d)
        if d.auth_secret:
            self._secrets[d.id] = d.auth_secret
        return d.to_dict()

    def update_draft(self, draft_id: str, fields: dict) -> dict:
        d = self._draft(draft_id)
        self._apply(d, fields)
        self.store.save_draft(d)
        self._secrets[d.id] = d.auth_secret
        return d.to_dict()

    def delete_draft(self, draft_id: str) -> None:
        self.store.delete_draft(draft_id)
        self._secrets.pop(draft_id, None)

    @staticmethod
    def _apply(d: RequestDraft, f: dict) -> None:
        for k in EDITABLE:
            if k in f:
                setattr(d, k, f[k])
        if "headers" in f:
            try:
                d.headers = [(str(k), str(v)) for k, v in f["headers"] if str(k).strip()]
            except (TypeError, ValueError) as e:
                raise InvalidInput("headers must be a list of [name, value] pairs") from e
        if "body_b64" in f:
            try:
                d.body = base64.b64decode(f["body_b64"]) if f["body_b64"] is not None else None
            except ValueError as e:
                raise InvalidInput("body_b64 is not valid base64") from e
        elif "body_text" in f:
            d.body = f["body_text"].encode("utf-8") if f["body_text"] not in (None, "") else None
        # "***" is the redacted placeholder the UI receives; it means "unchanged".
        if "auth_secret" in f and f["auth_secret"] != "***":
            d.auth_secret = f["auth_secret"]
        d.method = (d.method or "GET").strip().upper()
        if not d.method.isalpha():
            raise InvalidInput(f"invalid method: {d.method!r}")
        if d.cookie_mode not in COOKIE_MODES:
            raise InvalidInput(f"cookie_mode must be one of {COOKIE_MODES}")
        if d.auth_type not in (None, "", "basic", "bearer"):
            raise InvalidInput("auth_type must be basic, bearer or empty")
        d.auth_type = d.auth_type or None

    def list_drafts(self) -> list[dict]:
        return self.store.list_drafts()

    # ---- execution --------------------------------------------------------
    async def send(self, draft_id: str) -> dict:
        d = self._draft(draft_id)
        if not d.url.startswith(("http://", "https://")):
            raise InvalidInput("URL must start with http:// or https://")
        e = await self.repeater.execute(d)
        self.store.add_execution(e)
        self.events.publish({"type": "execution", "id": e.id, "draft_id": d.id})
        return self.execution_dict(e)

    def _execution(self, exec_id: str) -> Execution:
        e = self.store.get_execution(exec_id)
        if not e:
            raise NotFound(f"execution {exec_id} not found")
        return e

    def execution(self, exec_id: str) -> dict:
        return self.execution_dict(self._execution(exec_id))

    @staticmethod
    def execution_dict(e: Execution) -> dict:
        final = e.final
        return {
            "id": e.id, "draft_id": e.draft_id, "draft": e.draft,
            "started_at": e.started_at, "duration_ms": e.duration_ms, "error": e.error,
            "hops": [h.to_dict() for h in e.hops],
            "status": final.response.status if final and final.response else None,
            "curl": interop.to_curl(final.request) if final else None,
        }

    def executions(self, draft_id: str = "") -> list[dict]:
        return self.store.list_executions(draft_id)

    # ---- comparison -------------------------------------------------------
    def _resolve(self, ref: str) -> tuple[ResponseSpec | None, float | None]:
        kind, _, ident = ref.partition(":")
        if kind == "flow":
            f = self.flow(ident)
            return f.response, f.duration_ms
        if kind == "exec":
            e = self._execution(ident)
            return (e.final.response if e.final else None), e.duration_ms
        raise InvalidInput("reference must be 'flow:<id>' or 'exec:<id>'")

    def compare(self, a: str, b: str) -> dict:
        ra, ta = self._resolve(a)
        rb, tb = self._resolve(b)
        return diff.compare(ra, rb, ta, tb)

    # ---- export -----------------------------------------------------------
    def export_har(self, flow_ids: list[str] | None = None, **query) -> dict:
        if flow_ids:
            flows = [self.flow(i) for i in flow_ids]
        else:
            rows = self.store.query_flows(FlowQuery(limit=100_000, **query))
            flows = [f for r in rows if (f := self.store.get_flow(r["id"]))]
        har = interop.to_har(flows)
        har["log"]["comment"] = f"exported {time.strftime('%Y-%m-%d %H:%M:%S')}"
        return har
