import asyncio

import pytest

from soloify import browse, spotify
from soloify.player import Player


def request(op, *args, id="7"):
    return ("Q2SPOT/1\n" + "\t".join(map(browse.escape, (id, op, *args))) + "\n").encode()


def decode(raw):
    lines = raw.decode().splitlines()
    fields = [[browse.unescape(v) for v in line.split("\t")] for line in lines[1:]]
    assert len(fields[0]) == 4 and all(len(row) == 6 for row in fields[1:])
    return fields[0], fields[1:]


def track(id="T", **kw):
    return {"uri": f"spotify:track:{id}", "name": "音楽\t%\n", "artists": [{"name": "Björk"}], **kw}


class API:
    def __init__(self):
        self.calls, self.replies = [], []

    async def request(self, method, path, **kw):
        self.calls.append((method, path, kw))
        obj = self.replies.pop(0)
        if isinstance(obj, Exception):
            raise obj
        return obj


class Local:
    def __init__(self):
        self.calls = []
        self.upcoming = [{"uid": str(i), "item": {"uri": f"spotify:track:{i}"}} for i in range(4)]

    async def request(self, name, **kw):
        self.calls.append((name, kw))
        if name == "get_queue":
            return {"upcoming": self.upcoming.copy()}
        if name == "skip_next":
            self.upcoming.pop(0)
        return {}


@pytest.fixture
def browser(tmp_path):
    api, local = API(), Local()
    player = Player(ws=local)
    player.state.logged_in = True
    return browse.Browser(api, player, tmp_path)


async def test_caps_account_settings_and_unsupported(browser):
    h, rows = decode(await browser.handle(request("caps")))
    assert h[1] == "ok" and {r[1] for r in rows} == set(browse.OPS)
    browser.api.replies.append({"id": "user", "display_name": "Name", "product": None})
    h, rows = decode(await browser.handle(request("account")))
    assert rows == [["account", "", "Name", "", "", ""]]  # no invented subscription
    h, rows = decode(await browser.handle(request("settings")))
    assert [(r[1], r[2]) for r in rows] == [("shuffle", "off"), ("repeat", "off")]
    for op in ("signout", "download", "downloaded", "playdownload", "downloadstatus", "unknown"):
        args = ["spotify:playlist:P"] if op in ("download", "playdownload") else []
        h, _ = decode(await browser.handle(request(op, *args)))
        assert h[1] == "unsupported"


@pytest.mark.parametrize(
    ("op", "args", "path", "body", "kind"),
    [
        (
            "playlists",
            [],
            "/me/playlists",
            {"items": [{"uri": "spotify:playlist:P", "name": "P"}]},
            "playlist",
        ),
        ("albums", [], "/me/albums", {"items": [{"album": {"uri": "spotify:album:A"}}]}, "album"),
        (
            "artists",
            [],
            "/me/following",
            {"artists": {"items": [{"uri": "spotify:artist:A"}]}},
            "artist",
        ),
        ("album", ["spotify:album:A"], "/albums/A/tracks", {"items": [track()]}, "track"),
        (
            "artist",
            ["spotify:artist:A"],
            "/artists/A/albums",
            {"items": [{"uri": "spotify:album:A"}]},
            "album",
        ),
    ],
)
async def test_library_operations(browser, op, args, path, body, kind):
    browser.api.replies.append(body)
    h, rows = decode(await browser.handle(request(op, *args)))
    assert h[1] == "ok" and rows[0][0] == kind
    assert browser.api.calls[0][1] == path
    if op == "artist":
        assert "top tracks unavailable" in h[2]
        assert browser.api.calls[-1][2]["params"]["limit"] == 10


