"""Core data model.

Three request shapes are kept deliberately separate:

* ``RequestSpec``  - an immutable HTTP request (what was captured, or what was sent).
* ``RequestDraft`` - an editable request the user works on. It is created from a
  captured flow by *copying* its request, so editing a draft can never mutate the
  original record.
* ``Execution``    - one run of a draft: the draft as it was at send time, the
  request that actually went on the wire, the response and the redirect chain.

Headers are ordered lists of ``(name, value)`` pairs: HTTP allows duplicates and
order can matter, so a dict would silently lose information.
"""

from __future__ import annotations

import base64
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

Headers = list[tuple[str, str]]


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def header_get(headers: Headers, name: str) -> str | None:
    lname = name.lower()
    for k, v in headers:
        if k.lower() == lname:
            return v
    return None


def header_without(headers: Headers, *names: str) -> Headers:
    drop = {n.lower() for n in names}
    return [(k, v) for k, v in headers if k.lower() not in drop]


def _b64(data: bytes | None) -> str | None:
    return None if data is None else base64.b64encode(data).decode("ascii")


def _unb64(data: str | None) -> bytes | None:
    return None if data is None else base64.b64decode(data)


def body_preview(body: bytes | None) -> str | None:
    """Text view of a body, or None if it is not valid UTF-8."""
    if body is None:
        return None
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return None


@dataclass(frozen=True)
class RequestSpec:
    method: str
    url: str
    headers: Headers = field(default_factory=list)
    body: bytes | None = None

    @property
    def host(self) -> str:
        return urlsplit(self.url).hostname or ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "url": self.url,
            "headers": [list(h) for h in self.headers],
            "body_b64": _b64(self.body),
            "body_text": body_preview(self.body),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RequestSpec:
        return cls(
            method=d["method"],
            url=d["url"],
            headers=[(k, v) for k, v in d.get("headers", [])],
            body=_unb64(d.get("body_b64")),
        )


@dataclass(frozen=True)
class ResponseSpec:
    status: int
    reason: str = ""
    headers: Headers = field(default_factory=list)
    body: bytes | None = None
    http_version: str = ""
    # Why the body is missing, when it is (e.g. "redirect", "not available").
    body_note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reason": self.reason,
            "headers": [list(h) for h in self.headers],
            "body_b64": _b64(self.body),
            "body_text": body_preview(self.body),
            "http_version": self.http_version,
            "body_note": self.body_note,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ResponseSpec:
        return cls(
            status=d["status"],
            reason=d.get("reason", ""),
            headers=[(k, v) for k, v in d.get("headers", [])],
            body=_unb64(d.get("body_b64")),
            http_version=d.get("http_version", ""),
            body_note=d.get("body_note"),
        )


@dataclass(frozen=True)
class Flow:
    """A request/response pair observed in the browser."""

    id: str
    session_id: str
    request: RequestSpec
    response: ResponseSpec | None
    started_at: float
    duration_ms: float | None = None
    resource_type: str = ""
    error: str | None = None
    # Body notes for the request side (e.g. "multipart body not exposed").
    request_note: str | None = None


COOKIE_MODES = ("as_is", "session", "none")
"""How a draft gets its cookies when executed.

* ``as_is``   - send exactly the headers in the draft (including any Cookie
                header copied from the capture). No jar is consulted.
* ``session`` - drop any Cookie header in the draft and use the cookie jar of
                the selected browser session, filtered by the target URL's
                domain/path/secure rules. Set-Cookie responses update nothing
                in the browser (the jar is read-only from the repeater).
* ``none``    - strip every Cookie header.
"""


@dataclass
class RequestDraft:
    id: str
    name: str
    method: str = "GET"
    url: str = ""
    headers: Headers = field(default_factory=list)
    body: bytes | None = None
    source_flow_id: str | None = None
    cookie_mode: str = "as_is"
    session_id: str | None = None
    follow_redirects: bool = False
    max_redirects: int = 10
    timeout_s: float = 30.0
    verify_tls: bool = True
    # Optional explicit auth. It is only ever sent to ``auth_scope`` origin.
    auth_type: str | None = None  # "basic" | "bearer" | None
    auth_user: str = ""
    auth_secret: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @classmethod
    def from_flow(cls, flow: Flow, name: str | None = None) -> RequestDraft:
        r = flow.request
        return cls(
            id=new_id(),
            name=name or (urlsplit(r.url).path or "/"),
            method=r.method,
            url=r.url,
            # A copy, so the flow is never shared. Host and body framing are
            # dropped: they are derived from the URL and body at send time, and
            # stale copies would mislead (or, for Host, silently be sent).
            # Users can still add a Host header explicitly.
            headers=header_without(r.headers, "host", "content-length", "transfer-encoding"),
            body=r.body,
            source_flow_id=flow.id,
            session_id=flow.session_id,
        )

    def snapshot(self) -> RequestDraft:
        return replace(self, headers=list(self.headers))

    def to_dict(self, *, include_secret: bool = False) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "method": self.method,
            "url": self.url,
            "headers": [list(h) for h in self.headers],
            "body_b64": _b64(self.body),
            "body_text": body_preview(self.body),
            "source_flow_id": self.source_flow_id,
            "cookie_mode": self.cookie_mode,
            "session_id": self.session_id,
            "follow_redirects": self.follow_redirects,
            "max_redirects": self.max_redirects,
            "timeout_s": self.timeout_s,
            "verify_tls": self.verify_tls,
            "auth_type": self.auth_type,
            "auth_user": self.auth_user,
            # Secrets are never returned to the UI or exported by default.
            "auth_secret": self.auth_secret if include_secret else ("***" if self.auth_secret else ""),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RequestDraft:
        return cls(
            id=d["id"],
            name=d.get("name", ""),
            method=d.get("method", "GET"),
            url=d.get("url", ""),
            headers=[(k, v) for k, v in d.get("headers", [])],
            body=_unb64(d.get("body_b64")),
            source_flow_id=d.get("source_flow_id"),
            cookie_mode=d.get("cookie_mode", "as_is"),
            session_id=d.get("session_id"),
            follow_redirects=bool(d.get("follow_redirects", False)),
            max_redirects=int(d.get("max_redirects", 10)),
            timeout_s=float(d.get("timeout_s", 30.0)),
            verify_tls=bool(d.get("verify_tls", True)),
            auth_type=d.get("auth_type"),
            auth_user=d.get("auth_user", ""),
            auth_secret=d.get("auth_secret", ""),
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
        )


@dataclass(frozen=True)
class Hop:
    """One request/response exchange of an execution (one per redirect)."""

    request: RequestSpec
    response: ResponseSpec | None
    duration_ms: float
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "request": self.request.to_dict(),
            "response": self.response.to_dict() if self.response else None,
            "duration_ms": self.duration_ms,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Hop:
        return cls(
            request=RequestSpec.from_dict(d["request"]),
            response=ResponseSpec.from_dict(d["response"]) if d.get("response") else None,
            duration_ms=d["duration_ms"],
            note=d.get("note"),
        )


@dataclass(frozen=True)
class Execution:
    id: str
    draft_id: str
    draft: dict[str, Any]  # draft snapshot at send time (secret redacted)
    hops: tuple[Hop, ...]
    started_at: float
    duration_ms: float
    error: str | None = None

    @property
    def final(self) -> Hop | None:
        return self.hops[-1] if self.hops else None
