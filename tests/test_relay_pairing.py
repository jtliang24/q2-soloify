"""Pairing by code on the relay (pair.c) against the real bridge's side (pairing.py): the Soloify
app's Pair level, a relay with no remote showing the code as its page's error, the Away record and
its Status line. Discovery stays on loopback (SOLOIFY_PAIR_BROADCAST)."""

import asyncio
import queue
import socket
import stat
import tempfile
import time
from pathlib import Path

import pytest

from relay_rig import Bridge, Relay, relay_binary, until
from soloify import pairing, server, wire
from test_browse import decode, request
from test_contract import browse
from test_manage import Manager, make_card


def free_udp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PairingBridge(Bridge):
    """The relay rig's bridge with pairing, its admin socket in its data folder."""

    def __init__(self, data: Path):
        super().__init__(data)
        self.data = data
        self.pairing = pairing.Pairing(self.identity)
        self.server.pairing = self.pairing
        self.nonces: list[bytes] = []
        self.admin = None

    async def start(self, port: int = 0, host: str = "127.0.0.1"):
        await super().start(port, host)
        if not self.admin:
            self.admin = await self.pairing.serve_admin(self.data / pairing.ADMIN_SOCKET)
        return self

    async def stop(self):
        await super().stop()
        if self.admin:
            self.admin.close()


class Device:
    """`soloify pair-device` with the discovery responder, typed lines from a queue."""

    def __init__(self, bridge: PairingBridge, udp_port: int, endpoints=None, discovery=True):
        self.typed: queue.Queue = queue.Queue()
        self.said: list[str] = []
        self.task = asyncio.ensure_future(
            pairing.pair_device(
                bridge.data / pairing.ADMIN_SOCKET,
                endpoints or pairing.Endpoints("127.0.0.1", bridge.port),
                ask=lambda prompt: self.typed.get(),
                out=self.said.append,
                discovery=discovery,
                udp_port=udp_port,
            )
        )

    async def waiting(self, bridge):
        await until(lambda: bridge.pairing.admin is not None or self.task.done())
        assert not self.task.done(), self.said

    def type(self, text: str) -> None:
        self.typed.put(text)

    async def result(self) -> int:
        return await asyncio.wait_for(self.task, 10)

    def stop(self):
        self.typed.put(None)
        self.task.cancel()


@pytest.fixture
async def short():
    with tempfile.TemporaryDirectory(prefix="sf-") as folder:
        yield Path(folder)


@pytest.fixture
def udp(monkeypatch):
    port = free_udp_port()
    monkeypatch.setenv("SOLOIFY_PAIR_UDP_PORT", str(port))
    return port


@pytest.fixture
def nonces(monkeypatch):
    """The nonces the bridge's pair lines carry, as they arrive."""
    seen = []
    parse = pairing.parse_pair

    def record(line):
        parsed = parse(line)
        if parsed:
            seen.append(parsed[0])
        return parsed

    monkeypatch.setattr(pairing, "parse_pair", record)
    return seen


@pytest.fixture
async def managed(tmp_path, short):
    managers = []

    async def start(folder, **env):
        caps = short / "caps"
        caps.write_text("pages sections field fieldtext progress\n")
        m = await Manager(folder, short / "m", SOLOIFY_CAPS=str(caps), **env).start()
        managers.append(m)
        return m

    yield start
    for m in managers:
        await m.stop()


async def pair_level(m, until_title, timeout=15):
    """The Pair level's rows once its state row's title starts with until_title."""
    end = time.monotonic() + timeout
    while True:
        head, rows = await m.ask("pair")
        assert head[1] == "ok"
        if rows[0][2].startswith(until_title):
            return head, rows
        assert time.monotonic() < end, rows
        await asyncio.sleep(0.2)


async def menu_word(m, uri):
    return (await m.menu())[1][uri][3]


