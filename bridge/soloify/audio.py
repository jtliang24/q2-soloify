"""The bridge's audio: an encoder started per relay request, its Ogg pages split into bare Opus
packets, and the in-flight rule that pauses Soloist when the link stalls (docs/wire.md)."""

import asyncio
import logging
import os
import signal
import struct
from collections.abc import AsyncIterator, Callable

from . import wire
from .player import Player
from .soloist import SINK

log = logging.getLogger("soloify")

FRAME_MS = 20
# The relay sends an ack at least every 5 s (docs/wire.md); silence this long means it's gone
IDLE_TIMEOUT = 20
MIN_INFLIGHT_MS = 300
# A 100 ms 1 kHz beep each second, in real time: latency and stall tests without Soloist
TEST_SOURCE = [
    *("-re", "-readrate_initial_burst", "0", "-f", "lavfi", "-i"),
    "aevalsrc='if(lt(mod(t,1),0.1),0.5*sin(2*PI*1000*t),0)':s=44100:c=stereo",
]


def ffmpeg_args(source: str, bitrate: int, sink: str = SINK) -> list[str]:
    """docs/soloist.md's encoder line, with 20 ms Ogg pages flushed at once: ffmpeg's default of
    one page a second would add up to a second of delay."""
    if source == "test":
        inp = TEST_SOURCE
    else:
        inp = ["-f", "pulse", "-fragment_size", "3528", "-i", f"{sink}.monitor"]
    return [
        *inp,
        *("-af", "asetpts=N/SR/TB", "-ac", "2", "-ar", "48000"),
        *("-c:a", "libopus", "-b:a", f"{bitrate}k", "-application", "audio"),
        *("-frame_duration", str(FRAME_MS), "-page_duration", str(FRAME_MS * 1000)),
        *("-flush_packets", "1", "-f", "ogg", "pipe:1"),
    ]


class OggPackets:
    """Splits an Ogg stream into its packets (one logical stream, as ffmpeg writes it)."""

    _PAGE = struct.Struct("<4sBBqIIIB")

    def __init__(self):
        self.buf = b""
        self.partial = b""

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        out = []
        while len(self.buf) >= self._PAGE.size:
            magic, _, _, _, _, _, _, nsegs = self._PAGE.unpack_from(self.buf)
            if magic != b"OggS":
                raise ValueError("lost Ogg sync")
            head = self._PAGE.size + nsegs
            if len(self.buf) < head:
                break
            lacing = self.buf[self._PAGE.size : head]
            if len(self.buf) < head + sum(lacing):
                break
            pos = head
            for n in lacing:
                self.partial += self.buf[pos : pos + n]
                pos += n
                if n < 255:
                    out.append(self.partial)
                    self.partial = b""
            self.buf = self.buf[pos:]
        return out


class Encoder:
    """One ffmpeg run, from start to its end on stop."""

    def __init__(self, args: list[str]):
        self.args = args
        self.proc: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
        self.proc = await asyncio.create_subprocess_exec(
            *("ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"),
            *self.args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            env=env,
        )

    async def packets(self) -> AsyncIterator[bytes]:
        """Its Opus packets, without the OpusHead and OpusTags headers."""
        ogg = OggPackets()
        while chunk := await self.proc.stdout.read(65536):
            for packet in ogg.feed(chunk):
                if not packet.startswith((b"OpusHead", b"OpusTags")):
                    yield packet
        await self.proc.wait()

    async def stop(self) -> None:
        """SIGINT, so ffmpeg flushes what it has encoded; packets() then ends."""
        if self.proc and self.proc.returncode is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except TimeoutError:
                self.proc.kill()


