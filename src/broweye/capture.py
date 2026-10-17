"""Browser sessions and traffic capture via Playwright instrumentation.

Why instrumentation instead of a MITM proxy: Playwright receives request and
response events (including HTTPS) from inside the browser, so there is no CA
certificate to install and no system proxy to configure. The trade-off is that
we only see traffic of the browser BrowEye launched, not of arbitrary programs.
See docs/LIMITATIONS.md.

Each BrowEye session is its own BrowserContext: separate cookie jar, storage
and cache. That is what makes "identities" (e.g. admin vs. regular user in a
lab) explicit and isolated.
"""

from __future__ import annotations

import asyncio
import glob
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import Browser, BrowserContext, Error as PlaywrightError, Page
from playwright.async_api import Request, Response, async_playwright

from .models import Flow, RequestSpec, ResponseSpec, header_get, new_id
from .store import Store

log = logging.getLogger(__name__)

MAX_CAPTURED_BODY = 10 * 1024 * 1024
BODY_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def find_chromium() -> str | None:
    """Explicit executable if configured, else None (Playwright's default)."""
    if exe := os.environ.get("BROWEYE_CHROMIUM"):
        return exe
    root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if root:
        found = sorted(glob.glob(os.path.join(root, "chromium-*", "chrome-linux*", "chrome")))
        if found:
            return found[-1]
    return None


def _clean_headers(arr: list[dict[str, str]]) -> list[tuple[str, str]]:
    # HTTP/2 pseudo-headers (:authority, :path...) are not real headers.
    return [(h["name"], h["value"]) for h in arr if not h["name"].startswith(":")]


@dataclass
class BrowserSession:
    id: str
    name: str
    context: BrowserContext
    page: Page
    pending: set[asyncio.Task] = field(default_factory=set)


