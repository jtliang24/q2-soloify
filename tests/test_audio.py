import asyncio
import ssl
import struct

from soloify import audio, server, wire


def _page(segments: list[bytes], lacing: list[int]) -> bytes:
    head = struct.pack("<4sBBqIIIB", b"OggS", 0, 0, 0, 1, 0, 0, len(lacing))
    return head + bytes(lacing) + b"".join(segments)


def test_ogg_packets_across_pages_and_reads():
    big = bytes(range(256)) * 2  # 512 bytes: lacing 255, 255, 2
    data = _page([b"OpusHead", big[:510]], [8, 255, 255]) + _page([big[510:], b"x"], [2, 1])
    ogg = audio.OggPackets()
    out = []
    for i in range(0, len(data), 7):
        out += ogg.feed(data[i : i + 7])
    assert out == [b"OpusHead", big, b"x"]


class FakeEncoder:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.started = self.stopped = False

    async def start(self):
        self.started = True

    async def packets(self):
        while (packet := await self.queue.get()) is not None:
            yield packet

    async def stop(self):
        self.stopped = True
        await self.queue.put(None)


class FakeSoloist:
    def __init__(self, playing=True):
        self.is_playing = playing
        self.calls = []

    async def playing(self):
        return self.is_playing

    async def ctl(self, *args):
        self.calls.append(args)
        self.is_playing = args[0] == "play"
        return ""


class FakeWriter:
    def __init__(self):
        self.frames = []
        self.closed = False

    def write(self, data):
        self.frames.append(data)

    def is_closing(self):
        return self.closed


async def _settle():
    for _ in range(20):
        await asyncio.sleep(0)


class Rig:
    def __init__(self, limit=100, playing=True):
        audio.Player.PAUSE_SETTLE = 0
        self.soloist = FakeSoloist(playing)
        self.encoders = []
        self.writer = FakeWriter()
        self.reader = asyncio.StreamReader()
        self.session = audio.Session(audio.Player(self.soloist), self._encoder, limit)
        self.task = asyncio.create_task(self.session.run(self.reader, self.writer))

    def _encoder(self):
        self.encoders.append(FakeEncoder())
        return self.encoders[-1]

    async def send(self, kind, payload=b""):
        self.reader.feed_data(wire.frame(kind, payload))
        await _settle()

    async def packets(self, n):
        for i in range(n):
            await self.encoders[-1].queue.put(b"p%d" % i)
        await _settle()


async def test_audio_flows_with_sequence_numbers():
    rig = Rig()
    await rig.send(wire.AUDIO_REQUEST, b"1")
    await rig.packets(3)
    assert rig.writer.frames == [wire.audio(i, b"p%d" % i) for i in range(3)]
    await rig.send(wire.AUDIO_REQUEST, b"1")  # repeated while streaming: ignored
    assert len(rig.encoders) == 1


async def test_stall_pauses_soloist_and_stops_the_encoder():
    rig = Rig(limit=100)  # five packets
    await rig.send(wire.AUDIO_REQUEST, b"1")
    await rig.packets(5)
    assert not rig.encoders[0].stopped
    await rig.packets(1)
    assert rig.encoders[0].stopped
    assert rig.soloist.calls == [("pause",)]
    assert not rig.session.streaming


async def test_after_a_stall_audio_resumes_once_acks_drain():
    rig = Rig(limit=100)
    await rig.send(wire.AUDIO_REQUEST, b"1")
    await rig.packets(6)
    await rig.send(wire.AUDIO_REQUEST, b"1")  # 120 ms in flight: still stalled
    assert len(rig.encoders) == 1
    await rig.send(wire.ACK, struct.pack(">I", 5))
    await rig.send(wire.AUDIO_REQUEST, b"1")
    assert len(rig.encoders) == 2
    assert rig.soloist.calls == [("pause",), ("play",)]


async def test_relay_pause_and_resume():
    rig = Rig()
    await rig.send(wire.AUDIO_REQUEST, b"1")
    await rig.send(wire.AUDIO_REQUEST, b"0 pause")
    assert rig.encoders[0].stopped
    await rig.send(wire.AUDIO_REQUEST, b"1")
    assert rig.soloist.calls == [("pause",), ("play",)]


async def test_never_resumes_what_the_user_paused():
    rig = Rig(playing=False)
    await rig.send(wire.AUDIO_REQUEST, b"1")
    await rig.send(wire.AUDIO_REQUEST, b"0 pause")
    await rig.send(wire.AUDIO_REQUEST, b"1")
    assert rig.soloist.calls == []


async def test_relay_gone_pauses():
    rig = Rig()
    await rig.send(wire.AUDIO_REQUEST, b"1")
    rig.reader.feed_eof()
    await asyncio.wait([rig.task], timeout=5)
    assert isinstance(rig.task.exception(), asyncio.IncompleteReadError)
    assert rig.encoders[0].stopped
    assert rig.soloist.calls == [("pause",)]


async def test_beep_over_tls_stalls_and_resumes(tmp_path):
    """The real encoder with the test source, through the TLS listener."""
    identity = server.Identity.load(tmp_path)
    player = audio.Player(None)

    def encoder():
        return audio.Encoder(audio.ffmpeg_args("test", 160))

    async def session(reader, writer):
        await audio.Session(player, encoder, 400).run(reader, writer)

    listener = await server.Server(identity, session).start("127.0.0.1", 0)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    async with listener:
        port = listener.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=ctx)
        try:
            await _beep_stall_and_resume(identity, reader, writer)
        finally:
            writer.transport.abort()


async def _beep_stall_and_resume(identity, reader, writer):
    writer.write(b"hello 1 %s\n" % identity.token.encode() + wire.frame(wire.AUDIO_REQUEST, b"1"))
    assert await reader.readline() == wire.OK
    # No acks: the stall stops the encoder once 400 ms are in flight; ffmpeg's flush adds some
    seqs = []
    while True:
        try:
            kind, payload = await asyncio.wait_for(wire.read_frame(reader), 1.5)
        except TimeoutError:
            break
        assert kind == wire.AUDIO
        seqs.append(struct.unpack(">I", payload[:4])[0])
    assert seqs == list(range(len(seqs)))
    assert 20 <= len(seqs) <= 60
    writer.write(wire.frame(wire.ACK, struct.pack(">I", len(seqs))))
    writer.write(wire.frame(wire.AUDIO_REQUEST, b"1"))
    kind, payload = await asyncio.wait_for(wire.read_frame(reader), 3)
    assert (kind, payload[:4]) == (wire.AUDIO, struct.pack(">I", len(seqs)))


async def test_silent_relay_is_dropped(monkeypatch):
    monkeypatch.setattr(audio, "IDLE_TIMEOUT", 0.2)
    rig = Rig()
    await rig.send(wire.AUDIO_REQUEST, b"1")
    await asyncio.wait([rig.task], timeout=5)
    assert isinstance(rig.task.exception(), TimeoutError)
    assert rig.encoders[0].stopped
