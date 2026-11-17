"""Ramp simulated capture until this machine looks busy.

The number it prints is an estimate for this host, not a promise for a Pi 5.
Run it on the target Pi, then set SOUNDCORE_STREAM_CAP from the result.
16-bit 16 kHz mono is about 32 KB/s per channel. USB power and disk usually
bind before CPU does.
"""

from __future__ import annotations

import os
import time

from soundcore.audio.capture import SimulatedCapture
from soundcore.audio.pcm import pcm_rms


def _cpu_ratio(start, end) -> float:
    elapsed = end[4] - start[4]
    if elapsed <= 0:
        return 0.0
    busy = (end[0] - start[0]) + (end[1] - start[1])
    return busy / elapsed


def main() -> None:
    sample_rate = 16000
    frames = int(sample_rate * 0.02)
    recommended = 1
    print("channels  cpu_ratio  rms  write_kb_s")
    for count in range(1, 13):
        devices = [SimulatedCapture(f"sim:load-{index}", sample_rate, 0.0) for index in range(count)]
        for device in devices:
            device.start()
        start = os.times()
        began = time.perf_counter()
        written = 0
        level = 0.0
        while time.perf_counter() - began < 0.6:
            for device in devices:
                pcm = device.read(frames)
                written += len(pcm)
                level = pcm_rms(pcm)
        end = os.times()
        for device in devices:
            device.stop()
        elapsed = max(time.perf_counter() - began, 0.001)
        ratio = _cpu_ratio(start, end)
        kb_s = (written / elapsed) / 1024
        print(f"{count:8d}  {ratio:9.2f}  {level:4.2f}  {kb_s:8.1f}")
        if ratio < 0.70:
            recommended = count
        else:
            break
    print(f"recommended_stream_cap={recommended}")
    print("Set SOUNDCORE_STREAM_CAP to that value after you trust a longer soak on the Pi.")


if __name__ == "__main__":
    main()
