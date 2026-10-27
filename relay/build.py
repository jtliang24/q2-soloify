#!/usr/bin/env python3
"""Builds the relay. --host: for this machine, against its OpenSSL, for tests. --rootfs SQUASHFS:
for the Q2 and the M1 Plus, linked as q2-pod's helpers are (tools/build.py's compile_helper)
against the stock rootfs's glibc 2.28 and OpenSSL 1.1.1f, e.g. q2-pod's build-m1/stock.squashfs."""

import argparse
import hashlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCES = ["main", "tls", "wire", "remote", "audio"]
WARN = ["-Wall", "-Wextra", "-Werror", "-Wno-unused-parameter"]
# q2-pod's FLAGS, as compile_helper changes them for a dynamically linked helper
MIPS = [
    *("--target=mipsel-linux-gnu", "-march=mips32r2", "-mabi=32", "-mfp64"),
    *("-fno-pic", "-ffreestanding", "-fno-builtin", "-fno-stack-protector"),
    *("-fno-unwind-tables", "-fno-asynchronous-unwind-tables", "-Oz"),
    *("-mnan=2008", "-mabs=2008", "-mabicalls"),
]
# compile_helper's link
LINK = [
    *("-m", "elf32ltsmip", "-e", "__start", "--dynamic-linker", "/lib/ld-linux-mipsn8.so.1"),
    *("--image-base=0x400000", "-z", "noexecstack", "--gc-sections", "-s"),
    *("--hash-style=sysv", "--build-id=none"),
]
LIBS = ["lib/libc-2.28.so", "usr/lib/libssl.so.1.1", "usr/lib/libcrypto.so.1.1"]
# OpenSSL 3 deprecates the 1.1 names the device's library has
HOST = ["-DRELAY_HOST", "-O2", "-g", "-Wno-deprecated-declarations"]


def run(*cmd):
    subprocess.run([str(c) for c in cmd], check=True)


def host(out: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    exe = out / "q2-soloify"
    srcs = [ROOT / f"{s}.c" for s in SOURCES]
    run("cc", *HOST, *WARN, *srcs, "-lssl", "-lcrypto", "-o", exe)
    return exe


def mips(out: Path, rootfs: Path) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    libs = []
    for rel in LIBS:
        lib = out / rel.rsplit("/", 1)[-1]
        lib.write_bytes(subprocess.check_output(["unsquashfs", "-cat", str(rootfs), rel]))
        libs.append(lib)
    objs = []
    for s in [*SOURCES, "start"]:
        objs.append(out / f"{s}.o")
        run("clang", *MIPS, *WARN, "-c", ROOT / f"{s}.c", "-o", objs[-1])
    exe = out / "q2-soloify"
    run("ld.lld", *LINK, *objs, *libs, "-o", exe)
    return exe


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--host", action="store_true")
    g.add_argument("--rootfs", type=Path)
    p.add_argument("--out", type=Path, help="default build/relay-host or build/relay")
    a = p.parse_args()
    if a.host:
        exe = host(a.out or ROOT.parent / "build/relay-host")
    else:
        exe = mips(a.out or ROOT.parent / "build/relay", a.rootfs)
    print(f"{exe} sha256={hashlib.sha256(exe.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