@pytest.mark.parametrize("category", ["tracks", "albums", "artists", "playlists"])
async def test_search_categories_limit_cursor_binding(browser, category):
    obj = {"uri": f"spotify:{category[:-1]}:T", "name": "検索"}
    browser.api.replies.append(
        {category: {"items": [obj], "next": "https://api.spotify.com/v1/search?offset=10"}}
    )
    h, rows = decode(await browser.handle(request("search", category, "音楽 %")))
    assert h[1] == "ok" and rows[0][2] == "検索"
    assert browser.api.calls[-1][2]["params"]["limit"] == 10
    browser.api.replies.append({category: {"items": []}})
    assert decode(await browser.handle(request("search", category, "音楽 %", h[3])))[0][1] == "ok"
    assert browser.api.calls[-1][2]["params"]["offset"] == "10"
    assert decode(await browser.handle(request("search", category, "changed", h[3])))[0][1] != "ok"


async def test_playlist_positions_unavailable_duplicate_entries_and_restriction(browser):
    browser.api.replies.append(
        {
            "items": [
                {"item": track()},
                {"item": None},
                {"item": track("X", is_playable=False)},
                {"item": track()},
                {"item": track(uri=None)},
            ],
            "next": "https://api.spotify.com/v1/playlists/P/items?offset=5",
        }
    )
    h, rows = decode(await browser.handle(request("playlist", "spotify:playlist:P")))
    assert [r[5] for r in rows] == ["0", "3"]
    assert rows[0][1] == rows[1][1]
    assert rows[0][2:4] == ["音楽\t%\n", "Björk"]
    browser.api.replies.append({"items": [{"item": track("Next")}]})
    h2, r2 = decode(await browser.handle(request("playlist", "spotify:playlist:P", h[3])))
    assert h2[1] == "ok" and r2[0][5] == "5"
    browser.api.replies.append(spotify.APIError("refused", "restricted", 403))
    assert (
        decode(await browser.handle(request("playlist", "spotify:playlist:P")))[0][1]
        == "unavailable"
    )
    browser.api.replies.append({"name": "metadata only"})
    assert (
        decode(await browser.handle(request("playlist", "spotify:playlist:P")))[0][1]
        == "unavailable"
    )


async def test_play_uses_exact_device_and_offset_and_liked_fallback(browser):
    state = browser.data / "soloist-state"
    state.mkdir()
    (state / ".device_id").write_text("verified")
    browser.api.replies.extend([{"devices": [{"id": "verified", "name": "anything"}]}, {}])
    h, _ = decode(
        await browser.handle(request("play", "spotify:playlist:P", "3", "spotify:track:T"))
    )
    assert h[1] == "ok"
    assert browser.api.calls[-1][2] == {
        "params": {"device_id": "verified"},
        "json": {"context_uri": "spotify:playlist:P", "offset": {"position": 3}},
    }
    browser.api.replies.append({"devices": [{"id": "wrong", "name": "Q2"}]})
    assert decode(await browser.handle(request("play", "spotify:track:T")))[0][1] == "unavailable"
    browser.api.replies.extend(
        [
            {"id": "U"},
            {
                "items": [
                    {"track": track()},
                    {"track": track("X", is_local=True)},
                    {"track": track("Next")},
                ]
            },
        ]
    )
    _, rows = decode(await browser.handle(request("liked")))
    browser.api.replies.extend(
        [{"devices": [{"id": "verified"}]}, spotify.APIError("refused", "no context", 403), {}]
    )
    h, _ = decode(await browser.handle(request("play", rows[0][4], rows[0][5], rows[0][1])))
    assert h[1] == "ok" and "fetched page only" in h[2]
    assert browser.api.calls[-1][2]["json"] == {"uris": ["spotify:track:T", "spotify:track:Next"]}


async def test_queue_skip_rechecks_enqueue_shuffle_all_repeat_transitions(browser):
    h, rows = decode(await browser.handle(request("queue")))
    assert h[1] == "ok" and [r[5] for r in rows] == ["0", "1", "2", "3"]
    assert (
        decode(await browser.handle(request("skip", "2", "spotify:track:wrong")))[0][1]
        == "unavailable"
    )
    assert not [c for c in browser.player.ws.calls if c[0] == "skip_next"]
    assert decode(await browser.handle(request("skip", "2", "spotify:track:2")))[0][1] == "ok"
    assert len([c for c in browser.player.ws.calls if c[0] == "skip_next"]) == 3
    for mode in ("on", "off"):
        assert decode(await browser.handle(request("shuffle", mode)))[0][1] == "ok"
    for mode in ("off", "context", "track", "context", "off"):
        assert decode(await browser.handle(request("repeat", mode)))[0][1] == "ok"
        pair = browser.player.ws.calls[-2:]
        assert pair[0][1] == {"enabled": False}
        assert pair[1] == (
            "set_repeat_" + ("track" if mode == "track" else "context"),
            {"enabled": mode != "off"},
        )
    assert decode(await browser.handle(request("enqueue", "spotify:track:T")))[0][1] == "ok"
    assert browser.player.ws.calls[-1] == ("add_to_queue", {"uri": "spotify:track:T"})