async def test_manager_pairs_by_code_end_to_end(tmp_path, managed, udp, nonces):
    bridge = await PairingBridge(tmp_path / "data").start()
    device = Device(bridge, udp)
    try:
        await device.waiting(bridge)
        # Our relay in the Spotify slot: the pairing reaches its remote too
        soloify = make_card(tmp_path / "card", relay_binary(), slot=relay_binary().read_bytes())
        m = await managed(soloify)
        rows = (await m.menu())[1]
        assert rows["pair"][:3] == ["list", "pair", "Pair with server"]
        assert list(rows) == ["slot", "paired", "pair", "restore", "delay", "quality"]
        head, rows = await m.ask("pair")
        assert head[2] == "refresh"
        assert [r[:2] for r in rows] == [["info", "pairstate"]]
        head, rows = await pair_level(m, "Enter ")
        # Waiting, the level stays the same one row, asked again every 2 s and written in place
        assert head[2] == "refresh" and [r[:2] for r in rows] == [["info", "pairstate"]]
        code = rows[0][2].removeprefix("Enter ")
        assert rows[0][3] == "on your Soloify server"
        assert code == pairing.shown(pairing.code(bridge.identity.fingerprint, nonces[-1]))
        await until(lambda: any("Q2 wants to pair" in line for line in device.said))
        device.type(code)
        head, rows = await pair_level(m, "Paired")
        assert head[2] == "" and rows[0][3] == "with 127.0.0.1"
        assert await device.result() == 0
        line = f"127.0.0.1 {bridge.port} {bridge.identity.token} sha256={bridge.identity.pin}\n"
        for folder in (soloify, tmp_path / "card/.spotify"):
            assert (folder / "remote").read_text() == line
            assert stat.S_IMODE((folder / "remote").stat().st_mode) == 0o600
            assert not (folder / "remote.tmp").exists()
        rows = (await m.menu())[1]
        assert rows["paired"][3] == "Yes"
        assert rows["away"][2:4] == ["Away", "Not set up"]  # no WAN line
        rows = list(rows)
        assert rows == ["slot", "paired", "bridge", "away", "pair", "restore", "delay", "quality"]
        # The level asked again at once is the outcome; entered afresh later, a new run
        assert (await m.ask("pair"))[1][0][2] == "Paired"
    finally:
        device.stop()
        await bridge.stop()


async def test_relay_without_remote_shows_the_code_as_its_page_error(tmp_path, short, udp):
    """The Spotify slot (and the M1 Plus's Soloify row): no remote, so the relay pairs by itself."""
    bridge = await PairingBridge(tmp_path / "data").start()
    device = Device(bridge, udp)
    card = tmp_path / "card"
    (card / ".soloify").mkdir(parents=True)
    slot = card / ".spotify"
    slot.mkdir()
    base = short / "r"
    env = {"SOLOIFY_Q2POD": str(base), "SOLOIFY_NAME": "M1"}
    relay = await Relay(slot, [], "-q", env=env).start()  # an empty remote
    try:
        await device.waiting(bridge)
        await until(lambda: Path(f"{base}.browse.sock").exists())

        async def error():
            head = decode(await browse(base, request("menu")))[0]
            assert head[1] == "error"
            return head[2]

        message = await error()
        while not message.startswith("Pair: enter"):
            await asyncio.sleep(0.2)
            message = await error()
        code = message.removeprefix("Pair: enter ").removesuffix(" on your Soloify server")
        assert len(code) == 7 and code[3] == " "
        await until(lambda: any("M1 wants to pair" in line for line in device.said))
        device.type(code)
        await relay.expect(f"connected to 127.0.0.1:{bridge.port}")
        lines = (slot / "remote").read_text()
        assert lines.startswith(f"127.0.0.1 {bridge.port} {bridge.identity.token} sha256=")
        assert (card / ".soloify/remote").read_text() == lines  # for the Soloify app
        assert len(bridge.sessions) == 1
    finally:
        device.stop()
        await relay.stop()
        await bridge.stop()


async def test_relay_retry_pairs_again_after_a_refusal(tmp_path, short, udp):
    bridge = await PairingBridge(tmp_path / "data").start()
    device = Device(bridge, udp)
    base = short / "r"
    relay = await Relay(tmp_path, [], "-q", env={"SOLOIFY_Q2POD": str(base)}).start()
    try:
        await device.waiting(bridge)
        await until(lambda: Path(f"{base}.browse.sock").exists())
        await relay.expect("pair: enter")
        device.type("000000" if "000 000" not in relay.log[-1] else "000001")
        await relay.expect("pair: The code typed on the server was wrong")
        # The outcome shows once; the Retry after it starts another run, with a new code
        message = decode(await browse(base))[0][2]
        assert message == "Pair: The code typed on the server was wrong. Retry to pair again"
        message = decode(await browse(base))[0][2]
        assert message.startswith("Pair: looking for your Soloify server")
        await relay.expect("pair: enter")
        assert (tmp_path / "remote").read_text() == ""
    finally:
        device.stop()
        await relay.stop()
        await bridge.stop()


