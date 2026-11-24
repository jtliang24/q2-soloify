"""The swap (relay/swap.c): the Soloify app's Use, Update, Repair and Restore rows against a fake
card tree, a fake Spotify loop and a fake firmware client speaking Q2SPOT/1."""

import asyncio
import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path

import pytest

from relay_rig import ROOT, relay_binary
from test_browse import decode, request
from test_manage import Manager, make_card

STOCK = b"\x7fELF librespot 0.8.0"
STOCK_RUN = b"#!/bin/sh\n# librespot's run\n"
APLAY = (ROOT / "card/aplay.sh").read_bytes()
SWAP_ROWS = ["use", "update", "repair", "restore"]

# Spotify's loop as q2-librespot's run keeps it: its pid in the pid file, removed on TERM
LOOP = """
import os, signal, sys, time
def term(*_):
    os.unlink(sys.argv[1])
    sys.exit(0)
signal.signal(signal.SIGTERM, term)
open(sys.argv[1], "w").write(f"{os.getpid()}\\n")
while True:
    time.sleep(1)
"""
# A librespot the loop left behind, by a name of its own: killall leaves the host's librespot be
NAME = "sf-librespot"
LEFTOVER = f"""
import ctypes, time
ctypes.CDLL(None).prctl(15, b"{NAME}", 0, 0, 0)
print("named", flush=True)
time.sleep(60)
"""


@pytest.fixture
async def short():
    # Unix socket names must fit 108 bytes; pytest's directories often don't.
    with tempfile.TemporaryDirectory(prefix="sf-") as folder:
        yield Path(folder)


@pytest.fixture
async def manager(short):
    managers = []

    async def start(folder):
        m = await Manager(folder, short / "m", SOLOIFY_LIBRESPOT=NAME).start()
        managers.append(m)
        return m

    yield start
    for m in managers:
        await m.stop()


def card(root: Path, binary: Path | None = None) -> tuple[Path, Path]:
    """.soloify with aplay.sh, remote and .delay; .spotify with stock librespot and its cache."""
    soloify = make_card(root, binary or relay_binary(), slot=STOCK)
    shutil.copy(ROOT / "card/aplay.sh", soloify / "aplay.sh")
    (soloify / "remote").write_text("127.0.0.1 1 token\n")
    (soloify / ".delay").write_text("2000")
    slot = root / ".spotify"
    (slot / "run").write_bytes(STOCK_RUN)
    (slot / "aplay.sh").write_bytes(APLAY)
    (slot / "cache").mkdir()
    (slot / "cache/credentials.json").write_text("{}")
    (slot / ".cache-size").write_text("1024")
    return soloify, slot


async def ask(m, op, *args):
    """A request that may stop Spotify first: longer than the firmware's own toast wait."""
    reader, writer = await asyncio.open_unix_connection(f"{m.base}.browse.sock")
    try:
        writer.write(request(op, *args))
        writer.write_eof()
        return decode(await asyncio.wait_for(reader.read(), 10))
    finally:
        writer.close()
        await writer.wait_closed()


async def swap_rows(m):
    await asyncio.sleep(
        3.1
    )  # the slot's state is kept 3 s; a swap forgets it, a hand change doesn't
    return [uri for uri in (await m.menu())[1] if uri in SWAP_ROWS]


