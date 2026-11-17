#!/usr/bin/env bash
# Install SoundCore onto a Raspberry Pi (or another Debian system).
# Run as root: sudo ./install.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${ROOT}/deploy/install.conf"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run install.sh as root." >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y \
  python3 python3-venv python3-pip \
  ffmpeg alsa-utils \
  pipewire pipewire-pulse wireplumber pulseaudio-utils \
  bluez

SESSION_USER="${SOUNDCORE_SESSION_USER:-${SESSION_USER:-sensepart}}"
AUDIO_GROUP="${AUDIO_GROUP:-soundcore-audio}"

if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "${INSTALL_ROOT}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi
if ! getent group "${AUDIO_GROUP}" >/dev/null 2>&1; then
  groupadd --system "${AUDIO_GROUP}"
fi
usermod -aG audio,"${AUDIO_GROUP}" "${SERVICE_USER}" || true
if id "${SESSION_USER}" >/dev/null 2>&1; then
  usermod -aG "${AUDIO_GROUP}" "${SESSION_USER}" || true
fi

mkdir -p "${INSTALL_ROOT}" "${DATA_DIR}/db" "${DATA_DIR}/recordings" "${DATA_DIR}/logs" "$(dirname "${ENV_FILE}")"
if [[ "${ROOT}" != "${INSTALL_ROOT}" ]]; then
  tar -C "${ROOT}" \
    --exclude .venv \
    --exclude .git \
    --exclude data \
    --exclude .env \
    -cf - . | tar -C "${INSTALL_ROOT}" -xf -
fi
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${INSTALL_ROOT}" "${DATA_DIR}"

if [[ ! -d "${INSTALL_ROOT}/.venv" ]]; then
  sudo -u "${SERVICE_USER}" python3 -m venv "${INSTALL_ROOT}/.venv"
fi
# requirements.txt includes paho-mqtt and websocket-client for SanketPTT receive.
sudo -u "${SERVICE_USER}" "${INSTALL_ROOT}/.venv/bin/pip" install -r "${INSTALL_ROOT}/requirements.txt"

if [[ ! -f "${ENV_FILE}" ]]; then
  cp "${INSTALL_ROOT}/.env.example" "${ENV_FILE}"
  # The Pi install runs web and worker as two units, talks to ALSA, and
  # keeps Flask on localhost behind the proxy.
  sed -i \
    -e "s|^SOUNDCORE_DATA_DIR=.*|SOUNDCORE_DATA_DIR=${DATA_DIR}|" \
    -e "s|^SOUNDCORE_HOST=.*|SOUNDCORE_HOST=127.0.0.1|" \
    -e "s|^SOUNDCORE_PORT=.*|SOUNDCORE_PORT=${FLASK_PORT}|" \
    -e "s|^SOUNDCORE_AUDIO_BACKEND=.*|SOUNDCORE_AUDIO_BACKEND=hybrid|" \
    -e "s|^SOUNDCORE_EMBED_WORKER=.*|SOUNDCORE_EMBED_WORKER=0|" \
    -e "s|^SOUNDCORE_BEHIND_PROXY=.*|SOUNDCORE_BEHIND_PROXY=1|" \
    -e "s|^SOUNDCORE_COOKIE_SECURE=.*|SOUNDCORE_COOKIE_SECURE=1|" \
    "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
fi
# The shipped default lets the services start without a manual edit.
# Leave a password that was already chosen on a previous install alone.
if grep -Eq '^SOUNDCORE_ADMIN_PASSWORD=.+$' "${ENV_FILE}"; then
  :
elif grep -q '^SOUNDCORE_ADMIN_PASSWORD=' "${ENV_FILE}"; then
  sed -i 's|^SOUNDCORE_ADMIN_PASSWORD=.*|SOUNDCORE_ADMIN_PASSWORD=C1nb3rr1|' "${ENV_FILE}"
else
  printf '\nSOUNDCORE_ADMIN_PASSWORD=C1nb3rr1\n' >> "${ENV_FILE}"
fi
chown root:"${SERVICE_USER}" "${ENV_FILE}"
chmod 640 "${ENV_FILE}"

cp "${INSTALL_ROOT}/deploy/soundcore.service" /etc/systemd/system/soundcore.service
cp "${INSTALL_ROOT}/deploy/soundcore-worker.service" /etc/systemd/system/soundcore-worker.service
install -m 0755 "${INSTALL_ROOT}/scripts/set_admin_password.sh" /usr/local/bin/soundcore-passwd
install -m 0755 "${INSTALL_ROOT}/scripts/audio_diagnostics.sh" /usr/local/bin/soundcore-audio-diagnostics
install -m 0755 "${INSTALL_ROOT}/scripts/wait_for_pulse_socket.sh" "${INSTALL_ROOT}/scripts/wait_for_pulse_socket.sh"
install -m 0755 "${INSTALL_ROOT}/scripts/pulse_socket_access.sh" "${INSTALL_ROOT}/scripts/pulse_socket_access.sh"
chmod 0755 "${INSTALL_ROOT}/scripts/audio_bridge.py" || true

# Real hardware playback uses the desktop user's PipeWire. The worker stays
# unprivileged. A group-owned socket is the path; the localhost bridge is
# only turned on when that socket is not reachable.
install -d -m 0755 /etc/pipewire/pipewire-pulse.conf.d /etc/tmpfiles.d
install -m 0644 "${INSTALL_ROOT}/deploy/pipewire-pulse.conf.d/soundcore.conf" /etc/pipewire/pipewire-pulse.conf.d/soundcore.conf
printf 'd /run/soundcore 2770 root %s -\n' "${AUDIO_GROUP}" > /etc/tmpfiles.d/soundcore-audio.conf
systemd-tmpfiles --create /etc/tmpfiles.d/soundcore-audio.conf || mkdir -p /run/soundcore
chgrp "${AUDIO_GROUP}" /run/soundcore || true
chmod 2770 /run/soundcore || true