async def test_page_shrinks_to_fit_without_cutting_uris(browser):
    # Percent escaping can triple the display text's bytes.
    items = [
        {"uri": f"spotify:playlist:P{i}", "name": "\t" * 255, "owner": {"display_name": "%" * 255}}
        for i in range(50)
    ]
    browser.api.replies.append({"items": items})
    raw = await browser.handle(request("playlists"))
    h, rows = decode(raw)
    assert len(raw) <= 65536 and 0 < len(rows) < 50 and h[3]
    assert browser.continuation("playlists", [], h[3]) == {"offset": len(rows)}
    assert all(r[1].startswith("spotify:playlist:P") for r in rows)


@pytest.mark.parametrize(
    "raw",
    [
        b"x",
        b"Q2SPOT/1\nx\taccount\n",
        b"x" * 8193,
        b"Q2SPOT/1\n1\tsearch\ttracks\tbad%\n",
        b"Q2SPOT/1\n1\taccount\0\n",
        b"Q2SPOT/1\n1\taccount\nextra\n",
        b"Q2SPOT/1\n1\taccount%00\n",
    ],
)
async def test_bad_requests(browser, raw):
    assert decode(await browser.handle(raw))[0][1] == "bad"
    assert not browser.api.calls


async def test_slow_read_does_not_block_other_requests(browser):
    async def delayed(*args, **kw):
        await asyncio.sleep(0.2)
        return {"items": []}

    browser.api.request = delayed
    task = asyncio.create_task(browser.handle(request("playlists")))
    await asyncio.sleep(0)
    assert decode(await asyncio.wait_for(browser.handle(request("caps")), 0.1))[0][1] == "ok"
    await task


async def test_missing_oauth_keeps_existing_playback(tmp_path):
    player = Player()
    player.state.word = "playing"
    api = spotify.Spotify(tmp_path)
    browser = browse.Browser(api, player, tmp_path)
    try:
        h, _ = decode(await browser.handle(request("account")))
        assert h[1] == "refused" and "soloify login" in h[2]
        assert player.state.word == "playing"
    finally:
        await api.close()


async def test_cancel_before_control_lock_releases_reset_without_action(browser):
    from soloify import audio, wire

    class Writer:
        def __init__(self):
            self.data = []

        def write(self, data):
            self.data.append(data)

    session = audio.Session(browser.player, lambda: None, 2000, browser)
    session.writer = Writer()
    await session.lock.acquire()
    task = asyncio.create_task(session.browse(b"\0\0\0\1" + request("play", "spotify:track:T")))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    session.lock.release()
    assert session.writer.data == [wire.frame(wire.BOUNDARY, b"Q")]
    assert not browser.api.calls and not browser.player.ws.calls


async def test_followed_artist_after_cursor_and_malformed_upstream(browser):
    browser.api.replies.append(
        {
            "artists": {
                "items": [{"uri": "spotify:artist:A"}],
                "next": "https://api.spotify.com/v1/me/following?type=artist&after=A",
            }
        }
    )
    h, _ = decode(await browser.handle(request("artists")))
    browser.api.replies.append({"artists": {"items": []}})
    assert decode(await browser.handle(request("artists", h[3])))[0][1] == "ok"
    assert browser.api.calls[-1][2]["params"]["after"] == "A"
    browser.api.replies.append({"items": 123})
    assert decode(await browser.handle(request("albums")))[0][1] == "error"
