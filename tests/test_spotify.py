"""OAuth and Web API failures use local HTTP only; no live rate limits are provoked."""

import asyncio
import hashlib
import json
import time
from urllib.parse import parse_qs, urlsplit

import aiohttp
import pytest
from aiohttp import web

from soloify import spotify


@pytest.fixture
async def endpoint():
    calls, replies = [], []

    async def handle(req):
        calls.append((req.method, req.path, dict(req.headers), await req.post()))
        reply = replies.pop(0)
        if isinstance(reply, float):
            await asyncio.sleep(reply)
            return web.json_response({})
        return reply

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    yield f"http://127.0.0.1:{runner.addresses[0][1]}", calls, replies
    await runner.cleanup()


def token(access="new", refresh="refresh"):
    return web.json_response({"access_token": access, "refresh_token": refresh, "expires_in": 3600})


def authorized(tmp_path, url, timeout=0.1):
    spotify.save(
        tmp_path / "oauth.json",
        {
            "client_id": "client",
            "access_token": "old",
            "refresh_token": "refresh",
            "expires_at": time.time() + 3600,
        },
    )
    return spotify.Spotify(tmp_path, base=url, token_url=url + "/token", timeout=timeout)


async def test_login_pkce_state_atomic_permissions(tmp_path, endpoint):
    url, calls, replies = endpoint
    ready = asyncio.Queue()
    replies.append(token())
    task = asyncio.create_task(
        spotify.login(
            tmp_path, "client", port=18888, token_url=url + "/token", announce=ready.put_nowait
        )
    )
    try:
        auth = parse_qs(urlsplit(await ready.get()).query)
        assert auth["redirect_uri"] == [spotify.REDIRECT]
        assert set(spotify.SCOPES.split()) <= set(auth["scope"][0].split())
        async with aiohttp.ClientSession() as http:
            callback = "http://127.0.0.1:18888/callback"
            async with http.get(callback, params={"state": "wrong", "code": "secret"}) as r:
                assert r.status == 400
            assert not task.done()
            async with http.get(
                callback, params={"state": auth["state"][0], "code": "secret"}
            ) as r:
                assert r.status == 200
        await task
        fields = calls[0][3]
        challenge = (
            spotify.base64.urlsafe_b64encode(
                hashlib.sha256(fields["code_verifier"].encode()).digest()
            )
            .rstrip(b"=")
            .decode()
        )
        assert auth["code_challenge"] == [challenge]
        assert fields["code"] == "secret"
        path = tmp_path / "oauth.json"
        assert path.stat().st_mode & 0o777 == 0o600
        assert json.loads(path.read_text())["client_id"] == "client"
        assert not list(tmp_path.glob(".oauth-*"))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_login_denied_timeout_and_missing_client(tmp_path):
    ready = asyncio.Queue()
    task = asyncio.create_task(
        spotify.login(tmp_path, "client", port=18888, announce=ready.put_nowait)
    )
    auth = parse_qs(urlsplit(await ready.get()).query)
    async with aiohttp.ClientSession() as http:
        async with http.get(
            "http://127.0.0.1:18888/callback",
            params={"state": auth["state"][0], "error": "access_denied"},
        ):
            pass
    with pytest.raises(spotify.APIError, match="denied"):
        await task
    with pytest.raises(spotify.APIError, match="timed out"):
        await spotify.login(tmp_path, "client", port=18888, timeout=0.01, announce=lambda _: None)
    with pytest.raises(spotify.APIError, match="client-id"):
        await spotify.login(tmp_path, None)
    assert not (tmp_path / "oauth.json").exists()


async def test_refresh_concurrent_expiry_and_401(tmp_path, endpoint):
    url, calls, replies = endpoint
    api = authorized(tmp_path, url, timeout=2)
    api.auth["expires_at"] = 0
    replies.extend([token(), *[web.json_response({"ok": True}) for _ in range(5)]])
    try:
        assert all(await asyncio.gather(*[api.request("GET", "/me") for _ in range(5)]))
        assert sum(c[1] == "/token" for c in calls) == 1
        assert all(c[2].get("Authorization") == "Bearer new" for c in calls[1:])
        replies.extend([web.Response(status=401), token("next"), web.json_response({"ok": 1})])
        assert await api.request("GET", "/me") == {"ok": 1}
        assert api.auth["access_token"] == "next"
        # Late concurrent 401s for the old generation must not refresh the new token again.
        await asyncio.gather(*[api.refresh("new") for _ in range(5)])
        assert len(calls) == 9
    finally:
        await api.close()


@pytest.mark.parametrize(
    ("code", "status"), [(401, "refused"), (403, "refused"), (404, "unavailable"), (503, "offline")]
)
async def test_status_mapping(tmp_path, endpoint, code, status):
    url, calls, replies = endpoint
    api = authorized(tmp_path, url)
    replies.extend([web.Response(status=code), web.Response(status=400)])
    try:
        with pytest.raises(spotify.APIError) as e:
            await api.request("GET", "/me")
        assert e.value.status == status
        assert (tmp_path / "oauth.json").exists()
    finally:
        await api.close()


async def test_cooldown_timeout_malformed_missing_authorization(tmp_path, endpoint):
    url, calls, replies = endpoint
    api = authorized(tmp_path, url)
    try:
        replies.append(web.Response(status=429, headers={"Retry-After": "60"}))
        for _ in range(2):
            with pytest.raises(spotify.APIError) as e:
                await api.request("GET", "/me")
            assert e.value.status == "throttled"
        assert len(calls) == 1
        api.cooldown = 0
        replies.append(0.2)
        with pytest.raises(spotify.APIError) as e:
            await api.request("GET", "/me")
        assert e.value.status == "offline"
        for body in ("not json", "[]"):
            replies.append(web.Response(text=body))
            with pytest.raises(spotify.APIError) as e:
                await api.request("GET", "/me")
            assert e.value.status == "error"
        api.auth = {}
        with pytest.raises(spotify.APIError, match="soloify login"):
            await api.request("GET", "/me")
    finally:
        await api.close()


async def test_retries_401_only_once_and_rejects_bad_token_response(tmp_path, endpoint):
    url, calls, replies = endpoint
    api = authorized(tmp_path, url)
    try:
        replies.extend([web.Response(status=401), token(), web.Response(status=401)])
        with pytest.raises(spotify.APIError) as e:
            await api.request("GET", "/me")
        assert e.value.status == "refused" and len(calls) == 3
        api.auth["expires_at"] = 0
        replies.append(web.json_response({"access_token": "x", "expires_in": "bad"}))
        with pytest.raises(spotify.APIError, match="Malformed token"):
            await api.request("GET", "/me")
        assert api.auth["access_token"] == "new"
    finally:
        await api.close()
