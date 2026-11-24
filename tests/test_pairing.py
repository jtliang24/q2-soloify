"""Pairing by code (docs/wire.md, Pairing): a fake relay on the TLS port, `soloify pair-device` on
the admin socket, and the UDP discovery responder."""

import asyncio
import hashlib
import json
import queue
import socket
import ssl
import stat

import pytest

from soloify import cli, pairing, server

LAN = "192.0.2.10"


def test_code_vector_and_display():
    # The vector in docs/wire.md
    fingerprint, nonce = bytes(range(32)), bytes(range(16))
    digest = hashlib.sha256(b"soloify-pair-1" + fingerprint + nonce).digest()
    assert digest[:4].hex() == "d46f40dd"
    assert pairing.code(fingerprint, nonce) == 0xD46F40DD % 1_000_000 == 60893
    assert pairing.shown(pairing.code(fingerprint, nonce)) == "060 893"
    assert pairing.shown(42) == "000 042"
    assert pairing.shown(999_999) == "999 999"
    assert pairing.typed(" 123 456 ") == "123456" == pairing.typed("123-456")
    assert pairing.typed("12345") is None and pairing.typed("12a456") is None


def test_pair_line():
    nonce = "00112233445566778899aabbccddeeff"
    assert pairing.parse_pair(b"pair 1 %s Q2\n" % nonce.encode()) == (bytes.fromhex(nonce), "Q2")
    for bad in (
        b"pair 2 %s Q2\n" % nonce.encode(),
        b"pair 1 %s\n" % nonce.encode(),
        b"pair 1 0011 Q2\n",
        b"pair 1 %s Q 2\n" % nonce.encode(),
        b"pair 1 %s %s\n" % (nonce.encode(), b"x" * 33),
    ):
        assert pairing.parse_pair(bad) is None


@pytest.mark.parametrize(
    "host,ok",
    [
        ("192.168.1.5", True),
        ("10.0.0.2", True),
        ("172.16.4.4", True),
        ("127.0.0.1", True),
        ("fd00::1", True),
        ("fe80::1%eth0", True),
        ("::ffff:192.168.1.5", True),
        ("203.0.113.5", False),
        ("8.8.8.8", False),
        ("::ffff:8.8.8.8", False),
        ("100.64.0.1", False),
        ("2001:4860::1", False),
        ("not an address", False),
    ],
)
def test_private_sources(host, ok):
    assert pairing.private(host) is ok


def _client():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


class FakeRelay:
    """The relay's side: TLS without a pin, F from the certificate it saw, `pair 1`."""

    def __init__(self, port: int, nonce: bytes = bytes(range(16)), name: str = "Q2"):
        self.port, self.nonce, self.name = port, nonce, name

    async def open(self):
        self.reader, self.writer = await asyncio.open_connection(
            "127.0.0.1", self.port, ssl=_client()
        )
        der = self.writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        self.fingerprint = hashlib.sha256(der).digest()
        self.code = pairing.code(self.fingerprint, self.nonce)
        self.writer.write(b"pair 1 %s %s\n" % (self.nonce.hex().encode(), self.name.encode()))
        await self.writer.drain()
        return self

    async def answer(self) -> list[bytes]:
        lines = []
        while line := await asyncio.wait_for(self.reader.readline(), 5):
            lines.append(line)
        self.writer.close()
        return lines


class Bridge:
    def __init__(self, tmp_path, **options):
        self.data = tmp_path
        self.identity = server.Identity.load(tmp_path)
        self.pairing = pairing.Pairing(self.identity, **options)

    async def __aenter__(self):
        async def session(reader, writer):
            await reader.read()

        self.admin = await self.pairing.serve_admin(self.data / pairing.ADMIN_SOCKET)
        self.listener = await server.Server(self.identity, session, self.pairing).start(
            "127.0.0.1", 0
        )
        self.port = self.listener.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.listener.close()
        self.admin.close()

    def relay(self, **kw) -> FakeRelay:
        return FakeRelay(self.port, **kw)


class Device:
    """`soloify pair-device` with typed lines from a queue and its output in a list."""

    def __init__(self, bridge: Bridge, endpoints=None, **kw):
        self.typed: queue.Queue = queue.Queue()
        self.said: list[str] = []
        self.task = asyncio.ensure_future(
            pairing.pair_device(
                bridge.data / pairing.ADMIN_SOCKET,
                endpoints or pairing.Endpoints(LAN, bridge.port),
                ask=lambda prompt: self.typed.get(),
                out=self.said.append,
                discovery=False,
                **kw,
            )
        )

    def type(self, text: str) -> None:
        self.typed.put(text)

    async def saw(self, text: str, times: int = 1) -> None:
        for _ in range(500):
            if sum(text in line for line in self.said) >= times:
                return
            if self.task.done():
                break
            await asyncio.sleep(0.01)
        raise AssertionError(f"{text!r} not in {self.said}")

    async def result(self) -> int:
        return await asyncio.wait_for(self.task, 5)


