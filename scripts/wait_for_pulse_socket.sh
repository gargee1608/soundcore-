#!/usr/bin/env bash
# Best-effort wait so Bluetooth playback can attach after boot.
# Missing Pulse must not block ALSA capture, so this always exits 0.
set -u
socket="${PULSE_SERVER:-unix:/run/soundcore/pulse.sock}"
socket="${socket#unix:}"
for _ in $(seq 1 30); do
  if [[ -S "${socket}" ]]; then
    exit 0
  fi
  sleep 0.5
done
echo "Pulse socket ${socket} is not ready. ALSA capture will still start." >&2
exit 0
