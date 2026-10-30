"""The real C relay's device contract over TLS, with an explicitly driven bridge."""

import asyncio
import socket
import tempfile
import time
from pathlib import Path

import pytest

from relay_rig import Relay, until
from soloify import browse as protocol
from soloify import server, wire


def state(track="a", word="playing", position=1000, cover=True):
    return (
        f"state={word}\ntrack={track}\ntitle=Title {track}\nartist=Artist\nalbum=Album\n"
        f"duration=300000\nposition={position}\n" + ("cover=1\n" if cover else "")
    ).encode()


class Peer:
    def __init__(self, folder):
        self.identity = server.Identity.load(folder)
        self.writer = None
        self.frames = []
        self.server = server.Server(self.identity, self.session)
        self.listener = None
        self.capture_announced = False

    async def session(self, reader, writer):
        self.writer = writer
        await self.send(wire.STATE, state())
        try:
            while True:
                self.frames.append(await wire.read_frame(reader))
        except asyncio.IncompleteReadError:
            pass
        finally:
            if self.writer is writer:
                self.writer = None

    async def send(self, kind, payload, chunk=0):
        data = wire.frame(kind, payload)
        for start in range(0, len(data), chunk or len(data)):
            self.writer.write(data[start : start + (chunk or len(data))])
            await self.writer.drain()
            if chunk:
                await asyncio.sleep(0)

    async def packets(self, n):
        if not self.capture_announced:
            await self.send(wire.CAPTURE, (1).to_bytes(4, "big"))
            self.capture_announced = True
        for i in range(n):
            await self.send(wire.AUDIO, i.to_bytes(4, "big") + b"\xfc\xff\xfe")

    def controls(self):
        return [p for k, p in self.frames if k == wire.CONTROL]


@pytest.fixture
async def contract(tmp_path, request):
    # Unix socket names must fit 108 bytes; pytest's descriptive directories often don't.
    with tempfile.TemporaryDirectory(prefix="sf-") as folder:
        base = Path(folder) / "q"
        peer = Peer(tmp_path / "bridge")
        peer.listener = await peer.server.start("127.0.0.1", 0)
        port = peer.listener.sockets[0].getsockname()[1]
        line = f"127.0.0.1 {port} {peer.identity.token} sha256={peer.identity.pin}"
        env = {"SOLOIFY_Q2POD": str(base)}
        if getattr(request, "param", None) == "record":
            env["SOLOIFY_DECODE"] = f"cat >> {tmp_path / 'paused.ogg'}"
            env["SOLOIFY_SINK"] = "cat >/dev/null"  # closes on drain EOF even without PCM
        relay = Relay(tmp_path, [line], "-q", "-b", "200", env=env)
        await relay.start()
        try:
            await until(lambda: peer.writer is not None)
            await until(lambda: Path(f"{base}.state").exists() and "track=a" in read_state(base))
            yield relay, peer, base
        finally:
            if relay.proc.returncode is None:
                relay.proc.terminate()
                await asyncio.wait_for(relay.proc.wait(), 5)
            peer.listener.close()
            if peer.server.current:
                peer.server.current.cancel()
                await asyncio.gather(peer.server.current, return_exceptions=True)
            await peer.listener.wait_closed()


def read_state(base):
    return Path(f"{base}.state").read_text()


def controls(base, *commands):
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
        for command in commands:
            sock.sendto(command, f"{base}.sock")


async def browse(base, request=b"Q2SPOT/1\n7\taccount\n"):
    reader, writer = await asyncio.open_unix_connection(f"{base}.browse.sock")
    try:
        writer.write(request)
        await writer.drain()
        writer.write_eof()
        return await asyncio.wait_for(reader.read(), 3)
    finally:
        writer.close()
        await writer.wait_closed()


def children(pid):
    return [int(p) for p in Path(f"/proc/{pid}/task/{pid}/children").read_text().split()]


