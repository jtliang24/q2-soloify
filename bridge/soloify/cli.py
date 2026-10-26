"""The soloify command. Only --version works so far; serve, login and remote are PLAN.md's steps."""

import argparse
import sys

from . import __version__


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="soloify", description=__doc__)
    parser.add_argument("--version", action="version", version=f"soloify {__version__}")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("serve", help="run Soloist and the bridge")
    sub.add_parser("login", help="authorize the Spotify Web API")
    sub.add_parser("remote", help="print the card's .soloify/remote")
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    print(f"soloify {args.command}: not implemented yet", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
