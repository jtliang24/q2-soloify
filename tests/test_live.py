"""Opt in: SOLOIFY_LIVE=1 nix develop -c python -m pytest tests/test_live.py -s.

Uses the paired laptop session and saved OAuth. Playback remains in the null sink. Do not run
another Soloist with this data directory. No playlist/library mutations or forced 401/429s.
"""

import asyncio
import contextlib
import json
import os
from pathlib import Path

import pytest

from relay_rig import ROOT, until
from soloify import browse, soloist, spotify
from soloify.player import Player
from soloify.ws import SoloistWS
from test_browse import decode, request

pytestmark = pytest.mark.skipif(os.environ.get("SOLOIFY_LIVE") != "1", reason="opt-in live Spotify")


async def test_live_laptop_endpoints_and_controls():
    data = Path(os.environ.get("SOLOIFY_DATA", ROOT / "data"))
    assert (data / "oauth.json").exists(), "run soloify login first"
    assert not (data / "soloist-state/ws.port").exists(), "stop the competing Soloist first"
    await soloist.ensure_sink()
    solo = soloist.Soloist(
        data, Path(os.environ.get("SOLOIST_API_KEY_FILE", ROOT / "soloist_api.key"))
    )
    player = Player()
    link = player.ws = SoloistWS(solo.port_file, player.event)
    api = spotify.Spotify(data)
    browser = browse.Browser(api, player, data)
    tasks = [asyncio.create_task(solo.run()), asyncio.create_task(link.run())]
    results = {}
    original = None

    async def check(op, *args, name=None):
        h, rows = decode(await browser.handle(request(op, *args)))
        name = name or op
        results[name] = {"status": h[1], "message": h[2], "rows": len(rows), "more": bool(h[3])}
        print(f"LIVE {name}: {h[1]} ({len(rows)} rows) {h[2]}")
        return h, rows

    try:
        await until(lambda: player.state.connected and player.state.logged_in, timeout=40)
        original = (player.state.shuffle, player.state.repeat)
        print("LIVE Soloist:", await solo.version())
        h, _ = await check("account")
        assert h[1] == "ok"
        await check("caps")
        pages = {}
        for op in ("playlists", "liked", "albums", "artists"):
            h, rows = await check(op)
            pages[op] = rows
            if h[1] == "ok" and h[3]:
                await check(op, h[3], name=op + "_page2")
        for category in ("tracks", "albums", "artists", "playlists"):
            await check("search", category, "Daft Punk", name="search_" + category)
        album_rows, liked_rows = [], pages["liked"]
        if pages["albums"]:
            _, album_rows = await check("album", pages["albums"][0][1])
        if pages["artists"]:
            h, _ = await check("artist", pages["artists"][0][1])
            assert h[1] == "ok"
        if pages["playlists"]:
            await check("playlist", pages["playlists"][0][1])
        await check(
            "playlist", "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M", name="restricted_playlist"
        )
        # Persisted ID must be an exact match. No display-name fallback is permitted.
        devices = await api.request("GET", "/me/player/devices")
        device_id = (data / "soloist-state/.device_id").read_text().strip()
        results["device_id"] = {
            "exact_match": any(d.get("id") == device_id for d in devices["devices"])
        }
        print("LIVE exact device ID match:", results["device_id"]["exact_match"])
        assert results["device_id"]["exact_match"]
        for name, rows in (("album_context", album_rows), ("liked_context", liked_rows)):
            if not rows:
                results[name] = {"status": "deferred", "message": "No playable rows"}
                continue
            row = rows[min(1, len(rows) - 1)]
            h, _ = await check("play", row[4], row[5], row[1], name=name)
            assert h[1] == "ok", h
            await until(
                lambda row=row: (
                    player.state.active and player.state.track and player.state.track.uri == row[1]
                ),
                timeout=15,
            )
            results[name]["selected_track_verified"] = True
        rows = liked_rows or album_rows
        if rows:
            h, _ = await check("enqueue", rows[0][1])
            assert h[1] == "ok"
            await asyncio.sleep(0.5)
            h, queue = await check("queue")
            assert h[1] == "ok"
            if queue:
                h, _ = await check("skip", "0", "spotify:track:Missing", name="stale_queue")
                assert h[1] == "unavailable"
                row = queue[0]
                h, _ = await check("skip", row[5], row[1], name="queue_skip")
                assert h[1] == "ok"
                await until(
                    lambda: player.state.track and player.state.track.uri == row[1], timeout=15
                )
        await asyncio.sleep(1)  # state events precede creation of the audio stream
        rc, sinks_raw = await soloist._output("pactl", "-f", "json", "list", "sinks")
        assert rc == 0
        sinks = json.loads(sinks_raw)
        null_index = next(s["index"] for s in sinks if s["name"] == soloist.SINK)
        rc, streams_raw = await soloist._output("pactl", "-f", "json", "list", "sink-inputs")
        streams = json.loads(streams_raw)
        owned = [
            s
            for s in streams
            if str(s.get("properties", {}).get("application.process.id")) == str(solo.proc.pid)
        ]
        results["null_sink"] = {
            "verified": bool(owned) and all(s["sink"] == null_index for s in owned)
        }
        if not results["null_sink"]["verified"]:
            # Native PipeWire streams are not necessarily exposed as PulseAudio sink inputs.
            rc, graph_raw = await soloist._output("pw-dump")
            assert rc == 0
            graph = json.loads(graph_raw)
            nodes = {
                n["id"]: n.get("info", {}).get("props", {})
                for n in graph
                if n.get("type") == "PipeWire:Interface:Node"
            }
            null_nodes = {
                id for id, props in nodes.items() if props.get("node.name") == soloist.SINK
            }
            source_nodes = {
                id
                for id, props in nodes.items()
                if props.get("media.class") == "Stream/Output/Audio"
                and (
                    str(props.get("application.process.id")) == str(solo.proc.pid)
                    or str(props.get("application.name", "")).lower()
                    in ("spotify", "spotify soloist", "soloist")
                    or "soloist" in str(props.get("node.name", "")).lower()
                )
            }
            # Link direction is source output -> sink input.
            targets = [
                n["info"]["input-node-id"]
                for n in graph
                if n.get("type") == "PipeWire:Interface:Link"
                and n.get("info", {}).get("output-node-id") in source_nodes
            ]
            results["null_sink"] = {
                "verified": bool(targets) and set(targets) <= null_nodes,
                "method": "PipeWire graph",
            }
            print("LIVE null-sink graph:", results["null_sink"])
            if not targets:
                print(
                    "LIVE stream diagnostics:",
                    [
                        {
                            k: v
                            for k, v in props.items()
                            if k
                            in (
                                "node.name",
                                "media.class",
                                "application.name",
                                "application.process.id",
                                "application.process.binary",
                                "target.object",
                            )
                        }
                        for props in nodes.values()
                        if props.get("media.class") == "Stream/Output/Audio"
                    ],
                )
        assert results["null_sink"]["verified"], "Soloist stream must be on the null sink"
        # Confirm the real source honors the bookmark's paused seek; this does not
        # claim to measure a physical output cursor or listening alignment.
        await until(lambda: player.timeline.current is not None and player.state.playing)
        await until(lambda: player.state.position() >= 2500, timeout=10)
        position = player.state.position() - 1000
        token = player.timeline.current.token
        player.rewind.remember(b"\0\0\0\1" + f"token={token}\nposition={position}\n".encode())
        uri = player.state.track.uri
        # Advance the real source beyond the listener's saved occurrence. This
        # exercises verified Previous/queue semantics without claiming a natural
        # boundary or measuring a physical device's playback position.
        assert await link.command("skip_next")
        await until(lambda: player.state.track and player.state.track.uri != uri, timeout=10)
        results["pause_timeline"] = [
            {"token": item.token, "uri": item.state.track.uri, "anchor": item.state.anchor}
            for item in player.timeline.occurrences
        ]
        results["pause_previous"] = (await link.request("get_queue", limit=128))["previous"]
        await player.rewind.restore()
        assert player.state.track.uri == uri
        results["pause_restore"] = {
            "verified": player.state.word == "paused",
            "requested_ms": position,
            "confirmed_ms": player.state.position(),
            "hardware_measured": False,
            "previous_track_verified": True,
        }
        assert results["pause_restore"]["verified"]
        assert abs(player.state.position() - position) <= 100
        await player.rewind.command("play")
        player.rewind.bookmark = None
        await until(lambda: player.state.playing)
        for mode in ("on", "off"):
            h, _ = await check("shuffle", mode, name="shuffle_" + mode)
            assert h[1] == "ok"
            await until(lambda mode=mode: player.state.shuffle == (mode == "on"))
        for mode in ("off", "context", "track", "off"):
            h, _ = await check("repeat", mode, name="repeat_" + mode)
            assert h[1] == "ok"
            await until(lambda mode=mode: player.state.repeat == mode)
        await check("settings")
    finally:
        if original and link.connected:
            with contextlib.suppress(Exception):
                await browser.dispatch("shuffle", ["on" if original[0] else "off"])
                await browser.dispatch("repeat", [original[1]])
                await until(
                    lambda: (
                        player.state.shuffle == original[0] and player.state.repeat == original[1]
                    )
                )
                results["settings_restored"] = True
                await link.request("pause")
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for task in list(player.tasks):
            task.cancel()
        await asyncio.gather(*player.tasks, return_exceptions=True)
        await api.close()
        out = Path(os.environ.get("SOLOIFY_LIVE_RESULTS", ROOT / "build/step4-live.json"))
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(results, indent=2) + "\n")
