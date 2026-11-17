#!/usr/bin/env bash
# Stop SoundCore and remove the install. Recordings are kept unless --purge is passed.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "${ROOT}/deploy/install.conf"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run uninstall.sh as root." >&2
  exit 1
fi

systemctl disable --now soundcore.service soundcore-worker.service 2>/dev/null || true
rm -f /etc/systemd/system/soundcore.service /etc/systemd/system/soundcore-worker.service
systemctl daemon-reload

if [[ "${1:-}" == "--purge" ]]; then
  rm -rf "${INSTALL_ROOT}" "${DATA_DIR}"
  echo "Removed ${INSTALL_ROOT} and ${DATA_DIR}."
else
  rm -rf "${INSTALL_ROOT}"
  echo "Removed ${INSTALL_ROOT}. Recordings and the database remain in ${DATA_DIR}."
fi
echo "Left ${ENV_FILE} in place. Delete it yourself if you want the secrets gone."