def temps(*folders):
    return [p for f in folders for p in f.rglob("*.tmp")]


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def test_use_and_restore_stock_librespot(tmp_path, manager, short):
    soloify, slot = card(tmp_path)
    relay = relay_binary().read_bytes()
    loop = await asyncio.create_subprocess_exec(sys.executable, "-c", LOOP, str(short / "slot"))
    leftover = await asyncio.create_subprocess_exec(
        sys.executable, "-c", LEFTOVER, stdout=asyncio.subprocess.PIPE
    )
    try:
        await leftover.stdout.readline()
        while not (short / "slot").exists():
            await asyncio.sleep(0.05)
        m = await manager(soloify)
        head, rows = await m.menu()
        assert rows["use"][:4] == [
            "action",
            "use",
            "Use Soloify for Spotify",
            "Spotify's librespot is kept",
        ]
        assert [u for u in rows if u in SWAP_ROWS] == ["use"]
        assert list(rows).index("use") == list(rows).index("delay") - 1  # after Status

        head, _ = await ask(m, "use")
        assert head == ["7", "ok", "Spotify now plays through Soloify", ""]
        # Spotify stopped: its loop by its pid file, the leftover by name
        assert await asyncio.wait_for(loop.wait(), 1) == 0
        assert await asyncio.wait_for(leftover.wait(), 1) != 0
        assert not (short / "slot").exists()
    finally:
        for p in (loop, leftover):
            if p.returncode is None:
                p.kill()
                await p.wait()

    assert (slot / "librespot").read_bytes() == relay
    assert (slot / "run").read_bytes() == (ROOT / "card/run").read_bytes()
    assert (slot / "aplay.sh").read_bytes() == APLAY
    assert (slot / "remote").read_text() == "127.0.0.1 1 token\n"
    assert (slot / ".delay").read_text() == "2000"
    assert not (slot / ".quality").exists()
    assert (slot / "cache/credentials.json").read_text() == "{}"
    assert (slot / ".cache-size").read_text() == "1024"
    backup = soloify / "backup"
    assert (backup / "librespot").read_bytes() == STOCK
    assert (backup / "run").read_bytes() == STOCK_RUN
    assert (backup / "aplay.sh").read_bytes() == APLAY
    assert (backup / "SOURCE").read_text() == f"librespot {len(STOCK)} {sha(STOCK)}\n"
    assert (backup / "relay.sha256").read_text() == sha(relay) + "\n"
    assert not temps(slot, soloify)

    rows = (await m.menu())[1]  # at once: the swap forgot the slot's state
    assert rows["slot"][3] == "Soloify v0.0.9"
    assert [u for u in rows if u in SWAP_ROWS] == ["restore"]
    # Delay now writes the slot's copy too
    assert (await m.ask("setdelay", "4000"))[0][1] == "ok"
    assert (slot / ".delay").read_text() == "4000"

    head, _ = await ask(m, "restore")
    assert head == ["7", "ok", "Spotify's librespot is back", ""]
    assert (slot / "librespot").read_bytes() == STOCK
    assert (slot / "run").read_bytes() == STOCK_RUN
    assert (slot / "aplay.sh").read_bytes() == APLAY
    for name in ["remote", ".delay", ".quality", "q2-soloify"]:
        assert not (slot / name).exists()
    assert (slot / "cache/credentials.json").read_text() == "{}"
    assert (slot / ".cache-size").read_text() == "1024"
    assert (backup / "librespot").read_bytes() == STOCK  # kept for the next swap
    assert not (backup / "relay.sha256").exists()
    rows = (await m.menu())[1]
    assert rows["slot"][3] == "librespot"
    assert [u for u in rows if u in SWAP_ROWS] == ["use"]

    # After a Restore, the App Store's Spotify Update is no reason to Repair
    (slot / "librespot").write_bytes(STOCK + b" 0.8.1")
    assert await swap_rows(m) == ["use"]


async def test_update_an_older_copy(tmp_path, manager):
    soloify, slot = card(tmp_path)
    m = await manager(soloify)
    assert (await ask(m, "use"))[0][1] == "ok"
    # Soloify's App Store Update brings a new relay; the slot keeps the one the swap recorded
    old = (slot / "librespot").read_bytes()
    new = old + b"\0v0.0.10"
    (soloify / "q2-soloify").write_bytes(new)
    rows = (await m.menu())[1]
    assert rows["slot"][3] == "Soloify (older copy)"
    assert [u for u in rows if u in SWAP_ROWS] == ["update", "restore"]
    assert rows["update"][2:4] == ["Update Spotify's copy", "To Soloify v0.0.9"]

    assert (await ask(m, "update"))[0][1:3] == ["ok", "Spotify now plays through Soloify"]
    assert (slot / "librespot").read_bytes() == new
    assert (soloify / "backup/librespot").read_bytes() == STOCK  # never a relay
    assert (soloify / "backup/relay.sha256").read_text() == sha(new) + "\n"
    rows = (await m.menu())[1]
    assert rows["slot"][3] == "Soloify v0.0.9"
    assert [u for u in rows if u in SWAP_ROWS] == ["restore"]


async def test_repair_after_spotify_update(tmp_path, manager):
    soloify, slot = card(tmp_path)
    m = await manager(soloify)
    assert (await ask(m, "use"))[0][1] == "ok"
    # The App Store's Spotify Update writes librespot, run and aplay.sh over the swap
    newer = STOCK + b" 0.8.1"
    (slot / "librespot").write_bytes(newer)
    (slot / "run").write_bytes(STOCK_RUN + b"# 0.8.1\n")
    rows = (await m.menu())[1]
    assert rows["slot"][3] == "librespot"
    assert [u for u in rows if u in SWAP_ROWS] == ["repair"]
    assert rows["repair"][2] == "Repair"

    assert (await ask(m, "repair"))[0][1] == "ok"
    assert (slot / "librespot").read_bytes() == relay_binary().read_bytes()
    assert (slot / "run").read_bytes() == (ROOT / "card/run").read_bytes()
    backup = soloify / "backup"
    assert (backup / "librespot").read_bytes() == newer  # the new librespot is the backup
    assert (backup / "run").read_bytes() == STOCK_RUN + b"# 0.8.1\n"
    assert (backup / "SOURCE").read_text() == f"librespot {len(newer)} {sha(newer)}\n"
    assert [u for u in (await m.menu())[1] if u in SWAP_ROWS] == ["restore"]

    assert (await ask(m, "restore"))[0][1] == "ok"
    assert (slot / "librespot").read_bytes() == newer


