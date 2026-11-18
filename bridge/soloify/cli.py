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


def tcp_port(value: str) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535") from exc
    if not 1 <= number <= 65535:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535")
    return number


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

    def encoder(bitrate: int | None = None) -> audio.Encoder:
        return audio.Encoder(audio.ffmpeg_args(args.source, bitrate or args.bitrate))

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
    lines = server.remote_lines(identity, args.lan or [server.lan_address()], args.port)
    lines += server.remote_lines(identity, args.wan or [], args.wan_port or args.port)
    print("\n".join(lines))
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
    p.add_argument("--port", type=tcp_port, default=PORT)
    p.add_argument(
        "--name", default="soloify", help="Soloist's Spotify Connect name (default soloify)"
    )
    p.add_argument(
        "--bitrate",
        type=int,
        choices=(96, 128, 160),
        default=160,
        help="kbit/s, unless the relay asks for another",
    )
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
    p.add_argument("--port", type=tcp_port, default=PORT)
    p.add_argument("--wan-port", type=tcp_port, help="external TCP port (default: --port)")
    p = sub.add_parser("setup", help="guided bridge setup and card export")
    p.add_argument("--check", action="store_true", help="read-only prerequisite report")
    p.add_argument(
        "--json", action="store_true", help="read-only JSON readiness report (implies --check)"
    )
    p.add_argument("--root", type=Path, default=Path.home() / ".soloify-bridge")
    p = sub.add_parser("diagnostics", help="Soloist version and automatic renewal status")
    p = sub.add_parser("login", help="authorize the Spotify Web API")
    p.add_argument("--client-id", default=os.environ.get("SPOTIFY_CLIENT_ID"))
    p.add_argument("--timeout", type=float, default=180)
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "setup":
        from . import setup_bootstrap

        options = ["--root", str(args.root)]
        if args.check:
            options.append("--check")
        if args.json:
            options.append("--json")
        return setup_bootstrap.main(options, installed=True)
    if args.command == "diagnostics":
        import json

        print(
            json.dumps(asyncio.run(soloist.Soloist(args.data, Path("unused")).status()), indent=2)
        )
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
