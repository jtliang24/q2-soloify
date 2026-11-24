#!/usr/bin/env python3
"""Prepare a release from a maintainer-built MIPS relay and a matching wheel.

The only ZIP is the card package. Soloist, configuration and secrets are never included.
Publishing requires a separate, explicit maintainer action; see docs/releasing.md.
"""

import argparse
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bridge"))
from soloify import __version__  # noqa: E402
from soloify.setup import card_files, validate_mips  # noqa: E402
from soloify.setup_bootstrap import VERSION  # noqa: E402


def bootstrap() -> bytes:
    return (
        b"#!/usr/bin/env python3\n"
        b"# Generated from bridge/soloify/setup_bootstrap.py by tools/release.py.\n"
        + (ROOT / "bridge/soloify/setup_bootstrap.py").read_bytes()
    )


def versions(wheel: Path) -> None:
    project = (ROOT / "pyproject.toml").read_text()
    if VERSION != __version__ or f'version = "{VERSION}"' not in project:
        raise ValueError("Package, project and launcher versions differ")
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate wheel members")
        if any(name.startswith("soloify/") and not name.endswith(".py") for name in names):
            raise ValueError("Unexpected wheel package data")
        metadata = archive.read(f"soloify-{VERSION}.dist-info/METADATA").decode()
        if not re.search(rf"^Version: {re.escape(VERSION)}$", metadata, re.M):
            raise ValueError("Wheel metadata version mismatch")
        package = archive.read("soloify/__init__.py").decode()
        if f'__version__ = "{VERSION}"' not in package:
            raise ValueError("Wheel package version mismatch")
        if (
            archive.read("soloify/setup_bootstrap.py")
            != (ROOT / "bridge/soloify/setup_bootstrap.py").read_bytes()
        ):
            raise ValueError("Rebuild wheel after changing bootstrap")
        if not all(
            n.startswith(("soloify/", f"soloify-{VERSION}.dist-info/")) for n in archive.namelist()
        ):
            raise ValueError("Unexpected wheel contents")


def prepare(out: Path, wheel: Path, *, relay: Path | None = None, card: Path | None = None) -> None:
    versions(wheel)
    out.mkdir(parents=True, exist_ok=True)
    if relay:
        binary = relay.read_bytes()
        validate_mips(binary)
        files = {
            "q2-soloify": binary,
            "run": (ROOT / "card/run").read_bytes(),
            "aplay.sh": (ROOT / "card/aplay.sh").read_bytes(),
            "q2app": (ROOT / "card/q2app").read_bytes(),
            "VERSION": (VERSION + "\n").encode(),
        }
        card_path = out / f"soloify-card-{VERSION}.zip"
        with zipfile.ZipFile(card_path, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in files.items():
                entry = zipfile.ZipInfo(f".soloify/{name}", date_time=(2026, 1, 1, 0, 0, 0))
                entry.create_system = 3
                entry.external_attr = (0o100644 if name in ("VERSION", "q2app") else 0o100755) << 16
                archive.writestr(entry, content)
    else:
        card_path = out / f"soloify-card-{VERSION}.zip"
        card_path.write_bytes(card.read_bytes())
    card_files(card_path.read_bytes())
    launcher = out / "soloify-setup.py"
    launcher.write_bytes(bootstrap())
    launcher.chmod(0o755)
    target_wheel = out / f"soloify-{VERSION}-py3-none-any.whl"
    target_wheel.write_bytes(wheel.read_bytes())
    (out / "release.json").write_text(
        json.dumps(
            {
                "version": VERSION,
                "image": f"ghcr.io/jtliang24/q2-soloify:{VERSION}",
                "protocol": 2,
                "card_architecture": "MIPS32r2 o32 little-endian NaN2008",
            },
            indent=2,
        )
        + "\n"
    )
    expected = {launcher.name, target_wheel.name, card_path.name, "release.json"}
    actual = {p.name for p in out.iterdir()} - {"SHA256SUMS"}
    if actual != expected:
        raise ValueError(
            "Release directory contains unexpected files; use a dedicated empty directory"
        )
    (out / "SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256((out / name).read_bytes()).hexdigest()}  {name}\n"
            for name in sorted(expected)
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write-bootstrap", action="store_true")
    parser.add_argument("--wheel", type=Path)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--relay", type=Path)
    group.add_argument("--card", type=Path)
    parser.add_argument("--out", type=Path, default=ROOT / "build/release")
    args = parser.parse_args()
    if args.write_bootstrap:
        (ROOT / "soloify-setup.py").write_bytes(bootstrap())
        (ROOT / "soloify-setup.py").chmod(0o755)
        return 0
    if not args.wheel or not (args.relay or args.card):
        parser.error("--wheel and --relay (or --card) are required")
    prepare(args.out, args.wheel, relay=args.relay, card=args.card)
    return 0


if __name__ == "__main__":
    sys.exit(main())
