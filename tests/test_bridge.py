"""PLAN.md step 2's check: the bridge against a fake relay over TLS, with Soloist's WebSocket
replayed from the recording. A scripted session (cast, pause, play, seek, skip, previous, the
phone's volume, deactivate, a Soloist restart) gives the expected state lines, covers and
commands."""

import asyncio
import ssl

from soloify import audio, server, wire
from soloify.player import Player
from soloify.ws import SoloistWS
from soloist_fake import ALBUM, FakeSoloist


class FakeRelay:
    """The relay's side of the wire: hello, then controls out, and state and covers in."""

    async def connect(self, port: int, token: str) -> "FakeRelay":
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
        self.writer.write(b"hello 2 %s\n" % token.encode())
        assert await self.reader.readline() == wire.OK
        return self

    def control(self, c: bytes) -> None:
        self.writer.write(wire.frame(wire.CONTROL, c))

    async def state(self, timeout: float = 3) -> dict[str, str]:
        """The next state frame as a dict; a cover before it is kept in self.cover."""
        while True:
            kind, payload = await asyncio.wait_for(wire.read_frame(self.reader), timeout)
            if kind == wire.COVER:
                track, jpeg = payload.split(b"\n", 1)
                self.cover = (track.decode(), jpeg)
            elif kind == wire.STATE:
                lines = payload.decode().splitlines()
                return dict(line.split("=", 1) for line in lines)

    async def state_where(self, **want) -> dict[str, str]:
        """The first state frame with these values, skipping those on the way."""
        while True:
            s = await self.state()
            if all(s.get(k) == v for k, v in want.items()):
                return s

    def close(self) -> None:
        self.writer.transport.abort()


async def test_a_scripted_session(tmp_path):
    Player.PAUSE_SETTLE = 0
    fake = await FakeSoloist(tmp_path / "soloist-state").start()

    async def cover(url):
        return b"\xff\xd8 " + url[-6:].encode()

    player = Player(cover=cover)
    link = player.ws = SoloistWS(fake.port_file, player.event)
    link.RETRY = (0.05, 0.2)
    tasks = [asyncio.create_task(link.run())]

    async def session(reader, writer):
        await audio.Session(player, lambda: None, 2000).run(reader, writer)

    identity = server.Identity.load(tmp_path)
    listener = await server.Server(identity, session).start("127.0.0.1", 0)
    relay = None
    try:
        port = listener.sockets[0].getsockname()[1]
        relay = await FakeRelay().connect(port, identity.token)
        # Connected but not the active device: none
        assert await relay.state() == {"state": "none"}

        # Cast from the phone: Q2 becomes active, then plays the album
        await fake.inject("activate")
        s = await relay.state_where(state="paused")
        assert s["track"] == "2ByBBvpR9b7IynvRmnG7kG" and s["cover"] == "1"
        assert relay.cover[0] == "2ByBBvpR9b7IynvRmnG7kG"
        await fake.inject("play", uri=ALBUM)
        s = await relay.state_where(state="playing", track="1NeLwFETswx8Fzxl2AFl91")
        assert (s["title"], s["artist"], s["album"]) == (
            "Something About Us",
            "Daft Punk",
            "Discovery",
        )
        assert relay.cover[0] == "1NeLwFETswx8Fzxl2AFl91"

        # Q2 Pod's controls, through the relay
        relay.control(b"s")
        assert (await fake.wait_for("pause")) == {"type": "command", "command": "pause"}
        s = await relay.state_where(state="paused")
        assert s["position"] == "8080"
        relay.control(b"P")
        await fake.wait_for("play")
        await relay.state_where(state="playing")
        relay.control(b"S60000")
        assert (await fake.wait_for("seek"))["position_ms"] == 60000
        s = await relay.state_where(state="playing")
        assert 60000 <= int(s["position"]) < 61000
        relay.control(b"n")
        await fake.wait_for("skip_next")
        s = await relay.state_where(track="186hvCTyrni4KT9nwIQ7zS")
        assert s["title"] == "Superheroes" and int(s["position"]) < 1000
        relay.control(b"b")
        await fake.wait_for("skip_prev")

        # The phone turns Q2 down: the bridge puts it back to 100
        await fake.inject("set_volume", volume=50)
        assert (await fake.wait_for("set_volume"))["volume"] == 100

        # Another device takes over
        await fake.inject("deactivate")
        assert await relay.state_where(state="none") == {"state": "none"}

        # Soloist restarts: none while it's gone, and the bridge connects to the new one
        await fake.inject("activate")
        await relay.state_where(track="186hvCTyrni4KT9nwIQ7zS")
        await fake.stop()
        assert await relay.state_where(state="none") == {"state": "none"}
        fake = await FakeSoloist(tmp_path / "soloist-state").start()
        for _ in range(100):
            if fake.connections:
                break
            await asyncio.sleep(0.02)
        assert fake.connections == 1
    finally:
        if relay:
            relay.close()
        for task in tasks:
            task.cancel()
        listener.close()
        await fake.stop()
