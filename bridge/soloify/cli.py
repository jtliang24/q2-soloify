"""Serve the bridge, authorize Spotify browsing, or print card configuration."""

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from . import __version__, audio, browse, server, soloist, spotify, ws
from .player import Covers, Player

PORT = 7443


async def serve(args: argparse.Namespace) -> int:
    identity = server.Identity.load(args.data)
    tasks = []
    covers = Covers()
    if args.source == "test":
        player = Player()
    else:
        await soloist.ensure_sink()
        solo = soloist.Soloist(args.data, args.key, args.name)
        player = Player(cover=covers.get)
        link = player.ws = ws.SoloistWS(solo.port_file, player.event)
        tasks.append(asyncio.create_task(solo.run(idle=lambda: not player.state.playing)))
        tasks.append(asyncio.create_task(link.run()))

    api = spotify.Spotify(args.data)
    browser = browse.Browser(api, player, args.data)

    def encoder() -> audio.Encoder:
        return audio.Encoder(audio.ffmpeg_args(args.source, args.bitrate))

    async def session(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await audio.Session(player, encoder, args.inflight, browser).run(reader, writer)

    listener = await server.Server(identity, session).start(args.listen, args.port)
    server.log.info(
        "listening on %s, pin sha256=%s, %s at %d kbit/s",
        args.port,
        identity.pin,
        args.source,
        args.bitrate,
    )
    # SIGTERM (systemd, docker stop) and SIGINT end Soloist with the bridge, never orphan it; so
    # does a failed Soloist (no build, and none to download)
    main, failed = asyncio.current_task(), []
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, main.cancel)

    def ended(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception():
            server.log.error("stopping: %s", task.exception())
            failed.append(task)
            main.cancel()

    for task in tasks:
        task.add_done_callback(ended)
    try:
        async with listener:
            await listener.serve_forever()
    except asyncio.CancelledError:
        server.log.info("stopping")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await covers.close()
        await api.close()
    return 1 if failed else 0


def remote(args: argparse.Namespace) -> int:
    identity = server.Identity.load(args.data)
    hosts = (args.lan or [server.lan_address()]) + (args.wan or [])
    print("\n".join(server.remote_lines(identity, hosts, args.port)))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="soloify", description=__doc__)
    parser.add_argument("--version", action="version", version=f"soloify {__version__}")
    parser.add_argument(
        "--data",
        type=Path,
        default=Path(os.environ.get("SOLOIFY_DATA", "data")),
        help="Soloist, its session, the certificate and token (default $SOLOIFY_DATA or data)",
    )
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("serve", help="run Soloist and the bridge")
    p.add_argument("--listen", default=None, help="address to listen on (default all)")
    p.add_argument("--port", type=int, default=PORT)
    p.add_argument("--name", default="Q2", help="Soloist's Spotify Connect name (default Q2)")
    p.add_argument("--bitrate", type=int, choices=(96, 128, 160), default=160, help="kbit/s")
    p.add_argument(
        "--inflight", type=int, default=2000, help="ms of unacked audio that make a stall"
    )
    p.add_argument(
        "--source", choices=("pulse", "test"), default="pulse", help="test: a beep, no Soloist"
    )
    p.add_argument(
        "--key",
        type=Path,
        default=Path(os.environ.get("SOLOIST_API_KEY_FILE", "soloist_api.key")),
        help="Soloist's API key (default $SOLOIST_API_KEY_FILE or soloist_api.key)",
    )
    p = sub.add_parser("remote", help="print the card's .soloify/remote")
    p.add_argument("--lan", action="append", help="LAN address, tried first (default: this one's)")
    p.add_argument("--wan", action="append", help="name or address from outside, tried next")
    p.add_argument("--port", type=int, default=PORT)
    p = sub.add_parser("login", help="authorize the Spotify Web API")
    p.add_argument("--client-id", default=os.environ.get("SPOTIFY_CLIENT_ID"))
    p.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "serve":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        return asyncio.run(serve(args))
    if args.command == "remote":
        return remote(args)
    try:
        asyncio.run(spotify.login(args.data, args.client_id, args.timeout))
    except (spotify.APIError, OSError) as e:
        print(str(e), file=sys.stderr)
        return 2
    print("Spotify browsing authorized")
    return 0


if __name__ == "__main__":
    sys.exit(main())
