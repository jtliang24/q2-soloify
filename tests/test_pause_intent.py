"""Latest-intent regressions through the real relay, TLS bridge and delayed source."""

import asyncio
import tempfile
import time
from pathlib import Path

import pytest

from relay_rig import Relay, until
from soloify import audio, server, wire
from soloify.player import Player
from test_audio import FakeEncoder
from test_contract import children, controls, read_state
from test_rewind import Source


@pytest.mark.parametrize("target", [1000, 4000])
@pytest.mark.parametrize("final_action", ["pause", "play", "browse", "seek_play"])
async def test_ten_rapid_cycles_during_shutdown_and_refill(tmp_path, target, final_action):
    player = Player()
    player.SETTLE = 0.01
    source = player.ws = Source(player)
    source.serial = int(time.time() * 1000)
    source.emit()
    source.delay = 0.015
    captures, sessions = [], []

    class SlowStop(FakeEncoder):
        async def stop(self):
            await asyncio.sleep(0.025)
            await super().stop()

    def encoder():
        capture = SlowStop()
        capture.capture_at = source.serial / 1000
        captures.append(capture)
        return capture

    async def session(reader, writer):
        active = audio.Session(player, encoder, target)
        sessions.append(active)
        await active.run(reader, writer)

    identity = server.Identity.load(tmp_path / "bridge")
    bridge = server.Server(identity, session)
    listener = await bridge.start("127.0.0.1", 0)
    port = listener.sockets[0].getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="sf-") as folder:
        base = Path(folder) / "q"
        relay = await Relay(
            tmp_path,
            [f"127.0.0.1 {port} {identity.token} sha256={identity.pin}"],
            "-q",
            "-b",
            str(target),
            env={"SOLOIFY_Q2POD": str(base), "SOLOIFY_ASOUND": str(tmp_path / "asound")},
        ).start()
        try:
            await until(lambda: captures)
            await captures[0].queue.put(b"opening")
            await until(lambda: sessions[0].sent == 1)
            active = sessions[0]
            controls(base, b"s", b"P", b"s")  # includes encoder shutdown
            await until(lambda: not active.control_tasks and active.rewind.bookmark)
            bookmark = active.rewind.bookmark
            assert not bookmark.failed
            for _ in range(10):
                before = len(captures)
                controls(base, b"P")
                await until(lambda before=before: len(captures) > before)
                # Refill is waiting for the first fresh capture packet. Final Pause
                # must supersede it without waiting for the readiness deadline.
                controls(base, b"s")
                await until(lambda: not active.control_tasks, timeout=2)
                assert source.word == "paused" and not active.streaming
                assert active.rewind.bookmark is bookmark
                assert not children(relay.proc.pid)
                assert all(capture.stopped for capture in captures)
            assert "state=paused\n" in read_state(base)
            frozen = read_state(base)
            await asyncio.sleep(0.1)
            assert read_state(base) == frozen
            if final_action != "pause":
                if final_action == "seek_play":
                    before = len(captures)
                    controls(base, b"S60000")
                    await until(lambda: len(captures) > before)
                    await captures[-1].queue.put(b"seek warmup")
                    await until(lambda: not active.control_tasks and source.position == 60000)
                    assert source.word == "paused" and not active.streaming
                source.serial = int(time.time() * 1000)
                before = len(captures)
                if final_action == "browse":

                    async def prepare():
                        async def action():
                            await source.command("play")
                            player.event({"type": "playback_changed", "status": "paused"})

                        return action

                    await active.browse_boundary(prepare)
                else:
                    controls(base, b"P")
                await until(lambda: len(captures) > before)
                for _ in range(target // 20 + 15):
                    await captures[-1].queue.put(b"fresh")
                await until(lambda: len(children(relay.proc.pid)) == 2)
                await until(lambda: not active.control_tasks)
                assert source.word == "playing" and active.streaming
                assert active.rewind.bookmark is None
                assert not Path(f"{base}.pause-error").exists()
                await until(lambda: active.acked > 0)
            else:
                assert not any(name == "play" for name, _ in source.calls)
        finally:
            # Ask the relay to reap its output group before terminating the relay.
            controls(base, b"s")
            await until(lambda: not children(relay.proc.pid))
            await relay.stop()
            listener.close()
            if bridge.current:
                bridge.current.cancel()
                await asyncio.gather(bridge.current, return_exceptions=True)
            await listener.wait_closed()


async def test_old_pump_cannot_signal_new_capture_readiness_or_release():
    captures = []

    def encoder():
        capture = FakeEncoder()
        captures.append(capture)
        return capture

    from test_reliability import Writer

    active = audio.Session(Player(), encoder, 1000)
    active.writer = Writer()
    await active._start_capture()
    old, old_pump = active.capture, active.pump
    starting = asyncio.create_task(active._start_capture(priming=True))
    try:
        await until(lambda: len(captures) == 2)
        new = active.capture
        old.release.set()
        await old.encoder.queue.put(b"late old packet")
        await asyncio.sleep(0.03)
        assert not new.ready.is_set() and not new.release.is_set()
        assert not starting.done()
        await new.encoder.queue.put(b"new warmup")
        await asyncio.wait_for(starting, 1)
        assert new.ready.is_set() and not new.release.is_set()
        assert not any(frame[:1] == wire.AUDIO for frame in active.writer.data)
    finally:
        await active._halt("pause")
        await old.encoder.stop()
        await old_pump
        starting.cancel()
        await asyncio.gather(starting, return_exceptions=True)
