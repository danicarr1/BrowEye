"""Local lab server. It records every request exactly as received so tests can
assert on what the *server* got, not on what BrowEye claims it sent.

Run standalone for manual testing:  broweye lab --port 8081
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

from aiohttp import web

PAGE = """<!doctype html><html><head><title>Lab</title></head><body>
<h1>BrowEye lab</h1><div id=out>loading</div>
<script>
fetch('/api/items?page=1').then(r => r.json()).then(() =>
  fetch('/api/items', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Lab': 'browser'},
                       body: JSON.stringify({name: 'widget', qty: 3})}))
  .then(r => r.json()).then(j => { document.getElementById('out').textContent = 'done ' + j.received.qty; });
</script></body></html>"""


@dataclass
class Received:
    method: str
    path: str
    query: str
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> str | None:
        for k, v in self.headers:
            if k.lower() == name.lower():
                return v
        return None


@dataclass
class Lab:
    log: list[Received] = field(default_factory=list)

    def last(self, path: str, method: str | None = None) -> Received:
        for r in reversed(self.log):
            if r.path == path and (method is None or r.method == method):
                return r
        raise AssertionError(f"lab server never received {method or ''} {path}")


def make_app(lab: Lab) -> web.Application:
    @web.middleware
    async def record(request: web.Request, handler):
        body = await request.read()
        lab.log.append(Received(request.method, request.path, request.query_string,
                                [(k.decode("latin-1"), v.decode("latin-1")) for k, v in request.raw_headers],
                                body))
        return await handler(request)

    async def page(_):
        return web.Response(text=PAGE, content_type="text/html",
                            headers={"Set-Cookie": "visit=1; Path=/"})

    async def items_get(request):
        page_no = int(request.query.get("page", "1"))
        return web.json_response({"page": page_no, "items": [{"id": i} for i in range(page_no * 2 - 1, page_no * 2 + 1)]})

    async def items_post(request):
        data = await request.json()
        return web.json_response({"ok": True, "received": data, "x_lab": request.headers.get("X-Lab")}, status=201)

    async def echo(request):
        body = await request.read()
        return web.json_response({
            "method": request.method,
            "query": dict(request.query),
            "headers": {k: v for k, v in request.headers.items()},
            "body": body.decode("utf-8", "replace"),
        })

    async def login(request):
        user = request.query.get("user", "alice")
        resp = web.json_response({"logged_in": user})
        resp.set_cookie("sid", f"token-{user}", httponly=True, path="/")
        return resp

    async def me(request):
        sid = request.cookies.get("sid")
        if not sid:
            return web.json_response({"error": "not authenticated"}, status=401)
        return web.json_response({"user": sid.removeprefix("token-")})

    async def redirect(request):
        raise web.HTTPFound(request.query.get("to", "/echo"))

    async def redirect_post(request):
        raise web.HTTPTemporaryRedirect(request.query.get("to", "/echo"))  # 307 keeps the body

    async def redirect_post_302(request):
        raise web.HTTPFound(request.query.get("to", "/echo"))  # browsers switch POST -> GET

    async def slow(request):
        await asyncio.sleep(float(request.query.get("s", "0.2")))
        return web.json_response({"slow": True})

    async def binary(_):
        return web.Response(body=bytes(range(256)), content_type="application/octet-stream")

    async def compressed(request):
        import gzip

        import brotli
        import zstandard
        enc = request.query.get("enc", "gzip")
        raw = b'{"compressed": true, "enc": "' + enc.encode() + b'"}'
        data = {"gzip": gzip.compress, "br": brotli.compress,
                "zstd": zstandard.ZstdCompressor().compress}[enc](raw)
        return web.Response(body=data, headers={"Content-Encoding": enc, "Content-Type": "application/json"})

    app = web.Application(middlewares=[record])
    app.router.add_get("/", page)
    app.router.add_get("/api/items", items_get)
    app.router.add_post("/api/items", items_post)
    app.router.add_route("*", "/echo", echo)
    app.router.add_get("/login", login)
    app.router.add_get("/me", me)
    app.router.add_get("/redirect", redirect)
    app.router.add_post("/redirect-post", redirect_post)
    app.router.add_post("/redirect-post-302", redirect_post_302)
    app.router.add_get("/slow", slow)
    app.router.add_get("/binary", binary)
    app.router.add_get("/compressed", compressed)
    return app


async def start_lab(port: int = 0, host: str = "127.0.0.1") -> tuple[Lab, web.AppRunner, str]:
    lab = Lab()
    runner = web.AppRunner(make_app(lab))
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    real_port = site._server.sockets[0].getsockname()[1]
    return lab, runner, f"http://{host}:{real_port}"

