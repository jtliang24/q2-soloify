"""Q2SPOT/1 browsing. Text and upstream cursors never become URLs supplied by clients."""

import asyncio
import base64
import hashlib
import json
import logging
import re
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .spotify import APIError
from .state import Track

log = logging.getLogger("soloify")

OPS = (
    "caps account playlists playlist liked albums artists album artist search play queue skip "
    "enqueue settings shuffle repeat"
).split()
CHANGES = {"play", "skip", "enqueue", "shuffle", "repeat"}
ARITY = {
    "playlist": (1, 2),
    "album": (1, 2),
    "artist": (1, 2),
    "search": (2, 3),
    "play": (1, 3),
    "skip": (2, 2),
    "enqueue": (1, 1),
    "shuffle": (1, 1),
    "repeat": (1, 1),
    **{k: (0, 1) for k in ("liked", "albums", "artists", "playlists")},
}


def escape(s):
    return str(s).replace("%", "%25").replace("\t", "%09").replace("\n", "%0A").replace("\r", "%0D")


def unescape(s):
    if re.search(r"%(?![0-9a-fA-F]{2})", s):
        raise ValueError
    # Decode escapes as bytes so escaped UTF-8 is accepted without corrupting literal Unicode.
    raw = re.sub(rb"%([0-9a-fA-F]{2})", lambda m: bytes([int(m[1], 16)]), s.encode())
    text = raw.decode()
    if "\0" in text:
        raise ValueError
    return text


def parse(raw):
    if len(raw) > 8192 or b"\0" in raw or b"\r" in raw:
        raise ValueError
    lines = raw.decode().split("\n")
    if len(lines) != 3 or lines[0] != "Q2SPOT/1" or lines[2]:
        raise ValueError
    fields = [unescape(x) for x in lines[1].split("\t")]
    if len(fields) < 2 or not re.fullmatch(r"[0-9]{1,10}", fields[0]):
        raise ValueError
    return fields[0], fields[1], fields[2:]


def clipped(s, n):
    return str(s or "").encode()[:n].decode(errors="ignore")


def response(id, status="ok", message="", cursor="", rows=()):
    out = f"Q2SPOT/1\n{id}\t{status}\t{escape(clipped(message, 120))}\t{escape(cursor)}\n"
    for row in rows:
        if len(row) != 6:
            raise ValueError("row must have six fields")
        row = list(row)
        row[2:4] = [clipped(v, 255) for v in row[2:4]]
        out += "\t".join(escape(v) for v in row) + "\n"
    raw = out.encode()
    if len(raw) > 65536 or len(rows) > 50:
        raise APIError("error", "Browse page exceeds protocol limits")
    return raw


def entity(item, kind=None, context="", position=""):
    kind = kind or item.get("type", "track")
    subtitle = ", ".join(a.get("name", "") for a in item.get("artists", []))
    if kind == "playlist":
        owner = item.get("owner") or {}
        subtitle = owner.get("display_name") or owner.get("id", "")
    return (kind, item.get("uri", ""), item.get("name", ""), subtitle, context, position)


def uri_id(uri, kind):
    m = re.fullmatch(r"spotify:" + kind + r":([A-Za-z0-9]+)", uri)
    if not m:
        raise APIError("bad", f"Expected Spotify {kind} URI")
    return m[1]


def playable(t):
    return (
        isinstance(t, dict)
        and (t.get("uri") or "").startswith("spotify:track:")
        and not (t.get("is_local") or t.get("is_playable") is False or t.get("restrictions"))
    )