if id "${SESSION_USER}" >/dev/null 2>&1; then
  loginctl enable-linger "${SESSION_USER}" || true
  session_home="$(getent passwd "${SESSION_USER}" | cut -d: -f6)"
  session_uid="$(id -u "${SESSION_USER}")"
  if [[ -n "${session_home}" && -d "${session_home}" ]]; then
    install -d -m 0755 -o "${SESSION_USER}" -g "${SESSION_USER}" \
      "${session_home}/.config/systemd/user" \
      "${session_home}/.config/pipewire/pipewire-pulse.conf.d"
    install -m 0644 -o "${SESSION_USER}" -g "${SESSION_USER}" \
      "${INSTALL_ROOT}/deploy/pipewire-pulse.conf.d/soundcore.conf" \
      "${session_home}/.config/pipewire/pipewire-pulse.conf.d/soundcore.conf"
    install -m 0644 -o "${SESSION_USER}" -g "${SESSION_USER}" \
      "${INSTALL_ROOT}/deploy/soundcore-pulse-access.service" \
      "${session_home}/.config/systemd/user/soundcore-pulse-access.service"
    install -m 0644 -o "${SESSION_USER}" -g "${SESSION_USER}" \
      "${INSTALL_ROOT}/deploy/soundcore-audio-bridge.service" \
      "${session_home}/.config/systemd/user/soundcore-audio-bridge.service"
  fi
  systemctl start "user@${session_uid}.service" || true
  sudo -u "${SESSION_USER}" XDG_RUNTIME_DIR="/run/user/${session_uid}" systemctl --user daemon-reload || true
  sudo -u "${SESSION_USER}" XDG_RUNTIME_DIR="/run/user/${session_uid}" systemctl --user enable --now pipewire.service pipewire-pulse.service wireplumber.service soundcore-pulse-access.service || true
fi

if ! grep -q '^SOUNDCORE_SESSION_USER=' "${ENV_FILE}"; then
  printf '\nSOUNDCORE_SESSION_USER=%s\n' "${SESSION_USER}" >> "${ENV_FILE}"
fi
if ! grep -q '^PULSE_SERVER=' "${ENV_FILE}"; then
  printf 'PULSE_SERVER=unix:/run/soundcore/pulse.sock\n' >> "${ENV_FILE}"
fi
if grep -q '^SOUNDCORE_AUDIO_BACKEND=alsa$' "${ENV_FILE}"; then
  # The previous Pi default meant "real hardware", which now includes PipeWire outputs.
  sed -i 's|^SOUNDCORE_AUDIO_BACKEND=alsa$|SOUNDCORE_AUDIO_BACKEND=hybrid|' "${ENV_FILE}"
fi

bridge=0
if id "${SERVICE_USER}" >/dev/null 2>&1 && command -v pactl >/dev/null 2>&1; then
  if ! sudo -u "${SERVICE_USER}" env PULSE_SERVER=unix:/run/soundcore/pulse.sock pactl info >/dev/null 2>&1; then
    bridge=1
  fi
fi
if [[ "${bridge}" -eq 1 && -n "${session_uid:-}" ]]; then
  echo "Pulse socket is not reachable by ${SERVICE_USER}. Enabling the localhost audio bridge."
  sudo -u "${SESSION_USER}" XDG_RUNTIME_DIR="/run/user/${session_uid}" systemctl --user enable --now soundcore-audio-bridge.service || true
  if grep -q '^SOUNDCORE_AUDIO_BRIDGE=' "${ENV_FILE}"; then
    sed -i 's|^SOUNDCORE_AUDIO_BRIDGE=.*|SOUNDCORE_AUDIO_BRIDGE=1|' "${ENV_FILE}"
  else
    printf 'SOUNDCORE_AUDIO_BRIDGE=1\n' >> "${ENV_FILE}"
  fi
else
  if grep -q '^SOUNDCORE_AUDIO_BRIDGE=' "${ENV_FILE}"; then
    sed -i 's|^SOUNDCORE_AUDIO_BRIDGE=.*|SOUNDCORE_AUDIO_BRIDGE=0|' "${ENV_FILE}"
  else
    printf 'SOUNDCORE_AUDIO_BRIDGE=0\n' >> "${ENV_FILE}"
  fi
fi
chown root:"${SERVICE_USER}" "${ENV_FILE}"
chmod 640 "${ENV_FILE}"

systemctl daemon-reload
systemctl enable --now soundcore-worker.service soundcore.service

echo
echo "SoundCore is installed in ${INSTALL_ROOT}."
echo "Data directory: ${DATA_DIR}"
echo "Services started."
echo "Sign in as admin@localhost. The shipped admin password is C1nb3rr1."
echo "Change it now: sudo soundcore-passwd '<newpassword>'"
echo "Optional: S3 keys, SOUNDCORE_BOONDOCK_API_KEY, SMTP, SOUNDCORE_TIMEZONE in ${ENV_FILE}."
echo
echo "HTTPS: scripts/bootstrap.sh installs Caddy with deploy/Caddyfile."
echo "Or point nginx at deploy/nginx.conf.example."
echo "Do not publish port ${FLASK_PORT}."
