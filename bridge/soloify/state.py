"""Soloist's playback, as the relay's state lines: the keys of librespot's status file (q2-pod's
docs/internals.md, Spotify), less `at=`, which the relay stamps with its own clock."""

import time
from dataclasses import dataclass

# Soloist's status -> the status file's state; buffering (after a seek, a skip, a new context)
# keeps the word before it, so the relay's sink isn't stopped for a tenth of a second each time
WORDS = {"idle": "stopped", "paused": "paused", "playing": "playing"}


def _name(entity: dict) -> str:
    return ((entity.get("decorations") or {}).get("identity") or {}).get("name") or ""


def _clean(text: str) -> str:
    """One line of the status file: no newlines or NULs, at most librespot's 255 bytes."""
    text = text.replace("\n", " ").replace("\r", " ").replace("\0", "")
    return text.encode()[:255].decode(errors="ignore")


@dataclass(frozen=True)
class Track:
    uri: str
    title: str = ""
    artist: str = ""
    album: str = ""
    duration: int = 0
    cover_url: str = ""

    @property
    def id(self) -> str:
        """The Spotify id, as librespot's track= gives it."""
        return self.uri.rsplit(":", 1)[-1]

    @classmethod
    def from_entity(cls, item: dict) -> "Track | None":
        """None for an empty entity: Soloist sends one while a new context loads."""
        uri = item.get("uri") or ""
        if not uri:
            return None
        dec = item.get("decorations") or {}
        covers = (dec.get("visual_identity") or {}).get("cover") or []
        # default is about 300 px, the size librespot picks; Q2 Pod sizes it down itself
        cover = next((c for c in covers if c.get("size") == "default"), covers[0] if covers else {})
        return cls(
            uri=uri,
            title=_name(item),
            artist=", ".join(
                filter(None, (_name(c.get("entity") or {}) for c in dec.get("creators") or []))
            ),
            album=_name((dec.get("parent") or {}).get("entity") or {}),
            duration=int((dec.get("playback") or {}).get("duration_ms") or 0),
            cover_url=cover.get("url") or "",
        )


@dataclass
class State:
    """What Soloist last said, from its events. connected is the WebSocket; logged_in and active are
    auth_state's (or device_changed's), and without all three the state is none."""

    connected: bool = False
    logged_in: bool = False
    active: bool = False
    word: str = "stopped"
    track: Track | None = None
    # position.position_ms at timestamp_ms (Unix ms), advancing at speed
    anchor: tuple[int, int, float] = (0, 0, 0.0)
    volume: int = 100
    shuffle: bool = False
    repeat: str = "off"
    context: str = ""
    # The bridge paused Soloist for a stall: the device plays on from its buffer, so it's still
    # playing as far as Q2 Pod goes (whose idle stop would otherwise end the relay)
    stalled: bool = False

    def apply(self, event: dict) -> None:
        kind = event.get("type")
        if kind == "gone":
            self.connected = self.logged_in = self.active = False
            return
        self.connected = True
        if kind == "auth_state":
            self.logged_in = bool(event.get("logged_in"))
            self.active = bool(event.get("is_active"))
        elif kind == "device_changed":
            self.active = bool(event.get("is_active"))
        elif kind == "playback_state":
            self.active = bool(event.get("is_active", self.active))
            self.logged_in = True  # Soloist sends it only when logged in
            self._status(event.get("status"))
            self._item(event.get("item"))
            self._position(event.get("position"))
            self._options(event.get("options"))
            self.context = (event.get("context") or {}).get("uri") or self.context
            if isinstance(event.get("volume"), int | float):
                self.volume = int(event["volume"])
        elif kind == "track_changed":
            self._item(event.get("item"))
        elif kind == "playback_changed":
            self._status(event.get("status"))
        elif kind == "position_sync":
            self._position(event.get("position"))
        elif kind == "volume_changed":
            self.volume = int(event.get("volume") or 0)
        elif kind == "options_changed":
            self._options(event.get("options"))
        elif kind == "context_changed":
            self.context = (event.get("context") or {}).get("uri") or ""

    def _status(self, status) -> None:
        if status in WORDS:
            self.word = WORDS[status]

    def _item(self, item) -> None:
        track = Track.from_entity(item or {})
        if track:
            if not self.track or track.uri != self.track.uri:
                # A new track starts at 0 until its position_sync, which follows it
                self.anchor = (0, 0, 0.0)
            self.track = track

    def _position(self, pos) -> None:
        if pos:
            self.anchor = (
                int(pos.get("position_ms") or 0),
                int(pos.get("timestamp_ms") or 0),
                float(pos.get("speed") or 0),
            )

    def _options(self, opts) -> None:
        if opts:
            self.shuffle = bool(opts.get("shuffle"))
            self.repeat = opts.get("repeat") or "off"

    @property
    def state(self) -> str:
        if not (self.connected and self.logged_in and self.active):
            return "none"
        return "playing" if self.stalled and self.word == "paused" else self.word

    @property
    def playing(self) -> bool:
        return self.state == "playing"

    def position(self, now_ms: int | None = None) -> int:
        """The position now, from the last anchor; it advances only while playing."""
        pos, at, speed = self.anchor
        if self.word == "playing" and at:
            now = int(time.time() * 1000) if now_ms is None else now_ms
            pos += int(max(now - at, 0) * speed)
        if self.track and self.track.duration:
            pos = min(pos, self.track.duration)
        return max(pos, 0)

    def key(self) -> tuple:
        """What a state line shows, but for the position's advance: when it changes, send it."""
        if self.state == "none":
            return ("none",)
        return (self.state, self.track, self.anchor if self.track else None)

    def lines(self, cover: bool, now_ms: int | None = None) -> bytes:
        """The state frame: `key=value` lines; cover says the relay has this track's JPEG."""
        state = self.state
        out = [f"state={state}"]
        if state != "none" and self.track:
            t = self.track
            out += [
                f"track={_clean(t.id)}",
                f"title={_clean(t.title)}",
                f"artist={_clean(t.artist)}",
                f"album={_clean(t.album)}",
                f"duration={t.duration}",
                f"position={self.position(now_ms)}",
            ]
            if cover:
                out.append("cover=1")
        return ("\n".join(out) + "\n").encode()
