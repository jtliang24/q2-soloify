"""The card launcher: concurrent starts, pid ownership, backoff and clean restart."""

import asyncio
import contextlib
import os
import shutil
import signal
import sys
import tempfile
from pathlib import Path

from relay_rig import ROOT, relay_binary, until


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
