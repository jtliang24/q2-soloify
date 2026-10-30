"""Real relay pause bookmarks against simulated ALSA hardware status, without an M1."""

import asyncio
from pathlib import Path

import pytest

from relay_rig import until
from soloify import wire
from test_contract import children, controls, read_state, state
from test_contract import contract as contract


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


@pytest.mark.parametrize("fault", ["reset", "xrun", "rate", "unplug", "unowned"])
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
    elif fault == "unplug":
        status.unlink()
    else:
        status.write_text(f"state: RUNNING\nowner_pid   : {relay.proc.pid}\nhw_ptr      : 9600\n")
    controls(base, b"s")
    await until(lambda: peer.bookmarks())
    assert peer.bookmarks()[-1][4:] == b""
    assert "state=paused\n" in read_state(base)
    assert not children(relay.proc.pid)
