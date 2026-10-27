"""A fake Soloist WebSocket that replays tests/fixtures/soloist_ws.jsonl, a session recorded from
Soloist 1.3.10.16 (docs/step2.md): on connect, what Soloist sent then; on each command, the events
that followed the same command in the recording. Position timestamps are moved to now."""

import asyncio
import json
import time
from pathlib import Path

from aiohttp import WSMsgType, web

FIXTURE = Path(__file__).parent / "fixtures/soloist_ws.jsonl"
ALBUM = "spotify:album:2noRn2Aes5aoNVsU6iWThc"  # what the recording played


def recording() -> list[dict]:
    """Each line: t (s since the recording began), dir (in from Soloist, out to it) and msg."""
    return [json.loads(line) for line in FIXTURE.read_text().splitlines()]


def _replies() -> tuple[list[dict], list[tuple[dict, list[dict]]]]:
    connect: list[dict] = []
    after: list[tuple[dict, list[dict]]] = []
    current = connect
    for e in recording():
        if e["dir"] == "out":
            after.append((e["msg"], []))
            current = after[-1][1]
        else:
            current.append(e["msg"])
    return connect, after


def _now(event: dict) -> dict:
    event = json.loads(json.dumps(event))
    pos = event.get("position")
    if pos and "timestamp_ms" in pos:
        pos["timestamp_ms"] = int(time.time() * 1000)
    return event


class FakeSoloist:
    def __init__(self, folder: Path):
        self.port_file = folder / "ws.port"
        self.connect, self.after = _replies()
        self.used: set[int] = set()
        self.commands: list[dict] = []
        self.clients: list[web.WebSocketResponse] = []
        self.connections = 0
        self.runner: web.AppRunner | None = None

    async def start(self) -> "FakeSoloist":
        app = web.Application()
        app.router.add_get("/", self._ws)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = self.runner.addresses[0][1]
        self.port_file.parent.mkdir(parents=True, exist_ok=True)
        self.port_file.write_text(f"{port}\n")
        return self

    async def stop(self) -> None:
        """As Soloist's exit: the port file goes, and so do the connections."""
        self.port_file.unlink(missing_ok=True)
        for ws in list(self.clients):
            await ws.close()
        await self.runner.cleanup()

    async def _ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.clients.append(ws)
        self.connections += 1
        try:
            for event in self.connect[:2]:  # auth_state and playback_state
                await ws.send_json(_now(event))
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    cmd = json.loads(msg.data)
                    self.commands.append(cmd)
                    await self._reply(cmd, [ws])
        finally:
            self.clients.remove(ws)
        return ws

    def _find(self, cmd: dict) -> list[dict]:
        """The next unused reply to this command, else its last one, else a bare result."""
        last = None
        for i, (sent, events) in enumerate(self.after):
            if sent == cmd:
                if i not in self.used:
                    self.used.add(i)
                    return events
                last = events
        return last or [{"type": "command_result", "command": cmd.get("command")}]

    async def _reply(self, cmd: dict, to: list[web.WebSocketResponse]) -> None:
        for event in self._find(cmd):
            # Errors and query results go to the sender alone; the rest to every client
            for ws in to if event["type"] in ("command_result", "error") else self.clients:
                await ws.send_json(_now(event))

    async def inject(self, command: str, **fields) -> None:
        """A command from elsewhere (the phone, through Spotify's cloud), its events to all."""
        await self._reply({"type": "command", "command": command, **fields}, [])

    async def wait_for(self, command: str, timeout: float = 3) -> dict:
        """The first command of that name the bridge sent and isn't yet taken."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            for cmd in self.commands:
                if cmd.get("command") == command:
                    self.commands.remove(cmd)
                    return cmd
            await asyncio.sleep(0.01)
        raise AssertionError(f"no {command} command; got {self.commands}")