async def test_impostor_certificate_gives_another_code(tmp_path, managed, udp, nonces):
    real = server.Identity.load(tmp_path / "real")
    impostor = await PairingBridge(tmp_path / "impostor").start()
    device = Device(impostor, udp)
    try:
        await device.waiting(impostor)
        m = await managed(make_card(tmp_path / "card", relay_binary()))
        await m.ask("pair")
        code = (await pair_level(m, "Enter "))[1][0][2].removeprefix("Enter ")
        nonce = nonces[-1]
        assert code == pairing.shown(pairing.code(impostor.identity.fingerprint, nonce))
        assert code != pairing.shown(pairing.code(real.fingerprint, nonce))
        # The code the real bridge would have asked for is refused here
        await until(lambda: any("wants to pair" in line for line in device.said))
        device.type(pairing.shown(pairing.code(real.fingerprint, nonce)))
        rows = (await pair_level(m, "Not paired"))[1]
        assert rows[0][3] == "The code typed on the server was wrong"
        assert [r[:2] for r in rows][-1] == ["action", "pairagain"]
        assert not (tmp_path / "card/.soloify/remote").exists()
    finally:
        device.stop()
        await impostor.stop()


async def fake_bridge(identity, answer: bytes):
    """A TLS listener with identity's certificate that answers any line with answer."""

    async def handle(reader, writer):
        await reader.readline()
        writer.write(answer)
        await writer.drain()
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0, ssl=identity.ssl_context())


async def test_address_field_and_the_answers_checks(tmp_path, managed):
    """No answer to the broadcast: the field takes the address. A remote line pinned to another
    certificate, a refusal and a bridge without pairing each leave no remote."""
    soloify = make_card(tmp_path / "card", relay_binary())
    m = await managed(soloify, SOLOIFY_PAIR_UDP_PORT=str(free_udp_port()))
    await m.ask("pair")
    head, rows = await pair_level(m, "No server answered", timeout=20)
    assert head[2] == ""
    assert [r[:2] for r in rows] == [
        ["info", "pairstate"],
        ["field", "pairaddr"],
        ["action", "pairagain"],
    ]
    assert rows[1][2] == "Server address:" and rows[1][5] == ""  # nothing tried yet
    assert rows[2][2] == "Look again"
    assert (await m.ask("pairaddr", "not an address!"))[0][1] == "bad"

    ours, other = server.Identity.load(tmp_path / "a"), server.Identity.load(tmp_path / "b")
    wrong_pin = f"127.0.0.1 7443 {ours.token} sha256={other.pin}\n".encode()
    cases = [
        (b"remote 1 1\n" + wrong_pin, "The server's answer didn't match its certificate"),
        (b"denied idle\n", "Start pairing on the server first (soloify pair-device)"),
        (b"denied\n", "The server refused"),
        (b"", "The server's Soloify can't pair: update it"),
        (b"remote 1 2\n" + wrong_pin.replace(other.pin.encode(), ours.pin.encode()), None),
    ]
    for answer, why in cases:
        listener = await fake_bridge(ours, answer)
        port = listener.sockets[0].getsockname()[1]
        try:
            head, rows = await m.ask("pairaddr", f"127.0.0.1:{port}")
            assert head[1:3] == ["ok", "refresh"] and rows[0][2:4] == ["Connecting…", "127.0.0.1"]
            if why is None:
                # Two lines announced, one sent and the link closed: unreadable, and nothing stored
                why = "The server's answer was unreadable"
            rows = (await pair_level(m, "Not paired"))[1]
            assert rows[0][3] == why
            # The field offers the address last tried when the run failed (not when refused)
            if rows[1][0] == "field":
                assert rows[1][5] == "127.0.0.1"
        finally:
            listener.close()
        assert not (soloify / "remote").exists()


