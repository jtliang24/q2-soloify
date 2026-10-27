import asyncio

from soloify import wire
from soloify.player import Player
from soloify.state import State, Track
from soloist_fake import recording


class FakeWriter:
    def __init__(self):
        self.frames = []

    def write(self, data):
        self.frames.append(data)

    def is_closing(self):
        return False


class FakeWS:
    def __init__(self):
        self.calls = []

    async def command(self, name, **fields):
        self.calls.append((name, fields))
        return True


def _frames(raw: list[bytes]) -> list[str]:
    """Frames as readable text: `s` state lines on one line, `j` as its track id."""
    out = []
    for f in raw:
        kind, body = f[:1], f[5:]
        if kind == wire.STATE:
            out.append(body.decode().strip().replace("\n", " "))
        elif kind == wire.COVER:
            track, jpeg = body.split(b"\n", 1)
            out.append(f"cover {track.decode()} {jpeg.decode()}")
    return out


async def _replay() -> tuple[list[str], FakeWS]:
    """The recording through a player, in its own timing: each burst settles SETTLE after its
    first event, and the clock follows the recording's."""
    rec = recording()
    first = next(e for e in rec if e["dir"] == "in" and "position" in e["msg"])
    t0 = first["msg"]["position"]["timestamp_ms"] - int(first["t"] * 1000)
    now = 0.0

    async def cover(url):
        return b"jpeg " + url[-6:].encode()

    ws = FakeWS()
    player = Player(ws, cover, clock=lambda: t0 + int(now * 1000))
    writer = FakeWriter()
    player.attach(writer)
    since = None
    for e in rec:
        if e["dir"] != "in":
            continue
        if since is not None and e["t"] - since >= player.SETTLE:
            now = since + player.SETTLE
            player.flush()
            since = None
        now = e["t"]
        player.event(e["msg"])
        for _ in range(5):
            await asyncio.sleep(0)  # the cover's fetch
        if since is None and player.pending:
            since = e["t"]
    player.flush()
    return _frames(writer.frames), ws


SOMETHING = "track=1NeLwFETswx8Fzxl2AFl91 title=Something About Us artist=Daft Punk album=Discovery"
SUPERHEROES = "track=186hvCTyrni4KT9nwIQ7zS title=Superheroes artist=Daft Punk album=Discovery"


async def test_the_recorded_session_gives_the_state_lines():
    """Connect (not active), cast (activate, then an album), pause, play, seek, skip, previous,
    volume, shuffle, repeat, queue, pause and deactivate: one state line per change Q2 Pod shows,
    each cover ahead of the state that says cover=1, and the volume set back to 100."""
    frames, ws = await _replay()
    assert frames == [
        "state=none",
        "cover 2ByBBvpR9b7IynvRmnG7kG jpeg fc2cf0",
        "state=paused track=2ByBBvpR9b7IynvRmnG7kG title=Clean (Taylor's Version) "
        "artist=Taylor Swift album=1989 (Taylor's Version) duration=271000 position=104768 "
        "cover=1",
        "cover 1NeLwFETswx8Fzxl2AFl91 jpeg ce5ade",
        f"state=playing {SOMETHING} duration=233000 position=0 cover=1",
        f"state=playing {SOMETHING} duration=232666 position=100 cover=1",
        f"state=paused {SOMETHING} duration=232666 position=8080 cover=1",
        f"state=playing {SOMETHING} duration=232666 position=8459 cover=1",
        f"state=playing {SOMETHING} duration=232666 position=60003 cover=1",
        "cover 186hvCTyrni4KT9nwIQ7zS jpeg ce5ade",
        f"state=playing {SUPERHEROES} duration=238000 position=0 cover=1",
        f"state=playing {SUPERHEROES} duration=237800 position=100 cover=1",
        f"state=playing {SUPERHEROES} duration=237800 position=2 cover=1",
        f"state=playing {SUPERHEROES} duration=237800 position=14171 cover=1",
        f"state=paused {SUPERHEROES} duration=237800 position=16161 cover=1",
        "state=none",
    ]
    assert ws.calls == [("set_volume", {"volume": 100})]


def _item(uri, name="T", duration=1000):
    return {
        "uri": uri,
        "entity_type": "track",
        "decorations": {"identity": {"name": name}, "playback": {"duration_ms": duration}},
    }


def _active():
    s = State()
    s.apply({"type": "auth_state", "logged_in": True, "is_active": True})
    return s


