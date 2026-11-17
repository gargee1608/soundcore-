#!/usr/bin/env bash
# Run as the PipeWire session user. Make the shared socket group-accessible.
set -u
group="${SOUNDCORE_AUDIO_GROUP:-soundcore-audio}"
socket="/run/soundcore/pulse.sock"
for _ in $(seq 1 50); do
  if [[ -S "${socket}" ]]; then
    chgrp "${group}" "${socket}" 2>/dev/null || true
    chmod 0660 "${socket}" 2>/dev/null || true
    exit 0
  fi
  sleep 0.1
done
echo "Timed out waiting for ${socket}" >&2
exit 1
