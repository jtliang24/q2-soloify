"""The bridge's audio: an encoder started per relay request, its Ogg pages split into bare Opus
packets, and the in-flight rule that pauses Soloist when the link stalls (docs/wire.md)."""

import asyncio
import logging
import os
import signal
import struct
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from . import wire
from .player import Player
from .rewind import RestoreError
from .soloist import SINK

log = logging.getLogger("soloify")

FRAME_MS = 20
# The relay sends an ack at least every 5 s (docs/wire.md); silence this long means it's gone
IDLE_TIMEOUT = 20
MIN_INFLIGHT_MS = 300
CAPTURE_READY_TIMEOUT = 3
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
        inp = ["-copyts", "-f", "pulse", "-fragment_size", "3528", "-i", f"{sink}.monitor"]
    return [
        *inp,
        # Retain the first capture timestamp, then use a sample clock without Pulse jitter.
        *("-af", "asetpts=N/SR/TB+STARTPTS", "-ac", "2", "-ar", "48000"),
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
        self.start_granule = None
        self.final_granule = None

    def feed(self, data: bytes) -> list[bytes]:
        self.buf += data
        out = []
        while len(self.buf) >= self._PAGE.size:
            magic, _, flags, granule, _, _, _, nsegs = self._PAGE.unpack_from(self.buf)
            if magic != b"OggS":
                raise ValueError("lost Ogg sync")
            head = self._PAGE.size + nsegs
            if len(self.buf) < head:
                break
            lacing = self.buf[self._PAGE.size : head]
            if len(self.buf) < head + sum(lacing):
                break
            if flags & 4:
                self.final_granule = granule
            pos = head
            audio_packets = 0
            for n in lacing:
                self.partial += self.buf[pos : pos + n]
                pos += n
                if n < 255:
                    out.append(self.partial)
                    if not self.partial.startswith((b"OpusHead", b"OpusTags")):
                        audio_packets += 1
                    self.partial = b""
            if self.start_granule is None and audio_packets and not flags & 4:
                self.start_granule = granule - audio_packets * 960
            self.buf = self.buf[pos:]
        return out


@dataclass(frozen=True)
class OpusTail:
    data: bytes
    samples: int


class Encoder:
    """One ffmpeg run, from start to its end on stop."""

    def __init__(self, args: list[str]):
        self.args = args
        self.proc: asyncio.subprocess.Process | None = None
        self.capture_at = None
        self.present_at = None

    async def start(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "LD_LIBRARY_PATH"}
        self.proc = await asyncio.create_subprocess_exec(
            *("ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error"),
            *self.args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            env=env,
        )

    async def packets(self) -> AsyncIterator[bytes | OpusTail]:
        """Its Opus packets, without the OpusHead and OpusTags headers."""
        ogg = OggPackets()
        pending, samples = None, 0
        while chunk := await self.proc.stdout.read(65536):
            for packet in ogg.feed(chunk):
                if "-copyts" in self.args and ogg.start_granule is not None:
                    self.capture_at = ogg.start_granule / 48000
                if not packet.startswith((b"OpusHead", b"OpusTags")):
                    if pending is not None:
                        yield pending
                    pending = packet
                    samples += 960
        if pending is not None:
            valid = (
                960
                if ogg.final_granule is None
                else ogg.final_granule - (ogg.start_granule or 0) - (samples - 960)
            )
            yield OpusTail(pending, max(0, min(960, valid)))
        await self.proc.wait()

    async def stop(self) -> None:
        """SIGINT, so ffmpeg flushes what it has encoded; packets() then ends."""
        if self.proc and self.proc.returncode is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except TimeoutError:
                self.proc.kill()
                await self.proc.wait()


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
        self.generation = 0
        self.reset_controls = 0
        self.stall_generation = None
        self.unacked = deque()
        self.ack_progress = asyncio.Event()
        self.capture_stopping = asyncio.Event()
        self.capture_ready = asyncio.Event()
        self.capture_release = asyncio.Event()
        self.last_ack = 0.0
        self.capture_blocked = False
        self.rewind = player.rewind
        self.awaiting_bookmark = False
        self.mutating = False
        self.pausing = False

    @property
    def streaming(self) -> bool:
        return self.encoder is not None

    def inflight_ms(self) -> int:
        return (self.sent - self.acked) * FRAME_MS

    async def run(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.player.capture_changed = self.playback_changed
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
                            self.halt(
                                payload[2:].decode(errors="replace") or "stop", self.generation
                            )
                        )
                        self.control_tasks.add(task)
                        task.add_done_callback(self.control_tasks.discard)
                elif kind == wire.ACK:
                    count = wire.ack_count(payload)
                    if not self.acked <= count <= self.sent:
                        raise wire.WireError("invalid audio ack")
                    if count > self.acked:
                        self.acked = count
                        self.last_ack = asyncio.get_running_loop().time()
                        while self.unacked and self.unacked[0][0] < count:
                            self.unacked.popleft()
                        self.ack_progress.set()
                elif kind == wire.BOOKMARK:
                    if len(self.control_tasks) >= 32 or not 4 <= len(payload) <= 1540:
                        raise wire.WireError("invalid pause bookmark")
                    self.capture_blocked = True
                    self.awaiting_bookmark = True
                    task = asyncio.create_task(self.pause_bookmark(payload))
                    self.control_tasks.add(task)
                    task.add_done_callback(self.control_tasks.discard)
                elif kind == wire.CONTROL:
                    if len(self.control_tasks) >= 32:
                        raise wire.WireError("too many controls")
                    if payload[:1] in (b"n", b"b", b"S"):
                        self.reset_controls += 1
                    task = asyncio.create_task(self.control(payload, queued=True))
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
            # Disable event-driven work before awaiting cancelled controls: shutdown can
            # itself receive state events, which must not create another capture task.
            if self.player.capture_changed == self.playback_changed:
                self.player.capture_changed = None
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

        async def boundary(prepare, reset):
            return await self.browse_boundary(prepare, reset, payload[:4])

        if self.browser:
            reply = await self.browser.handle(payload[4:], boundary)
        else:
            try:
                id, _, _ = parse(payload[4:])
                reply = response(id, "unsupported", "Browsing is not configured on this server")
            except (ValueError, UnicodeError):
                reply = response("0", "bad", "Malformed request")
        self.writer.write(wire.frame(wire.BROWSE_RESPONSE, payload[:4] + reply))

    async def browse_boundary(self, prepare, reset=True, correlation=b"\0\0\0\0"):
        async with self.lock:
            action = await prepare()  # read-only: neither capture nor sink is disturbed
            self.player.deferred = True
            try:
                if reset:
                    log.info(
                        "reset begin browse=%d gen=%d", int.from_bytes(correlation), self.generation
                    )
                    self.rewind.bookmark = None
                    self.awaiting_bookmark = False
                    self.writer.write(wire.frame(wire.RESET_BEGIN, correlation))
                    await self._halt("control")
                result = await action()
                if reset:
                    self.player.explicit_paused = False
                    self.player._stalled(False)
                await asyncio.sleep(self.player.SETTLE)
                return result
            finally:
                # Once begun, even cancellation must drain old capture and finish the reset.
                if reset:
                    self.writer.write(wire.frame(wire.BOUNDARY, b"Q"))
                    log.info(
                        "reset end browse=%d gen=%d", int.from_bytes(correlation), self.generation
                    )
                self.player.sent = None
                self.player._send_cover()
                self.player.deferred = False
                self.player.flush()

    def playback_changed(self, playing):
        """External pause stops local output before capture shutdown or source restoration."""
        if (
            playing
            and self.player.transition_snapshot
            and (self.rewind.bookmark or self.awaiting_bookmark)
        ):
            return  # a reconnect snapshot is not an explicit phone Play
        if playing and self.pausing and not self.rewind.navigating:
            # Phone Play during encoder shutdown/seek must not disappear behind the
            # restore lock. Skip's temporary auto-play is internal navigation instead.
            self.writer.write(wire.frame(wire.PAUSE_NOW, b"P"))
            return
        if self.mutating or self.player.recovery_pausing or self.player.timeline.restoring:
            return
        if not playing:
            self.capture_blocked = True
            if self.player.state.state == "paused":
                self.awaiting_bookmark = True
                self.writer.write(wire.frame(wire.PAUSE_NOW))
            task = asyncio.create_task(self.halt("pause", self.generation))
            self.control_tasks.add(task)
            task.add_done_callback(self.control_tasks.discard)
        elif self.rewind.bookmark or self.awaiting_bookmark:
            # Ask the relay to order Play after its bookmark. Never capture a phone's
            # premature Play at the source's still-ahead position.
            self.writer.write(wire.frame(wire.PAUSE_NOW, b"P"))
        else:
            task = asyncio.create_task(self._playback_changed(playing, self.generation))
            self.control_tasks.add(task)
            task.add_done_callback(self.control_tasks.discard)

    async def _playback_changed(self, playing, generation):
        async with self.lock:
            if (
                playing
                and generation == self.generation
                and not self.awaiting_bookmark
                and not self.rewind.bookmark
            ):
                self.capture_blocked = False
                if (
                    self.audio_request is not None
                    and self.player.state.playing
                    and not self.streaming
                    and not self.reset_controls
                ):
                    self.player.sent = None
                    self.player.flush()
                    if self.player.paused_by_us:
                        await self._want()
                    else:
                        await self._start_capture()

    def restored(self, success, message=""):
        bookmark = self.rewind.bookmark
        if bookmark:
            self.writer.write(
                wire.frame(
                    wire.RESTORED, bookmark.correlation + bytes([success]) + message.encode()[:512]
                )
            )

    async def pause_bookmark(self, payload):
        async with self.lock:
            self.pausing = True
            self.mutating = self.player.deferred = True
            self.player.explicit_paused = True
            self.player._stalled(False)
            try:
                previous = self.rewind.bookmark
                try:
                    self.rewind.remember(payload)
                except (ValueError, UnicodeError):
                    self.rewind.remember(payload[:4])
                await self.player.control(b"s")
                await self._halt("pause")
                if self.rewind.bookmark is previous and previous.failed:
                    raise RestoreError(previous.failed)  # retry only on explicit Play
                await self.rewind.restore()
                self.restored(True)
            except RestoreError as error:
                log.warning("pause restore failed: %s", error)
                self.restored(False, str(error))
            finally:
                self.awaiting_bookmark = False
                self.pausing = False
                self.mutating = self.player.deferred = False
                self.player.sent = None
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
        if (
            self.streaming
            or self.halting
            or self.reset_controls
            or self.capture_blocked
            or self.awaiting_bookmark
            or self.rewind.bookmark
        ):
            return
        if self.inflight_ms() > self.limit // 2:
            return  # what's in flight from before a stall hasn't drained yet
        if self.player.explicit_paused:
            return
        if self.player.ws and not self.player.state.playing and not self.player.paused_by_us:
            return
        recovering = self.player.paused_by_us
        try:
            await self._start_capture(recovering)
            if recovering:
                self.encoder.present_at = time.time()
                await self.player.resume()
                if self.player.paused_by_us:
                    await self._halt("pause")
        except TimeoutError:
            log.info("capture readiness deadline gen=%d", self.generation)
            await self._halt("pause")
        finally:
            self.capture_release.set()

    async def _start_capture(self, priming=False):
        self.generation += 1
        self.capture_stopping = asyncio.Event()
        self.capture_ready = asyncio.Event()
        self.capture_release = asyncio.Event()
        self.encoder = self.encoder_factory()
        await self.encoder.start()
        log.info("audio on gen=%d inflight=%d", self.generation, self.inflight_ms())
        self.pump = asyncio.create_task(self._pump(self.encoder, self.generation, priming))
        if priming:
            await asyncio.wait_for(self.capture_ready.wait(), CAPTURE_READY_TIMEOUT)
            log.info(
                "capture ready gen=%d word=%s position=%d",
                self.generation,
                self.player.state.word,
                self.player.state.position(),
            )

    async def halt(self, reason: str, generation=None) -> None:
        async with self.lock:
            await self._halt(reason, generation)

    async def _halt(self, reason: str, generation=None) -> None:
        if not self.streaming or (generation is not None and generation != self.generation):
            return
        encoder, self.encoder = self.encoder, None
        pump = self.pump
        self.capture_stopping.set()
        self.capture_release.set()
        self.ack_progress.set()
        log.info(
            "audio off gen=%d reason=%s inflight=%d ack_age=%.3f oldest_age=%.3f",
            self.generation,
            reason,
            self.inflight_ms(),
            asyncio.get_running_loop().time() - self.last_ack if self.last_ack else -1,
            asyncio.get_running_loop().time() - self.unacked[0][1] if self.unacked else 0,
        )

        async def finish():
            try:
                async with asyncio.timeout(4):
                    if reason not in ("pause", "control"):
                        await self.player.pause()
                    await encoder.stop()
                    if pump:
                        await pump
            except TimeoutError:
                log.info("capture shutdown deadline gen=%d", self.generation)
                if getattr(encoder, "proc", None) and encoder.proc.returncode is None:
                    encoder.proc.kill()
                    await encoder.proc.wait()
                if pump:
                    pump.cancel()
                    await asyncio.gather(pump, return_exceptions=True)

        self.halting = asyncio.create_task(finish())
        cancelled = False
        try:
            while not self.halting.done():
                try:
                    await asyncio.shield(self.halting)
                except asyncio.CancelledError:
                    cancelled = True
            self.halting.result()
            if cancelled:
                raise asyncio.CancelledError
        finally:
            self.halting = None

    async def control(self, payload: bytes, queued=False) -> None:
        """Old capture drains before its boundary. Prime capture BEFORE the final mutation:
        Soloist can auto-play on skip even when paused, so starting afterward loses the intro."""
        async with self.lock:
            self.mutating = self.player.deferred = True
            primed = False
            try:
                reset = payload[:1] in (b"n", b"b", b"S")
                taking_audio = self.streaming or self.audio_request is not None
                if reset:
                    self.rewind.bookmark = None
                    self.awaiting_bookmark = False
                if payload[:1] == b"P" and self.rewind.bookmark:
                    await self.rewind.restore()
                    self.restored(True)
                if payload[:1] == b"P" and taking_audio and not self.streaming:
                    self.capture_blocked = False
                    await self._start_capture(priming=True)
                    primed = True
                if reset:
                    if taking_audio:
                        await self.player.pause()
                    await self._halt("control")
                    if taking_audio and self.reset_controls <= 1:
                        await self._start_capture(priming=True)
                        primed = True
                if primed:
                    self.encoder.present_at = time.time()
                if payload[:1] == b"P" and self.rewind.bookmark:
                    self.player.explicit_paused = False
                    self.player._stalled(False)
                    await self.rewind.command("play")
                    await self.rewind.snapshot(lambda state: state.word == "playing")
                else:
                    await self.player.control(payload)
                if payload[:1] == b"s":
                    # Local output has already stopped; no pause fade needs to be retained.
                    self.capture_blocked = True
                    await self._halt("pause")
                if primed:
                    if self.player.state.word == "playing":
                        self.player._stalled(False)  # Soloist auto-played; capture already has it
                    else:
                        await self.player.resume()
                if payload[:1] == b"P":
                    self.rewind.bookmark = None
                await asyncio.sleep(self.player.SETTLE)
            except RestoreError as error:
                self.capture_blocked = True
                self.player.explicit_paused = True
                self.restored(False, str(error))
                log.warning("resume restore failed: %s", error)
                await self._halt("pause")
                if self.player.ws:
                    await self.player.ws.command("pause")
            except TimeoutError:
                log.info("control capture deadline gen=%d", self.generation)
                await self._halt("pause")  # no mutation was dispatched; never replay it
                if self.rewind.bookmark:
                    self.player.explicit_paused = True
                    self.capture_blocked = True
                    self.restored(False, "Capture or playback confirmation timed out")
                    if self.player.ws:
                        await self.player.ws.command("pause")
            finally:
                self.writer.write(wire.frame(wire.BOUNDARY, payload))
                self.player.sent = None
                self.player._send_cover()
                self.mutating = self.player.deferred = False
                if queued and payload[:1] in (b"n", b"b", b"S"):
                    self.reset_controls -= 1
                self.player.flush()
                self.capture_release.set()  # new audio follows the final boundary and state

    async def _capacity(self, generation):
        """Yield bursts to the ACK reader, bounded by the oldest packet's target deadline."""
        loop = asyncio.get_running_loop()
        while self.inflight_ms() >= self.limit and self.unacked:
            if generation != self.generation or self.capture_stopping.is_set():
                return True  # shutdown drains a bounded tail, without waiting for the network
            remaining = self.unacked[0][1] + self.limit / 1000 - loop.time()
            if remaining <= 0:
                return False
            self.ack_progress.clear()
            try:
                await asyncio.wait_for(self.ack_progress.wait(), remaining)
            except TimeoutError:
                return False
        return True

    async def _pump(self, encoder: Encoder, generation: int, priming=False) -> None:
        loop = asyncio.get_running_loop()
        burst, burst_at, tail = 0, loop.time(), 0
        announced, last_burst_log = False, 0.0
        primer = b""
        skip, discard = 312, 0
        source_samples = 0
        remaining_skip = None
        async for packet in encoder.packets():
            packet_start = source_samples
            source_samples += 960
            end_samples = packet.samples if isinstance(packet, OpusTail) else None
            if isinstance(packet, OpusTail):
                packet = packet.data
            if not self.capture_ready.is_set():
                self.capture_ready.set()
                if priming:
                    await self.capture_release.wait()
                    start = getattr(encoder, "capture_at", None)
                    present = getattr(encoder, "present_at", None)
                    skip = (
                        312 + max(0, round((present - start) * 48000))
                        if (start is not None and present is not None)
                        else 1272
                    )
                    # Keep at least 80 ms of decoder history even for slow priming. Earlier
                    # paused samples are omitted, bounding pre-skip and the warmup queue.
                    discard = max(0, (skip - 4800) // 960)
                    skip -= discard * 960
                    log.info("capture prefix gen=%d skip=%d", generation, skip)
                    if discard:
                        discard -= 1
                        continue
                    elif skip >= 960:
                        primer = packet
                        continue
            if discard:
                discard -= 1
                continue
            now = loop.time()
            if now - burst_at > 0.005:
                if burst >= 10 and now - last_burst_log >= 1:
                    log.info(
                        "capture burst gen=%d packets=%d inflight=%d oldest=%.3f",
                        generation,
                        burst,
                        self.inflight_ms(),
                        now - self.unacked[0][1] if self.unacked else 0,
                    )
                    last_burst_log = now
                burst, burst_at = 0, now
            burst += 1
            if self.writer.is_closing() or generation != self.generation:
                continue
            if not await self._capacity(generation):
                if self.stall_generation != generation:
                    self.stall_generation = generation
                    task = asyncio.create_task(self.halt("stall", generation))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
                # Let shutdown acquire the lock and pause Soloist, then drain only a short tail.
                await asyncio.sleep(0)
            if self.encoder is not encoder or self.stall_generation == generation:
                tail += 1
                if tail > 15:  # at most 300 ms beyond the in-flight limit
                    continue
            if not announced:
                self.writer.write(
                    wire.frame(
                        wire.CAPTURE,
                        struct.pack(">IH", generation, skip) + primer,
                    )
                )
                announced = True
                remaining_skip = max(0, skip - (960 if primer else 0))
            valid = 960 if end_samples is None else end_samples
            hidden = min(remaining_skip, valid)
            remaining_skip -= hidden
            capture_at = getattr(encoder, "capture_at", None)
            if capture_at is not None and valid > hidden:
                at = capture_at * 1000 + (packet_start + hidden - 312) / 48
                event_wait = (at / 1000 + self.player.SETTLE) - time.time()
                if 0 < event_wait <= self.player.SETTLE and self.player.ws:
                    await asyncio.sleep(event_wait)
                if generation != self.generation or self.writer.is_closing():
                    continue
                anchor = self.player.timeline.anchor(at)
                # An explicit invalid anchor prevents accidentally extending the previous
                # track through a timestamp gap or an unconfirmed source transition.
                self.writer.write(wire.frame(wire.TIMELINE, anchor or b"token=0\nposition=0\n"))
            elif valid > hidden:
                self.writer.write(wire.frame(wire.TIMELINE, b"token=0\nposition=0\n"))
            if end_samples is not None:
                self.writer.write(
                    wire.frame(wire.AUDIO_END, struct.pack(">IH", self.sent, end_samples))
                )
            self.writer.write(wire.audio(self.sent, packet))
            self.unacked.append((self.sent, loop.time()))
            self.sent += 1