class Session:
    """One relay's connection: its controls go to the player, which sends it the state and covers;
    and its audio, an encoder while the relay asks for audio, stopped when unacked audio goes over
    the in-flight limit (a stall), on `A 0`, or when the relay goes."""

    def __init__(
        self, player: Player, encoder: Callable[[], Encoder], inflight_ms: int, browser=None
    ):
        self.player = player
        self.browser = browser
        self.pending_browse = set()
        self.control_tasks = set()
        self.audio_request = None
        self.encoder_factory = encoder
        self.limit = inflight_ms
        self.encoder: Encoder | None = None
        self.pump: asyncio.Task | None = None
        self.halting: asyncio.Task | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.sent = self.acked = 0
        self.tasks: set[asyncio.Task] = set()
        self.lock = asyncio.Lock()

    @property
    def streaming(self) -> bool:
        return self.encoder is not None

    def inflight_ms(self) -> int:
        return (self.sent - self.acked) * FRAME_MS

    async def run(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.player.attach(writer)
        try:
            while True:
                kind, payload = await asyncio.wait_for(wire.read_frame(reader), IDLE_TIMEOUT)
                if kind == wire.AUDIO_REQUEST:
                    if payload.startswith(b"1"):
                        self.target(payload)
                        if self.audio_request is None or self.audio_request.done():
                            self.audio_request = asyncio.create_task(self.want())
                            self.control_tasks.add(self.audio_request)
                            self.audio_request.add_done_callback(self.control_tasks.discard)
                    else:
                        if len(self.control_tasks) >= 32:
                            raise wire.WireError("too many audio requests")
                        task = asyncio.create_task(
                            self.halt(payload[2:].decode(errors="replace") or "stop")
                        )
                        self.control_tasks.add(task)
                        task.add_done_callback(self.control_tasks.discard)
                elif kind == wire.ACK:
                    self.acked = wire.ack_count(payload)
                elif kind == wire.CONTROL:
                    if len(self.control_tasks) >= 32:
                        raise wire.WireError("too many controls")
                    task = asyncio.create_task(self.control(payload))
                    self.control_tasks.add(task)
                    task.add_done_callback(self.control_tasks.discard)
                elif kind == wire.BROWSE_REQUEST:
                    if len(payload) < 4 or len(payload) > 8196:
                        raise wire.WireError("invalid browse frame")
                    if len(self.pending_browse) >= 4:
                        raise wire.WireError("too many browse operations")
                    task = asyncio.create_task(self.browse(payload))
                    self.pending_browse.add(task)
                    task.add_done_callback(self.pending_browse.discard)
                elif kind == wire.STATS:
                    log.info(
                        "relay: %s inflight=%d",
                        payload.decode(errors="replace"),
                        self.inflight_ms(),
                    )
        finally:
            for task in self.pending_browse | self.control_tasks | self.tasks:
                task.cancel()
            await asyncio.gather(
                *self.pending_browse, *self.control_tasks, *self.tasks, return_exceptions=True
            )
            self.player.detach(writer)
            # A relay gone is a stall: Soloist shouldn't play to nobody
            await self.halt("gone")
            if self.pump:
                await asyncio.wait([self.pump], timeout=5)

    async def browse(self, payload):
        from .browse import parse, response

        # The relay resets as soon as it forwards these op names. Even a bad argument list
        # must finish that boundary, or the relay would discard audio indefinitely.
        fields = payload[4:].split(b"\t")
        reset = len(fields) > 1 and fields[1].rstrip(b"\n") in (b"play", b"skip")
        bounded = False

        def finished():
            nonlocal bounded
            bounded = True

        async def boundary(action, needs_reset):
            return await self.browse_boundary(action, needs_reset, finished)

        try:
            if self.browser:
                reply = await self.browser.handle(payload[4:], boundary)
            else:
                try:
                    id, _, _ = parse(payload[4:])
                    reply = response(id, "unsupported", "Browsing is not configured on this server")
                except (ValueError, UnicodeError):
                    reply = response("0", "bad", "Malformed request")
        finally:
            if reset and not bounded:
                # Validation failed, or the deadline expired before obtaining the control lock.
                # No playback action ran. Release this relay reset without queueing another action.
                self.writer.write(wire.frame(wire.BOUNDARY, b"Q"))
                self.player.sent = None
                self.player.flush()
        self.writer.write(wire.frame(wire.BROWSE_RESPONSE, payload[:4] + reply))

    async def browse_boundary(self, action, reset=True, finished=None):
        async with self.lock:
            self.player.deferred = True
            try:
                if reset:
                    await self._halt("control")
                result = await action()
                if reset:
                    self.player._stalled(False)
                return result
            finally:
                # Also release the reset on failure or cancellation; no action is replayed.
                await asyncio.sleep(self.player.SETTLE)
                if reset:
                    self.writer.write(wire.frame(wire.BOUNDARY, b"Q"))
                    if finished:
                        finished()
                self.player.sent = None
                self.player._send_cover()
                self.player.deferred = False
                self.player.flush()

    def target(self, payload: bytes) -> None:
        """`A 1 MS` carries the relay's buffer target, which is also the in-flight limit: a stall
        longer than the relay's buffer pauses Soloist, and leaves a backlog of one buffer."""
        try:
            self.limit = max(int(payload[2:]), MIN_INFLIGHT_MS)
        except ValueError:
            pass

    async def want(self) -> None:
        async with self.lock:
            await self._want()

    async def _want(self) -> None:
        if self.streaming or self.halting:
            return
        if self.inflight_ms() > self.limit // 2:
            return  # what's in flight from before a stall hasn't drained yet
        if self.player.ws and not self.player.state.playing and not self.player.paused_by_us:
            return
        await self.player.resume()
        self.encoder = self.encoder_factory()
        await self.encoder.start()
        log.info("audio on (inflight %d ms)", self.inflight_ms())
        self.pump = asyncio.create_task(self._pump(self.encoder))

    async def halt(self, reason: str) -> None:
        async with self.lock:
            await self._halt(reason)

    async def _halt(self, reason: str) -> None:
        if not self.streaming:
            return
        encoder, self.encoder = self.encoder, None
        log.info("audio off: %s (inflight %d ms)", reason, self.inflight_ms())
        self.halting = asyncio.current_task()
        try:
            if reason not in ("pause", "control"):
                await self.player.pause()
            await encoder.stop()
            if self.pump and self.pump is not asyncio.current_task():
                await self.pump
        finally:
            self.halting = None

    async def control(self, payload: bytes) -> None:
        """Explicit user pauses never become stall pauses. Seek/skip drains the old encoder
        before its ordered boundary; a relay discards those packets until it sees that marker."""
        async with self.lock:
            self.player.deferred = True
            try:
                reset = payload[:1] in (b"n", b"b", b"S")
                if reset:
                    await self._halt("control")
                await self.player.control(payload)
                if payload[:1] == b"s":
                    await asyncio.sleep(self.player.PAUSE_SETTLE)
                    await self._halt("pause")
                # Soloist sends the new state around its command response. Settle the burst
                # before the snapshot, even when the command didn't change the state.
                await asyncio.sleep(self.player.SETTLE)
                self.writer.write(wire.frame(wire.BOUNDARY, payload))
                self.player.sent = None
                self.player._send_cover()
            finally:
                self.player.deferred = False
            self.player.flush()

    async def _pump(self, encoder: Encoder) -> None:
        # Never awaits the writer: a blocked link must not back up into ffmpeg and PulseAudio,
        # where it would play later as a backlog. The in-flight limit bounds what's buffered.
        async for packet in encoder.packets():
            if self.writer.is_closing():
                continue
            self.writer.write(wire.audio(self.sent, packet))
            self.sent += 1
            if self.encoder is encoder and self.inflight_ms() > self.limit:
                task = asyncio.create_task(self.halt("stall"))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