async def test_away_verified_not_reached_and_cleared_by_a_new_pairing(tmp_path, managed, udp):
    """A fake WAN line on 127.0.0.2: verified once the relay reaches the bridge through it, not
    reached after 3 rounds with both lines dead, and gone after a pairing with another WAN host."""
    bridge = await PairingBridge(tmp_path / "data").start()
    wan = await bridge.server.start("127.0.0.2", 0)
    wan_port = wan.sockets[0].getsockname()[1]
    soloify = make_card(tmp_path / "card", relay_binary())
    m = await managed(soloify)
    relay = None
    try:
        endpoints = pairing.Endpoints("127.0.0.1", bridge.port, "127.0.0.2", wan_port)
        device = Device(bridge, udp, endpoints)
        await device.waiting(bridge)
        await m.ask("pair")
        code = (await pair_level(m, "Enter "))[1][0][2].removeprefix("Enter ")
        await until(lambda: any("wants to pair" in line for line in device.said))
        device.type(code)
        await pair_level(m, "Paired")
        assert len((soloify / "remote").read_text().splitlines()) == 2
        assert await menu_word(m, "away") == "Not yet verified"

        # Away from home: the LAN line is dead, the WAN line answers
        bridge.listener.close()
        lines = endpoints.lines(bridge.identity.token, bridge.identity.pin)
        env = {"SOLOIFY_Q2POD": str(m.base.parent / "r")}
        relay = await Relay(soloify, lines, "-q", env=env).start()
        await relay.expect(f"connected to 127.0.0.2:{wan_port}")
        record = (soloify / "away").read_text().split()
        assert record[0] == "verified" and record[2] == "127.0.0.2"
        assert abs(int(record[1]) - time.time()) < 60
        word = await menu_word(m, "away")
        assert word == "Verified " + time.strftime("%-d %b %Y", time.gmtime(int(record[1])))
        await relay.stop()

        # Both dead: 3 rounds, no LAN success between them
        wan.close()
        await bridge.stop()
        relay = await Relay(soloify, lines, "-q", env=env).start()
        count = Path(m.env["SOLOIFY_AWAY_COUNT"])
        await until(lambda: count.exists() and int(count.read_text()) >= 3, timeout=20)
        await relay.stop()
        assert (await menu_word(m, "away")).startswith("Not reached")

        # Paired again with another WAN host: the record and the count go
        bridge = await PairingBridge(tmp_path / "data").start()
        device = Device(bridge, udp, pairing.Endpoints("127.0.0.1", bridge.port, "127.0.0.3", 7443))
        await device.waiting(bridge)
        await m.ask("pairagain")
        code = (await pair_level(m, "Enter "))[1][0][2].removeprefix("Enter ")
        await until(lambda: any("wants to pair" in line for line in device.said))
        device.type(code)
        await pair_level(m, "Paired")
        assert not (soloify / "away").exists() and not count.exists()
        assert await menu_word(m, "away") == "Not yet verified"
    finally:
        if relay:
            await relay.stop()
        wan.close()
        await bridge.stop()


@pytest.mark.parametrize("ours_running", [True, False])
async def test_bridge_check_against_a_bridge_without_ping(
    tmp_path, short, monkeypatch, ours_running
):
    """An older bridge closes on ping in silence: the check says hello instead, but never while our
    relay runs, which the hello would take the bridge from."""
    hello_ok = wire.hello_ok
    monkeypatch.setattr(
        wire,
        "hello_ok",
        lambda line, token, word=b"hello": word == b"hello" and hello_ok(line, token),
    )
    soloify = make_card(tmp_path, relay_binary(), slot=relay_binary().read_bytes())
    bridge = await Bridge(tmp_path / "data").start()
    holder = await asyncio.create_subprocess_exec("sleep", "30")
    m = None
    try:
        (soloify / "remote").write_text(bridge.line() + "\n")
        if ours_running:
            (short / "slot").write_text(f"{holder.pid}\n")
        m = await Manager(soloify, short / "m").start()
        while (r := await m.menu())[0][2] == "refresh":
            await asyncio.sleep(0.2)
        assert r[1]["bridge"][3] == ("In use by Soloify" if ours_running else "Reachable (LAN)")
        assert len(bridge.sessions) == (0 if ours_running else 1)
    finally:
        if m:
            await m.stop()
        holder.kill()
        await holder.wait()
        await bridge.stop()
