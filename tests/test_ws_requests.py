"""Soloist has no request IDs: errors and late replies must not cross transactions."""

import asyncio

import pytest
from aiohttp import web

from relay_rig import until
from soloify.ws import SoloistWS


@pytest.fixture
async def local_ws(tmp_path):
    seen, sockets = [], []
    ready = asyncio.Event()

    async def handler(req):
        ws = web.WebSocketResponse()
        await ws.prepare(req)
        sockets.append(ws)
        ready.set()
        async for msg in ws:
            seen.append(msg.json())
        return ws

    app = web.Application()
    app.router.add_get("/", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    path = tmp_path / "ws.port"
    path.write_text(str(runner.addresses[0][1]))
    events = []
    link = SoloistWS(path, events.append)
    link.RETRY = (0.01, 0.02)
    task = asyncio.create_task(link.run())
    await ready.wait()
    await until(lambda: link.connected)
    yield link, seen, sockets, events
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    for ws in sockets:
        await ws.close()
    await runner.cleanup()


async def test_serializes_queries_and_commands(local_ws):
    link, seen, sockets, events = local_ws
    query = asyncio.create_task(link.request("get_queue", limit=50))
    command = asyncio.create_task(link.request("set_shuffle", enabled=True))
    await until(lambda: len(seen) == 1)
    assert seen[0]["command"] == "get_queue"
    await sockets[-1].send_json({"type": "options_changed", "options": {"shuffle": True}})
    assert not query.done()
    await sockets[-1].send_json({"type": "queue_changed", "upcoming": []})
    assert (await query)["upcoming"] == []
    await until(lambda: len(seen) == 2)
    await sockets[-1].send_json({"type": "command_result", "command": "wrong"})
    await asyncio.sleep(0.01)
    assert not command.done()
    await sockets[-1].send_json({"type": "command_result", "command": "set_shuffle"})
    await command
    assert any(e["type"] == "options_changed" for e in events)


async def test_error_disconnect_and_timeout_fail_pending(local_ws, monkeypatch):
    link, seen, sockets, events = local_ws
    task = asyncio.create_task(link.request("get_queue"))
    await until(lambda: bool(seen))
    await sockets[-1].send_json({"type": "error", "message": "requires authentication"})
    with pytest.raises(ConnectionError):
        await task
    task = asyncio.create_task(link.request("get_queue"))
    await until(lambda: len(seen) == 2)
    await sockets[-1].close()
    with pytest.raises(ConnectionError):
        await task
    await until(lambda: len(sockets) == 2 and link.connected)
    monkeypatch.setattr(link, "TIMEOUT", 0.03)
    with pytest.raises(TimeoutError):
        await link.request("get_queue")
    await until(lambda: len(sockets) >= 3 and link.connected)
    assert any(e["type"] == "gone" for e in events)
    assert len(seen) == 3  # no request replay after reconnect