async def test_state_cover_and_large_fragmented_frame(contract):
    relay, peer, base = contract
    # More than the previous 128 KiB input limit; exactly the wire's 1 MiB payload maximum.
    jpeg = b"\xff\xd8" + b"z" * ((1 << 20) - 6) + b"\xff\xd9"
    await peer.send(wire.COVER, b"a\n" + jpeg, chunk=4093)
    await peer.send(wire.STATE, state())
    await until(lambda: "cover=1" in read_state(base))
    assert Path(f"{base}.state.jpg").read_bytes() == jpeg
    at = int(dict(line.split("=", 1) for line in read_state(base).splitlines())["at"])
    assert abs(at - time.monotonic() * 1000) < 2000
    assert not Path(f"{base}.download").exists()
    assert not Path(f"{base}").exists()  # launcher pid is not the relay's
    assert relay.proc.returncode is None


async def test_initial_position_anchors_when_audio_starts(contract):
    _, peer, base = contract
    assert "position=800\n" in read_state(base)  # Soloist is 200 ms ahead during refill
    await peer.packets(20)
    await until(lambda: "position=1000\n" in read_state(base))


async def test_late_cover_appears_while_paused(contract):
    _, peer, base = contract
    await peer.send(wire.STATE, state(word="paused", cover=False))
    await until(lambda: "state=paused\n" in read_state(base))
    frozen = next(line for line in read_state(base).splitlines() if line.startswith("position="))
    await peer.send(wire.COVER, b"a\n\xff\xd8late\xff\xd9")
    await until(lambda: "cover=1\n" in read_state(base))
    assert frozen in read_state(base)
    assert Path(f"{base}.state.jpg").read_bytes() == b"\xff\xd8late\xff\xd9"


async def test_oversized_frame_reconnects(contract):
    relay, peer, _ = contract
    peer.writer.write(b"j" + ((1 << 20) + 1).to_bytes(4, "big"))
    await peer.writer.drain()
    await relay.expect("a frame too big")
    await relay.expect("connected to")


async def test_metadata_waits_with_audio(contract):
    _, peer, base = contract
    await peer.send(wire.COVER, b"a\n\xff\xd8aaa\xff\xd9")
    await peer.send(wire.STATE, state())
    await until(lambda: "cover=1" in read_state(base))
    await peer.packets(40)  # 800 ms ahead of a natural track boundary
    await peer.send(wire.COVER, b"b\n\xff\xd8bbb\xff\xd9")
    await peer.send(wire.STATE, state("b", position=0))
    await asyncio.sleep(0.1)
    assert "track=a\n" in read_state(base)
    assert Path(f"{base}.state.jpg").read_bytes() == b"\xff\xd8aaa\xff\xd9"
    await until(lambda: "track=b\n" in read_state(base))
    assert Path(f"{base}.state.jpg").read_bytes() == b"\xff\xd8bbb\xff\xd9"


async def test_pause_drains_sink_before_state_and_resume_requires_fresh_capture(contract):
    relay, peer, base = contract
    await peer.packets(40)
    await until(lambda: len(children(relay.proc.pid)) == 2)
    pids = children(relay.proc.pid)
    controls(base, b"s")
    await until(lambda: peer.controls() == [b"s"])
    assert "state=playing\n" in read_state(base)
    assert children(relay.proc.pid) == pids
    await peer.send(wire.BOUNDARY, b"s")
    await peer.send(wire.STATE, state(word="paused", position=9000))
    await until(lambda: "state=paused\n" in read_state(base))
    assert all(not Path(f"/proc/{pid}").exists() for pid in pids)
    await until(lambda: (wire.AUDIO_REQUEST, b"0 pause") in peer.frames)
    frozen = read_state(base)
    await asyncio.sleep(0.1)
    await peer.send(wire.STATE, state(word="paused", position=9000))
    await asyncio.sleep(0.1)
    # The paused position remains frozen, including at=.
    assert read_state(base) == frozen
    controls(base, b"P")
    await until(lambda: peer.controls() == [b"s", b"P"])
    await peer.send(wire.BOUNDARY, b"P")
    await peer.send(wire.STATE, state())
    await asyncio.sleep(0.3)  # startup may be slow: no old packets may restart the sink
    assert children(relay.proc.pid) == []
    await peer.send(wire.CAPTURE, (2).to_bytes(4, "big"))
    await peer.packets(12)  # fresh refill includes Opus pre-skip
    await until(lambda: len(children(relay.proc.pid)) == 2)