async def test_restore_without_backup(tmp_path, manager):
    # The relay copied in by hand, as the README's manual way does: no backup to put back
    soloify, slot = card(tmp_path)
    shutil.copy(relay_binary(), slot / "librespot")
    shutil.copy(ROOT / "card/run", slot / "run")
    (slot / "remote").write_text("127.0.0.1 1 token\n")
    (slot / ".quality").write_text("96")
    m = await manager(soloify)
    rows = (await m.menu())[1]
    assert [u for u in rows if u in SWAP_ROWS] == ["restore"]

    assert (await ask(m, "restore"))[0] == ["7", "ok", "Install Spotify from the App Store", ""]
    for name in ["librespot", "run", "remote", ".quality"]:
        assert not (slot / name).exists()
    assert (slot / "aplay.sh").read_bytes() == APLAY  # librespot's own too
    assert (slot / "cache/credentials.json").read_text() == "{}"
    assert not (soloify / "backup").exists()
    rows = (await m.menu())[1]
    assert rows["slot"][3] == "Missing"
    assert [u for u in rows if u in SWAP_ROWS] == ["use"]


async def test_a_relay_never_replaces_the_backup(tmp_path, manager):
    soloify, slot = card(tmp_path)
    m = await manager(soloify)
    assert (await ask(m, "use"))[0][1] == "ok"
    # An unrecorded relay of ours (an older one copied by hand) in the slot
    (slot / "librespot").write_bytes(b"\x7fELF q2-soloify 0.0.1 SOLOIFY_PARENT")
    (soloify / "backup/relay.sha256").unlink()
    assert (await ask(m, "use"))[0][1] == "ok"
    assert (soloify / "backup/librespot").read_bytes() == STOCK
    assert (await ask(m, "restore"))[0][2] == "Spotify's librespot is back"


async def test_from_a_missing_spotify(tmp_path, manager):
    soloify = make_card(tmp_path, relay_binary(), slot=None)
    shutil.copy(ROOT / "card/aplay.sh", soloify / "aplay.sh")
    m = await manager(soloify)
    rows = (await m.menu())[1]
    assert rows["slot"][3] == "Missing"
    assert [u for u in rows if u in SWAP_ROWS] == ["use"]
    assert (await ask(m, "use"))[0][1] == "ok"
    assert (tmp_path / ".spotify/librespot").read_bytes() == relay_binary().read_bytes()
    assert not (soloify / "backup/SOURCE").exists()
    assert (await ask(m, "restore"))[0][2] == "Install Spotify from the App Store"
    assert not (tmp_path / ".spotify/librespot").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="pid 1 stands for root's Spotify")
async def test_root_owned_spotify_is_left_to_the_firmware(tmp_path, manager, short):
    # Before 1.9.1 the firmware ran Spotify as root: the manager can't stop it
    soloify, slot = card(tmp_path)
    (short / "slot").write_text("1\n")
    m = await manager(soloify)
    head, _ = await ask(m, "use")
    assert head == ["7", "error", "Choose Stop Streaming Services, then try again", ""]
    assert (slot / "librespot").read_bytes() == STOCK
    assert (slot / "run").read_bytes() == STOCK_RUN
    assert not (soloify / "backup").exists()
    assert (await ask(m, "restore"))[0][1] == "error"


async def test_a_cut_copy_leaves_the_binary_and_mends(tmp_path, manager):
    soloify, slot = card(tmp_path)
    # A cut copy's leftovers, and a .delay that can't be replaced
    (slot / "librespot.tmp").write_bytes(b"half a relay")
    (slot / "run.tmp").write_bytes(b"half a run")
    (slot / ".delay").mkdir()
    (slot / ".delay/x").write_text("")
    m = await manager(soloify)
    head, _ = await ask(m, "use")
    assert head == ["7", "error", "Couldn't copy .delay: try again", ""]
    assert (slot / "librespot").read_bytes() == STOCK  # the binary goes last
    assert (soloify / "backup/librespot").read_bytes() == STOCK
    assert [u for u in (await m.menu())[1] if u in SWAP_ROWS] == ["use"]

    shutil.rmtree(slot / ".delay")
    assert (await ask(m, "use"))[0][1] == "ok"
    assert (slot / "librespot").read_bytes() == relay_binary().read_bytes()
    assert (slot / ".delay").read_text() == "2000"
    assert not temps(slot, soloify)
    # The first try had already put our run in the slot: the backup kept librespot's
    assert (soloify / "backup/run").read_bytes() == STOCK_RUN
    assert (await ask(m, "restore"))[0][1] == "ok"
    assert (slot / "run").read_bytes() == STOCK_RUN
