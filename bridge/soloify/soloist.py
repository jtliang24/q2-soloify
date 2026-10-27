"""Soloist, minimally: started and restarted by the bridge, its audio routed to the null sink, and
paused or resumed through `soloist ctl`. Downloading, the expiry refresh and the WebSocket client
are PLAN.md's step 2."""

import asyncio
import json
import logging
import os
import shlex
from pathlib import Path

log = logging.getLogger("soloify")

SINK = "q2_soloify"


async def _output(*cmd: str, env: dict | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
        env=env,
    )
    out, _ = await proc.communicate()
    return proc.returncode, out.decode(errors="replace")


async def pipewire() -> bool:
    """Whether the sound server is PipeWire, where only -d routes Soloist (docs/spikes.md)."""
    rc, out = await _output("pactl", "info")
    return rc == 0 and any(
        line.startswith("Server Name:") and "PipeWire" in line for line in out.splitlines()
    )


async def ensure_sink(sink: str = SINK) -> None:
    """Loads the null sink unless it's there (the container's pulse.pa makes it already)."""
    rc, out = await _output("pactl", "list", "short", "modules")
    if rc == 0 and any(
        "module-null-sink" in line and f"sink_name={sink}" in line for line in out.splitlines()
    ):
        return
    rc, _ = await _output(
        "pactl",
        "load-module",
        "module-null-sink",
        f"sink_name={sink}",
        "rate=44100",
        "channels=2",
        "format=s16le",
        "sink_properties=device.description=Soloify",
    )
    if rc:
        raise RuntimeError(f"pactl couldn't load the {sink} null sink")


class Soloist:
    """One Soloist child, restarted on exit with a backoff. $SOLOIFY_CTL (a command line) stands in
    for `soloist ctl` in tests."""

    def __init__(self, data: Path, key_file: Path, name: str = "Q2", binary: str = "soloist"):
        self.data = data
        self.key_file = key_file
        self.name = name
        self.binary = binary
        self.state = data / "soloist-state"
        ctl = os.environ.get("SOLOIFY_CTL")
        self.ctl_cmd = shlex.split(ctl) if ctl else [binary, "ctl", "-D", str(self.state)]

    async def _args(self) -> tuple[list[str], dict]:
        key = self.key_file.read_text().strip()
        cache = self.data / "cache"
        for d in (self.state, cache):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
        env = dict(os.environ)
        args = [self.binary, "-n", self.name, "-k", key, "-D", str(self.state), "-C", str(cache)]
        args += ["-z", "100", "-i", "100", "--ws", "127.0.0.1:0"]
        if await pipewire():
            args += ["-d", SINK]
        else:
            env["PULSE_SINK"] = SINK
        return args, env

    async def run(self) -> None:
        wait = 5
        while True:
            args, env = await self._args()
            started = asyncio.get_running_loop().time()
            # Soloist takes its key only as -k, so it's on the command line; never log args
            proc = await asyncio.create_subprocess_exec(*args, env=env)
            try:
                rc = await proc.wait()
            except asyncio.CancelledError:
                proc.terminate()
                await proc.wait()
                raise
            ran = asyncio.get_running_loop().time() - started
            wait = 5 if ran > 60 else min(wait * 2, 60)
            log.warning("soloist exited %s after %.0f s; again in %d s", rc, ran, wait)
            await asyncio.sleep(wait)

    async def ctl(self, *args: str) -> str | None:
        rc, out = await _output(*self.ctl_cmd, *args)
        if rc:
            log.warning("soloist ctl %s failed (%s)", " ".join(args), rc)
            return None
        return out

    async def playing(self) -> bool:
        out = await self.ctl("now", "--json")
        try:
            return json.loads(out or "{}").get("status") == "playing"
        except ValueError:
            return False
