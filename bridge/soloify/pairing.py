"""Pairing a device with a code (docs/wire.md, Pairing): the bridge's side of `pair 1`, the admin
socket that `soloify pair-device` drives, and the UDP discovery responder that runs on the host.

The relay derives a code from the certificate it saw and the user types it here, so a match proves
the relay talks to this bridge; only then does the held TLS connection carry the `remote` lines.
"""

import asyncio
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("soloify")

VERSION = 1
LABEL = b"soloify-pair-1"
UDP_PORT = 7444
ADMIN_SOCKET = "pair.sock"
# How long a held pair request waits for the code
REQUEST_TIMEOUT = 180.0
# pair requests accepted in RATE_WINDOW seconds
RATE = 5
RATE_WINDOW = 60.0
ADMIN_MAX = 4096
DATAGRAM_MAX = 128

_PAIR = re.compile(rb"pair 1 ([0-9a-fA-F]{32}) ([A-Za-z0-9._-]{1,32})\n?")
_DISCOVER = re.compile(rb"SOLOIFY-PAIR 1 ([0-9a-f]{32})\n?")
_HOST = re.compile(r"[A-Za-z0-9.:-]{1,253}")


def code(fingerprint: bytes, nonce: bytes) -> int:
    """The first 4 bytes, as a big-endian u32, of SHA-256("soloify-pair-1" || F || nonce), modulo
    1 000 000: F the 32 raw bytes of the certificate's DER SHA-256, nonce the 16 raw bytes the relay
    chose. Six digits hold just under 20 bits; 32 bits reduced keep the bias below 2^-12."""
    if len(fingerprint) != 32 or len(nonce) != 16:
        raise ValueError("fingerprint is 32 bytes, nonce 16")
    digest = hashlib.sha256(LABEL + fingerprint + nonce).digest()
    return int.from_bytes(digest[:4], "big") % 1_000_000


def shown(value: int) -> str:
    """A code as the Q2 shows it: six digits, zero-padded, in two groups (`012 345`)."""
    text = f"{value:06d}"
    return f"{text[:3]} {text[3:]}"


def typed(text: str) -> str | None:
    """Six digits from what the user typed (spaces and hyphens ignored), or None."""
    digits = re.sub(r"[\s-]", "", text)
    return digits if re.fullmatch(r"[0-9]{6}", digits) else None


def parse_pair(line: bytes) -> tuple[bytes, str] | None:
    """(nonce, name) from `pair 1 NONCE NAME\\n`, or None."""
    match = _PAIR.fullmatch(line)
    return (bytes.fromhex(match[1].decode()), match[2].decode()) if match else None


_PRIVATE = [
    ipaddress.ip_network(net)
    for net in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",
        "127.0.0.0/8",
        "fc00::/7",
        "fe80::/10",
        "::1/128",
    )
]


def private(host: str) -> bool:
    """Whether a peer address is on a private network: RFC 1918, link-local, loopback or ULA.
    Not 100.64.0.0/10 (carrier-grade NAT is outside the home), nor the documentation ranges that
    Python's is_private counts."""
    try:
        address = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return any(address in net for net in _PRIVATE if net.version == address.version)


def raw_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class Endpoints:
    """What a paired device gets: the LAN endpoint, then the WAN one when setup's away step set
    it up."""

    lan: str
    port: int
    wan: str | None = None
    wan_port: int | None = None

    def __post_init__(self):
        for host in filter(None, (self.lan, self.wan)):
            if not _HOST.fullmatch(host):
                raise ValueError(f"bad host {host!r}")
        for port in filter(None, (self.port, self.wan_port)):
            if not (isinstance(port, int) and 1 <= port <= 65535):
                raise ValueError(f"bad port {port!r}")

    def lines(self, token: str, pin: str) -> list[str]:
        """The card's `remote` lines, as `soloify remote` prints them."""
        hosts = [(self.lan, self.port)]
        if self.wan:
            hosts.append((self.wan, self.wan_port or self.port))
        return [f"{host} {port} {token} sha256={pin}" for host, port in hosts]

    def json(self) -> dict:
        return {"lan": self.lan, "port": self.port, "wan": self.wan, "wan_port": self.wan_port}

    @classmethod
    def from_json(cls, value) -> "Endpoints":
        if not isinstance(value, dict) or not isinstance(value.get("lan"), str):
            raise ValueError("endpoints need a LAN host")
        wan = value.get("wan") or None
        if wan is not None and not isinstance(wan, str):
            raise ValueError("bad WAN host")
        return cls(value["lan"], value.get("port"), wan, value.get("wan_port") if wan else None)