class Browser:
    def __init__(self, api, player, data):
        self.api, self.player, self.data = api, player, data
        self.user = None
        # Only the most recent 16 fetched liked pages are retained for bounded continuation.
        self.liked_pages = {}
        self.change_lock = asyncio.Lock()

    async def handle(self, raw, boundary=None):
        # Preserve a valid firmware ID even when a later argument is malformed UTF-8.
        match = re.match(rb"Q2SPOT/1\n([0-9]{1,10})\t", raw)
        id = match[1].decode() if match else "0"
        try:
            id, op, args = parse(raw)
        except (ValueError, UnicodeError):
            return response(id, "bad", "Malformed request")
        try:
            lo, hi = ARITY.get(op, (0, 0))
            if op in OPS and not lo <= len(args) <= hi:
                raise APIError("bad", "Invalid operation arguments")
            async with asyncio.timeout(10):
                if op in CHANGES:
                    async with self.change_lock:
                        if boundary:
                            result = await boundary(
                                lambda: self.prepare(op, args), op in ("play", "skip")
                            )
                        else:
                            result = await self.dispatch(op, args)
                else:
                    result = await self.dispatch(op, args)
            rows, cursor, message = result
            return response(id, cursor=cursor, rows=rows, message=message)
        except (ValueError, UnicodeError, TypeError, KeyError, AttributeError, IndexError):
            return response(id, "error", "Malformed Spotify response or operation arguments")
        except APIError as e:
            return response(id, e.status, e.message)
        except (TimeoutError, ConnectionError):
            return response(id, "offline", "Browse operation timed out or Soloist disconnected")

    async def profile(self):
        if self.user is None:
            self.user = await self.api.request("GET", "/me")
        return self.user

    def cursor(self, op, args, value):
        signature = hashlib.sha256(json.dumps([op, args]).encode()).hexdigest()[:16]
        return base64.urlsafe_b64encode(json.dumps([signature, value]).encode()).decode()

    def continuation(self, op, args, cursor):
        if not cursor:
            return {}
        if len(cursor) > 1024:
            raise APIError("bad", "Invalid operation arguments")
        try:
            sig, value = json.loads(base64.urlsafe_b64decode(cursor))
        except (ValueError, TypeError) as e:
            raise APIError("bad", "Invalid browse cursor") from e
        expected = hashlib.sha256(json.dumps([op, args]).encode()).hexdigest()[:16]
        if sig != expected or not isinstance(value, dict) or set(value) - {"after", "offset"}:
            raise APIError("bad", "Invalid operation arguments")
        if "offset" in value and (not str(value["offset"]).isdigit()):
            raise APIError("bad", "Invalid operation arguments")
        return value

    async def page(self, op, args):
        fixed = 2 if op == "search" else 1 if op in ("playlist", "album", "artist") else 0
        original = args[:fixed]
        params = self.continuation(op, original, args[fixed] if len(args) > fixed else "")
        params["limit"] = 10 if op in ("search", "artist") else 50
        context, kind = "", "track"
        if op in ("playlists", "liked", "albums", "artists"):
            path = {
                "playlists": "/me/playlists",
                "liked": "/me/tracks",
                "albums": "/me/albums",
                "artists": "/me/following",
            }[op]
            kind = {"playlists": "playlist", "albums": "album", "artists": "artist"}.get(op, kind)
            if op == "artists":
                params["type"] = "artist"
            if op == "liked":
                user = await self.profile()
                if not user.get("id"):
                    raise APIError("unavailable", "Spotify profile has no account ID")
                context = f"spotify:user:{user['id']}:collection"
        elif op == "search":
            if args[0] not in ("tracks", "albums", "artists", "playlists") or not args[1]:
                raise APIError("bad", "Invalid operation arguments")
            kind = args[0][:-1]
            path, params["q"], params["type"] = "/search", args[1], kind
            log.info("search category=%s term_bytes=%d", args[0], len(args[1].encode()))
        else:
            context = args[0]
            ident = uri_id(context, "artist" if op == "artist" else op)
            path = f"/{op}s/{ident}/" + (
                "items" if op == "playlist" else "albums" if op == "artist" else "tracks"
            )
            if op == "artist":
                context, kind = "", "album"
        try:
            obj = await self.api.request("GET", path, params=params)
        except APIError as e:
            if op == "playlist" and e.code in (403, 404):
                raise APIError("unavailable", "Spotify restricts this playlist's contents") from e
            raise
        if op == "artists":
            obj = obj["artists"]
        if op == "search":
            obj = obj[args[0]]
        items = obj.get("items")
        if not isinstance(items, list):
            raise APIError(
                "unavailable" if op == "playlist" else "error", "Spotify omitted contents"
            )
        offset = int(params.get("offset", 0))
        rows = []
        for i, item in enumerate(items[:50]):
            if item is None:
                continue
            if op in ("liked", "playlist"):
                item = item.get("item", item.get("track"))
            elif op == "albums":
                item = item.get("album")
            if not isinstance(item, dict) or (kind == "track" and not playable(item)):
                continue
            row = entity(item, kind, context, str(offset + i) if context else "")
            try:
                response("9999999999", cursor="x" * 1024, rows=rows + [row])
            except APIError:
                if not rows:
                    raise
                value = {"after": items[i - 1]["id"]} if op == "artists" else {"offset": offset + i}
                if op == "liked":
                    self.keep_liked(offset, rows)
                return rows, self.cursor(op, original, value), ""
            rows.append(row)
        next_cursor = ""
        if obj.get("next"):
            q = parse_qs(urlsplit(obj["next"]).query)
            value = {k: q[k][0] for k in ("offset", "after") if k in q}
            if not value:
                raise APIError("error", "Spotify omitted pagination position")
            next_cursor = self.cursor(op, original, value)
        if op == "liked":
            self.keep_liked(offset, rows)
        return (
            rows,
            next_cursor,
            "Artist albums (top tracks unavailable in development mode)" if op == "artist" else "",
        )

    def keep_liked(self, offset, rows):
        self.liked_pages.pop(offset, None)
        self.liked_pages[offset] = rows
        while len(self.liked_pages) > 16:
            del self.liked_pages[next(iter(self.liked_pages))]

    async def local(self, command, **fields):
        if not self.player.ws or not self.player.state.logged_in:
            raise APIError("login", "Pair Soloist on the server and choose Q2 in Spotify")
        return await self.player.ws.request(command, **fields)

    async def dispatch(self, op, args):
        empty = ([], "", "")
        if op == "caps":
            return [("op", name, "", "", "", "") for name in OPS], "", f"soloify {__version__}"
        if op in ("signout", "downloaded", "download", "downloadstatus", "playdownload"):
            raise APIError(
                "unsupported",
                "Sign out on the server" if op == "signout" else "Not available through Soloist",
            )
        if op not in OPS:
            raise APIError("unsupported", "Operation not supported by Soloify")
        if op == "account":
            p = await self.profile()
            if not self.player.state.logged_in:
                raise APIError("login", "Pair Soloist on the server and choose Q2 in Spotify")
            return (
                [
                    (
                        "account",
                        p.get("uri") or "",
                        p.get("display_name") or p.get("id") or "",
                        p.get("product") or "",
                        "",
                        "",
                    )
                ],
                "",
                "",
            )
        if op in (
            "playlists",
            "playlist",
            "liked",
            "albums",
            "artists",
            "album",
            "artist",
            "search",
        ):
            return await self.page(op, args)
        if op == "settings":
            s = self.player.state
            return (
                [
                    ("setting", "shuffle", "on" if s.shuffle else "off", "", "", ""),
                    ("setting", "repeat", s.repeat, "", "", ""),
                ],
                "",
                "",
            )
        if op == "shuffle":
            if args[0] not in ("on", "off"):
                raise APIError("bad", "Invalid operation arguments")
            await self.local("set_shuffle", enabled=args[0] == "on")
        elif op == "repeat":
            mode = args[0]
            if mode not in ("off", "context", "track"):
                raise APIError("bad", "Invalid operation arguments")
            first = "context" if mode == "track" else "track"
            await self.local("set_repeat_" + first, enabled=False)
            await self.local(
                "set_repeat_" + ("track" if mode == "track" else "context"), enabled=mode != "off"
            )
        elif op == "enqueue":
            uri_id(args[0], "track")
            await self.local("add_to_queue", uri=args[0])
        elif op in ("queue", "skip"):
            q = await self.local("get_queue", limit=50)
            upcoming = q["upcoming"]
            if op == "queue":
                rows = []
                for i, entry in enumerate(upcoming[:50]):
                    t = Track.from_entity(entry.get("item") or {})
                    if t:
                        rows.append(("track", t.uri, t.title, t.artist, "", str(i)))
                return rows, "", ""
            return await (await self.prepare_skip(args, upcoming))()
        elif op == "play":
            return await (await self.prepare_play(args))()
        return empty

    async def prepare(self, op, args):
        """Read-only preflight under the session control lock, while capture keeps running."""
        if op == "play":
            return await self.prepare_play(args)
        if op == "skip":
            q = await self.local("get_queue", limit=50)
            return await self.prepare_skip(args, q["upcoming"])
        return lambda: self.dispatch(op, args)

    @staticmethod
    def check_queue(upcoming, expected):
        first = (upcoming or [{}])[0]
        if first.get("item", {}).get("uri") != expected.get("item", {}).get("uri") or first.get(
            "uid"
        ) != expected.get("uid"):
            raise APIError("unavailable", "Queue changed; open Queue again")

    async def prepare_skip(self, args, upcoming):
        if not args[0].isdigit() or not 0 <= int(args[0]) < min(len(upcoming), 50):
            raise APIError("unavailable", "Queue position changed; open Queue again")
        pos = int(args[0])
        if upcoming[pos].get("item", {}).get("uri") != args[1]:
            raise APIError("unavailable", "Queue changed; open Queue again")
        expected = upcoming[: pos + 1]
        fresh = await self.local("get_queue", limit=50)
        self.check_queue(fresh.get("upcoming"), expected[0])

        async def execute():
            for entry in expected:
                # Capture drainage takes time: also check immediately before EVERY mutation.
                fresh = await self.local("get_queue", limit=50)
                self.check_queue(fresh.get("upcoming"), entry)
                await self.local("skip_next")
            return [], "", ""

        return execute

    async def prepare_play(self, args):
        context = args[0]
        body = {}
        if len(args) == 1:
            if context.startswith("spotify:track:"):
                uri_id(context, "track")
                body["uris"] = [context]
            else:
                if not re.fullmatch(r"spotify:(album|playlist|artist):[A-Za-z0-9]+", context):
                    raise APIError("bad", "Invalid operation arguments")
                body["context_uri"] = context
        else:
            if len(args) != 3 or not args[1].isdigit():
                raise APIError("bad", "Invalid operation arguments")
            uri_id(args[2], "track")
            if not re.fullmatch(
                r"spotify:(album|playlist):[A-Za-z0-9]+|spotify:user:[A-Za-z0-9]+:collection",
                context,
            ):
                raise APIError("bad", "Invalid operation arguments")
            body = {"context_uri": context, "offset": {"position": int(args[1])}}
        uris = []
        if context.endswith(":collection"):
            rows = next(
                (
                    r
                    for r in reversed(list(self.liked_pages.values()))
                    if any(x[4] == context and x[5] == args[1] and x[1] == args[2] for x in r)
                ),
                [],
            )
            uris = [r[1] for r in rows if int(r[5]) >= int(args[1])]
            if not uris:
                raise APIError("unavailable", "Open Liked Songs again before playing")
        # Soloist persists its Connect ID; match it to the Web API, never to the display name.
        try:
            device = (self.data / "soloist-state/.device_id").read_text().strip()
        except OSError as e:
            raise APIError(
                "unavailable", "Soloist device ID is missing; pair it on the server"
            ) from e
        devices = await self.api.request("GET", "/me/player/devices")
        if not device or not any(d.get("id") == device for d in devices.get("devices", [])):
            raise APIError("unavailable", "Soloist device is unavailable; activate it in Spotify")

        async def execute():
            message = ""
            try:
                await self.api.request(
                    "PUT", "/me/player/play", params={"device_id": device}, json=body
                )
            except APIError as e:
                if not context.endswith(":collection") or e.code not in (400, 403, 404):
                    raise
                await self.api.request(
                    "PUT", "/me/player/play", params={"device_id": device}, json={"uris": uris}
                )
                message = "Liked Songs plays to the end of this fetched page only"
            return [], "", message

        return execute
