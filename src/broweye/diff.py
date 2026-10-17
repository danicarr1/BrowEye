"""Compare two HTTP responses and report the differences that matter.

"Relevant" here means: status, headers (with volatile ones such as Date listed
separately rather than hidden), and the body. JSON bodies are compared
structurally, so key order and whitespace do not produce noise; everything
else gets a line-based unified diff.
"""

from __future__ import annotations

import difflib
import json
from typing import Any

from .models import ResponseSpec, body_preview

VOLATILE_HEADERS = {"date", "age", "expires", "last-modified", "etag", "x-request-id",
                    "server-timing", "set-cookie", "content-length", "report-to", "nel"}
MAX_DIFF_LINES = 400
MAX_JSON_CHANGES = 500


def _header_map(headers) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for k, v in headers:
        out.setdefault(k.lower(), []).append(v)
    return out


def diff_headers(a, b) -> dict[str, Any]:
    ma, mb = _header_map(a), _header_map(b)
    changes = []
    for name in sorted(set(ma) | set(mb)):
        va, vb = ma.get(name), mb.get(name)
        if va == vb:
            continue
        kind = "added" if va is None else "removed" if vb is None else "changed"
        changes.append({"name": name, "kind": kind, "a": va, "b": vb,
                        "volatile": name in VOLATILE_HEADERS})
    return {"changes": changes}


def _json_diff(a: Any, b: Any, path: str, out: list[dict[str, Any]]) -> None:
    if len(out) >= MAX_JSON_CHANGES:
        return
    if isinstance(a, dict) and isinstance(b, dict):
        for k in sorted(set(a) | set(b), key=str):
            p = f"{path}.{k}" if path else str(k)
            if k not in a:
                out.append({"path": p, "kind": "added", "b": b[k]})
            elif k not in b:
                out.append({"path": p, "kind": "removed", "a": a[k]})
            else:
                _json_diff(a[k], b[k], p, out)
    elif isinstance(a, list) and isinstance(b, list):
        for i in range(max(len(a), len(b))):
            p = f"{path}[{i}]"
            if i >= len(a):
                out.append({"path": p, "kind": "added", "b": b[i]})
            elif i >= len(b):
                out.append({"path": p, "kind": "removed", "a": a[i]})
            else:
                _json_diff(a[i], b[i], p, out)
    elif a != b or type(a) is not type(b):
        out.append({"path": path or "$", "kind": "changed", "a": a, "b": b})


def _try_json(body: bytes | None) -> tuple[bool, Any]:
    if not body:
        return False, None
    try:
        return True, json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False, None


def diff_bodies(a: bytes | None, b: bytes | None) -> dict[str, Any]:
    a, b = a or b"", b or b""
    result: dict[str, Any] = {"identical": a == b, "size_a": len(a), "size_b": len(b)}
    if a == b:
        result["kind"] = "identical"
        return result
    ja_ok, ja = _try_json(a)
    jb_ok, jb = _try_json(b)
    if ja_ok and jb_ok:
        changes: list[dict[str, Any]] = []
        _json_diff(ja, jb, "", changes)
        result.update(kind="json", changes=changes,
                      truncated=len(changes) >= MAX_JSON_CHANGES,
                      semantically_equal=not changes)
        return result
    ta, tb = body_preview(a), body_preview(b)
    if ta is None or tb is None:
        result["kind"] = "binary"
        return result
    lines = list(difflib.unified_diff(ta.splitlines(), tb.splitlines(), "A", "B", lineterm="", n=2))
    result.update(kind="text", diff=lines[:MAX_DIFF_LINES], truncated=len(lines) > MAX_DIFF_LINES)
    return result


def compare(a: ResponseSpec | None, b: ResponseSpec | None,
            time_a: float | None = None, time_b: float | None = None) -> dict[str, Any]:
    if a is None or b is None:
        return {"error": "one of the responses is missing", "a_missing": a is None, "b_missing": b is None}
    headers = diff_headers(a.headers, b.headers)
    body = diff_bodies(a.body, b.body)
    relevant_headers = [c for c in headers["changes"] if not c["volatile"]]
    return {
        "status": {"a": a.status, "b": b.status, "changed": a.status != b.status},
        "headers": headers,
        "body": body,
        "timing_ms": {"a": time_a, "b": time_b},
        "summary": {
            "status_changed": a.status != b.status,
            "relevant_header_changes": len(relevant_headers),
            "body_changed": not body["identical"] and not body.get("semantically_equal", False),
        },
    }
