"""The host relay against an in-process bridge, for the relay's tests."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

from soloify import audio, server

ROOT = Path(__file__).resolve().parent.parent
_built: Path | None = None


def relay_binary() -> Path:
    global _built
    if _built is None:
        build = [sys.executable, str(ROOT / "relay/build.py"), "--host"]
        out = subprocess.check_output(build, text=True)
        _built = Path(out.split()[0])
    return _built


class Bridge:
    """A bridge with the test source; keeps its sessions to look at."""

    def __init__(self, data: Path, inflight: int = 2000):
        self.identity = server.Identity.load(data)
        self.inflight = inflight
        self.sessions: list[audio.Session] = []
        self.server = server.Server(self.identity, self._session)
        self.listener = None
        self.port = 0

    def _encoder(self):
        return audio.Encoder(audio.ffmpeg_args("test", 160))

    async def _session(self, reader, writer):
        s = audio.Session(audio.Player(None), self._encoder, self.inflight)
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
        self.env = {**os.environ, **(env or {})}
        self.proc = None
        self.log: list[str] = []

    async def start(self):
        self.proc = await asyncio.create_subprocess_exec(
            str(relay_binary()),
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