def test_buffering_keeps_the_state_before_it():
    s = _active()
    s.apply({"type": "playback_changed", "status": "playing"})
    s.apply({"type": "playback_changed", "status": "buffering"})
    assert s.state == "playing"
    s.apply({"type": "playback_changed", "status": "idle"})
    assert s.state == "stopped"


def test_none_unless_connected_logged_in_and_active():
    s = _active()
    s.apply({"type": "playback_changed", "status": "paused"})
    assert s.state == "paused"
    s.apply({"type": "device_changed", "is_active": False})
    assert s.state == "none"
    s.apply({"type": "device_changed", "is_active": True})
    s.apply({"type": "gone"})
    assert s.state == "none"
    assert s.lines(cover=True) == b"state=none\n"


def test_an_empty_item_keeps_the_track():
    s = _active()
    s.apply({"type": "track_changed", "item": _item("spotify:track:a")})
    s.apply({"type": "playback_state", "status": "playing", "item": {"uri": ""}, "is_active": True})
    assert s.track.id == "a"


def test_position_advances_only_while_playing_and_stops_at_the_end():
    s = _active()
    s.apply({"type": "track_changed", "item": _item("spotify:track:a", duration=10_000)})
    sync = {"position_ms": 1000, "timestamp_ms": 50_000, "speed": 1}
    s.apply({"type": "position_sync", "position": sync})
    s.apply({"type": "playback_changed", "status": "playing"})
    assert s.position(now_ms=52_500) == 3500
    assert s.position(now_ms=90_000) == 10_000
    s.apply({"type": "playback_changed", "status": "paused"})
    assert s.position(now_ms=52_500) == 1000


def test_lines_are_one_line_each():
    t = Track("spotify:track:x", title="a\nb", artist="c\0d", album="é" * 200)
    s = _active()
    s.track = t
    lines = s.lines(cover=False).decode().splitlines()
    assert "title=a b" in lines and "artist=cd" in lines
    album = next(v for v in lines if v.startswith("album="))
    assert len(album[6:].encode()) <= 255


async def test_controls_become_commands():
    ws = FakeWS()
    player = Player(ws)
    for c in (b"P", b"s", b"n", b"b", b"S60000", b"Sx", b"?"):
        await player.control(c)
    player.event({"type": "auth_state", "logged_in": True, "is_active": True})
    player.event({"type": "playback_changed", "status": "playing"})
    await player.control(b"p")  # a toggle: playing, so pause
    assert ws.calls == [
        ("play", {}),
        ("pause", {}),
        ("skip_next", {}),
        ("skip_prev", {}),
        ("seek", {"position_ms": 60000}),
        ("pause", {}),
    ]


async def test_a_new_relay_gets_the_cover_then_the_state():
    async def cover(url):
        return b"jpeg"

    player = Player(FakeWS(), cover)
    player.event({"type": "auth_state", "logged_in": True, "is_active": True})
    item = _item("spotify:track:a")
    item["decorations"]["visual_identity"] = {"cover": [{"url": "https://x/c", "size": "default"}]}
    player.event({"type": "track_changed", "item": item})
    await asyncio.sleep(0.01)
    writer = FakeWriter()
    player.attach(writer)
    assert _frames(writer.frames) == [
        "cover a jpeg",
        "state=stopped track=a title=T artist= album= duration=1000 position=0 cover=1",
    ]


async def test_a_stall_still_says_playing():
    """The bridge's own pause for a stall isn't one for Q2 Pod: the device plays on from its
    buffer, and a paused state would free its DAC and start its idle stop."""
    Player.PAUSE_SETTLE = 0
    ws = FakeWS()
    player = Player(ws)
    writer = FakeWriter()
    player.attach(writer)

    def says(status=None):
        if status:
            player.event({"type": "playback_changed", "status": status})
        player.flush()
        return _frames(writer.frames)[-1].split()[0]

    player.event({"type": "auth_state", "logged_in": True, "is_active": True})
    player.event({"type": "track_changed", "item": _item("spotify:track:a")})
    assert says("playing") == "state=playing"
    await player.pause()
    assert says("paused") == "state=playing"
    await player.resume()
    assert says("playing") == "state=playing"
    await player.control(b"s")  # the user's pause is one
    assert says("paused") == "state=paused"
    assert [c for c, _ in ws.calls] == ["pause", "play", "pause"]
