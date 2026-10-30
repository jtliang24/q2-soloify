import asyncio
import hashlib
import ssl
import stat

import pytest

from soloify import server, wire


def test_identity_is_made_once_and_private(tmp_path):
    first = server.Identity.load(tmp_path)
    again = server.Identity.load(tmp_path)
    assert (first.token, first.pin) == (again.token, again.pin)
    assert len(first.token) == 32
    for name in ("key.pem", "token"):
        assert stat.S_IMODE((tmp_path / "bridge" / name).stat().st_mode) == 0o600


def test_hello():
    assert wire.hello_ok(b"hello 2 abc\n", "abc")
    assert not wire.hello_ok(b"hello 2 abd\n", "abc")
    assert not wire.hello_ok(b"hello 1 abc\n", "abc")
    assert not wire.hello_ok(b"hello 2 abc extra\n", "abc")


async def test_frame_round_trip():
    reader = asyncio.StreamReader()
    reader.feed_data(wire.frame(wire.STATS, b"ring=1000") + wire.audio(7, b"opus"))
    reader.feed_eof()
    assert await wire.read_frame(reader) == (wire.STATS, b"ring=1000")
    assert await wire.read_frame(reader) == (wire.AUDIO, b"\0\0\0\x07opus")
    with pytest.raises(asyncio.IncompleteReadError):
        await wire.read_frame(reader)


async def test_oversized_frame_is_refused():
    reader = asyncio.StreamReader()
    reader.feed_data(b"t" + (wire.MAX_FRAME + 1).to_bytes(4, "big"))
    with pytest.raises(wire.WireError):
        await wire.read_frame(reader)


def _client():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


async def _start(tmp_path, session):
    identity = server.Identity.load(tmp_path)
    listener = await server.Server(identity, session).start("127.0.0.1", 0)
    return identity, listener, listener.sockets[0].getsockname()[1]


async def test_pin_is_the_served_certificate(tmp_path):
    async def session(reader, writer):
        await reader.read()

    identity, listener, port = await _start(tmp_path, session)
    async with listener:
        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=_client())
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        assert hashlib.sha256(der).hexdigest() == identity.pin
        writer.close()


async def test_good_hello_gets_ok_and_session(tmp_path):
    got = asyncio.Queue()

    async def session(reader, writer):
        await got.put(await wire.read_frame(reader))

    identity, listener, port = await _start(tmp_path, session)
    async with listener:
        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=_client())
        writer.write(b"hello 2 %s\n" % identity.token.encode() + wire.frame(wire.STATS, b"hi"))
        assert await reader.readline() == wire.OK
        assert await asyncio.wait_for(got.get(), 5) == (wire.STATS, b"hi")
        writer.close()


async def test_bad_token_closes(tmp_path):
    async def session(reader, writer):
        raise AssertionError("no session for a bad token")

    _, listener, port = await _start(tmp_path, session)
    async with listener:
        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=_client())
        writer.write(b"hello 2 wrong\n")
        assert await asyncio.wait_for(reader.read(), 5) == b""
        writer.close()


async def test_new_relay_replaces_the_old(tmp_path):
    async def session(reader, writer):
        await asyncio.sleep(3600)

    identity, listener, port = await _start(tmp_path, session)
    hello = b"hello 2 %s\n" % identity.token.encode()
    async with listener:
        old_r, old_w = await asyncio.open_connection("127.0.0.1", port, ssl=_client())
        old_w.write(hello)
        assert await old_r.readline() == wire.OK
        new_r, new_w = await asyncio.open_connection("127.0.0.1", port, ssl=_client())
        new_w.write(hello)
        assert await new_r.readline() == wire.OK
        assert await asyncio.wait_for(old_r.read(), 5) == b""
        old_w.close()
        new_w.close()
