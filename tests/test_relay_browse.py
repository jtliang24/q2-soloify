"""Exercise four real C relay slots, correlation, deadlines, and audio independence."""

import asyncio
import tempfile
from pathlib import Path

from relay_rig import Bridge, Relay, until
from soloify import browse as protocol
from soloify import wire
from test_browse import request
from test_contract import browse, controls, state
from test_contract import contract as contract


async def requests(peer, n):
    await until(lambda: len([p for k, p in peer.frames if k == wire.BROWSE_REQUEST]) >= n)
    return [p for k, p in peer.frames if k == wire.BROWSE_REQUEST]


async def test_four_slots_duplicate_firmware_ids_late_reply_and_audio(contract):
    _, peer, base = contract
    tasks = [asyncio.create_task(browse(base, request("caps"))) for _ in range(4)]
    try:
        frames = await requests(peer, 4)
        assert len({p[:4] for p in frames}) == 4
        assert all(p[4:] == request("caps") for p in frames)
        # Browse waits while controls and acknowledgements still cross the same TLS link.
        controls(base, b"s")
        await until(lambda: peer.controls() == [b"s"])
        await peer.packets(20)
        await until(lambda: any(k == wire.ACK for k, _ in peer.frames))
        for p in reversed(frames):
            await peer.send(
                wire.BROWSE_RESPONSE, p[:4] + protocol.response("7", message=p[:4].hex())
            )
        replies = await asyncio.gather(*tasks)
        assert len(set(replies)) == 4
        # Reusing a slot never reuses the correlation ID, even with the same firmware ID.
        late = asyncio.create_task(browse(base, request("caps")))
        fresh = (await requests(peer, 5))[-1]
        await peer.send(
            wire.BROWSE_RESPONSE, frames[0][:4] + protocol.response("7", message="obsolete")
        )
        await asyncio.sleep(0.05)
        assert not late.done()
        await peer.send(wire.BROWSE_RESPONSE, fresh[:4] + protocol.response("7", message="fresh"))
        assert b"fresh" in await late
    finally:
        for task in tasks:
            task.cancel()


async def test_disconnect_finishes_pending_without_replay(contract):
    _, peer, base = contract
    task = asyncio.create_task(browse(base, request("enqueue", "spotify:track:T")))
    await requests(peer, 1)
    old = peer.writer
    old.close()
    assert b"\toffline\t" in await task
    await until(lambda: peer.writer is not None and peer.writer is not old)
    await asyncio.sleep(0.1)
    assert len([p for k, p in peer.frames if k == wire.BROWSE_REQUEST]) == 1


async def test_timeout_discards_late_reply(contract):
    _, peer, base = contract
    reader, writer = await asyncio.open_unix_connection(f"{base}.browse.sock")
    await peer.send(wire.STATE, state(word="paused"))
    started = asyncio.get_running_loop().time()
    try:
        writer.write(request("liked"))
        writer.write_eof()
        p = (await requests(peer, 1))[0]
        reply = await asyncio.wait_for(reader.read(), 12)
        elapsed = asyncio.get_running_loop().time() - started
        assert b"timed out" in reply and 10 <= elapsed < 12
        await peer.send(wire.BROWSE_RESPONSE, p[:4] + protocol.response("7", message="late"))
    finally:
        writer.close()
        await writer.wait_closed()


async def test_actual_bridge_slow_browsing_keeps_audio_and_controls(tmp_path):
    class SlowBrowser:
        def __init__(self):
            self.cancelled = False

        async def handle(self, raw, boundary):
            id, op, args = protocol.parse(raw)
            if op == "playlists":
                await asyncio.sleep(0.6)
            return protocol.response(id, message=op)

    bridge = Bridge(tmp_path / "data")
    bridge.browser = SlowBrowser()
    await bridge.start()
    with tempfile.TemporaryDirectory(prefix="sf-") as folder:
        base = Path(folder) / "q"
        relay = await Relay(
            tmp_path, [bridge.line()], "-q", "-b", "200", env={"SOLOIFY_Q2POD": str(base)}
        ).start()
        try:
            await until(lambda: bool(bridge.sessions))
            session = bridge.sessions[0]
            session.player.event({"type": "auth_state", "logged_in": True, "is_active": True})
            session.player.event({"type": "playback_changed", "status": "playing"})
            await until(lambda: session.acked > 10)
            before = session.acked
            slow = [asyncio.create_task(browse(base, request("playlists"))) for _ in range(4)]
            await asyncio.sleep(0.1)
            controls(base, b"S1000")
            assert all(b"\tok\tplaylists" in r for r in await asyncio.gather(*slow))
            await until(lambda: session.acked > before + 10)
            # Invalid play still releases the reset, with the actual Browser implementation.
            from soloify.browse import Browser

            session.browser = Browser(None, session.player, tmp_path)
            assert b"\tbad\t" in await browse(base, request("play"))
            await until(lambda: not session.player.deferred)
        finally:
            await relay.stop()
            await bridge.stop()
