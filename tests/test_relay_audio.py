"""The relay's buffer and sink (relay/audio.c, ogg.c, sink.c) with the bridge's test source."""

import asyncio
import subprocess

import pytest

from relay_rig import DECODE, REALTIME_SINK, Bridge, FreezingProxy, Relay, cpu_seconds, stat, until


@pytest.fixture
async def bridge(tmp_path):
    b = await Bridge(tmp_path / "data").start()
    yield b
    await b.stop()


@pytest.fixture
async def relays():
    made = []
    yield made
    for r in made:
        await r.stop()


async def test_ogg_stream_decodes_cleanly(tmp_path, bridge, relays):
    """The relay's Ogg (its own pages and CRCs) is what the device's ffmpeg reads."""
    out = tmp_path / "out.ogg"
    env = {"SOLOIFY_DECODE": f"cat > {out}", "SOLOIFY_STATS_MS": "500"}
    relay = await Relay(tmp_path, [bridge.line()], env=env).start()
    relays.append(relay)
    await relay.expect("connected to")
    await until(lambda: bridge.sessions and bridge.sessions[0].acked > 150)
    await relay.stop()
    decoded = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "warning", "-i", out, "-f", "s16le", "-ac", "2", "-"],
        capture_output=True,
    )
    assert decoded.returncode == 0
    assert decoded.stderr == b""  # no CRC or granule complaints
    seconds = len(decoded.stdout) / (48000 * 4)
    assert seconds > 2


async def test_ring_settles_at_its_target(tmp_path, bridge, relays):
    env = {"SOLOIFY_DECODE": DECODE, "SOLOIFY_SINK": REALTIME_SINK, "SOLOIFY_STATS_MS": "500"}
    relay = await Relay(tmp_path, [bridge.line() + " buffer=1000"], env=env).start()
    relays.append(relay)
    await relay.expect("connected to")
    await asyncio.sleep(4)
    levels = [stat(await relay.expect("ring="), "ring") for _ in range(4)]
    assert all(500 <= x <= 1100 for x in levels), levels
    line = await relay.expect("ring=")
    assert stat(line, "sinks") == 1
    assert stat(line, "underruns") == 0


async def test_stalls_pause_the_bridge_and_the_delay_stays_one_buffer(tmp_path, bridge, relays):
    proxy = FreezingProxy(bridge.port)
    port = await proxy.start()
    env = {"SOLOIFY_DECODE": DECODE, "SOLOIFY_SINK": REALTIME_SINK, "SOLOIFY_STATS_MS": "500"}
    line = bridge.line(port=port) + " buffer=1000"
    relay = await Relay(tmp_path, [line], env=env).start()
    relays.append(relay)
    try:
        await relay.expect("connected to")
        await asyncio.sleep(3)
        session = bridge.sessions[0]
        after = []
        for _ in range(2):
            await proxy.freeze(3)
            # The stall stopped the encoder; a new one starts once the link carries data again
            await until(lambda: session.streaming and bridge.encoders >= 2 + len(after), 10)
            await asyncio.sleep(4)
            after.append(stat(await relay.expect("ring="), "ring"))
        # One buffer plus at most what the bridge flushes as it pauses: never a growing backlog
        assert all(x <= 1000 + 1200 for x in after), after
        assert after[1] <= after[0] + 300, after
        assert len(bridge.sessions) == 1  # no reconnect: the link only stalled
    finally:
        await proxy.stop()


async def test_a_stall_neither_spins_the_relay_nor_repeats(tmp_path, relays):
    """While the bridge sends nothing the relay still sleeps in poll, and it reads again after."""
    bridge = await Bridge(tmp_path / "data", start_delay=0.5).start()
    proxy = FreezingProxy(bridge.port)
    port = await proxy.start()
    env = {"SOLOIFY_DECODE": DECODE, "SOLOIFY_SINK": REALTIME_SINK}
    relay = await Relay(tmp_path, [bridge.line(port=port) + " buffer=1000"], env=env).start()
    relays.append(relay)
    try:
        await relay.expect("connected to")
        await asyncio.sleep(3)
        session = bridge.sessions[0]
        before = cpu_seconds(relay.proc.pid)
        await proxy.freeze(3)
        assert cpu_seconds(relay.proc.pid) - before < 0.3
        await until(lambda: session.streaming and bridge.encoders >= 2, 10)
        for _ in range(60):  # 6 s: past the 5 s idle ack, which let the old spin read again
            assert session.streaming, "the bridge stalled again"
            await asyncio.sleep(0.1)
        assert bridge.encoders == 2
        assert len(bridge.sessions) == 1
    finally:
        await proxy.stop()
        await bridge.stop()
