"""Passive checks on already-captured traffic.

Passive means these functions only look at data BrowEye already has; they never
send a request. They are hints for a human, not a vulnerability scanner.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from .models import Flow, header_get

SEVERITY_ORDER = {"medium": 0, "low": 1, "info": 2}


def _finding(check: str, severity: str, detail: str) -> dict:
    return {"check": check, "severity": severity, "detail": detail}


def _set_cookie_attrs(raw: str) -> tuple[str, set[str], str | None]:
    """Return (cookie name, lowercase flag/attr names, samesite value)."""
    first, *attrs = [p.strip() for p in raw.split(";")]
    name = first.split("=", 1)[0]
    keys, samesite = set(), None
    for a in attrs:
        k, _, v = a.partition("=")
        keys.add(k.strip().lower())
        if k.strip().lower() == "samesite":
            samesite = v.strip().lower()
    return name, keys, samesite


def analyze(flow: Flow) -> list[dict]:
    out: list[dict] = []
    resp = flow.response
    if resp is None:
        return out
    https = urlsplit(flow.request.url).scheme == "https"
    hdrs = resp.headers
    is_html = "text/html" in (header_get(hdrs, "content-type") or "")

    for k, v in hdrs:
        if k.lower() != "set-cookie":
            continue
        name, attrs, samesite = _set_cookie_attrs(v)
        if https and "secure" not in attrs:
            out.append(_finding("cookie-secure", "medium", f"cookie '{name}' set over HTTPS without Secure"))
        if "httponly" not in attrs:
            out.append(_finding("cookie-httponly", "low", f"cookie '{name}' is readable from JavaScript (no HttpOnly)"))
        if samesite is None:
            out.append(_finding("cookie-samesite", "info", f"cookie '{name}' has no explicit SameSite"))
        elif samesite == "none" and "secure" not in attrs:
            out.append(_finding("cookie-samesite", "medium", f"cookie '{name}' is SameSite=None without Secure (browsers reject it)"))

    if https and header_get(hdrs, "strict-transport-security") is None:
        out.append(_finding("hsts", "low", "HTTPS response without Strict-Transport-Security"))
    if is_html:
        if header_get(hdrs, "content-security-policy") is None:
            out.append(_finding("csp", "info", "HTML response without Content-Security-Policy"))
        xfo = header_get(hdrs, "x-frame-options")
        csp = header_get(hdrs, "content-security-policy") or ""
        if xfo is None and "frame-ancestors" not in csp:
            out.append(_finding("framing", "info", "HTML page can be framed (no X-Frame-Options / frame-ancestors)"))
    if header_get(hdrs, "x-content-type-options") is None and header_get(hdrs, "content-type"):
        out.append(_finding("nosniff", "info", "no X-Content-Type-Options: nosniff"))

    acao = header_get(hdrs, "access-control-allow-origin")
    acac = (header_get(hdrs, "access-control-allow-credentials") or "").lower()
    if acao == "*" and acac == "true":
        out.append(_finding("cors", "medium", "Access-Control-Allow-Origin '*' together with credentials (invalid; check intent)"))
    req_origin = header_get(flow.request.headers, "origin")
    if req_origin and acao == req_origin and acac == "true":
        out.append(_finding("cors", "info", "origin is reflected with credentials allowed - verify the allow-list"))

    server = header_get(hdrs, "server") or ""
    if any(c.isdigit() for c in server):
        out.append(_finding("server-version", "info", f"Server header discloses a version: {server}"))
    if header_get(hdrs, "x-powered-by"):
        out.append(_finding("server-version", "info", f"X-Powered-By: {header_get(hdrs, 'x-powered-by')}"))

    if https:
        loc = header_get(hdrs, "location") or ""
        if loc.startswith("http://"):
            out.append(_finding("downgrade", "medium", f"HTTPS response redirects to plain HTTP: {loc}"))

    out.sort(key=lambda f: SEVERITY_ORDER[f["severity"]])
    return out