async def test_rapid_toggles_ignore_stale_state(contract):
    _, peer, base = contract
    controls(base, b"p", b"p", b"p")
    await until(lambda: len(peer.controls()) == 3)
    assert peer.controls() == [b"s", b"P", b"s"]
    await peer.send(wire.BOUNDARY, b"s")
    await peer.send(wire.STATE, state(word="paused"))
    await peer.send(wire.BOUNDARY, b"P")
    await peer.send(wire.STATE, state())
    await asyncio.sleep(0.1)
    assert "state=paused\n" in read_state(base)

    await peer.send(wire.BOUNDARY, b"s")
    await peer.send(wire.STATE, state(word="paused"))
    await asyncio.sleep(0.1)
    assert "state=paused\n" in read_state(base)


@pytest.mark.parametrize("contract", ["record"], indirect=True)
async def test_pause_drains_every_packet_and_resume_has_no_retained_packets(contract, tmp_path):
    from soloify.audio import OggPackets

    relay, peer, base = contract
    await peer.packets(100)
    await until(lambda: len(children(relay.proc.pid)) == 2)
    controls(base, b"s")
    await until(lambda: peer.controls() == [b"s"])
    await peer.send(wire.BOUNDARY, b"s")
    await peer.send(wire.STATE, state(word="paused"))
    await until(lambda: "state=paused\n" in read_state(base))
    old = (tmp_path / "paused.ogg").read_bytes()
    packets = OggPackets().feed(old)
    assert len([p for p in packets if not p.startswith((b"OpusHead", b"OpusTags"))]) == 100
    controls(base, b"P")
    await until(lambda: peer.controls() == [b"s", b"P"])
    await peer.send(wire.BOUNDARY, b"P")
    await peer.send(wire.STATE, state())
    await peer.send(wire.CAPTURE, (2).to_bytes(4, "big"))
    await peer.packets(12)
    await asyncio.sleep(2.5)
    packets = OggPackets().feed((tmp_path / "paused.ogg").read_bytes()[len(old) :])
    assert len([p for p in packets if not p.startswith((b"OpusHead", b"OpusTags"))]) == 12


@pytest.mark.parametrize("contract", ["record"], indirect=True)
async def test_pause_drains_partial_startup_and_capture_tail(contract, tmp_path):
    from soloify.audio import OggPackets

    relay, peer, base = contract
    await peer.packets(5)  # below the 200 ms startup target
    controls(base, b"s")
    await until(lambda: peer.controls() == [b"s"])
    await peer.packets(2)  # the source's final fade can still arrive after Pause
    await peer.send(wire.BOUNDARY, b"s")
    await peer.send(wire.STATE, state(word="paused"))
    await until(lambda: "state=paused\n" in read_state(base))
    assert children(relay.proc.pid) == []
    packets = OggPackets().feed((tmp_path / "paused.ogg").read_bytes())
    assert len([p for p in packets if not p.startswith((b"OpusHead", b"OpusTags"))]) == 7


async def test_pause_without_capture_completion_still_releases_sink(contract):
    relay, peer, base = contract
    await peer.packets(40)
    await until(lambda: len(children(relay.proc.pid)) == 2)
    controls(base, b"s")
    await until(lambda: peer.controls() == [b"s"])
    await until(lambda: "state=paused\n" in read_state(base), 9)
    assert children(relay.proc.pid) == []
    await relay.expect("pause drainage deadline")


@pytest.mark.parametrize("contract", ["record"], indirect=True)
async def test_play_during_drain_discards_old_tail_until_fresh_capture(contract, tmp_path):
    from soloify.audio import OggPackets

    relay, peer, base = contract
    await peer.packets(40)
    await until(lambda: len(children(relay.proc.pid)) == 2)
    controls(base, b"s", b"P")
    await until(lambda: peer.controls() == [b"s", b"P"])
    assert children(relay.proc.pid) == []
    old = (tmp_path / "paused.ogg").read_bytes()
    await peer.packets(30)  # old encoder tail is acknowledged but never queued again
    await peer.send(wire.BOUNDARY, b"s")
    await peer.send(wire.STATE, state(word="paused"))
    await peer.send(wire.BOUNDARY, b"P")
    await peer.send(wire.STATE, state())
    await asyncio.sleep(0.25)
    assert children(relay.proc.pid) == []
    await peer.send(wire.CAPTURE, (2).to_bytes(4, "big"))
    await peer.packets(12)
    await until(lambda: len(children(relay.proc.pid)) == 2)
    await asyncio.sleep(0.5)
    fresh = (tmp_path / "paused.ogg").read_bytes()[len(old) :]
    packets = OggPackets().feed(fresh)
    assert len([p for p in packets if not p.startswith((b"OpusHead", b"OpusTags"))]) == 12