async def _waiting(bridge: Bridge) -> None:
    for _ in range(500):
        if bridge.pairing.admin:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("pair-device never waited")


async def test_right_code_delivers_lan_line_home_only(tmp_path):
    async with Bridge(tmp_path) as bridge:
        device = Device(bridge)
        await _waiting(bridge)
        relay = await bridge.relay().open()
        await device.saw("Q2 wants to pair (from 127.0.0.1)")
        assert relay.fingerprint == bridge.identity.fingerprint
        device.type(pairing.shown(relay.code))
        identity = bridge.identity
        assert await relay.answer() == [
            b"remote 1 1\n",
            f"{LAN} {bridge.port} {identity.token} sha256={identity.pin}\n".encode(),
        ]
        assert await device.result() == 0
        assert device.said[-1] == pairing.HOME_ONLY
        # One pairing per wait: the next request needs another pair-device
        later = await bridge.relay().open()
        assert await later.answer() == [b"denied idle\n"]


async def test_wan_line_follows_lan_with_same_token_and_pin_ddns(tmp_path):
    async with Bridge(tmp_path) as bridge:
        endpoints = pairing.Endpoints(LAN, 7443, "music.example.com", 8443)
        device = Device(bridge, endpoints)
        await _waiting(bridge)
        relay = await bridge.relay().open()
        await device.saw("wants to pair")
        device.type(f"{relay.code:06d}")
        tail = f"{bridge.identity.token} sha256={bridge.identity.pin}\n"
        assert await relay.answer() == [
            b"remote 1 2\n",
            f"{LAN} 7443 {tail}".encode(),
            f"music.example.com 8443 {tail}".encode(),
        ]
        assert await device.result() == 0
        assert not any("Warning" in line or "home only" in line for line in device.said)


async def test_raw_ip_wan_warns_but_pairs(tmp_path):
    async with Bridge(tmp_path) as bridge:
        device = Device(bridge, pairing.Endpoints(LAN, 7443, "198.51.100.7", 7443))
        await _waiting(bridge)
        relay = await bridge.relay().open()
        await device.saw("wants to pair")
        device.type(f"{relay.code:06d}")
        answer = await relay.answer()
        assert answer[0] == b"remote 1 2\n" and answer[2].startswith(b"198.51.100.7 7443 ")
        assert await device.result() == 0
        assert device.said[-1] == pairing.ip_warning("198.51.100.7")
        assert "DDNS" in device.said[-1]


async def test_wrong_code_ends_that_request_only(tmp_path):
    async with Bridge(tmp_path) as bridge:
        device = Device(bridge)
        await _waiting(bridge)
        relay = await bridge.relay().open()
        await device.saw("wants to pair")
        device.type(f"{(relay.code + 1) % 1_000_000:06d}")
        assert await relay.answer() == [b"denied code\n"]
        await device.saw("Wrong code")
        assert not device.task.done()
        retry = await bridge.relay(nonce=bytes(16)).open()
        assert retry.code != relay.code
        await device.saw("wants to pair", times=2)
        device.type(f"{retry.code:06d}")
        assert (await retry.answer())[0] == b"remote 1 1\n"
        assert await device.result() == 0


async def test_request_times_out(tmp_path):
    async with Bridge(tmp_path, timeout=0.2) as bridge:
        device = Device(bridge)
        await _waiting(bridge)
        relay = await bridge.relay().open()
        assert await relay.answer() == [b"denied timeout\n"]
        await device.saw("timed out")
        assert not device.task.done()
        device.task.cancel()


async def test_new_request_replaces_the_pending_one(tmp_path):
    async with Bridge(tmp_path) as bridge:
        device = Device(bridge)
        await _waiting(bridge)
        first = await bridge.relay().open()
        await device.saw("wants to pair")
        second = await bridge.relay(nonce=b"\xff" * 16, name="M1").open()
        assert await first.answer() == [b"denied replaced\n"]
        await device.saw("M1 wants to pair")
        # The first request's code no longer pairs anything
        device.type(f"{second.code:06d}")
        assert (await second.answer())[0] == b"remote 1 1\n"
        assert await device.result() == 0


