"""Pairing by code on the relay (pair.c) against the real bridge's side (pairing.py): the Soloify
app's Pair level, a relay with no remote showing the code as its page's error, the Away record and
its Status line. Discovery stays on loopback (SOLOIFY_PAIR_BROADCAST)."""

import asyncio
import queue
import socket
import tempfile
import time
from pathlib import Path

import pytest

from relay_rig import Bridge, Relay, until
from soloify import pairing
from test_browse import decode, request
from test_contract import browse
from test_manage import Manager


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