@dataclass
class _Request:
    id: int
    name: str
    peer: str
    code: int
    done: asyncio.Future = field(default_factory=lambda: asyncio.get_running_loop().create_future())

    def finish(self, answer: bytes) -> None:
        if not self.done.done():
            self.done.set_result(answer)


class _Admin:
    """The waiting `soloify pair-device`."""

    def __init__(self, writer: asyncio.StreamWriter, endpoints: Endpoints):
        self.writer, self.endpoints = writer, endpoints

    def send(self, **event) -> None:
        with contextlib.suppress(OSError, RuntimeError):
            self.writer.write(json.dumps(event).encode() + b"\n")


class Pairing:
    """One pending request at a time, held until the waiting `soloify pair-device` sends the code,
    the time runs out, or a newer request replaces it."""

    def __init__(
        self,
        identity,
        *,
        timeout: float = REQUEST_TIMEOUT,
        rate: int = RATE,
        window: float = RATE_WINDOW,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.identity = identity
        self.timeout, self.rate, self.window, self.clock = timeout, rate, window, clock
        self.admin: _Admin | None = None
        self.pending: _Request | None = None
        self.recent: deque[float] = deque()
        self.ids = 0

    def _limited(self) -> bool:
        now = self.clock()
        while self.recent and now - self.recent[0] >= self.window:
            self.recent.popleft()
        if len(self.recent) >= self.rate:
            return True
        self.recent.append(now)
        return False

    async def relay(
        self, line: bytes, host: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """A relay's `pair` line: answer `remote 1 N` and N lines, or `denied REASON`."""
        answer = await self._decide(line, host, reader)
        writer.write(answer)
        await writer.drain()

    async def _decide(self, line: bytes, host: str, reader: asyncio.StreamReader) -> bytes:
        parsed = parse_pair(line)
        if not parsed:
            log.warning("pair from %s: malformed", host)
            return b"denied malformed\n"
        if not private(host):
            log.warning("pair from %s refused: not a private address", host)
            return b"denied address\n"
        if self._limited():
            log.warning("pair from %s refused: rate limit", host)
            return b"denied rate\n"
        if not self.admin:
            log.info("pair from %s refused: no soloify pair-device waiting", host)
            return b"denied idle\n"
        nonce, name = parsed
        self.ids += 1
        request = _Request(self.ids, name, host, code(self.identity.fingerprint, nonce))
        if self.pending:
            self.pending.finish(b"denied replaced\n")
            self.admin.send(event="ended", id=self.pending.id, reason="replaced")
        self.pending = request
        self.admin.send(event="request", id=request.id, name=name, peer=host)
        log.info("pair request %d from %s (%s)", request.id, host, name)
        # Nothing may follow `pair`: bytes or EOF from the relay end the request
        gone = asyncio.ensure_future(reader.read(1))
        try:
            await asyncio.wait(
                {request.done, gone}, timeout=self.timeout, return_when=asyncio.FIRST_COMPLETED
            )
            if not request.done.done():
                reason = "gone" if gone.done() else "timeout"
                request.finish(b"denied %s\n" % reason.encode())
                if self.admin:
                    self.admin.send(event="ended", id=request.id, reason=reason)
            return request.done.result()
        finally:
            gone.cancel()
            if self.pending is request:
                self.pending = None

    def _code(self, admin: _Admin, message: dict) -> None:
        request = self.pending
        if not request or message.get("id") != request.id:
            admin.send(event="stale", id=message.get("id"))
            return
        expected = f"{request.code:06d}".encode()
        given = str(message.get("code", "")).encode()
        if hmac.compare_digest(given, expected):
            lines = admin.endpoints.lines(self.identity.token, self.identity.pin)
            body = "".join(line + "\n" for line in lines)
            request.finish(b"remote 1 %d\n" % len(lines) + body.encode())
            admin.send(event="paired", id=request.id, name=request.name, **admin.endpoints.json())
            log.info("pair request %d: paired", request.id)
            # One pairing per wait: another needs another `soloify pair-device`
            self.admin = None
        else:
            request.finish(b"denied code\n")
            admin.send(event="ended", id=request.id, reason="code")
            log.warning("pair request %d: wrong code", request.id)

    async def _admin_connected(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        admin = None
        try:
            line = await reader.readline()
            message = json.loads(line) if line else {}
            if message.get("op") != "wait":
                return
            if self.admin:
                writer.write(b'{"event": "busy"}\n')
                return
            admin = self.admin = _Admin(writer, Endpoints.from_json(message.get("endpoints")))
            admin.send(event="waiting", pin=self.identity.pin)
            while line := await reader.readline():
                message = json.loads(line)
                if message.get("op") == "code" and self.admin is admin:
                    self._code(admin, message)
        except (ValueError, OSError, asyncio.LimitOverrunError) as e:
            log.warning("pair-device: %s", e.__class__.__name__)
        finally:
            if admin and self.admin is admin:
                self.admin = None
                if self.pending:
                    self.pending.finish(b"denied cancelled\n")
            with contextlib.suppress(OSError):
                writer.close()

    async def serve_admin(self, path: Path) -> asyncio.Server:
        """The admin socket: a unix socket only this user can open (mode 0600)."""
        path.unlink(missing_ok=True)
        old = os.umask(0o177)
        try:
            listener = await asyncio.start_unix_server(self._admin_connected, path, limit=ADMIN_MAX)
        finally:
            os.umask(old)
        os.chmod(path, 0o600)
        return listener


# Host side: `soloify pair-device` and the setup wizard


def discovery_reply(data: bytes, host: str, port: int) -> bytes | None:
    """The answer to a `SOLOIFY-PAIR 1 NONCE` datagram, or None to stay silent."""
    match = _DISCOVER.fullmatch(data) if len(data) <= DATAGRAM_MAX else None
    return b"SOLOIFY-HERE 1 %s %s %d" % (match[1], host.encode(), port) if match else None


class Discovery:
    """Answers the Q2's broadcasts on UDP 7444 with this bridge's LAN endpoint, from a thread, so
    the wizard (which waits on a subprocess) and `pair-device` (asyncio) can both run it."""

    def __init__(self, host: str, port: int, *, bind: str = "0.0.0.0", udp_port: int = UDP_PORT):
        self.host, self.port, self.address = host, port, (bind, udp_port)
        self.sock: socket.socket | None = None
        self.stopped = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> bool:
        """False when the UDP port is taken (another pair-device, or the wizard's own)."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.bind(self.address)
        except OSError:
            sock.close()
            return False
        sock.settimeout(0.2)
        self.sock = sock
        self.thread = threading.Thread(target=self._run, name="soloify-discovery", daemon=True)
        self.thread.start()
        return True

    @property
    def bound(self) -> tuple[str, int]:
        return self.sock.getsockname()

    def _run(self) -> None:
        while not self.stopped.is_set():
            try:
                data, source = self.sock.recvfrom(DATAGRAM_MAX + 1)
            except TimeoutError:
                continue
            except OSError:
                return
            reply = discovery_reply(data, self.host, self.port)
            if reply and private(source[0]):
                with contextlib.suppress(OSError):
                    self.sock.sendto(reply, source)

    def stop(self) -> None:
        self.stopped.set()
        if self.thread:
            self.thread.join(1)
        if self.sock:
            self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.stop()


def in_container() -> bool:
    """Broadcasts don't reach a container's own network, so it doesn't listen for them."""
    return Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


HOME_ONLY = "Paired for home only. Run setup's away step, then pair again to listen away."


def ip_warning(host: str) -> str:
    return (
        f"Warning: the away address {host} is an IP address, which changes whenever your ISP "
        "changes it; the Q2 then can't reach the bridge away from home. Use a DDNS name in "
        "setup's away step, then pair again."
    )


def _say(text: str) -> None:
    # Flushed: under `docker compose exec -T` stdout is a pipe, and the prompt waits in a thread
    print(text, flush=True)


def _lines(ask: Callable[[str], str], prompt: str, loop, queue: asyncio.Queue) -> None:
    """Typed lines into the loop, from a daemon thread: input() can't be cancelled, and a daemon
    doesn't hold the process when pairing ends while it waits."""
    while True:
        try:
            text = ask(prompt)
        except (EOFError, OSError, RuntimeError):
            text = None
        try:
            loop.call_soon_threadsafe(queue.put_nowait, text)
        except RuntimeError:  # the loop has closed
            return
        if text is None:
            return


async def pair_device(
    socket_path: Path,
    endpoints: Endpoints,
    *,
    ask: Callable[[str], str] | None = None,
    out: Callable[[str], None] = _say,
    discovery: bool = True,
    udp_port: int = UDP_PORT,
    timeout: float | None = 600,
) -> int:
    """`soloify pair-device`: wait for a Q2's request, take its code, report what it got."""
    try:
        reader, writer = await asyncio.open_unix_connection(socket_path, limit=ADMIN_MAX)
    except OSError:
        out(f"The bridge isn't running here (no {socket_path}). Start it, then run this again.")
        return 2
    listener = Discovery(endpoints.lan, endpoints.port, udp_port=udp_port) if discovery else None
    try:
        writer.write(json.dumps({"op": "wait", "endpoints": endpoints.json()}).encode() + b"\n")
        await writer.drain()
        first = json.loads(await reader.readline() or b"{}")
        if first.get("event") != "waiting":
            out(
                "Another soloify pair-device is already waiting."
                if first.get("event") == "busy"
                else "The bridge refused pairing."
            )
            return 2
        if listener and not listener.start():
            out(f"UDP {udp_port} is in use, so the Q2 won't find this server by itself.")
            listener = None
        out("Waiting for a Q2: on it, open Library → Apps → Soloify → Pair with server.")
        if not listener:
            out(f"If the Q2 asks for the server's address, enter {endpoints.lan}.")
        typed_lines: asyncio.Queue = asyncio.Queue()
        prompt = "Code shown on the Q2: "
        threading.Thread(
            target=_lines,
            args=(
                ask or (lambda text: input(text)),
                prompt,
                asyncio.get_running_loop(),
                typed_lines,
            ),
            daemon=True,
        ).start()
        return await asyncio.wait_for(_converse(reader, writer, typed_lines, out), timeout)
    except TimeoutError:
        out("No Q2 paired in time. Run this again to pair.")
        return 1
    except (OSError, ValueError, asyncio.IncompleteReadError):
        out("Lost the bridge while pairing. Check that it is running, then try again.")
        return 2
    finally:
        if listener:
            listener.stop()
        writer.close()


async def _converse(reader, writer, typed_lines: asyncio.Queue, out) -> int:
    current = None
    event_task = asyncio.ensure_future(reader.readline())
    typed_task = asyncio.ensure_future(typed_lines.get())
    try:
        while True:
            done, _ = await asyncio.wait(
                {event_task, typed_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if typed_task in done:
                text = typed_task.result()
                typed_task = asyncio.ensure_future(typed_lines.get())
                if text is None:
                    out("No more input; pairing cancelled.")
                    return 1
                if not text.strip():
                    continue
                digits = typed(text)
                if current is None:
                    out("No Q2 is asking to pair yet; start pairing on the Q2 first.")
                elif digits is None:
                    out("Enter the six digits the Q2 shows, such as 123 456.")
                else:
                    message = {"op": "code", "id": current, "code": digits}
                    writer.write(json.dumps(message).encode() + b"\n")
                    await writer.drain()
            if event_task in done:
                line = event_task.result()
                if not line:
                    out("The bridge stopped while pairing. Start it and try again.")
                    return 2
                event = json.loads(line)
                event_task = asyncio.ensure_future(reader.readline())
                kind = event.get("event")
                if kind == "request":
                    current = event["id"]
                    out(f"{event.get('name') or 'Q2'} wants to pair (from {event.get('peer')}).")
                elif kind == "ended" and event.get("id") == current:
                    current = None
                    out(
                        {
                            "code": "Wrong code: that request was refused. Start pairing on the "
                            "Q2 again.",
                            "timeout": "The Q2's request timed out. Start pairing on the Q2 again.",
                            "gone": "The Q2 stopped asking. Start pairing on the Q2 again.",
                            "replaced": "A newer request replaced it.",
                        }.get(event.get("reason"), "The request ended.")
                    )
                elif kind == "paired":
                    out(f"Paired: {event.get('name') or 'the Q2'} received this bridge's address.")
                    wan = event.get("wan")
                    if not wan:
                        out(HOME_ONLY)
                    elif raw_ip(wan):
                        out(ip_warning(wan))
                    return 0
    finally:
        event_task.cancel()
        typed_task.cancel()


def load_endpoints(data: Path, lan: str | None, port: int | None, wan, wan_port) -> Endpoints:
    """Explicit options (remembered in DATA/endpoints.json, so the Docker `exec` without options
    reuses what setup gave), else that file, else setup's state beside a native DATA, else this
    machine's address."""
    saved = data / "endpoints.json"
    if lan:
        endpoints = Endpoints(lan, port or 7443, wan, (wan_port or port or 7443) if wan else None)
        with contextlib.suppress(OSError):
            fd = os.open(saved, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                json.dump(endpoints.json(), f)
        return endpoints
    for path in (saved, data.parent / "setup-state.json"):
        with contextlib.suppress(OSError, ValueError):
            found = Endpoints.from_json(json.loads(path.read_text()))
            if port:
                found = Endpoints(found.lan, port, found.wan, found.wan_port)
            return found
    from .server import lan_address

    return Endpoints(lan_address(), port or 7443)