async def test_refused_without_a_waiting_pair_device(tmp_path):
    async with Bridge(tmp_path) as bridge:
        relay = await bridge.relay().open()
        assert await relay.answer() == [b"denied idle\n"]


async def test_relay_leaving_ends_its_request(tmp_path):
    async with Bridge(tmp_path) as bridge:
        device = Device(bridge)
        await _waiting(bridge)
        relay = await bridge.relay().open()
        await device.saw("wants to pair")
        relay.writer.close()
        await device.saw("stopped asking")
        assert bridge.pairing.pending is None
        device.task.cancel()


class _Writer:
    def __init__(self):
        self.data = b""

    def write(self, data):
        self.data += data

    async def drain(self):
        pass


async def _ask(bridge_pairing, host, line=None):
    reader = asyncio.StreamReader()
    writer = _Writer()
    line = line or b"pair 1 %s Q2\n" % bytes(16).hex().encode()
    task = asyncio.ensure_future(bridge_pairing.relay(line, host, reader, writer))
    await asyncio.sleep(0.01)
    if not task.done():
        reader.feed_eof()  # the relay hangs up: its held request ends
    await asyncio.wait_for(task, 5)
    return writer.data


async def test_wan_source_refused_even_with_pair_device_waiting(tmp_path):
    identity = server.Identity.load(tmp_path)
    pair = pairing.Pairing(identity)
    pair.admin = pairing._Admin(_Writer(), pairing.Endpoints(LAN, 7443))
    assert await _ask(pair, "203.0.113.5") == b"denied address\n"
    assert await _ask(pair, "::ffff:203.0.113.5") == b"denied address\n"
    assert pair.pending is None and not pair.recent
    assert await _ask(pair, "192.168.1.5") == b"denied gone\n"
    assert await _ask(pair, "192.168.1.5", b"pair 1 zz Q2\n") == b"denied malformed\n"


async def test_rate_limit_five_a_minute(tmp_path):
    now = [1000.0]
    identity = server.Identity.load(tmp_path)
    pair = pairing.Pairing(identity, clock=lambda: now[0])
    answers = [await _ask(pair, "192.168.1.5") for _ in range(6)]
    assert answers == [b"denied idle\n"] * 5 + [b"denied rate\n"]
    now[0] += 59
    assert await _ask(pair, "192.168.1.5") == b"denied rate\n"
    now[0] += 1.5
    assert await _ask(pair, "192.168.1.5") == b"denied idle\n"


async def test_rate_limit_over_tls(tmp_path):
    async with Bridge(tmp_path, rate=2) as bridge:
        answers = [await (await bridge.relay().open()).answer() for _ in range(3)]
        assert answers == [[b"denied idle\n"], [b"denied idle\n"], [b"denied rate\n"]]


async def test_hello_still_works_beside_pairing(tmp_path):
    async with Bridge(tmp_path) as bridge:
        reader, writer = await asyncio.open_connection("127.0.0.1", bridge.port, ssl=_client())
        writer.write(b"hello 2 %s\n" % bridge.identity.token.encode())
        assert await asyncio.wait_for(reader.readline(), 5) == b"ok 2\n"
        writer.close()


async def test_admin_socket_is_private_and_single(tmp_path):
    async with Bridge(tmp_path) as bridge:
        path = tmp_path / pairing.ADMIN_SOCKET
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        first = Device(bridge)
        await _waiting(bridge)
        second = Device(bridge)
        assert await second.result() == 2
        assert "already waiting" in second.said[-1]
        first.task.cancel()


async def test_pair_device_without_bridge(tmp_path):
    said = []
    endpoints = pairing.Endpoints(LAN, 7443)
    assert await pairing.pair_device(tmp_path / "pair.sock", endpoints, out=said.append) == 2
    assert "isn't running" in said[0]


async def test_pair_device_times_out(tmp_path):
    async with Bridge(tmp_path) as bridge:
        device = Device(bridge, timeout=0.2)
        assert await device.result() == 1
        assert "No Q2 paired" in device.said[-1]
        assert bridge.pairing.admin is None or not bridge.pairing.pending


