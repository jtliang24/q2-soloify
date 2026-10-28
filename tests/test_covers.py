"""Covers must consume the complete HTTP body: a short read rendered half an album gray on M1."""

import asyncio

import aiohttp
from aiohttp import web

from soloify.player import COVER_MAX, Covers
from soloify.server import Identity


async def test_fragmented_cover_and_limits(tmp_path):
    calls = []
    first, last = b"\xff\xd8" + b"a" * 15878, b"b" * 8000 + b"\xff\xd9"

    async def cover(request):
        calls.append(request.path)
        if request.path == "/large":
            return web.Response(body=b"x" * (COVER_MAX + 1), content_type="image/jpeg")
        response = web.StreamResponse(headers={"Content-Type": "image/jpeg"})
        await response.prepare(request)
        await response.write(first)
        await asyncio.sleep(0.03)  # force two separate reads over the network
        if request.path != "/truncated":
            await response.write(last)
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_get("/{name}", cover)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=Identity.load(tmp_path).ssl_context())
    await site.start()
    port = runner.addresses[0][1]
    covers = Covers()
    # The test's own TLS certificate; production Covers uses normal CA verification.
    covers.http = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=False))
    url = f"https://127.0.0.1:{port}"
    try:
        assert await covers.get(url + "/cover") == first + last
        assert await covers.get(url + "/cover") == first + last
        assert calls == ["/cover"]  # only complete covers are cached
        assert await covers.get(url + "/truncated") is None
        assert await covers.get(url + "/large") is None
        assert await covers.get(url + "/cover") == first + last
        assert await covers.get("http://127.0.0.1/cover") is None
    finally:
        await covers.close()
        await runner.cleanup()
