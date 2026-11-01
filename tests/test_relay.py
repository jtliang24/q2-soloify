"""The relay's link: endpoints, TLS, hello, reconnects (relay/main.c, tls.c, remote.c)."""

import datetime
import socket
import tempfile
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from relay_rig import Bridge, Relay, until


@pytest.fixture
async def rig(tmp_path):
    made = []

    async def make(remote, *args, bridge=None, env=None):
        relay = await Relay(tmp_path, remote, *args, env=env).start()
        made.append(relay)
        return relay

    yield make
    for relay in made:
        await relay.stop()


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_pinned_link_gets_audio_and_acks(tmp_path, rig):
    bridge = await Bridge(tmp_path / "data").start()
    try:
        relay = await rig([bridge.line()])
        await relay.expect("connected to 127.0.0.1")
        await until(lambda: bridge.sessions and bridge.sessions[0].acked > 25)
        assert bridge.sessions[0].streaming
    finally:
        await bridge.stop()


async def test_wrong_pin_falls_to_the_next_endpoint(tmp_path, rig):
    bridge = await Bridge(tmp_path / "data").start()
    try:
        relay = await rig([bridge.line(pin="00" * 32), bridge.line(host="localhost")])
        await relay.expect("127.0.0.1: not the bridge's certificate")
        await relay.expect("connected to localhost")
    finally:
        await bridge.stop()


async def test_dead_endpoint_falls_to_the_next(tmp_path, rig):
    bridge = await Bridge(tmp_path / "data").start()
    try:
        relay = await rig([bridge.line(port=_closed_port()), bridge.line(host="localhost")])
        await relay.expect("can't connect")
        await relay.expect("connected to localhost")
    finally:
        await bridge.stop()


async def test_bad_token_is_refused(tmp_path, rig):
    bridge = await Bridge(tmp_path / "data").start()
    try:
        line = bridge.line().replace(bridge.identity.token, "0" * 32)
        relay = await rig([line])
        await relay.expect("refused the hello")
        assert not bridge.sessions
    finally:
        await bridge.stop()


async def test_reconnects_after_the_bridge_restarts(tmp_path, rig):
    bridge = await Bridge(tmp_path / "data").start()
    relay = await rig([bridge.line()])
    await relay.expect("connected to")
    port = bridge.port
    await bridge.stop()
    await relay.expect("the bridge closed the link")
    bridge = await Bridge(tmp_path / "data").start(port)
    try:
        await relay.expect("connected to", timeout=20)
        await until(lambda: bridge.sessions and bridge.sessions[0].acked > 5)
    finally:
        await bridge.stop()


async def test_no_remote_file_says_so(tmp_path, rig):
    relay = await rig([])
    await relay.expect("copy remote to .soloify")


async def test_unreadable_line_is_skipped(tmp_path, rig):
    bridge = await Bridge(tmp_path / "data").start()
    try:
        relay = await rig(["# a comment", "only-a-host", "", bridge.line() + " buffer=1500"])
        await relay.expect("line 2 unreadable")
        assert "buffer 1500 ms" in await relay.expect("connected to")
    finally:
        await bridge.stop()


async def _connected(tmp_path, rig, files, line_suffix="", *args):
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    bridge = await Bridge(tmp_path / "data").start()
    relay = await rig([bridge.line() + line_suffix], *args)
    return bridge, await relay.expect("connected to")


async def test_delay_setting_beats_the_endpoint_buffer(tmp_path, rig):
    bridge, line = await _connected(tmp_path, rig, {".delay": "2000\n"}, " buffer=1500")
    try:
        assert "buffer 2000 ms" in line and "quality" not in line
        await until(lambda: bridge.sessions and bridge.sessions[0].limit == 2000)
    finally:
        await bridge.stop()


async def test_b_option_beats_the_delay_setting(tmp_path, rig):
    bridge, line = await _connected(tmp_path, rig, {".delay": "2000"}, "", "-b", "3000")
    await bridge.stop()
    assert "buffer 3000 ms" in line


@pytest.mark.parametrize("text", ["", "fast", "100", "30000", "-5", "2000x"])
async def test_an_invalid_delay_setting_is_ignored(tmp_path, rig, text):
    bridge, line = await _connected(tmp_path, rig, {".delay": text}, " buffer=1500")
    await bridge.stop()
    assert "buffer 1500 ms" in line


async def test_quality_setting_reaches_the_bridge(tmp_path, rig):
    bridge, line = await _connected(tmp_path, rig, {".quality": "96\n"})
    try:
        assert "quality 96 kbit/s" in line
        await until(lambda: bridge.sessions and bridge.sessions[0].bitrate == 96)
    finally:
        await bridge.stop()


@pytest.mark.parametrize("text", ["", "64", "fast", "960"])
async def test_an_invalid_quality_setting_is_not_sent(tmp_path, rig, text):
    bridge, line = await _connected(tmp_path, rig, {".quality": text})
    try:
        assert "quality" not in line
        await until(lambda: bridge.sessions and bridge.sessions[0].streaming)
        assert bridge.sessions[0].bitrate is None
    finally:
        await bridge.stop()


def _ca_and_leaf(folder, name):
    """A CA, and a leaf for name signed by it, as the bridge's certificate and key."""
    now = datetime.datetime.now(datetime.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(1)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(2)
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    bridge = folder / "data" / "bridge"
    bridge.mkdir(parents=True)
    (bridge / "cert.pem").write_bytes(leaf.public_bytes(pem))
    (bridge / "key.pem").write_bytes(
        key.private_bytes(pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    )
    (folder / "ca.pem").write_bytes(ca.public_bytes(pem))


async def test_ca_mode_checks_chain_and_name(tmp_path, rig):
    _ca_and_leaf(tmp_path, "localhost")
    bridge = await Bridge(tmp_path / "data").start()
    try:
        token = bridge.identity.token
        env = {"SOLOIFY_CA": str(tmp_path / "ca.pem")}
        remote = [f"127.0.0.1 {bridge.port} {token}", f"localhost {bridge.port} {token}"]
        relay = await rig(remote, env=env)
        await relay.expect("127.0.0.1: TLS handshake failed")  # the name doesn't match
        await relay.expect("connected to localhost")
    finally:
        await bridge.stop()


async def test_ca_mode_refuses_another_ca(tmp_path, rig):
    _ca_and_leaf(tmp_path, "localhost")
    bridge = await Bridge(tmp_path / "data").start()
    try:
        own = tmp_path / "own.pem"
        own.write_bytes((tmp_path / "data/bridge/cert.pem").read_bytes())  # not a CA
        relay = await rig(
            [f"localhost {bridge.port} {bridge.identity.token}"], env={"SOLOIFY_CA": str(own)}
        )
        await relay.expect("TLS handshake failed")
        assert not bridge.sessions
    finally:
        await bridge.stop()


async def test_q2pod_starts_without_claiming_playback(tmp_path, rig):
    """The launcher owns the pid file; the relay binds liveness and starts at none."""
    with tempfile.TemporaryDirectory(prefix="sf-") as folder:
        base = Path(folder) / "q"
        base.write_text("12345\n")
        Path(f"{base}.tmp").mkdir()  # librespot's leftover directory
        relay = await rig([], "-q", env={"SOLOIFY_Q2POD": str(base)})
        await relay.expect("state and controls ready")
        assert base.read_text() == "12345\n"
        assert Path(f"{base}.state").read_text().startswith("state=none\n")
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.connect(f"{base}.sock")  # firmware's spot_alive sends nothing
