"""The Soloify app's page (q2-soloify --manage) against a fake card tree and a fake firmware client
speaking Q2SPOT/1."""

import asyncio
import hashlib
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from relay_rig import ROOT, Bridge, relay_binary, until
from test_browse import decode, request
from test_contract import browse


def make_card(root: Path, binary: Path, slot: bytes | None = b"librespot") -> Path:
    """A card with .soloify (the relay, run, VERSION) and .spotify holding slot as librespot."""
    soloify = root / ".soloify"
    soloify.mkdir(parents=True)
    shutil.copy(binary, soloify / "q2-soloify")
    shutil.copy(ROOT / "card/run", soloify / "run")
    (soloify / "VERSION").write_text("0.0.9\n")
    (soloify / "q2app").write_text("name=Soloify\nrow=library\n")
    if slot is not None:
        (root / ".spotify").mkdir()
        (root / ".spotify/librespot").write_bytes(slot)
    return soloify


class Manager:
    """q2-soloify --manage as a child in .soloify, with its log kept."""

    def __init__(self, folder: Path, base: Path, binary: list[str] | None = None, **env):
        self.folder = folder
        self.base = base
        self.binary = binary or [str(relay_binary())]
        self.env = {
            **os.environ,
            "SOLOIFY_Q2POD": str(base),
            "SOLOIFY_RELAY_BASE": str(base.parent / "slot"),
            **env,
        }
        self.proc = None

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            *self.binary,
            "-v",
            "--manage",
            cwd=self.folder,
            env=self.env,
            stdin=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await until(lambda: Path(f"{self.base}.browse.sock").exists(), timeout=20)
        return self

    async def ask(self, op, *args):
        return decode(await browse(self.base, request(op, *args)))

    async def menu(self):
        head, rows = await self.ask("menu")
        return head, {r[1]: r for r in rows}

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()


@pytest.fixture
async def short():
    # Unix socket names must fit 108 bytes; pytest's directories often don't.
    with tempfile.TemporaryDirectory(prefix="sf-") as folder:
        yield Path(folder)


@pytest.fixture
async def manager(tmp_path, short):
    managers = []

    async def start(folder, **env):
        m = await Manager(folder, short / "m", **env).start()
        managers.append(m)
        return m

    yield start
    for m in managers:
        await m.stop()


async def test_status_delay_and_quality_without_remote(tmp_path, manager):
    soloify = make_card(tmp_path, relay_binary())
    m = await manager(soloify)
    head, rows = await m.menu()
    assert head == ["7", "ok", "", ""]
    assert rows["slot"][:4] == ["info", "slot", "Spotify slot", "librespot"]
    assert rows["paired"][2:4] == ["Paired", "No"]
    assert "bridge" not in rows  # nothing to check without a remote
    assert rows["delay"][:4] == ["list", "delay", "Delay", "Automatic"]
    assert rows["quality"][:4] == ["list", "quality", "Quality", "160 kbit/s"]
    assert list(rows) == ["slot", "paired", "use", "delay", "quality"]  # use: test_swap


async def test_spotify_slot_by_content(tmp_path, manager):
    soloify = make_card(tmp_path, relay_binary(), slot=relay_binary().read_bytes())
    slot = tmp_path / ".spotify"
    m = await manager(soloify)
    assert (await m.menu())[1]["slot"][3] == "Soloify v0.0.9"
    # An older relay, recorded by the swap
    old = b"an older relay"
    (slot / "librespot").write_bytes(old)
    (soloify / "backup").mkdir()
    (soloify / "backup/relay.sha256").write_text(hashlib.sha256(old).hexdigest().upper() + "\n")
    await asyncio.sleep(3.1)  # the slot's state is kept 3 s
    assert (await m.menu())[1]["slot"][3] == "Soloify (older copy)"
    # run prefers q2-soloify in the slot
    shutil.copy(relay_binary(), slot / "q2-soloify")
    (slot / "librespot").write_bytes(b"librespot")
    await asyncio.sleep(3.1)
    assert (await m.menu())[1]["slot"][3] == "Soloify v0.0.9"
    shutil.rmtree(slot)
    await asyncio.sleep(3.1)
    assert (await m.menu())[1]["slot"][3] == "Missing"


