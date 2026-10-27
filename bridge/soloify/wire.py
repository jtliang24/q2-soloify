"""The relay <-> bridge wire (docs/wire.md): a hello line each way, then frames of one type byte and
a big-endian u32 length."""

import asyncio
import hmac
import struct

VERSION = 1
OK = b"ok %d\n" % VERSION
# The largest frame either side accepts: a cover JPEG is the biggest
MAX_FRAME = 1 << 20
HELLO_MAX = 256

# relay -> bridge
AUDIO_REQUEST = b"A"  # b"1" to start, b"0 pause" or b"0 stall" to stop
ACK = b"k"  # u32: audio packets received since the hello
STATS = b"t"  # text, logged
CONTROL = b"c"  # a control datagram from Q2 Pod: p, P, s, n, b or S<ms>
# bridge -> relay
AUDIO = b"a"  # u32 sequence, then one Opus packet
STATE = b"s"  # the status file's key=value lines, less at=
COVER = b"j"  # a track id, a newline, then the track's cover JPEG

_HEAD = struct.Struct(">cI")


class WireError(Exception):
    """The peer broke the wire's rules; the connection is closed."""


def frame(kind: bytes, payload: bytes = b"") -> bytes:
    return _HEAD.pack(kind, len(payload)) + payload


async def read_frame(reader: asyncio.StreamReader) -> tuple[bytes, bytes]:
    """The next frame as (type, payload); IncompleteReadError at a clean end."""
    kind, size = _HEAD.unpack(await reader.readexactly(_HEAD.size))
    if size > MAX_FRAME:
        raise WireError(f"frame {kind!r} of {size} bytes")
    return kind, await reader.readexactly(size)


def hello_ok(line: bytes, token: str) -> bool:
    """Whether the relay's first line is `hello 1 TOKEN` with our token, in constant time."""
    parts = line.rstrip(b"\n").split(b" ")
    return (
        len(parts) == 3
        and parts[0] == b"hello"
        and parts[1] == b"%d" % VERSION
        and hmac.compare_digest(parts[2], token.encode())
    )


def audio(seq: int, packet: bytes) -> bytes:
    return frame(AUDIO, struct.pack(">I", seq & 0xFFFFFFFF) + packet)


def cover(track_id: str, jpeg: bytes) -> bytes:
    return frame(COVER, track_id.encode() + b"\n" + jpeg)


def ack_count(payload: bytes) -> int:
    if len(payload) != 4:
        raise WireError(f"ack of {len(payload)} bytes")
    return struct.unpack(">I", payload)[0]
