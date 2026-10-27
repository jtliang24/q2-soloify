"""Soloist: fetched for this machine, refreshed before its build expires, started and restarted by
the bridge with its WebSocket on loopback, and its audio routed to the null sink."""

import asyncio
import datetime
import io
import logging
import os
import platform
import re
import shutil
import tarfile
import urllib.request
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger("soloify")

SINK = "q2_soloify"
URL = "https://soloist-builds.spotifycdn.com/soloist_release_{}.tar.gz"
# uname -m -> the build's name; macOS and Windows run the Linux build in the container
TARGETS = {
    "x86_64": "x86_64",
    "aarch64": "arm64",
    "arm64": "arm64",
    "armv7l": "arm32",
    "armv8l": "arm32",
}
LIFETIME = datetime.timedelta(days=90)  # a build exits 10 this long after its build date
REFRESH = datetime.timedelta(days=14)  # fetch a new one when less than this is left
EXPIRED = 10
CHECK_EVERY = 24 * 3600


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


def target(machine: str | None = None) -> str:
    machine = machine or platform.machine()
    if platform.system() != "Linux" or machine not in TARGETS:
        raise RuntimeError(f"no Soloist build for {platform.system()} {machine}")
    return TARGETS[machine]


def build_date(version: str) -> datetime.date | None:
    """`soloist 1.3.10.16 build 1793016086 (20261026) ...` -> 2026-10-26."""
    m = re.search(r"\((\d{8})\)", version)
    return datetime.datetime.strptime(m.group(1), "%Y%m%d").date() if m else None


def unpack(tarball: bytes, dest: Path) -> None:
    """The build's soloist, changelog and licences into dest, each replaced whole, so a running
    Soloist keeps its file and the next start takes the new one."""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(tarball)) as tar:
        names = {m.name.removeprefix("./"): m for m in tar.getmembers() if m.isfile()}
        if "soloist" not in names:
            raise RuntimeError("the Soloist download holds no soloist binary")
        for name in ("soloist", "CHANGELOG.md", "THIRD_PARTY_LICENSES.txt"):
            if name in names:
                new = dest / f".{name}.new"
                new.write_bytes(tar.extractfile(names[name]).read())
                new.chmod(0o755 if name == "soloist" else 0o644)
                new.replace(dest / name)


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as r:
        return r.read()


class Soloist:
    """One Soloist child, with its build in data/soloist/, its session in data/soloist-state/ and
    its cache in data/cache/; restarted on exit with a backoff, and on a new build once idle() says
    nothing plays. Builds may not be redistributed, so each machine fetches its own."""

    def __init__(self, data: Path, key_file: Path, name: str = "Q2", launcher: str | None = None):
        self.data = data
        # Nix's launcher, or in the container the build itself; "" runs the build directly
        self.launcher = shutil.which("soloist") if launcher is None else launcher
        self.key_file = key_file
        self.name = name
        self.home = data / "soloist"
        self.binary = self.home / "soloist"
        self.state = data / "soloist-state"
        self.port_file = self.state / "ws.port"
        self.proc: asyncio.subprocess.Process | None = None
        self.stale = False  # a newer build is in place: restart when idle

    def _command(self) -> tuple[list[str], dict]:
        """How to run the build: through `soloist` on PATH when there is one (Nix's launcher takes
        the binary from SOLOIST_BIN), else directly."""
        env = dict(os.environ, SOLOIST_BIN=str(self.binary.resolve()))
        return [self.launcher or str(self.binary)], env

    async def version(self) -> str:
        cmd, env = self._command()
        rc, out = await _output(*cmd, "--version", env=env)
        return out.strip() if rc == 0 else ""

    async def days_left(self) -> float | None:
        built = build_date(await self.version())
        if built is None:
            return None
        left = built + LIFETIME - datetime.date.today()
        return left.days

    async def fetch(self) -> None:
        url = URL.format(target())
        log.info("soloist: fetching %s", url)
        data = await asyncio.to_thread(_download, url)
        await asyncio.to_thread(unpack, data, self.home)
        log.info("soloist: %s", await self.version())

    async def ensure(self, force: bool = False) -> bool:
        """A build that runs, fetched when missing, expiring within REFRESH, or forced; True when a
        new one is in place. A failed refresh keeps the old build while it lasts."""
        have = self.binary.exists()
        left = await self.days_left() if have else None
        if have and not force and left is not None and left >= REFRESH.days:
            return False
        before = await self.version() if have else ""
        try:
            await self.fetch()
        except (OSError, RuntimeError, tarfile.TarError) as e:
            if not have:
                raise
            log.warning("soloist: refresh failed (%s); %s days left", e, left)
            return False
        return await self.version() != before

    async def _args(self) -> tuple[list[str], dict]:
        key = self.key_file.read_text().strip()
        cache = self.data / "cache"
        for d in (self.state, cache):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
        cmd, env = self._command()
        args = [*cmd, "-n", self.name, "-k", key, "-D", str(self.state), "-C", str(cache)]
        args += ["-z", "100", "-i", "100", "--ws", "127.0.0.1:0"]
        if await pipewire():
            args += ["-d", SINK]
        else:
            env["PULSE_SINK"] = SINK
        return args, env

    async def run(self, idle: Callable[[], bool] = lambda: True) -> None:
        """Soloist, for good: started, restarted on exit, fetched again on an expired build, and
        restarted on a refreshed one once idle()."""
        await self.ensure()
        checker = asyncio.create_task(self._refresh(idle))
        try:
            wait = 5
            while True:
                rc, ran = await self._once()
                if rc == EXPIRED:
                    log.warning("soloist: build expired")
                    if await self.ensure(force=True):
                        continue
                elif self.stale:
                    self.stale = False
                    continue
                wait = 5 if ran > 60 else min(wait * 2, 60)
                log.warning("soloist exited %s after %.0f s; again in %d s", rc, ran, wait)
                await asyncio.sleep(wait)
        finally:
            checker.cancel()

    async def _once(self) -> tuple[int, float]:
        args, env = await self._args()
        self.port_file.unlink(missing_ok=True)  # a crash leaves the last run's
        loop = asyncio.get_running_loop()
        started = loop.time()
        # Soloist takes its key only as -k, so it's on the command line; never log args
        self.proc = await asyncio.create_subprocess_exec(*args, env=env)
        try:
            rc = await self.proc.wait()
        except asyncio.CancelledError:
            self.proc.terminate()
            await self.proc.wait()
            raise
        return rc, loop.time() - started

    async def _refresh(self, idle: Callable[[], bool]) -> None:
        """Once a day, a build close to expiry is replaced; Soloist restarts on it when nothing
        plays, so a song isn't cut."""
        while True:
            await asyncio.sleep(CHECK_EVERY)
            if not await self.ensure():
                continue
            self.stale = True
            while self.stale and not idle():
                await asyncio.sleep(60)
            # Unless Soloist exited meanwhile, and so started on the new build already
            if self.stale and self.proc and self.proc.returncode is None:
                log.info("soloist: restarting on the new build")
                self.proc.terminate()