async def test_seek_discards_old_audio_and_metadata(contract):
    relay, peer, base = contract
    await peer.packets(40)
    await peer.send(wire.STATE, state("obsolete"))
    controls(base, b"S 60000")
    await until(lambda: peer.controls() == [b"S 60000"])
    await peer.packets(30)  # old audio already in flight: acknowledged, never played
    await peer.send(wire.STATE, state("obsolete"))
    await asyncio.sleep(0.1)
    assert children(relay.proc.pid) == []
    assert "obsolete" not in read_state(base)
    await peer.send(wire.BOUNDARY, b"S 60000")
    await peer.send(wire.STATE, state("new", position=60000))
    await peer.packets(20)
    await until(lambda: "track=new\n" in read_state(base))
    await until(lambda: "position=60000\n" in read_state(base))
    await until(lambda: len(children(relay.proc.pid)) == 2)


async def test_browser_forwarding_and_bad_requests(contract):
    _, peer, base = contract
    task = asyncio.create_task(browse(base))
    await until(lambda: any(k == wire.BROWSE_REQUEST for k, _ in peer.frames))
    payload = next(p for k, p in peer.frames if k == wire.BROWSE_REQUEST)
    assert payload[4:] == b"Q2SPOT/1\n7\taccount\n"
    reply = protocol.response("7", rows=[("account", "spotify:user:U", "U", "", "", "")])
    await peer.send(wire.BROWSE_RESPONSE, payload[:4] + reply)
    assert await task == reply
    for bad in (
        b"x",
        b"Q2SPOT/1\nx\taccount\n",
        b"x" * 9000,
        b"Q2SPOT/1\n7\tacc\0ount\n",
        b"Q2SPOT/1\n7\tsearch\ttracks\tbad%\n",
    ):
        assert b"\tbad\t" in await browse(base, bad)


async def test_missing_remote_retry_and_shutdown(tmp_path):
    with tempfile.TemporaryDirectory(prefix="sf-") as folder:
        base = Path(folder) / "q"
        relay = await Relay(tmp_path, [], "-q", env={"SOLOIFY_Q2POD": str(base)}).start()
        try:
            await until(lambda: Path(f"{base}.browse.sock").exists())
            assert b"Copy remote to .soloify" in await browse(base)
            (tmp_path / "remote").write_text("127.0.0.1 1 token sha256=" + "00" * 32 + "\n")
            await browse(base)  # Retry wakes the reconnect backoff and reloads remote
            # Read the log to observe the reload, then the browser reports offline instead.
            await relay.expect("can't connect")
            assert b"\toffline\t" in await browse(base)
            relay.proc.terminate()
            await asyncio.wait_for(relay.proc.wait(), 3)
            assert not Path(f"{base}.state").exists()
            assert not Path(f"{base}.sock").exists()
        finally:
            await relay.stop()


async def test_crash_and_singleton(contract, tmp_path):
    relay, peer, base = contract
    other = await Relay(tmp_path, [], "-q", env={"SOLOIFY_Q2POD": str(base)}).start()
    await asyncio.wait_for(other.proc.wait(), 3)
    assert b"another relay owns" in await other.proc.stderr.read()
    await peer.packets(40)
    await until(lambda: len(children(relay.proc.pid)) == 2)
    pids = children(relay.proc.pid)
    relay.proc.kill()
    await relay.proc.wait()

    def dead(pid):
        try:
            return Path(f"/proc/{pid}/stat").read_text().split()[2] == "Z"
        except (FileNotFoundError, ProcessLookupError):
            return True

    await until(lambda: all(dead(pid) for pid in pids))
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock, pytest.raises(OSError):
        sock.connect(f"{base}.sock")  # firmware's spot_alive detects the crash
