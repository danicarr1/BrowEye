"""Local HTTP API + static UI.

The API can drive a browser and send arbitrary requests, so it must not be
reachable by random web pages the user visits:

* it binds to 127.0.0.1 only;
* every /api call needs the per-run token (header ``X-BrowEye-Token``, or
  ``?token=`` for the event stream, since EventSource cannot set headers);
* the Host header must be the loopback address we listen on, which defeats
  DNS-rebinding attacks.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
from pathlib import Path

from aiohttp import web
from playwright.async_api import Error as PlaywrightError

from .app import BrowEye, InvalidInput, NotFound

STATIC = Path(__file__).parent / "static"
TOKEN = web.AppKey("token", str)
ALLOWED_HOSTS = web.AppKey("allowed_hosts", set)


def _json(data, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, default=str))


def make_server(app: BrowEye, *, token: str | None = None, port: int = 0) -> web.Application:
    token = token or secrets.token_urlsafe(24)
    web_app = web.Application(client_max_size=50 * 1024 * 1024)
    web_app[TOKEN] = token
    web_app[ALLOWED_HOSTS] = set()

    @web.middleware
    async def guard(request: web.Request, handler):
        host = request.host.lower()
        if web_app[ALLOWED_HOSTS] and host not in web_app[ALLOWED_HOSTS]:
            return _json({"error": "forbidden host"}, 403)
        if request.path.startswith("/api/"):
            supplied = request.headers.get("X-BrowEye-Token") or request.query.get("token", "")
            if not hmac.compare_digest(supplied, token):
                return _json({"error": "missing or invalid token"}, 401)
        try:
            return await handler(request)
        except NotFound as e:
            return _json({"error": str(e)}, 404)
        except InvalidInput as e:
            return _json({"error": str(e)}, 400)
        except json.JSONDecodeError:
            return _json({"error": "request body must be JSON"}, 400)

    r = web.RouteTableDef()

    @r.get("/")
    async def index(_):
        resp = web.FileResponse(STATIC / "index.html")
        resp.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self' 'unsafe-inline'"
        resp.headers["X-Frame-Options"] = "DENY"
        return resp

    @r.get("/favicon.ico")
    async def favicon(_):
        return web.Response(status=204)

    @r.get("/api/state")
    async def state(_):
        from . import __version__
        return _json({"version": __version__, "sessions": app.sessions(),
                      "browser": app.browser is not None})

    @r.post("/api/sessions")
    async def open_session(req):
        body = await req.json()
        return _json(await app.open_session(body.get("name") or "session",
                                            reuse_state=bool(body.get("reuse_state")),
                                            ignore_https_errors=bool(body.get("ignore_https_errors"))), 201)

    @r.post("/api/sessions/{sid}/navigate")
    async def navigate(req):
        body = await req.json()
        try:
            return _json(await app.navigate(req.match_info["sid"], body.get("url", "")))
        except PlaywrightError as e:
            return _json({"error": f"navigation failed: {e.message.splitlines()[0]}"}, 502)

    @r.post("/api/sessions/{sid}/save-state")
    async def save_state(req):
        if not app.browser or req.match_info["sid"] not in app.browser.sessions:
            raise NotFound("session is not open")
        return _json({"path": await app.browser.save_state(req.match_info["sid"])})

    @r.get("/api/sessions/{sid}/cookies")
    async def cookies(req):
        if not app.browser:
            raise NotFound("no browser")
        jar = await app.browser.all_cookies(req.match_info["sid"])
        reveal = req.query.get("reveal") == "1"
        for c in jar:
            if not reveal:
                c["value"] = c["value"][:4] + "…" if len(c["value"]) > 4 else "…"
        return _json(jar)

    @r.get("/api/flows")
    async def flows(req):
        q = req.query
        return _json(app.query_flows(
            text=q.get("text", ""), method=q.get("method", ""), host=q.get("host", ""),
            status=q.get("status", ""), body=q.get("body", ""), session_id=q.get("session_id", ""),
            after_seq=int(q.get("after_seq", 0) or 0), limit=min(int(q.get("limit", 500) or 500), 5000),
        ))

    @r.get("/api/flows/{fid}")
    async def flow(req):
        return _json(app.flow_detail(req.match_info["fid"]))

    @r.get("/api/sitemap")
    async def sitemap(req):
        return _json(app.store.site_map(req.query.get("session_id", "")))

    @r.get("/api/export/har")
    async def har(req):
        ids = [i for i in req.query.get("ids", "").split(",") if i]
        data = app.export_har(ids or None, session_id=req.query.get("session_id", ""))
        return web.Response(
            text=json.dumps(data, indent=1), content_type="application/json",
            headers={"Content-Disposition": 'attachment; filename="broweye.har"'},
        )

    @r.get("/api/drafts")
    async def drafts(_):
        return _json(app.list_drafts())

    @r.post("/api/drafts")
    async def create_draft(req):
        body = await req.json()
        return _json(app.create_draft(from_flow=body.get("from_flow"), from_execution=body.get("from_execution"),
                                      curl=body.get("curl"), fields=body.get("fields")), 201)

    @r.get("/api/drafts/{did}")
    async def get_draft(req):
        return _json(app.draft(req.match_info["did"]))

    @r.put("/api/drafts/{did}")
    async def put_draft(req):
        return _json(app.update_draft(req.match_info["did"], await req.json()))

    @r.delete("/api/drafts/{did}")
    async def del_draft(req):
        app.delete_draft(req.match_info["did"])
        return _json({"ok": True})

    @r.post("/api/drafts/{did}/send")
    async def send(req):
        return _json(await app.send(req.match_info["did"]))

    @r.get("/api/executions")
    async def executions(req):
        return _json(app.executions(req.query.get("draft_id", "")))

    @r.get("/api/executions/{eid}")
    async def execution(req):
        return _json(app.execution(req.match_info["eid"]))

    @r.get("/api/compare")
    async def compare(req):
        return _json(app.compare(req.query.get("a", ""), req.query.get("b", "")))

    @r.get("/api/events")
    async def events(req):
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await resp.prepare(req)
        q = app.events.subscribe()
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except asyncio.TimeoutError:
                    await resp.write(b": keepalive\n\n")
                    continue
                # Coalesce bursts: the UI only needs "something new after seq N".
                batch = [ev]
                while not q.empty() and len(batch) < 500:
                    batch.append(q.get_nowait())
                await resp.write(f"data: {json.dumps(batch)}\n\n".encode())
                await asyncio.sleep(0.1)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            app.events.unsubscribe(q)
        return resp

    web_app.add_routes(r)
    web_app.router.add_static("/static/", STATIC)
    web_app.middlewares.append(guard)
    return web_app


async def serve(app: BrowEye, host: str = "127.0.0.1", port: int = 8765,
                token: str | None = None) -> tuple[web.AppRunner, str]:
    web_app = make_server(app, token=token)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    real_port = site._server.sockets[0].getsockname()[1]
    web_app[ALLOWED_HOSTS].update({f"127.0.0.1:{real_port}", f"localhost:{real_port}"})
    return runner, f"http://127.0.0.1:{real_port}/#token={web_app[TOKEN]}"
