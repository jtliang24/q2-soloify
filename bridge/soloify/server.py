"""The bridge's TLS listener: its certificate and token, the relay's hello, one relay at a time."""

import asyncio
import datetime
import hashlib
import logging
import os
import secrets
import socket
import ssl
from collections.abc import Awaitable, Callable
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from . import wire

log = logging.getLogger("soloify")

HELLO_TIMEOUT = 10
# A session gets the relay's reader and writer after its hello
Session = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


def _private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)


class Identity:
    """data/bridge/: a self-signed ECDSA P-256 certificate, which the relay pins by its SHA-256, its
    key and the relay's token. Made on first use; the certificate's dates don't matter to a pin."""

    def __init__(self, folder: Path):
        self.cert = folder / "cert.pem"
        self.key = folder / "key.pem"
        self.token = (folder / "token").read_text().strip()
        der = x509.load_pem_x509_certificate(self.cert.read_bytes()).public_bytes(
            serialization.Encoding.DER
        )
        self.pin = hashlib.sha256(der).hexdigest()

    @classmethod
    def load(cls, data: Path) -> "Identity":
        folder = data / "bridge"
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not (folder / "cert.pem").exists():
            key = ec.generate_private_key(ec.SECP256R1())
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "soloify")])
            now = datetime.datetime.now(datetime.UTC)
            cert = (
                x509.CertificateBuilder()
                .subject_name(name)
                .issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=365 * 30))
                .sign(key, hashes.SHA256())
            )
            _private(
                folder / "key.pem",
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                ),
            )
            (folder / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        if not (folder / "token").exists():
            _private(folder / "token", secrets.token_hex(16).encode() + b"\n")
        return cls(folder)

    def ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(self.cert, self.key)
        return ctx


class Server:
    """Accepts relays, checks their hello and runs session on each. A newly authenticated relay
    replaces the current one, whose link may be dead without either side knowing yet (a network
    switch)."""

    def __init__(self, identity: Identity, session: Session):
        self.identity = identity
        self.session = session
        self.current: asyncio.Task | None = None

    async def start(self, host: str | None, port: int) -> asyncio.Server:
        return await asyncio.start_server(
            self._connected, host, port, ssl=self.identity.ssl_context()
        )

    async def _connected(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        try:
            line = await asyncio.wait_for(reader.readline(), HELLO_TIMEOUT)
            if len(line) > wire.HELLO_MAX or not wire.hello_ok(line, self.identity.token):
                log.warning("relay %s: bad hello", peer)
                return
            writer.write(wire.OK)
            await writer.drain()
            old, self.current = self.current, asyncio.current_task()
            if old:
                # The old session pauses Soloist as it ends; let it finish before this one resumes
                log.info("relay %s replaces the current one", peer)
                old.cancel()
                await asyncio.wait([old])
            log.info("relay %s connected", peer)
            await self.session(reader, writer)
        except (TimeoutError, OSError, ssl.SSLError, asyncio.IncompleteReadError) as e:
            log.info("relay %s: %s", peer, e.__class__.__name__)
        except wire.WireError as e:
            log.warning("relay %s: %s", peer, e)
        except asyncio.CancelledError:
            pass
        finally:
            if self.current is asyncio.current_task():
                self.current = None
            writer.close()
            log.info("relay %s gone", peer)


def remote_lines(identity: Identity, hosts: list[str], port: int) -> list[str]:
    """The card's .soloify/remote: one endpoint a line, tried in order."""
    return [f"{host} {port} {identity.token} sha256={identity.pin}" for host in hosts]


def lan_address() -> str:
    """This machine's address on its default route (no packet is sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]
