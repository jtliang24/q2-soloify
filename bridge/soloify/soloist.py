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
import tempfile
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
RENEW_AGE = datetime.timedelta(days=60)  # measured from build date, never download date
REFRESH = LIFETIME - RENEW_AGE  # remaining lifetime at renewal eligibility
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
    try:
        out, _ = await proc.communicate()
    except asyncio.CancelledError:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
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
    try:
        return datetime.datetime.strptime(m.group(1), "%Y%m%d").date() if m else None
    except ValueError:
        return None


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
        # In the container PATH points at this binary, not a Nix loader. Validation must
        # execute the staged binary itself instead of accidentally querying the old one.
        if self.launcher and Path(self.launcher).resolve() == self.binary.resolve():
            self.launcher = ""
        self.state = data / "soloist-state"
        self.port_file = self.state / "ws.port"
        self.proc: asyncio.subprocess.Process | None = None
        self.stale = False  # a validated candidate waits for an idle point
        self.pending = self.home / ".pending"
        self._renew_lock = asyncio.Lock()

    def _command(self) -> tuple[list[str], dict]:
        """How to run the build: through `soloist` on PATH when there is one (Nix's launcher takes
        the binary from SOLOIST_BIN), else directly."""
        env = dict(os.environ, SOLOIST_BIN=str(self.binary.resolve()))
        return [self.launcher or str(self.binary)], env

    async def version(self, binary: Path | None = None) -> str:
        cmd, env = self._command()
        if binary is not None:
            env["SOLOIST_BIN"] = str(binary.resolve())
            if not self.launcher:
                cmd = [str(binary)]
        try:
            rc, out = await asyncio.wait_for(_output(*cmd, "--version", env=env), 15)
        except (OSError, TimeoutError):
            return ""
        return out.strip() if rc == 0 else ""

    async def days_left(self) -> float | None:
        built = build_date(await self.version())
        if built is None:
            return None
        left = built + LIFETIME - datetime.date.today()
        return left.days

    async def status(self) -> dict:
        """Build lifetime diagnostics without downloading or changing the installation."""
        version = await self.version() if self.binary.exists() else ""
        built = build_date(version)
        expires = built + LIFETIME if built else None
        return {
            "version": version,
            "build_date": str(built) if built else None,
            "estimated_expiry": str(expires) if expires else None,
            "days_left": (expires - datetime.date.today()).days if expires else None,
            "renewal_eligible": built is None or datetime.date.today() - built >= RENEW_AGE,
            "automatic_renewal": "on bridge startup and every 24 hours; activates when idle",
            "activation_pending": self.stale or (self.pending / "soloist").exists(),
        }

    async def fetch(self, archive: bytes | None = None) -> None:
        """Stage and validate before touching the usable build. Only newer, unexpired builds
        qualify. Archives only contribute the three known files, never session data."""
        url = URL.format(target())
        log.info("soloist: fetching %s", url)
        data = archive if archive is not None else await asyncio.to_thread(_download, url)
        self.home.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".validate-", dir=self.home) as folder:
            stage = Path(folder)
            await asyncio.to_thread(unpack, data, stage)
            await self._validate(stage)
            shutil.rmtree(self.pending, ignore_errors=True)
            self.pending.mkdir(mode=0o700)
            for item in stage.iterdir():
                item.replace(self.pending / item.name)

    async def _validate(self, stage: Path) -> None:
        """Validate staged builds again after restart and while waiting for idle."""
        version = await self.version(stage / "soloist")
        built = build_date(version)
        today = datetime.date.today()
        if (
            not re.search(r"^soloist \d+(?:\.\d+)* build \d+", version)
            or built is None
            or not today - LIFETIME < built <= today
        ):
            raise RuntimeError("download has an invalid or expired build date")
        if self.binary.exists():
            old = await self.version()
            previous = build_date(old)

            # Date alone isn't enough: Spotify can issue multiple builds on one day.
            def order(value: str) -> tuple:
                match = re.search(r"soloist (\d+(?:\.\d+)*) build (\d+)", value)
                return (tuple(map(int, match[1].split("."))), int(match[2])) if match else ()

            if (
                version == old
                or (stage / "soloist").read_bytes() == self.binary.read_bytes()
                or (previous and built < previous)
                or (order(old) and order(version) <= order(old) and built == previous)
                or (order(old) and order(version) and order(version) < order(old))
            ):
                raise RuntimeError("download is identical to or older than the installed build")

    def activate(self) -> None:
        """Install only staged application files; retain state, credentials and bridge identity."""
        for item in self.pending.iterdir():
            item.replace(self.home / item.name)
        self.pending.rmdir()
        self.stale = False

    async def ensure(self, force: bool = False) -> bool:
        """Check at build age 60 days, including startup. A failed renewal keeps a usable build;
        a staged replacement waits for idle when a child is running."""
        async with self._renew_lock:
            have = self.binary.exists()
            left = await self.days_left() if have else None
            if (self.pending / "soloist").exists():
                try:
                    await self._validate(self.pending)
                except (OSError, RuntimeError):
                    shutil.rmtree(self.pending)
                    self.stale = False
                    log.warning("soloist: staged replacement is no longer usable")
                else:
                    if self.proc and self.proc.returncode is None:
                        self.stale = True
                    else:
                        self.activate()
                    return True
            if have and not force and left is not None and left > REFRESH.days:
                log.info("soloist: %s days left; renewal begins at build age 60 days", left)
                return False
            try:
                await self.fetch()
            except (OSError, RuntimeError, ValueError, EOFError, tarfile.TarError) as e:
                if not have or left is None or left <= 0:
                    raise RuntimeError("no usable Soloist replacement available") from e
                log.warning(
                    "soloist: renewal unavailable (%s); %s days left; retry in 24h", e, left
                )
                return False
            if self.proc and self.proc.returncode is None:
                self.stale = True
            else:
                self.activate()
            log.info(
                "soloist: validated replacement%s",
                "; waiting for idle" if self.stale else " installed",
            )
            return True

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
                if self.stale:
                    if self.pending.exists():
                        self.activate()
                    self.stale = False
                    continue
                if rc == EXPIRED:
                    log.warning("soloist: build expired")
                    if await self.ensure(force=True):
                        if self.stale:
                            self.activate()
                        continue
                    raise RuntimeError("Soloist expired; no usable replacement available")
                wait = 5 if ran > 60 else min(wait * 2, 60)
                log.warning("soloist exited %s after %.0f s; again in %d s", rc, ran, wait)
                await asyncio.sleep(wait)
        finally:
            checker.cancel()
            await asyncio.gather(checker, return_exceptions=True)

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
            try:
                renewed = await self.ensure()
            except RuntimeError:
                log.error("soloist: no usable replacement; retry in 24h")
                continue
            if not renewed or not self.stale:
                continue
            next_check = asyncio.get_running_loop().time() + CHECK_EVERY
            while self.stale and not idle():
                await asyncio.sleep(60)
                if asyncio.get_running_loop().time() >= next_check:
                    # Continuous playback must not postpone lifetime checks indefinitely.
                    try:
                        await self.ensure(force=True)
                    except RuntimeError:
                        log.error("soloist: no usable replacement; retry in 24h")
                    next_check = asyncio.get_running_loop().time() + CHECK_EVERY
            # Unless Soloist exited meanwhile, and so started on the new build already
            if self.stale and self.proc and self.proc.returncode is None:
                log.info("soloist: restarting on the new build")
                self.activate()
                self.proc.terminate()
                # _once reports the deliberate restart through this flag.
                self.stale = True
