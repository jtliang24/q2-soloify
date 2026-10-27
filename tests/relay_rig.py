"""The host relay against an in-process bridge, for the relay's tests."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from soloify import audio, server
from soloify.player import Player

ROOT = Path(__file__).resolve().parent.parent
_built: Path | None = None


def relay_binary() -> Path:
    global _built
    if _built is None:
        build = [sys.executable, str(ROOT / "relay/build.py"), "--host"]
        out = subprocess.check_output(build, text=True)
        _built = Path(out.split()[0])
    return _built


SINK_NOWHERE = {"SOLOIFY_DECODE": "cat", "SOLOIFY_SINK": "cat >/dev/null"}
# The device's decoder line, with the host's ffmpeg
DECODE = (
    "ffmpeg -nostdin -loglevel error -probesize 32 -analyzeduration 0 -fflags nobuffer "
    "-f ogg -i pipe:0 -ac 2 -ar 44100 -f s16le pipe:1"
)
# A sink that plays in real time, as aplay does
REALTIME_SINK = f"{sys.executable} -I {ROOT / 'tests/fake_aplay.py'}"


class SlowEncoder(audio.Encoder):
    """An encoder that takes a while to start, as Soloist's resume and the pulse capture do."""

    def __init__(self, args: list[str], delay: float):
        super().__init__(args)
        self.delay = delay

    async def start(self) -> None:
        await asyncio.sleep(self.delay)
        await super().start()


class Bridge:
    """A bridge with the test source; keeps its sessions to look at."""

    def __init__(self, data: Path, inflight: int = 2000, start_delay: float = 0):
        self.identity = server.Identity.load(data)
        self.inflight = inflight
        self.start_delay = start_delay
        self.sessions: list[audio.Session] = []
        self.encoders = 0
        self.server = server.Server(self.identity, self._session)
        self.listener = None
        self.port = 0

    def _encoder(self):
        self.encoders += 1
        return SlowEncoder(audio.ffmpeg_args("test", 160), self.start_delay)

    async def _session(self, reader, writer):
        s = audio.Session(Player(), self._encoder, self.inflight)
        self.sessions.append(s)
        await s.run(reader, writer)

    async def start(self, port: int = 0):
        self.listener = await self.server.start("127.0.0.1", port)
        self.port = self.listener.sockets[0].getsockname()[1]
        return self

    async def stop(self):
        self.listener.close()
        if self.server.current:
            self.server.current.cancel()
            await asyncio.wait([self.server.current], timeout=5)
        await self.listener.wait_closed()

    def line(self, host: str = "127.0.0.1", port: int | None = None, pin: str | None = None) -> str:
        return f"{host} {port or self.port} {self.identity.token} sha256={pin or self.identity.pin}"


class Relay:
    """The relay as a child, with its log read line by line."""

    def __init__(self, folder: Path, remote: list[str], *args: str, env: dict | None = None):
        self.folder = folder
        (folder / "remote").write_text("".join(f"{line}\n" for line in remote))
        self.args = args
        # The device's ffmpeg isn't here: by default the sink takes everything at once
        self.env = {**os.environ, **SINK_NOWHERE, **(env or {})}
        self.proc = None
        self.log: list[str] = []
        self.binary: list[str] | None = None  # how to run it: the host build by default

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            *(self.binary or [str(relay_binary())]),
            "-v",
            *self.args,
            cwd=self.folder,
            env=self.env,
            stdin=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        return self

    async def expect(self, text: str, timeout: float = 10) -> str:
        """The next log line containing text."""

        async def find():
            while line := await self.proc.stderr.readline():
                self.log.append(line.decode().rstrip())
                if text in self.log[-1]:
                    return self.log[-1]
            raise AssertionError(f"relay exited before {text!r}: {self.log}")

        try:
            return await asyncio.wait_for(find(), timeout)
        except TimeoutError:
            raise AssertionError(f"no {text!r} in {timeout} s: {self.log}") from None

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()


async def until(check, timeout: float = 10, step: float = 0.05):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not check():
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(step)


def cpu_seconds(pid: int) -> float:
    """The CPU time (user and system) a process has used so far."""
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def stat(line: str, key: str) -> int:
    """A number from a relay stats line."""
    return int(line.split(f"{key}=")[1].split()[0])


class FreezingProxy:
    """A TCP proxy between relay and bridge that can stop carrying bytes, as a blocked link does."""

    def __init__(self, port: int):
        self.port = port
        self.flowing = asyncio.Event()
        self.flowing.set()
        self.listener = None
        self.tasks: set[asyncio.Task] = set()

    async def start(self):
        self.listener = await asyncio.start_server(self._client, "127.0.0.1", 0)
        return self.listener.sockets[0].getsockname()[1]

    async def _pipe(self, reader, writer):
        try:
            while data := await reader.read(65536):
                await self.flowing.wait()
                writer.write(data)
                await writer.drain()
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            writer.close()

    async def _client(self, reader, writer):
        up_r, up_w = await asyncio.open_connection("127.0.0.1", self.port)
        for t in (self._pipe(reader, up_w), self._pipe(up_r, writer)):
            task = asyncio.create_task(t)
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def freeze(self, seconds: float):
        self.flowing.clear()
        await asyncio.sleep(seconds)
        self.flowing.set()

    async def stop(self):
        for t in list(self.tasks):
            t.cancel()
        self.listener.close()
