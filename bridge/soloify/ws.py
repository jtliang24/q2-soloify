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

    RETRY = (0.2, 5.0)  # the first wait and the longest between tries

    def __init__(self, port_file: Path, on_event: Callable[[dict], None], host: str = "127.0.0.1"):
        self.port_file = port_file
        self.on_event = on_event
        self.host = host
        self.ws: aiohttp.ClientWebSocketResponse | None = None

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
            if event.get("type") == "error":
                log.warning("soloist: %s", event.get("message"))
            else:
                self.on_event(event)

    async def command(self, name: str, **fields) -> bool:
        """Sends a command; False when there's no connection. Its effects arrive as events."""
        if not self.connected:
            log.info("soloist: no websocket for %s", name)
            return False
        try:
            await self.ws.send_json({"type": "command", "command": name, **fields})
        except (aiohttp.ClientError, ConnectionError) as e:
            log.info("soloist: %s not sent: %s", name, e)
            return False
        return True
