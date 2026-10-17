"""Execute a RequestDraft and record exactly what went on the wire.

Design choices:

* We build ``httpx.Request`` objects directly instead of using
  ``client.build_request``: that skips httpx's default headers (User-Agent,
  Accept, ...), so the server receives the draft's headers plus only what HTTP
  itself requires (Host, Content-Length).
* Content-Length / Transfer-Encoding from the draft are always recomputed. A
  stale Content-Length after editing a body is the classic "my edit was not
  sent" bug.
* Redirects are followed manually so every hop is recorded and so credential
  handling is explicit: Cookie and Authorization headers never cross to a
  different origin.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urljoin, urlsplit

import httpx

from .models import (
    Execution,
    Headers,
    Hop,
    RequestDraft,
    RequestSpec,
    ResponseSpec,
    header_get,
    header_without,
    new_id,
)

MAX_BODY = 20 * 1024 * 1024
REDIRECT_CODES = {301, 302, 303, 307, 308}
# Headers that describe the body framing; they are always recomputed.
FRAMING_HEADERS = ("content-length", "transfer-encoding")

# (session_id, url) -> list of (name, value) cookies applicable to url.
CookieProvider = Callable[[str, str], Awaitable[list[tuple[str, str]]]]


def origin(url: str) -> tuple[str, str, int | None]:
    u = urlsplit(url)
    port = u.port or {"http": 80, "https": 443}.get(u.scheme)
    return (u.scheme, (u.hostname or "").lower(), port)


def _auth_header(d: RequestDraft) -> tuple[str, str] | None:
    if d.auth_type == "basic":
        token = base64.b64encode(f"{d.auth_user}:{d.auth_secret}".encode()).decode()
        return ("Authorization", f"Basic {token}")
    if d.auth_type == "bearer":
        return ("Authorization", f"Bearer {d.auth_secret}")
    return None


def _decode_headers(raw: list[tuple[bytes, bytes]]) -> Headers:
    return [(k.decode("latin-1"), v.decode("latin-1")) for k, v in raw]


class Repeater:
    def __init__(self, cookie_provider: CookieProvider | None = None):
        self.cookie_provider = cookie_provider

    async def _headers_for_hop(
        self, d: RequestDraft, url: str, base: Headers, first_origin: tuple, notes: list[str]
    ) -> Headers:
        headers = header_without(base, *FRAMING_HEADERS)
        same_origin = origin(url) == first_origin

        if d.cookie_mode == "none":
            headers = header_without(headers, "cookie")
        elif d.cookie_mode == "session":
            headers = header_without(headers, "cookie")
            if self.cookie_provider is None or not d.session_id:
                notes.append("cookie_mode=session but no browser session available; no cookies sent")
            else:
                jar = await self.cookie_provider(d.session_id, url)
                if jar:
                    headers.append(("Cookie", "; ".join(f"{k}={v}" for k, v in jar)))
        elif not same_origin and header_get(headers, "cookie") is not None:
            headers = header_without(headers, "cookie")
            notes.append("Cookie header removed: redirect left the original origin")

        if not same_origin and header_get(headers, "authorization") is not None:
            headers = header_without(headers, "authorization")
            notes.append("Authorization header removed: redirect left the original origin")

        auth = _auth_header(d)
        if auth:
            if same_origin:
                headers = header_without(headers, "authorization") + [auth]
            else:
                notes.append("configured auth not sent: different origin")
        return headers

    async def execute(self, draft: RequestDraft) -> Execution:
        d = draft.snapshot()
        started = time.time()
        t0 = time.perf_counter()
        hops: list[Hop] = []
        error: str | None = None

        method, url, body = d.method.upper(), d.url, d.body
        base_headers = list(d.headers)
        first_origin = origin(url)
        max_hops = (d.max_redirects + 1) if d.follow_redirects else 1

        timeout = httpx.Timeout(d.timeout_s)
        async with httpx.AsyncClient(verify=d.verify_tls, timeout=timeout, follow_redirects=False) as client:
            for _ in range(max_hops):
                notes: list[str] = []
                try:
                    headers = await self._headers_for_hop(d, url, base_headers, first_origin, notes)
                    req = httpx.Request(method, url, headers=headers, content=body)
                except (httpx.InvalidURL, httpx.UnsupportedProtocol, ValueError, TypeError) as e:
                    error = f"invalid request: {e}"
                    break

                h0 = time.perf_counter()
                try:
                    resp = await client.send(req, stream=True)
                    try:
                        chunks, size, truncated = [], 0, False
                        async for chunk in resp.aiter_bytes():
                            size += len(chunk)
                            if size > MAX_BODY:
                                truncated = True
                                break
                            chunks.append(chunk)
                    finally:
                        await resp.aclose()
                except httpx.DecodingError as e:
                    error = f"could not decode response body: {e}"
                    sent = RequestSpec(method, str(req.url), _decode_headers(req.headers.raw), body)
                    hops.append(Hop(sent, None, (time.perf_counter() - h0) * 1000, "; ".join(notes) or None))
                    break
                except httpx.HTTPError as e:
                    error = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                    sent = RequestSpec(method, str(req.url), _decode_headers(req.headers.raw), body)
                    hops.append(Hop(sent, None, (time.perf_counter() - h0) * 1000, "; ".join(notes) or None))
                    break

                if truncated:
                    notes.append(f"response body truncated at {MAX_BODY} bytes")
                sent = RequestSpec(method, str(req.url), _decode_headers(req.headers.raw), body)
                response = ResponseSpec(
                    status=resp.status_code,
                    reason=resp.reason_phrase,
                    headers=_decode_headers(resp.headers.raw),
                    body=b"".join(chunks),
                    http_version=resp.http_version,
                )
                hops.append(Hop(sent, response, (time.perf_counter() - h0) * 1000, "; ".join(notes) or None))

                location = header_get(response.headers, "location")
                if not (d.follow_redirects and response.status in REDIRECT_CODES and location):
                    break
                if len(hops) >= max_hops:
                    error = f"too many redirects (max {d.max_redirects})"
                    break
                url = urljoin(url, location)
                if response.status == 303 or (response.status in (301, 302) and method == "POST"):
                    # Same behaviour as browsers: switch to GET and drop the body.
                    method, body = "GET", None
                    base_headers = header_without(base_headers, "content-type")

        return Execution(
            id=new_id(),
            draft_id=d.id,
            draft=d.to_dict(),
            hops=tuple(hops),
            started_at=started,
            duration_ms=(time.perf_counter() - t0) * 1000,
            error=error,
        )
