"""The device build under qemu-mipsel with the stock rootfs's libraries: its declarations, its link
and OpenSSL 1.1.1f, with the device's own ffmpeg 4.2 decoding. Needs a stock squashfs:
$SOLOIFY_ROOTFS, else q2-pod's build-m1/stock.squashfs beside this repo."""

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from relay_rig import REALTIME_SINK, ROOT, Bridge, Relay, stat

SQUASHFS = Path(os.environ.get("SOLOIFY_ROOTFS", ROOT.parent / "q2-pod/build-m1/stock.squashfs"))
# The loader and libraries the relay links, and the device's ffmpeg with its libraries
PARTS = ["lib", "usr/lib", "usr/bin/ffmpeg"]
pytestmark = pytest.mark.skipif(
    not SQUASHFS.exists() or not shutil.which("qemu-mipsel") or not shutil.which("unsquashfs"),
    reason="needs a stock rootfs, qemu-mipsel and unsquashfs",
)


@pytest.fixture(scope="module")
def device(tmp_path_factory):
    """The MIPS relay and the rootfs parts it and ffmpeg need."""
    out = tmp_path_factory.mktemp("mips")
    build = [sys.executable, str(ROOT / "relay/build.py"), "--rootfs", str(SQUASHFS)]
    exe = Path(subprocess.check_output([*build, "--out", str(out / "build")], text=True).split()[0])
    rootfs = out / "rootfs"
    subprocess.run(
        ["unsquashfs", "-q", "-no-xattrs", "-d", rootfs, SQUASHFS, *PARTS],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return exe, rootfs


async def test_device_build_plays_through_the_stock_ffmpeg(tmp_path, device):
    exe, rootfs = device
    bridge = await Bridge(tmp_path / "data").start()
    decode = f"qemu-mipsel -L {rootfs} {rootfs}/usr/bin/ffmpeg" + (
        " -nostdin -loglevel error -probesize 32 -analyzeduration 0"
        " -f ogg -i pipe:0 -ac 2 -ar 44100 -f s16le pipe:1"
    )
    env = {
        "SOLOIFY_DECODE": decode,
        "SOLOIFY_SINK": REALTIME_SINK,
        "SOLOIFY_SH": shutil.which("sh"),  # the host's: the rootfs's busybox isn't extracted
        "SOLOIFY_STATS_MS": "1000",
    }
    relay = Relay(tmp_path, [bridge.line() + " buffer=1000"], env=env)
    relay.binary = ["qemu-mipsel", "-L", str(rootfs), str(exe)]
    await relay.start()
    try:
        await relay.expect("connected to 127.0.0.1", timeout=20)
        await asyncio.sleep(6)
        line = await relay.expect("ring=")
        assert 400 <= stat(line, "ring") <= 1100, line
        assert stat(line, "sinks") == 1 and stat(line, "underruns") == 0, line
        assert bridge.sessions[0].acked > 250
        assert not [x for x in relay.log if "rror" in x], relay.log  # nothing from ffmpeg
    finally:
        await relay.stop()
        await bridge.stop()


async def test_device_build_forwards_browser(tmp_path, device):
    import tempfile

    from soloify import browse as protocol
    from test_contract import browse

    class Browser:
        async def handle(self, raw, boundary):
            id, op, args = protocol.parse(raw)
            return protocol.response(
                id, rows=[("playlist", "spotify:playlist:P", "音楽%", "", "", "")]
            )

    exe, rootfs = device
    bridge = Bridge(tmp_path / "data")
    bridge.browser = Browser()
    await bridge.start()
    with tempfile.TemporaryDirectory(prefix="sf-") as folder:
        base = Path(folder) / "q"
        relay = Relay(tmp_path, [bridge.line()], "-q", env={"SOLOIFY_Q2POD": str(base)})
        relay.binary = ["qemu-mipsel", "-L", str(rootfs), str(exe)]
        await relay.start()
        try:
            await relay.expect("connected to 127.0.0.1", timeout=20)
            reply = await browse(base, b"Q2SPOT/1\n7\tplaylists\n")
            assert "音楽%25".encode() in reply
            assert b"spotify:playlist:P" in reply
        finally:
            await relay.stop()
            await bridge.stop()


@pytest.mark.parametrize("warmup", [False, True])
@pytest.mark.parametrize("paused", [False, True])
async def test_stock_decoder_joins_encoder_generations_without_a_blip(
    tmp_path, device, warmup, paused
):
    from test_codec_join import check_codec_join

    exe, rootfs = device
    decode = f"qemu-mipsel -L {rootfs} {rootfs}/usr/bin/ffmpeg" + (
        " -nostdin -loglevel error -probesize 32 -analyzeduration 0"
        " -f ogg -i pipe:0 -ac 2 -ar 44100 -f s16le pipe:1"
    )
    await check_codec_join(
        tmp_path,
        warmup,
        ["qemu-mipsel", "-L", str(rootfs), str(exe)],
        decode,
        shutil.which("sh"),
        paused=paused,
    )
