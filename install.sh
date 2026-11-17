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
apt-get install -y python3 python3-venv python3-pip ffmpeg alsa-utils

if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "${INSTALL_ROOT}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi
usermod -aG audio "${SERVICE_USER}" || true

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
sudo -u "${SERVICE_USER}" "${INSTALL_ROOT}/.venv/bin/pip" install -r "${INSTALL_ROOT}/requirements.txt"

if [[ ! -f "${ENV_FILE}" ]]; then
  cp "${INSTALL_ROOT}/.env.example" "${ENV_FILE}"
  # The Pi install runs web and worker as two units, talks to ALSA, and
  # keeps Flask on localhost behind the proxy.
  sed -i \
    -e "s|^SOUNDCORE_DATA_DIR=.*|SOUNDCORE_DATA_DIR=${DATA_DIR}|" \
    -e "s|^SOUNDCORE_HOST=.*|SOUNDCORE_HOST=127.0.0.1|" \
    -e "s|^SOUNDCORE_PORT=.*|SOUNDCORE_PORT=${FLASK_PORT}|" \
    -e "s|^SOUNDCORE_AUDIO_BACKEND=.*|SOUNDCORE_AUDIO_BACKEND=alsa|" \
    -e "s|^SOUNDCORE_EMBED_WORKER=.*|SOUNDCORE_EMBED_WORKER=0|" \
    -e "s|^SOUNDCORE_BEHIND_PROXY=.*|SOUNDCORE_BEHIND_PROXY=1|" \
    -e "s|^SOUNDCORE_COOKIE_SECURE=.*|SOUNDCORE_COOKIE_SECURE=1|" \
    "${ENV_FILE}"
  chmod 600 "${ENV_FILE}"
fi
chown root:"${SERVICE_USER}" "${ENV_FILE}"
chmod 640 "${ENV_FILE}"

cp "${INSTALL_ROOT}/deploy/soundcore.service" /etc/systemd/system/soundcore.service
cp "${INSTALL_ROOT}/deploy/soundcore-worker.service" /etc/systemd/system/soundcore-worker.service
systemctl daemon-reload

echo
echo "SoundCore is installed in ${INSTALL_ROOT}."
echo "Data directory: ${DATA_DIR}"
echo "Edit ${ENV_FILE} and set SOUNDCORE_ADMIN_PASSWORD before starting."
echo "Optional: S3 keys, SOUNDCORE_BOONDOCK_API_KEY, SMTP, SOUNDCORE_TIMEZONE."
echo
if grep -Eq '^SOUNDCORE_ADMIN_PASSWORD=.+$' "${ENV_FILE}"; then
  systemctl enable --now soundcore-worker.service soundcore.service
  echo "Services started."
else
  echo "Not started yet. After the password is set:"
  echo "  sudo systemctl enable --now soundcore-worker.service soundcore.service"
fi
echo
echo "HTTPS: point Caddy at deploy/Caddyfile, or nginx at deploy/nginx.conf.example."
echo "Do not publish port ${FLASK_PORT}."
