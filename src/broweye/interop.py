"""cURL import/export and HAR 1.2 export."""

from __future__ import annotations

import base64
import shlex
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlsplit

from . import __version__
from .models import Flow, Headers, RequestDraft, RequestSpec, ResponseSpec, header_get, new_id

# Headers curl computes itself; exporting them would make the command lie.
_CURL_SKIP = {"content-length", "host"}


def to_curl(req: RequestSpec | RequestDraft, *, redact: bool = True) -> str:
    out =["curl", "-X", shlex.quote(req.method), shlex.quote(req.url)]
    for k, v in req.headers:
        if k.lower() in _CURL_SKIP:
            continue
        if redact and k.lower() in ("authorization", "cookie", "proxy-authorization"):
            v = "<redacted>"
        out += ["-H", shlex.quote(f"{k}: {v}")]
    if req.body:
        try:
            out += ["--data-raw", shlex.quote(req.body.decode("utf-8"))]
        except UnicodeDecodeError:
            # Binary bodies cannot be inlined safely; write them via printf.
            hexed = "".join(f"\\x{b:02x}" for b in req.body)
            out = ["printf", shlex.quote(hexed), "|"] + out + ["--data-binary", "@-"]
    return " ".join(out)


class CurlParseError(ValueError):
    pass


def from_curl(command: str) -> RequestDraft:
    try:
        argv = shlex.split(command.replace("\\\n", " "))
    except ValueError as e:
        raise CurlParseError(f"cannot tokenize command: {e}") from e
    if not argv or argv[0] != "curl":
        raise CurlParseError("command must start with 'curl'")
    method, url, headers, data = None, None, [], []
    user, insecure, follow = None, False, False
    it = iter(argv[1:])
    for arg in it:
        if arg in ("-X", "--request"):
            method = next(it, None)
        elif arg in ("-H", "--header"):
            h = next(it, "")
            if ":" in h:
                k, v = h.split(":", 1)
                headers.append((k.strip(), v.strip()))
        elif arg in ("-d", "--data", "--data-raw", "--data-binary", "--data-ascii"):
            value = next(it, "")
            if value.startswith("@") and arg != "--data-raw":
                raise CurlParseError("reading the body from a file (@file) is not supported")
            data.append(value)
        elif arg in ("-b", "--cookie"):
            headers.append(("Cookie", next(it, "")))
        elif arg in ("-u", "--user"):
            user = next(it, "")
        elif arg in ("-A", "--user-agent"):
            headers.append(("User-Agent", next(it, "")))
        elif arg in ("-e", "--referer"):
            headers.append(("Referer", next(it, "")))
        elif arg == "--url":
            url = next(it, None)
        elif arg in ("-k", "--insecure"):
            insecure = True
        elif arg in ("-L", "--location"):
            follow = True
        elif arg in ("-I", "--head"):
            method = "HEAD"
        elif arg in ("--compressed", "-s", "--silent", "-v", "--verbose", "-i", "--include"):
            continue
        elif arg.startswith("-"):
            raise CurlParseError(f"unsupported curl option: {arg}")
        elif url is None:
            url = arg
    if not url:
        raise CurlParseError("no URL found")
    body = "&".join(data).encode() if data else None
    if body is not None and header_get(headers, "content-type") is None:
        headers.append(("Content-Type", "application/x-www-form-urlencoded"))
    d = RequestDraft(
        id=new_id(), name=f"curl {urlsplit(url).path or '/'}",
        method=(method or ("POST" if body is not None else "GET")).upper(),
        url=url, headers=headers, body=body, verify_tls=not insecure, follow_redirects=follow,
    )
    if user is not None:
        u, _, p = user.partition(":")
        d.auth_type, d.auth_user, d.auth_secret = "basic", u, p
    return d


def _har_headers(h: Headers) -> list[dict]:
    return [{"name": k, "value": v} for k, v in h]


def _har_content(resp: ResponseSpec) -> dict:
    mime = header_get(resp.headers, "content-type") or ""
    body = resp.body or b""
    content = {"size": len(body), "mimeType": mime}
    if body:
        try:
            content["text"] = body.decode("utf-8")
        except UnicodeDecodeError:
            content["text"] = base64.b64encode(body).decode()
            content["encoding"] = "base64"
    if resp.body_note:
        content["comment"] = resp.body_note
    return content


def _har_entry(f: Flow) -> dict:
    r = f.request
    entry = {
        "startedDateTime": datetime.fromtimestamp(f.started_at, timezone.utc).isoformat(),
        "time": f.duration_ms or 0,
        "request": {
            "method": r.method, "url": r.url, "httpVersion": "HTTP/1.1",
            "headers": _har_headers(r.headers), "cookies": [],
            "queryString": [{"name": k, "value": v} for k, v in parse_qsl(urlsplit(r.url).query, keep_blank_values=True)],
            "headersSize": -1, "bodySize": len(r.body) if r.body else 0,
        },
        "response": {
            "status": 0, "statusText": "", "httpVersion": "", "headers": [], "cookies": [],
            "content": {"size": 0, "mimeType": ""}, "redirectURL": "", "headersSize": -1, "bodySize": -1,
        },
        "cache": {},
        "timings": {"send": 0, "wait": f.duration_ms or 0, "receive": 0},
    }
    if r.body is not None:
        try:
            text = r.body.decode("utf-8")
        except UnicodeDecodeError:
            text = base64.b64encode(r.body).decode()  # HAR has no encoding field here; documented
        entry["request"]["postData"] = {"mimeType": header_get(r.headers, "content-type") or "", "text": text}
    if f.response:
        resp = f.response
        entry["response"].update(
            status=resp.status, statusText=resp.reason, httpVersion=resp.http_version,
            headers=_har_headers(resp.headers), content=_har_content(resp),
            redirectURL=header_get(resp.headers, "location") or "",
            bodySize=len(resp.body) if resp.body else -1,
        )
    if f.error:
        entry["comment"] = f"error: {f.error}"
    return entry


def to_har(flows: list[Flow]) -> dict:
    return {"log": {
        "version": "1.2",
        "creator": {"name": "BrowEye", "version": __version__},
        "entries": [_har_entry(f) for f in flows],
    }}