class BrowserManager:
    def __init__(self, store: Store, *, headless: bool = True,
                 on_flow: Callable[[Flow, int], None] | None = None,
                 state_dir: Path | None = None):
        self.store = store
        self.headless = headless
        self.on_flow = on_flow
        self.state_dir = state_dir
        self.sessions: dict[str, BrowserSession] = {}
        self._pw = None
        self._browser: Browser | None = None
        self._starts: dict[Request, float] = {}
        # Body fetches started as soon as the response arrives (see _on_response).
        self._bodies: dict[Request, asyncio.Future] = {}

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        exe = find_chromium()
        self._browser = await self._pw.chromium.launch(headless=self.headless, executable_path=exe)

    async def stop(self) -> None:
        for s in list(self.sessions.values()):
            await self.close_session(s.id)
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    # ---- sessions ---------------------------------------------------------
    def _state_path(self, name: str) -> Path | None:
        if not self.state_dir:
            return None
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name)
        return self.state_dir / f"{safe}.storage.json"

    async def open_session(self, name: str, *, reuse_state: bool = False,
                           ignore_https_errors: bool = False) -> BrowserSession:
        assert self._browser, "BrowserManager.start() not called"
        state = self._state_path(name)
        kwargs = {"ignore_https_errors": ignore_https_errors}
        if reuse_state and state and state.exists():
            kwargs["storage_state"] = str(state)
        context = await self._browser.new_context(**kwargs)
        session = BrowserSession(id=new_id(), name=name, context=context, page=await context.new_page())
        context.on("request", lambda r: self._starts.__setitem__(r, time.time()))
        context.on("response", self._on_response)
        context.on("requestfinished", lambda r: self._spawn(session, self._record(session, r, None)))
        context.on("requestfailed", lambda r: self._spawn(session, self._record(session, r, r.failure)))
        self.sessions[session.id] = session
        self.store.add_session(session.id, name)
        return session

    async def save_state(self, session_id: str) -> str | None:
        s = self.sessions[session_id]
        path = self._state_path(s.name)
        if path:
            path.parent.mkdir(parents=True, exist_ok=True)
            await s.context.storage_state(path=str(path))
            return str(path)
        return None

    async def close_session(self, session_id: str) -> None:
        if session_id not in self.sessions:
            return
        await self.drain(session_id)
        s = self.sessions.pop(session_id)
        await s.context.close()

    async def navigate(self, session_id: str, url: str) -> int | None:
        s = self.sessions[session_id]
        resp = await s.page.goto(url)
        return resp.status if resp else None

    async def cookies_for(self, session_id: str, url: str) -> list[tuple[str, str]]:
        """Cookies the browser would send to url (domain/path/secure filtered)."""
        s = self.sessions.get(session_id)
        if not s:
            return []
        return [(c["name"], c["value"]) for c in await s.context.cookies([url])]

    async def all_cookies(self, session_id: str) -> list[dict]:
        s = self.sessions.get(session_id)
        return await s.context.cookies() if s else []

    async def drain(self, session_id: str | None = None) -> None:
        """Wait until every in-flight capture has been written (used by tests)."""
        sessions = [self.sessions[session_id]] if session_id else list(self.sessions.values())
        for s in sessions:
            while s.pending:
                await asyncio.gather(*list(s.pending), return_exceptions=True)

    # ---- capture ----------------------------------------------------------
    def _spawn(self, session: BrowserSession, coro) -> None:
        task = asyncio.get_running_loop().create_task(coro)
        session.pending.add(task)
        task.add_done_callback(session.pending.discard)

    def _on_response(self, resp: Response) -> None:
        # Ask for the body immediately, from the event callback itself. If we
        # waited until "requestfinished" was processed, a fast navigation can
        # evict the previous document and the body is lost
        # ("No resource with given identifier found").
        if 300 <= resp.status < 400 or resp.request.url.startswith(("data:", "blob:")):
            return
        fut = asyncio.ensure_future(resp.body())
        fut.add_done_callback(lambda f: f.cancelled() or f.exception())  # retrieved later; silence warning
        self._bodies[resp.request] = fut

    async def _record(self, session: BrowserSession, req: Request, failure: str | None) -> None:
        body_future = self._bodies.pop(req, None)
        if req.url.startswith(("data:", "blob:")):
            return
        started = self._starts.pop(req, time.time())
        try:
            req_headers = _clean_headers(await req.headers_array())
        except PlaywrightError:
            req_headers = [(k, v) for k, v in req.headers.items()]

        body = req.post_data_buffer
        req_note = None
        if body is None and req.method in BODY_METHODS and header_get(req_headers, "content-length") not in (None, "0"):
            req_note = "request body not exposed by the browser (e.g. file upload or streamed body)"

        response, duration = None, None
        try:
            pw_resp = await req.response() if failure is None else None
        except PlaywrightError:
            pw_resp = None
        if pw_resp is not None:
            resp_body, note = None, None
            if 300 <= pw_resp.status < 400:
                note = "redirect responses have no body in the browser"
            else:
                try:
                    resp_body = await (body_future if body_future is not None else pw_resp.body())
                    if len(resp_body) > MAX_CAPTURED_BODY:
                        resp_body, note = resp_body[:MAX_CAPTURED_BODY], f"truncated at {MAX_CAPTURED_BODY} bytes"
                except PlaywrightError as e:
                    note = f"body not available: {e.message.splitlines()[0]}"
            try:
                resp_headers = _clean_headers(await pw_resp.headers_array())
            except PlaywrightError:
                resp_headers = list(pw_resp.headers.items())
            response = ResponseSpec(
                status=pw_resp.status, reason=pw_resp.status_text, headers=resp_headers,
                body=resp_body, body_note=note,
            )
            timing = req.timing
            if timing.get("responseEnd", -1) > 0:
                duration = timing["responseEnd"]
        if duration is None:
            duration = (time.time() - started) * 1000

        flow = Flow(
            id=new_id(), session_id=session.id,
            request=RequestSpec(req.method, req.url, req_headers, body),
            response=response, started_at=started, duration_ms=round(duration, 2),
            resource_type=req.resource_type, error=failure, request_note=req_note,
        )
        seq = self.store.add_flow(flow)
        if self.on_flow:
            self.on_flow(flow, seq)
