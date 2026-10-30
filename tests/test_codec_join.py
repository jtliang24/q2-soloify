"""Independent Opus runs must join as continuous PCM without restarting the ALSA player."""

import asyncio
import math
import struct
import subprocess
import tempfile
from pathlib import Path

import pytest

from relay_rig import DECODE, Relay, until
from soloify import audio, wire
from test_contract import Peer, children, controls, read_state, state


def encoded_tone(warmup=False, warmup_ms=140):
    source = "sine=frequency=1000:sample_rate=48000:duration=1"
    args = ["-f", "lavfi", "-i", source]
    if warmup:
        args += ["-af", f"adelay={warmup_ms}|{warmup_ms}"]
    data = subprocess.check_output(
        [
            "ffmpeg",
            "-v",
            "error",
            *args,
            "-ac",
            "2",
            "-c:a",
            "libopus",
            "-b:a",
            "160k",
            "-frame_duration",
            "20",
            "-f",
            "ogg",
            "pipe:1",
        ]
    )
    packets = [
        p for p in audio.OggPackets().feed(data) if not p.startswith((b"OpusHead", b"OpusTags"))
    ]
    samples = 48000 + (warmup_ms * 48 if warmup else 0)
    return packets, samples + 312 - (len(packets) - 1) * 960


async def check_codec_join(tmp_path, warmup, binary=None, decode=DECODE, shell=None, paused=False):
    runs = [encoded_tone(), encoded_tone(warmup)]
    with tempfile.TemporaryDirectory(prefix="sf-") as folder:
        base = Path(folder) / "q"
        peer = Peer(tmp_path / "bridge")
        peer.listener = await peer.server.start("127.0.0.1", 0)
        port = peer.listener.sockets[0].getsockname()[1]
        pcm = tmp_path / "joined.pcm"
        env = {"SOLOIFY_Q2POD": str(base), "SOLOIFY_DECODE": decode, "SOLOIFY_SINK": f"cat > {pcm}"}
        if shell:
            env["SOLOIFY_SH"] = shell
        relay = Relay(
            tmp_path,
            [f"127.0.0.1 {port} {peer.identity.token} sha256={peer.identity.pin}"],
            "-q",
            "-b",
            "200",
            env=env,
        )
        relay.binary = binary
        await relay.start()
        try:
            await until(lambda: peer.writer is not None)
            await peer.send(wire.CAPTURE, (1).to_bytes(4, "big"))  # old format also supported
            seq = 0
            player_pid = None
            for generation, (packets, tail) in enumerate(runs, 1):
                primer = packets.pop(0) if warmup and generation == 2 else b""
                skip = 312 + (140 * 48 if primer else 0)
                await peer.send(wire.CAPTURE, struct.pack(">IH", generation, skip) + primer)
                for i, packet in enumerate(packets):
                    if i == len(packets) - 1:
                        await peer.send(b"e", struct.pack(">IH", seq, tail))
                    await peer.send(wire.AUDIO, seq.to_bytes(4, "big") + packet)
                    seq += 1
                if generation == 1:

                    def players():
                        found = []
                        for pid in children(relay.proc.pid):
                            try:
                                if Path(f"/proc/{pid}/comm").read_text().strip() == "cat":
                                    found.append(pid)
                            except (FileNotFoundError, ProcessLookupError):
                                pass
                        return found

                    await until(lambda: bool(players()))
                    player_pid = players()[0]
                    if paused:
                        await until(lambda: pcm.exists() and pcm.stat().st_size >= 4410 * 4)
                        controls(base, b"s")
                        await until(lambda: peer.controls() == [b"s"])
                        await peer.send(wire.BOUNDARY, b"s")
                        await peer.send(wire.STATE, state(word="paused"))
                        await until(lambda: "state=paused\n" in read_state(base))
                        assert children(relay.proc.pid) == []
                        assert 0 < pcm.stat().st_size // 4 < 44100
                        controls(base, b"P")
                        await until(lambda: peer.controls() == [b"s", b"P"])
                        await peer.restored()
                        await peer.send(wire.BOUNDARY, b"P")
                        await peer.send(wire.STATE, state())
                        await asyncio.sleep(0.3)  # resume waits for fresh capture
                        assert children(relay.proc.pid) == []
            if paused:
                # Initial playback was discarded; resume starts only the fresh encoder.
                for _ in range(2):
                    await relay.expect("decoder gen=")
                await until(lambda: bool(players()))
                assert players()[0] != player_pid  # pause released the old player
                player_pid = players()[0]
                await until(lambda: children(relay.proc.pid) == [player_pid])
            else:
                await until(lambda: pcm.exists() and pcm.stat().st_size >= 88200 * 4 - 256, 10)
            await asyncio.sleep(0.2)
            pids = children(relay.proc.pid)
            assert len(pids) == 1  # decoder drained; PCM player stays open across both runs
            assert pids == [player_pid]
            raw = pcm.read_bytes()
            if paused:
                # The sink file starts again on resume, with one exact fresh second.
                assert abs(len(raw) // 4 - 44100) <= 2
            else:
                assert abs(len(raw) // 4 - 88200) <= 2  # exactly two seconds, padding removed
            samples = struct.unpack("<" + "h" * (len(raw) // 2), raw)[::2]

            def rms(start, n=441):
                return math.sqrt(sum(x * x for x in samples[start : start + n]) / n)

            join = 0 if paused else 44100
            reference = rms(join + 22050)
            windows = [rms(join + i * 441) for i in range(0 if paused else -5, 10)]
            assert all(0.85 * reference < v < 1.15 * reference for v in windows), windows
        finally:
            await relay.stop()
            peer.listener.close()
            if peer.server.current:
                peer.server.current.cancel()
                await asyncio.gather(peer.server.current, return_exceptions=True)
            await peer.listener.wait_closed()


@pytest.mark.parametrize("warmup", [False, True])
@pytest.mark.parametrize("paused", [False, True])
async def test_encoder_generations_join_without_padding_or_decoder_transients(
    tmp_path, warmup, paused
):
    await check_codec_join(tmp_path, warmup, paused=paused)
