"""PKCE authorization and bounded asynchronous Spotify Web API access."""

import asyncio
import base64
import hashlib
import json
import math
import os
import secrets
import tempfile
import time
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
from aiohttp import web

REDIRECT = "http://127.0.0.1:8888/callback"
TOKEN = "https://accounts.spotify.com/api/token"
SCOPES = (
    "user-library-read user-follow-read playlist-read-private playlist-read-collaborative "
    "user-read-playback-state user-modify-playback-state"
)
LOGIN = "Run soloify login --client-id ID on the server to authorize browsing"


class APIError(Exception):
    def __init__(self, status, message, code=0):
        super().__init__(message)
        self.status, self.message, self.code = status, message, code


def save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".oauth-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class Spotify:
    def __init__(self, data: Path, base="https://api.spotify.com/v1", token_url=TOKEN, timeout=4):
        self.path = data / "oauth.json"
        try:
            self.auth = json.loads(self.path.read_text())
            if not isinstance(self.auth, dict):
                self.auth = {}
        except (OSError, ValueError):
            self.auth = {}
        self.base, self.token_url, self.timeout = base, token_url, timeout
        self.http = None
        self.lock = asyncio.Lock()
        self.cooldown = 0

    async def close(self):
        if self.http:
            await self.http.close()

    async def _http(self, method, url, **kw):
        if self.cooldown > time.monotonic():
            raise APIError("throttled", f"Retry in {math.ceil(self.cooldown - time.monotonic())} s")
        if self.http is None:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.timeout))
        try:
            async with self.http.request(method, url, **kw) as r:
                if r.status == 429:
                    try:
                        delay = max(1, float(r.headers.get("Retry-After", "1")))
                        if not math.isfinite(delay):
                            delay = 60
                    except ValueError:
                        delay = 60
                    self.cooldown = time.monotonic() + delay
                    raise APIError("throttled", f"Retry in {math.ceil(delay)} s", 429)
                if r.status >= 400:
                    status = "refused" if r.status in (400, 401, 403) else "unavailable"
                    if r.status >= 500:
                        status = "offline"
                    raise APIError(status, f"Spotify returned {r.status}", r.status)
                if r.status == 204:
                    return {}
                raw = bytearray()
                async for chunk in r.content.iter_chunked(65536):
                    raw.extend(chunk)
                    if len(raw) > 2 * 1024 * 1024:
                        raise APIError("error", "Spotify response too large")
                obj = json.loads(raw)
                if not isinstance(obj, dict):
                    raise ValueError
                return obj
        except (aiohttp.ClientError, TimeoutError, OSError) as e:
            raise APIError("offline", "Can't reach Spotify") from e
        except (ValueError, UnicodeError) as e:
            raise APIError("error", "Malformed Spotify response") from e

    async def tokens(self, fields):
        obj = await self._http("POST", self.token_url, data=fields)
        if not isinstance(obj.get("access_token"), str) or not obj["access_token"]:
            raise APIError("refused", LOGIN)
        try:
            expires = float(obj["expires_in"])
            if not math.isfinite(expires) or expires <= 0:
                raise ValueError
        except (KeyError, TypeError, ValueError) as e:
            raise APIError("error", "Malformed token response") from e
        self.auth = {**self.auth, **obj, "expires_at": time.time() + expires}
        save(self.path, self.auth)

    async def refresh(self, rejected=None):
        async with self.lock:
            if rejected is not None and self.auth.get("access_token") != rejected:
                return
            if rejected is None and self.auth.get("expires_at", 0) > time.time() + 15:
                return
            if not self.auth.get("refresh_token") or not self.auth.get("client_id"):
                raise APIError("refused", LOGIN)
            await self.tokens(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": self.auth["refresh_token"],
                    "client_id": self.auth["client_id"],
                }
            )

    async def request(self, method, path, **kw):
        if not path.startswith("/") or path.startswith("//"):
            raise APIError("bad", "Invalid API path")
        await self.refresh()
        for attempt in range(2):
            token = self.auth.get("access_token")
            try:
                return await self._http(
                    method, self.base + path, headers={"Authorization": f"Bearer {token}"}, **kw
                )
            except APIError as e:
                if e.code != 401 or attempt:
                    raise
                await self.refresh(token)


async def login(data, client_id, timeout=180, *, token_url=TOKEN, port=8888, announce=print):
    if not client_id:
        raise APIError("refused", "Supply --client-id ID or SPOTIFY_CLIENT_ID")
    verifier, state = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    result = asyncio.get_running_loop().create_future()

    async def callback(req):
        if not secrets.compare_digest(req.query.get("state", ""), state):
            return web.Response(status=400, text="Invalid OAuth state")
        if not result.done():
            if req.query.get("error"):
                result.set_exception(APIError("refused", "Spotify authorization denied"))
            elif req.query.get("code"):
                result.set_result(req.query["code"])
            else:
                return web.Response(status=400, text="Missing authorization code")
        return web.Response(text="Return to soloify on the server.")

    app = web.Application()
    app.router.add_get("/callback", callback)
    runner = web.AppRunner(app, access_log=None)  # callback contains authorization secrets
    api = Spotify(data, token_url=token_url)
    try:
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", port).start()
        announce(
            "https://accounts.spotify.com/authorize?"
            + urlencode(
                {
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": REDIRECT,
                    "scope": SCOPES,
                    "state": state,
                    "code_challenge_method": "S256",
                    "code_challenge": challenge.decode(),
                }
            )
        )
        try:
            code = await asyncio.wait_for(result, timeout)
        except TimeoutError as e:
            raise APIError("refused", "Authorization timed out; run soloify login again") from e
        api.auth = {"client_id": client_id}
        await api.tokens(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT,
                "client_id": client_id,
                "code_verifier": verifier,
            }
        )
    finally:
        await runner.cleanup()
        await api.close()
