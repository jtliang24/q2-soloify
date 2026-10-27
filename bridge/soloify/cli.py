"""The soloify command: serve runs the bridge, remote prints the card's .soloify/remote. login is
PLAN.md's step 4."""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

from . import __version__, server, wire

PORT = 7443


async def _drain(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while True:
        kind, payload = await wire.read_frame(reader)
        if kind == wire.STATS:
            server.log.info("relay: %s", payload.decode(errors="replace"))


async def serve(args: argparse.Namespace) -> int:
    identity = server.Identity.load(args.data)
    listener = await server.Server(identity, _drain).start(args.listen, args.port)
    server.log.info("listening on %s, pin sha256=%s", args.port, identity.pin)
    async with listener:
        await listener.serve_forever()
    return 0


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
    p = sub.add_parser("remote", help="print the card's .soloify/remote")
    p.add_argument("--lan", action="append", help="LAN address, tried first (default: this one's)")
    p.add_argument("--wan", action="append", help="name or address from outside, tried next")
    p.add_argument("--port", type=int, default=PORT)
    sub.add_parser("login", help="authorize the Spotify Web API")
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "serve":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
        return asyncio.run(serve(args))
    if args.command == "remote":
        return remote(args)
    print(f"soloify {args.command}: not implemented yet", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
