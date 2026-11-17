#!/usr/bin/env bash
# Report ALSA, PipeWire, Bluetooth, and the soundcore user's access.
# Usage: sudo /opt/soundcore/scripts/audio_diagnostics.sh
set -u

section() {
  printf '\n== %s ==\n' "$1"
}

run() {
  if command -v "$1" >/dev/null 2>&1; then
    shift
    "$@" || true
  else
    echo "missing command: $1"
  fi
}

section "ALSA capture (arecord -l)"
if command -v arecord >/dev/null 2>&1; then
  arecord -l || true
else
  echo "arecord is not installed"
fi

section "ALSA playback (aplay -l)"
if command -v aplay >/dev/null 2>&1; then
  aplay -l || true
else
  echo "aplay is not installed"
fi

section "PipeWire / WirePlumber"
if command -v systemctl >/dev/null 2>&1; then
  systemctl --user --machine="${SOUNDCORE_SESSION_USER:-sensepart}@" status pipewire pipewire-pulse wireplumber --no-pager 2>/dev/null || \
    systemctl status pipewire pipewire-pulse wireplumber --no-pager 2>/dev/null || true
fi
if command -v wpctl >/dev/null 2>&1; then
  wpctl status || true
else
  echo "wpctl is not installed"
fi

section "Pulse sinks"
if command -v pactl >/dev/null 2>&1; then
  PULSE_SERVER="${PULSE_SERVER:-unix:/run/soundcore/pulse.sock}" pactl list short sinks || pactl list short sinks || true
else
  echo "pactl is not installed"
fi

section "Pulse sources"
if command -v pactl >/dev/null 2>&1; then
  PULSE_SERVER="${PULSE_SERVER:-unix:/run/soundcore/pulse.sock}" pactl list short sources || pactl list short sources || true
fi

section "Bluetooth"
if command -v bluetoothctl >/dev/null 2>&1; then
  bluetoothctl show || true
  echo "-- paired --"
  bluetoothctl devices || true
  echo "-- connected --"
  bluetoothctl devices Connected || true
  echo "-- audio info --"
  bluetoothctl info || true
else
  echo "bluetoothctl is not installed"
fi

section "soundcore user"
id soundcore 2>/dev/null || echo "user soundcore does not exist"
groups soundcore 2>/dev/null || true
namei -l /run/soundcore/pulse.sock 2>/dev/null || ls -l /run/soundcore /run/soundcore/pulse.sock 2>/dev/null || echo "shared pulse socket is absent"

section "socket access as soundcore"
if id soundcore >/dev/null 2>&1; then
  sudo -u soundcore env PULSE_SERVER="${PULSE_SERVER:-unix:/run/soundcore/pulse.sock}" pactl info || \
    echo "soundcore cannot open the Pulse socket"
else
  echo "skipped"
fi

section "worker logs"
if command -v journalctl >/dev/null 2>&1; then
  journalctl -u soundcore-worker -n 80 --no-pager || true
fi
if [[ -f /var/lib/soundcore/logs/soundcore.log ]]; then
  echo "-- soundcore.log (tail) --"
  tail -n 40 /var/lib/soundcore/logs/soundcore.log || true
fi
if [[ -f /var/lib/soundcore/logs/audio_status.json ]]; then
  echo "-- audio_status.json --"
  cat /var/lib/soundcore/logs/audio_status.json || true
fi
