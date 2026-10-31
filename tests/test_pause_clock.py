"""Real relay pause bookmarks against simulated ALSA hardware status, without an M1."""

import asyncio
import struct
from pathlib import Path

import pytest

from relay_rig import until
from soloify import wire
from test_contract import children, controls, read_state, state
from test_contract import contract as contract


def hardware(relay, card=0, device=0, owner_index=1, rate=44100):
    root = Path(relay.env["SOLOIFY_ASOUND"])
    pcm = root / f"card{card}/pcm{device}p/sub0"
    pcm.mkdir(parents=True, exist_ok=True)
    (root / "pcm").write_text(f"{card:02}-{device:02}: device : playback 1\n")
    (pcm / "hw_params").write_text(f"rate: {rate} ({rate}/1)\n")
    owner = children(relay.proc.pid)[owner_index]

    def status(word, frames=0):
        # Proc snapshots change atomically; avoid testing temporary empty files.
        pending = pcm / "pending"
        pending.write_text(f"state: {word}\nowner_pid   : {owner}\nhw_ptr      : {frames}\n")
        pending.replace(pcm / "status")

    return status


@pytest.mark.parametrize("card,owner,rate", [(0, 1, 44100), (1, 0, 44100), (2, 1, 96000)])
async def test_open_setup_prepared_running_startup_recovers(contract, card, owner, rate):
    relay, peer, base = contract
    await peer.send(wire.TIMELINE, state(position=12000, token=71))
    await peer.packets(60)
    await until(lambda: len(children(relay.proc.pid)) == 2)
    status = hardware(relay, card=card, owner_index=owner, rate=rate)
    for word in ("OPEN", "SETUP", "PREPARED", "SETUP", "PREPARED"):
        status(word)
        await asyncio.sleep(0.08)
    status("RUNNING", rate // 5)
    await asyncio.sleep(0.08)
    controls(base, b"s")
    await until(lambda: peer.bookmarks())
    assert peer.bookmarks()[-1][4:] == b"token=71\nposition=12200\n"
    assert not children(relay.proc.pid)


async def test_hardware_epoch_spans_trimmed_capture_and_track_change(contract):
    relay, peer, base = contract
    # 393.5 ms of old-track presentation, then a new decoder with a hidden warmup
    # packet, 312 samples of pre-skip and an end trimmed to 312 samples.
    await peer.send(wire.TIMELINE, state(position=12000, token=71))
    await peer.packets(20)
    await until(lambda: len(children(relay.proc.pid)) == 2)
    status = hardware(relay)
    status("RUNNING", 8820)
    await asyncio.sleep(0.08)
    player = children(relay.proc.pid)[1]
    await peer.send(wire.CAPTURE, struct.pack(">IH", 2, 1272) + b"\xfc\xff\xfe")
    await peer.send(wire.TIMELINE, state("b", position=3000, token=72))
    await peer.packets(20)
    await peer.send(wire.AUDIO_END, struct.pack(">IH", 20, 312))
    await peer.send(wire.AUDIO, (20).to_bytes(4, "big") + b"\xfc\xff\xfe")
    await relay.expect("decoder gen=3")
    assert player in children(relay.proc.pid)
    status("RUNNING", 22050)  # 500 ms into the SAME hardware epoch
    await asyncio.sleep(0.08)
    controls(base, b"s")
    await until(lambda: peer.bookmarks())
    assert peer.bookmarks()[-1][4:] == b"token=72\nposition=3107\n"
    assert "track=b\n" in read_state(base)


async def test_startup_pause_and_established_setup_reset_are_distinct(contract):
    relay, peer, base = contract
    await peer.send(wire.TIMELINE, state(token=7))
    await peer.packets(50)
    await until(lambda: len(children(relay.proc.pid)) == 2)
    status = hardware(relay)
    status("RUNNING", 8820)
    await asyncio.sleep(0.08)
    status("SETUP")
    await asyncio.sleep(0.08)
    status("RUNNING", 17640)
    controls(base, b"s")
    await until(lambda: peer.bookmarks())
    assert peer.bookmarks()[-1][4:] == b""
    assert "hardware reset" in await relay.expect("bookmark clock")


async def test_repeated_resume_starts_new_hardware_epoch(contract):
    relay, peer, base = contract
    for cycle in range(3):
        if cycle:
            before = len(peer.controls())
            controls(base, b"P")
            await until(lambda before=before: len(peer.controls()) > before)
            await peer.restored()
            await peer.send(wire.BOUNDARY, b"P")
            await peer.send(wire.STATE, state(position=12000))
            await peer.send(wire.CAPTURE, struct.pack(">IH", cycle + 1, 312))
        await peer.send(wire.TIMELINE, state(position=12000, token=71))
        await peer.packets(50)
        await until(lambda: len(children(relay.proc.pid)) == 2)
        status = hardware(relay)
        status("RUNNING", 8820)
        await asyncio.sleep(0.08)
        controls(base, b"s")
        await until(lambda cycle=cycle: len(peer.bookmarks()) == cycle + 1)
        assert peer.bookmarks()[-1][4:] == b"token=71\nposition=12200\n"
        assert not children(relay.proc.pid)


@pytest.mark.parametrize("rate,card,device", [(44100, 0, 0), (48000, 2, 1), (96000, 3, 0)])
async def test_hardware_cursor_not_queued_or_wall_clock(contract, rate, card, device):
    relay, peer, base = contract
    # The proc tree was installed in the process environment by the shared fixture.
    root = Path(relay.env["SOLOIFY_ASOUND"])
    await peer.send(wire.TIMELINE, state(position=12000, token=71))
    await peer.packets(20)
    await peer.send(wire.TIMELINE, state("b", position=0, token=72))
    await peer.packets(40)
    await until(lambda: len(children(relay.proc.pid)) == 2)
    owner = children(relay.proc.pid)[0]
    pcm = root / f"card{card}/pcm{device}p/sub0"
    pcm.mkdir(parents=True)
    (root / "pcm").write_text(f"{card:02}-{device:02}: device : playback 1\n")
    (pcm / "hw_params").write_text(f"rate: {rate} ({rate}/1)\n")
    (pcm / "status").write_text(
        f"state: RUNNING\nowner_pid   : {owner}\nhw_ptr      : {rate // 5}\n"
        f"appl_ptr    : {rate}\ndelay: {4 * rate // 5}\n"
    )
    await asyncio.sleep(0.05)
    controls(base, b"s")
    await until(lambda: peer.bookmarks())
    assert peer.bookmarks()[-1][4:] == b"token=71\nposition=12200\n"
    assert "track=a\n" in read_state(base)
    assert "position=12200\n" in read_state(base)
    assert not children(relay.proc.pid)


@pytest.mark.parametrize(
    "fault", ["reset", "xrun", "rate", "invalid_rate", "missing_pointer", "unplug", "unowned"]
)
async def test_unreliable_hardware_clock_fails_paused(contract, fault):
    relay, peer, base = contract
    root = Path(relay.env["SOLOIFY_ASOUND"])
    await peer.send(wire.TIMELINE, state(token=7))
    await peer.packets(50)
    await until(lambda: len(children(relay.proc.pid)) == 2)
    owner = children(relay.proc.pid)[0]
    pcm = root / "card2/pcm0p/sub0"
    pcm.mkdir(parents=True)
    (root / "pcm").write_text("02-00: USB : playback 1\n")
    params = pcm / "hw_params"
    params.write_text("rate: 48000 (48000/1)\n")
    status = pcm / "status"
    status.write_text(f"state: RUNNING\nowner_pid   : {owner}\nhw_ptr      : 9600\n")
    await asyncio.sleep(0.08)  # give audio_tick a known cursor before injecting a fault
    if fault == "reset":
        status.write_text(f"state: RUNNING\nowner_pid   : {owner}\nhw_ptr      : 0\n")
    elif fault == "xrun":
        status.write_text(f"state: XRUN\nowner_pid   : {owner}\nhw_ptr      : 9600\n")
    elif fault == "rate":
        params.write_text("rate: 44100 (44100/1)\n")
    elif fault == "invalid_rate":
        params.write_text("rate: 0\n")
    elif fault == "missing_pointer":
        status.write_text(f"state: RUNNING\nowner_pid   : {owner}\n")
    elif fault == "unplug":
        status.unlink()
    else:
        status.write_text(f"state: RUNNING\nowner_pid   : {relay.proc.pid}\nhw_ptr      : 9600\n")
    controls(base, b"s")
    await until(lambda: peer.bookmarks())
    assert peer.bookmarks()[-1][4:] == b""
    assert "state=paused\n" in read_state(base)
    assert not children(relay.proc.pid)
