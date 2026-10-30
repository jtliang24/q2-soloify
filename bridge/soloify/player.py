"""What every relay connection shares: Soloist's state and cover, sent to the current relay as they
change, its controls, and the bridge's own pause for a stall (docs/wire.md)."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

import aiohttp

from . import wire
from .state import State

log = logging.getLogger("soloify")

# Soloist stays at full volume, as librespot's --volume-ctrl fixed: the device's volume is its own
VOLUME = 100
# A cover and its track id fit one frame
COVER_MAX = wire.MAX_FRAME - 256


class Covers:
    """A track's cover over HTTPS, as a JPEG; the last one is kept, since an album's tracks share
    it."""

    def __init__(self):
        self.http: aiohttp.ClientSession | None = None
        self.last: tuple[str, bytes] | None = None

    async def get(self, url: str) -> bytes | None:
        if self.last and self.last[0] == url:
            return self.last[1]
        if not url.startswith("https://"):
            return None
        if self.http is None:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        try:
            async with self.http.get(url) as r:
                if r.status != 200 or r.content_type != "image/jpeg":
                    log.info("cover: %s, %s", r.status, r.content_type)
                    return None
                # read(n) may return the first network chunk, rather than the whole JPEG.
                # Consume to EOF with a bound; otherwise firmware renders the missing half gray.
                data = bytearray()
                async for chunk in r.content.iter_chunked(65536):
                    if len(data) + len(chunk) > COVER_MAX:
                        return None
                    data.extend(chunk)
        except (aiohttp.ClientError, TimeoutError) as e:
            log.info("cover: %s", e.__class__.__name__)
            return None
        if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
            return None
        self.last = (url, bytes(data))
        return self.last[1]

    async def close(self) -> None:
        if self.http:
            await self.http.close()


class Player:
    """Soloist through its WebSocket (None for the test source: no state, nothing to pause), and the
    relay it reports to. paused_by_us remembers a stall's pause, so the bridge resumes only what it
    paused itself, never what the user paused. clock is Unix ms, Soloist's position clock."""

    PAUSE_SETTLE = 0.3
    # Soloist's events come in bursts: a play says playing ~75 ms before the new track arrives, so
    # the state waits this long after the first change for the rest
    SETTLE = 0.1

    def __init__(
        self,
        ws=None,
        cover: Callable[[str], Awaitable[bytes | None]] | None = None,
        clock: Callable[[], int] = lambda: int(time.time() * 1000),
    ):
        self.ws = ws
        self.fetch_cover = cover
        self.clock = clock
        self.state = State()
        self.pending: asyncio.TimerHandle | None = None
        self.paused_by_us = False
        self.explicit_paused = False
        self.recovery_pausing = False
        self.capture_changed = None
        self.relay: asyncio.StreamWriter | None = None
        self.sent: tuple | None = None
        self.deferred = False  # a control's state follows its ordered boundary
        self.cover: tuple[str, bytes] | None = None  # (track uri, JPEG)
        self.fetching: str | None = None
        self.tasks: set[asyncio.Task] = set()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    # Soloist's side

    def event(self, event: dict) -> None:
        """One event from Soloist's WebSocket."""
        was_playing = self.state.playing
        self.state.apply(event)
        s = self.state
        if event.get("status") == "paused" and not self.recovery_pausing and not self.paused_by_us:
            self.explicit_paused = True
            self._stalled(False)
        elif event.get("status") == "playing":
            self.explicit_paused = False
        if self.capture_changed and was_playing != s.playing and not self.recovery_pausing:
            self.capture_changed(s.playing)
        if (
            event.get("type")
            in ("auth_state", "device_changed", "playback_state", "volume_changed")
            and s.state != "none"
            and s.volume != VOLUME
        ):
            self._spawn(self.ws.command("set_volume", volume=VOLUME))
        track = s.track
        if (
            self.fetch_cover
            and track
            and track.cover_url
            and not self._has_cover()
            and self.fetching != track.uri
        ):
            self.fetching = track.uri
            self._spawn(self._fetch(track))
        self.push()

    async def _fetch(self, track) -> None:
        try:
            jpeg = await self.fetch_cover(track.cover_url)
        finally:
            if self.fetching == track.uri:
                self.fetching = None
        if jpeg and self.state.track and self.state.track.uri == track.uri:
            self.cover = (track.uri, jpeg)
            self._send_cover()
            self.push()

    def _has_cover(self) -> bool:
        track = self.state.track
        return bool(self.cover and track and self.cover[0] == track.uri)

    # The relay's side

    def attach(self, writer: asyncio.StreamWriter) -> None:
        """A new relay: it gets the cover, then the state."""
        self.relay, self.sent = writer, None
        self._send_cover()
        self.flush()

    def detach(self, writer: asyncio.StreamWriter) -> None:
        if self.relay is writer:
            self.relay = None

    def push(self) -> None:
        """The state to the relay once the burst has settled."""
        if self.relay is not None and self.pending is None:
            self.pending = asyncio.get_running_loop().call_later(self.SETTLE, self.flush)

    def flush(self) -> None:
        """The state to the relay now, when what it shows has changed."""
        if self.pending:
            self.pending.cancel()
            self.pending = None
        if self.deferred or self.relay is None or self.relay.is_closing():
            return
        key = (self.state.key(), self.state.state != "none" and self._has_cover())
        if key == self.sent:
            return
        self.sent = key
        lines = self.state.lines(self._has_cover(), self.clock())
        self.relay.write(wire.frame(wire.STATE, lines))

    def _send_cover(self) -> None:
        if self.relay and not self.relay.is_closing() and self._has_cover():
            self.relay.write(wire.cover(self.state.track.id, self.cover[1]))

    async def control(self, payload: bytes) -> None:
        """A control datagram from Q2 Pod, as the relay got it: p, P, s, n, b or S<ms>."""
        if not self.ws or not payload:
            return
        op, arg = payload[:1], payload[1:].strip()
        if op == b"p":
            op = b"s" if self.state.playing else b"P"
        if op in (b"P", b"s"):
            self.explicit_paused = op == b"s"
            self._stalled(False)  # the user's choice from now on
            await self.ws.command("play" if op == b"P" else "pause")
        elif op == b"n":
            await self.ws.command("skip_next")
        elif op == b"b":
            await self.ws.command("skip_prev")
        elif op == b"S" and arg.isdigit():
            await self.ws.command("seek", position_ms=int(arg))
        else:
            log.info("relay: unknown control %r", payload[:16])

    # The stall rule

    async def pause(self) -> None:
        if (
            self.ws
            and not self.explicit_paused
            and self.state.playing
            and (not self.paused_by_us or self.state.word == "playing")
        ):
            self.recovery_pausing = True
            try:
                if await self.ws.command("pause"):
                    self._stalled(True)
                    await asyncio.sleep(self.PAUSE_SETTLE)
            finally:
                self.recovery_pausing = False

    async def resume(self) -> None:
        if self.ws and self.paused_by_us and not self.explicit_paused:
            if await self.ws.command("play"):
                self._stalled(False)

    def _stalled(self, paused_by_us: bool) -> None:
        self.paused_by_us = self.state.stalled = paused_by_us
        self.push()