async def test_docker_path_cli_without_discovery(tmp_path, monkeypatch):
    """`docker compose ... exec -T -u soloify soloify soloify pair-device`: endpoints from the
    options once (setup passes them), remembered for a later bare run; no UDP listener."""

    def no_discovery(*a, **kw):
        raise AssertionError("must not listen for broadcasts")

    monkeypatch.setattr(pairing, "Discovery", no_discovery)
    typed: queue.Queue = queue.Queue()
    monkeypatch.setattr("builtins.input", lambda prompt: typed.get())
    async with Bridge(tmp_path) as bridge:
        args = ["--data", str(tmp_path), "pair-device", "--no-discovery", "--lan", LAN]
        args += ["--port", "7443", "--wan", "music.example.com", "--wan-port", "8443"]
        run = asyncio.ensure_future(asyncio.to_thread(cli.main, args))
        await _waiting(bridge)
        relay = await bridge.relay().open()
        for _ in range(500):
            if bridge.pairing.pending:
                break
            await asyncio.sleep(0.01)
        typed.put(pairing.shown(relay.code))
        answer = await relay.answer()
        assert [line.split()[:2] for line in answer[1:]] == [
            [LAN.encode(), b"7443"],
            [b"music.example.com", b"8443"],
        ]
        assert await asyncio.wait_for(run, 5) == 0
        saved = tmp_path / "endpoints.json"
        assert stat.S_IMODE(saved.stat().st_mode) == 0o600
        assert json.loads(saved.read_text())["wan"] == "music.example.com"

        # A bare run later (the documented exec, without options) reuses them; in a container it
        # doesn't listen for broadcasts either
        monkeypatch.setattr(pairing, "in_container", lambda: True)
        # (the first run's input thread still waits on the old queue, as a process would not)
        typed = queue.Queue()
        monkeypatch.setattr("builtins.input", lambda prompt: typed.get())
        run = asyncio.ensure_future(
            asyncio.to_thread(cli.main, ["--data", str(tmp_path), "pair-device"])
        )
        await _waiting(bridge)
        relay = await bridge.relay(nonce=bytes(16)).open()
        for _ in range(500):
            if bridge.pairing.pending:
                break
            await asyncio.sleep(0.01)
        typed.put(f"{relay.code:06d}")
        assert (await relay.answer())[2].startswith(b"music.example.com 8443 ")
        assert await asyncio.wait_for(run, 5) == 0


def test_endpoints_from_native_setup_state(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (tmp_path / "setup-state.json").write_text(
        json.dumps({"lan": LAN, "port": 7444, "wan": "music.example.com", "wan_port": 8443})
    )
    found = pairing.load_endpoints(data, None, None, None, None)
    assert found == pairing.Endpoints(LAN, 7444, "music.example.com", 8443)
    (tmp_path / "setup-state.json").write_text(json.dumps({"lan": LAN, "port": 7443}))
    assert pairing.load_endpoints(data, None, None, None, None).wan is None


def test_bad_endpoints_are_refused(tmp_path, capsys):
    assert cli.main(["--data", str(tmp_path), "pair-device", "--lan", "a b"]) == 2
    assert cli.main(["--data", str(tmp_path), "pair-device", "--wan-port", "8443"]) == 2


def test_discovery_reply():
    nonce = b"00112233445566778899aabbccddeeff"
    assert pairing.discovery_reply(b"SOLOIFY-PAIR 1 " + nonce, LAN, 7443) == (
        b"SOLOIFY-HERE 1 " + nonce + b" 192.0.2.10 7443"
    )
    assert pairing.discovery_reply(b"SOLOIFY-PAIR 1 " + nonce + b"\n", LAN, 7443)
    for bad in (b"SOLOIFY-PAIR 2 " + nonce, b"SOLOIFY-PAIR 1 " + nonce.upper(), b"hello", b""):
        assert pairing.discovery_reply(bad, LAN, 7443) is None


def test_discovery_answers_by_unicast(tmp_path):
    with pairing.Discovery(LAN, 7443, bind="127.0.0.1", udp_port=0) as listener:
        assert listener.start()
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as q2:
            q2.settimeout(2)
            q2.sendto(b"SOLOIFY-PAIR 1 " + b"ab" * 16, listener.bound)
            reply, source = q2.recvfrom(256)
            assert reply == b"SOLOIFY-HERE 1 " + b"ab" * 16 + b" 192.0.2.10 7443"
            assert source == listener.bound
            q2.settimeout(0.3)
            q2.sendto(b"SOLOIFY-PAIR 1 nope", listener.bound)
            with pytest.raises(TimeoutError):
                q2.recvfrom(256)
        # The port is taken while it runs: a second listener reports it rather than failing
        assert not pairing.Discovery(
            LAN, 7443, bind="127.0.0.1", udp_port=listener.bound[1]
        ).start()
