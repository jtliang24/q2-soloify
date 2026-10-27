"""aplay for tests: takes 44.1 kHz S16 stereo from stdin in real time, with a 50 ms buffer."""

import os
import time

RATE = 44100 * 4
BUFFER = 0.05

clock = None  # when the audio read so far finishes playing
while data := os.read(0, 4096):
    now = time.monotonic()
    clock = max(clock or now, now) + len(data) / RATE  # an underrun restarts the clock
    if clock - now > BUFFER:
        time.sleep(clock - now - BUFFER)
