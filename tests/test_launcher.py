"""The card launcher: concurrent starts, pid ownership, backoff and clean restart, and its choice
between the manager and the relay loop."""

import asyncio
import contextlib
import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path

import pytest

from relay_rig import ROOT, relay_binary, until
from test_browse import decode, request
from test_contract import browse
from test_manage import make_card
from test_manage import short as short  # noqa: F401 (a fixture)


async def launch(folder, env):
    proc = await asyncio.create_subprocess_exec("/bin/sh", str(folder / "run"), env=env)
    assert await proc.wait() == 0


async def stop_loop(base):
    if base.exists():
        with contextlib.suppress(ProcessLookupError):
            os.kill(int(base.read_text()), signal.SIGTERM)
        await until(lambda: not base.exists())


async def test_launcher_singleton_stop_and_restart(tmp_path):
    with tempfile.TemporaryDirectory(prefix="sf-") as folder:
        base = Path(folder) / "q"
        shutil.copy(ROOT / "card/run", tmp_path / "run")
        shutil.copy(relay_binary(), tmp_path / "q2-soloify")
        env = {**os.environ, "SOLOIFY_Q2POD": str(base)}
        try:
            await asyncio.gather(*(launch(tmp_path, env) for _ in range(3)))
            await until(lambda: Path(f"{base}.sock").exists())
            # Contending background launchers allow the owner up to one second to
            # publish its pid. Let all three startup attempts settle before Stop.
            await asyncio.sleep(1.1)
            loop = int(base.read_text())
            child_path = Path(f"/proc/{loop}/task/{loop}/children")
            child = int(child_path.read_text().split()[0])
            assert child != loop
            assert "state=none" in Path(f"{base}.state").read_text()
            await launch(tmp_path, env)
            await asyncio.sleep(0.15)
            assert base.read_text() == f"{loop}\n"
            assert child_path.read_text().split() == [str(child)]
            await stop_loop(base)
            assert not Path(f"{base}.sock").exists()
            assert not Path(f"/proc/{child}").exists()
            await launch(tmp_path, env)
            await until(lambda: Path(f"{base}.sock").exists())
            assert int(base.read_text()) != loop
        finally:
            await stop_loop(base)


async def test_launcher_backoff_and_stale_lock(tmp_path):
    with tempfile.TemporaryDirectory(prefix="sf-") as folder:
        base = Path(folder) / "q"
        shutil.copy(ROOT / "card/run", tmp_path / "run")
        # A crashed backend and a clock stand-in: record actual sleep arguments without
        # spending three minutes proving the 5,10,20,40,60,60 schedule.
        binary = tmp_path / "q2-soloify"
        binary.write_text("#!/bin/sh\nexit 1\n")
        binary.chmod(0o755)
        sleeper = tmp_path / "sleep"
        clock_log = tmp_path / "sleeps"
        sleeper.write_text(
            f"#!{sys.executable}\nimport time,sys\n"
            f"with open({str(clock_log)!r}, 'a') as f: f.write(sys.argv[1]+'\\n')\n"
            "time.sleep(0.1)\n"
        )
        sleeper.chmod(0o755)
        env = {**os.environ, "SOLOIFY_Q2POD": str(base), "PATH": f"{tmp_path}:{os.environ['PATH']}"}
        # SIGKILL leaves the mkdir lock and a dead pid: the next invocation recovers both.
        Path(f"{base}.runlock").mkdir()
        base.write_text("2147483647\n")
        try:
            await launch(tmp_path, env)
            await until(lambda: clock_log.exists() and len(clock_log.read_text().split()) >= 6)
            assert clock_log.read_text().split()[:6] == ["5", "10", "20", "40", "60", "60"]
        finally:
            await stop_loop(base)


async def test_launcher_keeps_an_existing_backend(tmp_path):
    with tempfile.TemporaryDirectory(prefix="sf-") as folder:
        base = Path(folder) / "q"
        shutil.copy(ROOT / "card/run", tmp_path / "run")
        original = await asyncio.create_subprocess_exec("sleep", "30")
        base.write_text(f"{original.pid}\n")
        try:
            await launch(tmp_path, {**os.environ, "SOLOIFY_Q2POD": str(base)})
            await asyncio.sleep(0.15)
            assert base.read_text() == f"{original.pid}\n"
            assert not Path(f"{base}.runlock").exists()
        finally:
            original.terminate()
            await original.wait()


# card/run's choice


def device(root: Path, kind: str | None):
    """A fake /sys and /proc for card/run: the Q2's 320 x 375 framebuffer, two pages, or the M1
    Plus's 320 x 480 with its Goodix touchscreen."""
    if kind is None:
        return
    fb = root / "sys/class/graphics/fb0"
    fb.mkdir(parents=True)
    (fb / "virtual_size").write_text("320,750\n" if kind == "q2" else "320,960\n")
    (root / "proc/bus/input").mkdir(parents=True)
    names = ["axp2101-pek", "md-gpio-keys"] + (["Goodix-CTP"] if kind == "m1plus" else [])
    (root / "proc/bus/input/devices").write_text(
        "".join(f'I: Bus=0019\nN: Name="{n}"\n\n' for n in names)
    )


