"""The bridge's audio: an encoder started per relay request, its Ogg pages split into bare Opus
packets, and the in-flight rule that pauses Soloist when the link stalls (docs/wire.md)."""

import asyncio
import logging
import os
import signal
import struct
from collections.abc import AsyncIterator, Callable

from . import wire
from .soloist import SINK, Soloist

log = logging.getLogger("soloify")

FRAME_MS = 20
# The relay sends an ack at least every 5 s (docs/wire.md); silence this long means it's gone
IDLE_TIMEOUT = 20
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


class Player:
    """What every relay connection shares: Soloist, and whether the bridge paused it (so it resumes
    only what it paused itself, never what the user paused)."""

    PAUSE_SETTLE = 0.3

    def __init__(self, soloist: Soloist | None):
        self.soloist = soloist
        self.paused_by_us = False

    async def pause(self) -> None:
        if self.soloist and not self.paused_by_us and await self.soloist.playing():
            await self.soloist.ctl("pause")
            self.paused_by_us = True
            # The encoder runs on until Soloist has stopped, so no audio is lost between the two
            await asyncio.sleep(self.PAUSE_SETTLE)

    async def resume(self) -> None:
        if self.soloist and self.paused_by_us:
            self.paused_by_us = False
            await self.soloist.ctl("play")


class Session:
    """One relay's audio: an encoder while the relay asks for audio, stopped when unacked audio goes
    over the in-flight limit (a stall), on `A 0`, or when the relay goes."""

    def __init__(self, player: Player, encoder: Callable[[], Encoder], inflight_ms: int):
        self.player = player
        self.encoder_factory = encoder
        self.limit = inflight_ms
        self.encoder: Encoder | None = None
        self.pump: asyncio.Task | None = None
        self.halting: asyncio.Task | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.sent = self.acked = 0
        self.tasks: set[asyncio.Task] = set()

    @property
    def streaming(self) -> bool:
        return self.encoder is not None

    def inflight_ms(self) -> int:
        return (self.sent - self.acked) * FRAME_MS

    async def run(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        try:
            while True:
                kind, payload = await asyncio.wait_for(wire.read_frame(reader), IDLE_TIMEOUT)
                if kind == wire.AUDIO_REQUEST:
                    if payload.startswith(b"1"):
                        await self.want()
                    else:
                        await self.halt(payload[2:].decode(errors="replace") or "stop")
                elif kind == wire.ACK:
                    self.acked = wire.ack_count(payload)
                elif kind == wire.STATS:
                    log.info(
                        "relay: %s inflight=%d",
                        payload.decode(errors="replace"),
                        self.inflight_ms(),
                    )
        finally:
            # A relay gone is a stall: Soloist shouldn't play to nobody
            await self.halt("gone")
            if self.pump:
                await asyncio.wait([self.pump], timeout=5)

    async def want(self) -> None:
        if self.streaming or self.halting:
            return
        if self.inflight_ms() > self.limit // 2:
            return  # what's in flight from before a stall hasn't drained yet
        await self.player.resume()
        self.encoder = self.encoder_factory()
        await self.encoder.start()
        log.info("audio on (inflight %d ms)", self.inflight_ms())
        self.pump = asyncio.create_task(self._pump(self.encoder))

    async def halt(self, reason: str) -> None:
        if not self.streaming:
            return
        encoder, self.encoder = self.encoder, None
        log.info("audio off: %s (inflight %d ms)", reason, self.inflight_ms())
        self.halting = asyncio.current_task()
        try:
            await self.player.pause()
            await encoder.stop()
        finally:
            self.halting = None

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