@pytest.mark.parametrize("ours", [False, True])
async def test_choices_write_soloify_and_our_slot(tmp_path, manager, ours):
    soloify = make_card(
        tmp_path, relay_binary(), slot=relay_binary().read_bytes() if ours else b"x"
    )
    slot = tmp_path / ".spotify"
    m = await manager(soloify)
    head, rows = await m.ask("delay")
    assert head[1:3] == ["ok", "refresh"]
    assert [(r[0], r[1], r[2], r[3], r[4]) for r in rows] == [
        ("action", "setdelay", "Automatic", "Selected", "0"),
        ("action", "setdelay", "1 s", "", "1000"),
        ("action", "setdelay", "2 s", "", "2000"),
        ("action", "setdelay", "4 s", "", "4000"),
        ("action", "setdelay", "6 s", "", "6000"),
    ]
    assert (await m.ask("setdelay", "2000"))[0] == ["7", "ok", "", ""]
    assert (soloify / ".delay").read_text() == "2000"  # as Q2 Pod's own rows write it
    assert (slot / ".delay").exists() == ours
    assert [r[3] for r in (await m.ask("delay"))[1]] == ["", "", "Selected", "", ""]
    assert (await m.menu())[1]["delay"][3] == "2 s"
    assert (await m.ask("setdelay", "0"))[0][1] == "ok"  # Automatic: no file
    assert not (soloify / ".delay").exists() and not (slot / ".delay").exists()
    assert (await m.ask("setdelay", "3000"))[0][1] == "bad"
    assert (await m.ask("setdelay", "%32000"))[0][1] == "bad"

    head, rows = await m.ask("quality")
    assert [(r[2], r[3], r[4]) for r in rows] == [
        ("96 kbit/s", "", "96"),
        ("128 kbit/s", "", "128"),
        ("160 kbit/s", "Selected", "160"),
    ]
    assert (await m.ask("setquality", "96"))[0][1] == "ok"
    assert (soloify / ".quality").read_text() == "96"
    assert ((slot / ".quality").read_text() == "96") if ours else not (slot / ".quality").exists()
    assert (await m.menu())[1]["quality"][3] == "96 kbit/s"
    assert (await m.ask("setquality", "100"))[0][1] == "bad"
    assert (await m.ask("nonsense"))[0][1] == "bad"


async def test_bridge_check_is_cached(tmp_path, manager):
    bridge = await Bridge(tmp_path / "data").start()
    try:
        soloify = make_card(tmp_path, relay_binary())
        (soloify / "remote").write_text(bridge.line() + "\n")
        m = await manager(soloify)
        head, rows = await m.menu()
        assert head[2] == "refresh" and rows["bridge"][2:4] == ["Bridge", "Checking…"]
        assert rows["paired"][3] == "Yes"

        async def settled():
            while True:
                head, rows = await m.menu()
                if head[2] != "refresh":
                    return rows
                await asyncio.sleep(0.2)

        rows = await asyncio.wait_for(settled(), 15)
        assert rows["bridge"][3] == "Reachable (LAN)"
        assert len(bridge.sessions) == 1
        await asyncio.sleep(0.5)
        assert (await m.menu())[0][2] == "" and len(bridge.sessions) == 1  # cached
    finally:
        await bridge.stop()


async def bridge_word(m):
    """The Bridge row once its check is over."""
    while (r := await m.menu())[0][2] == "refresh":
        await asyncio.sleep(0.2)
    return r[1]["bridge"][3]


async def test_bridge_not_reached(tmp_path, manager):
    bridge = await Bridge(tmp_path / "data").start()
    port = bridge.port
    await bridge.stop()
    soloify = make_card(tmp_path, relay_binary())
    (soloify / "remote").write_text(f"127.0.0.1 {port} token\n")
    m = await manager(soloify)
    assert await asyncio.wait_for(bridge_word(m), 15) == "Not reached"


async def test_bridge_left_to_our_running_relay(tmp_path, manager, short):
    # Our relay in the Spotify slot holds the bridge: a hello would replace it
    soloify = make_card(tmp_path, relay_binary(), slot=relay_binary().read_bytes())
    bridge = await Bridge(tmp_path / "data").start()
    holder = await asyncio.create_subprocess_exec("sleep", "30")
    try:
        (soloify / "remote").write_text(bridge.line() + "\n")
        (short / "slot").write_text(f"{holder.pid}\n")
        m = await manager(soloify)
        assert await asyncio.wait_for(bridge_word(m), 15) == "In use by Soloify"
        assert not bridge.sessions
    finally:
        holder.kill()
        await holder.wait()
        await bridge.stop()


async def test_stop_op_and_idle_end(tmp_path, manager):
    soloify = make_card(tmp_path, relay_binary())
    m = await manager(soloify)
    assert (await m.ask("stop"))[0] == ["7", "ok", "", ""]
    assert await asyncio.wait_for(m.proc.wait(), 5) == 0
    assert not Path(f"{m.base}.browse.sock").exists()
    m = await manager(soloify, SOLOIFY_IDLE_MS="400")
    await m.menu()
    assert await asyncio.wait_for(m.proc.wait(), 5) == 0
