"""Soloist's WebSocket API (developer.spotify.com/documentation/soloist/reference/websocket-api):
JSON events in, `{"type": "command", ...}` out. Soloist writes the port it took to ws.port in its
data directory and removes it on exit, so each connection reads it again."""

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path

import aiohttp

log = logging.getLogger("soloify")

# Sent to the event handler when the connection drops: nothing Soloist said before holds any more
GONE = {"type": "gone"}


class SoloistWS:
    """One connection at a time to Soloist's WebSocket, made again whenever it drops (a Soloist
    restart). Each event goes to on_event as it arrives; on connect Soloist sends auth_state, and
    playback_state when logged in, so the handler starts from a full picture each time."""

    TIMEOUT = 3
    RETRY = (0.2, 5.0)  # the first wait and the longest between tries

    def __init__(self, port_file: Path, on_event: Callable[[dict], None], host: str = "127.0.0.1"):
        self.port_file = port_file
        self.on_event = on_event
        self.host = host
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.lock = asyncio.Lock()
        self.pending = None
        self.expected = None
        self.command_name = None

    @property
    def connected(self) -> bool:
        return self.ws is not None and not self.ws.closed

    def _port(self) -> int | None:
        try:
            return int(self.port_file.read_text().strip())
        except (OSError, ValueError):
            return None

    async def run(self) -> None:
        wait = self.RETRY[0]
        async with aiohttp.ClientSession() as http:
            while True:
                port = self._port()
                if port:
                    try:
                        async with http.ws_connect(f"ws://{self.host}:{port}", heartbeat=30) as ws:
                            log.info("soloist: websocket on port %d", port)
                            self.ws, wait = ws, self.RETRY[0]
                            await self._read(ws)
                    except (aiohttp.ClientError, OSError) as e:
                        log.debug("soloist: websocket: %s", e)
                    finally:
                        if self.ws is not None:
                            self.ws = None
                            self._fail(ConnectionError("Soloist disconnected"))
                            self.on_event(GONE)
                await asyncio.sleep(wait)
                wait = min(wait * 2, self.RETRY[1])

    async def _read(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                event = json.loads(msg.data)
            except ValueError:
                log.warning("soloist: not JSON: %.80s", msg.data)
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "error":
                self._fail(ConnectionError("Soloist refused command"))
            else:
                pending = self.pending
                if pending and not pending.done() and kind == self.expected:
                    if kind != "command_result" or event.get("command") == self.command_name:
                        pending.set_result(event)
                self.on_event(event)

    def _fail(self, error):
        if self.pending and not self.pending.done():
            self.pending.set_exception(error)

    async def request(self, name: str, **fields) -> dict:
        """No request IDs exist in Soloist's API. One query/control may be outstanding.

        A timeout closes the connection so a late response cannot satisfy the next command.
        """
        async with self.lock:
            if not self.connected:
                raise ConnectionError("Soloist disconnected")
            self.expected = {
                "get_queue": "queue_changed",
                "get_state": "playback_state",
                "get_auth_state": "auth_state",
            }.get(name, "command_result")
            self.command_name = name
            self.pending = asyncio.get_running_loop().create_future()
            try:
                await self.ws.send_json({"type": "command", "command": name, **fields})
                return await asyncio.wait_for(self.pending, self.TIMEOUT)
            except (TimeoutError, asyncio.CancelledError):
                await self.ws.close()
                raise
            finally:
                self.pending = None

    async def command(self, name: str, **fields) -> bool:
        try:
            await self.request(name, **fields)
            return True
        except (aiohttp.ClientError, ConnectionError, TimeoutError):
            return False