async def launch_now(folder: Path, env: dict):
    """run as the firmware calls it: it must return at once."""
    loop = asyncio.get_running_loop()
    started = loop.time()
    proc = await asyncio.create_subprocess_exec("/bin/sh", str(folder / "run"), env=env)
    assert await proc.wait() == 0
    assert loop.time() - started < 1


async def end(pidfile: Path):
    if pidfile.exists():
        with contextlib.suppress(ProcessLookupError, ValueError):
            os.kill(int(pidfile.read_text()), signal.SIGTERM)
        await until(lambda: not pidfile.exists())


@pytest.fixture
def launcher(tmp_path, short):
    card = tmp_path / "card"
    soloify = make_card(card, relay_binary())
    env = {
        **os.environ,
        "SOLOIFY_ROOT": str(tmp_path / "dev"),
        "SOLOIFY_MANAGE": str(short / "m"),
        "SOLOIFY_RELAY_BASE": str(short / "r"),
    }
    env.pop("SOLOIFY_Q2POD", None)
    return soloify, env, short / "m", short / "r"


async def test_run_starts_the_manager_on_a_q2(tmp_path, launcher):
    soloify, env, manage, relay = launcher
    device(tmp_path / "dev", "q2")
    try:
        await launch_now(soloify, env)
        await until(lambda: Path(f"{manage}.browse.sock").exists())
        loop = int(manage.read_text())
        head, rows = decode(await browse(manage, request("menu")))
        assert head[1] == "ok" and rows[0][3] == "librespot"
        assert (soloify / "q2app").exists()  # a Q2 keeps it
        assert not relay.exists()
        await launch_now(soloify, env)  # a second start keeps the running one
        await asyncio.sleep(0.15)
        assert int(manage.read_text()) == loop
        # The page's stop op ends the manager and its loop
        assert decode(await browse(manage, request("stop")))[0][1] == "ok"
        await until(lambda: not manage.exists())
        await until(lambda: not Path(f"{manage}.runlock").exists())
        assert not Path(f"{manage}.browse.sock").exists()
    finally:
        await end(manage)


async def test_run_manager_idle_end_and_term(tmp_path, launcher):
    soloify, env, manage, _ = launcher
    device(tmp_path / "dev", "q2")
    env["SOLOIFY_IDLE_MS"] = "500"
    try:
        await launch_now(soloify, env)
        await until(lambda: manage.exists())
        await until(lambda: not manage.exists(), timeout=5)  # idle: the loop ends with it
        assert not Path(f"{manage}.browse.sock").exists()
        env["SOLOIFY_IDLE_MS"] = "60000"
        await launch_now(soloify, env)
        await until(lambda: Path(f"{manage}.browse.sock").exists())
        loop = int(manage.read_text())
        child = int(Path(f"/proc/{loop}/task/{loop}/children").read_text().split()[0])
        os.kill(loop, signal.SIGTERM)
        await until(lambda: not manage.exists())
        await until(lambda: not Path(f"/proc/{child}").exists())
        assert not Path(f"{manage}.browse.sock").exists()
    finally:
        await end(manage)


@pytest.mark.parametrize("kind", ["m1plus", None])
async def test_run_keeps_the_relay_off_a_q2(tmp_path, launcher, kind):
    soloify, env, manage, relay = launcher
    device(tmp_path / "dev", kind)
    try:
        await launch_now(soloify, env)
        await until(lambda: Path(f"{relay}.sock").exists())  # the relay's control socket: -q
        assert not manage.exists()
        # The M1 Plus's firmware would list q2app as a second Soloify row: run removes it there
        assert (soloify / "q2app").exists() == (kind is None)
    finally:
        await end(relay)


async def test_run_in_the_spotify_slot_is_the_relay_on_a_q2(tmp_path, launcher):
    soloify, env, manage, relay = launcher
    device(tmp_path / "dev", "q2")
    slot = soloify.parent / ".spotify"
    shutil.copy(soloify / "run", slot / "run")
    shutil.copy(relay_binary(), slot / "librespot")
    try:
        await launch_now(slot, env)
        await until(lambda: Path(f"{relay}.sock").exists())
        assert not manage.exists()
    finally:
        await end(relay)


async def test_run_override_keeps_the_relay_in_soloify(tmp_path, launcher):
    soloify, env, manage, _ = launcher
    device(tmp_path / "dev", "q2")
    with tempfile.TemporaryDirectory(prefix="sf-") as folder:
        base = Path(folder) / "q"
        try:
            await launch_now(soloify, {**env, "SOLOIFY_Q2POD": str(base)})
            await until(lambda: Path(f"{base}.sock").exists())
            assert not manage.exists() and (soloify / "q2app").exists()
        finally:
            await end(base)
