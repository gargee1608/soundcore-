#!/usr/bin/env bash
# One-line install for a Raspberry Pi 5 (or another Debian system):
#   curl -fsSL https://raw.githubusercontent.com/gargee1608/soundcore-/main/scripts/bootstrap.sh | sudo bash
set -euo pipefail

# curl | bash feeds this script on stdin. Save the rest and re-exec so apt and
# git do not consume the remainder of the script. A normal file path runs as-is.
if [[ "${SOUNDCORE_BOOTSTRAP_SAVED:-}" != 1 ]]; then
  _src="${BASH_SOURCE[0]:-}"
  if [[ ! -f "${_src}" ]]; then
    tmp="$(mktemp)"
    cat > "${tmp}"
    chmod 700 "${tmp}"
    export SOUNDCORE_BOOTSTRAP_SAVED=1
    export SOUNDCORE_BOOTSTRAP_FILE="${tmp}"
    exec bash "${tmp}"
  fi
fi

set -euo pipefail

if [[ -n "${SOUNDCORE_BOOTSTRAP_FILE:-}" ]]; then
  trap 'rm -f "${SOUNDCORE_BOOTSTRAP_FILE}"' EXIT
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: curl -fsSL https://raw.githubusercontent.com/gargee1608/soundcore-/main/scripts/bootstrap.sh | sudo bash" >&2
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
REPO_URL="https://github.com/gargee1608/soundcore-.git"
INSTALL_ROOT="/opt/soundcore"
BRANCH="main"

apt-get update
apt-get install -y git ca-certificates curl debian-keyring debian-archive-keyring apt-transport-https gnupg

install -d -m 0755 /usr/share/keyrings
rm -f /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
  | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
  > /etc/apt/sources.list.d/caddy-stable.list
apt-get update
apt-get install -y caddy

if command -v systemctl >/dev/null 2>&1; then
  systemctl stop soundcore.service soundcore-worker.service 2>/dev/null || true
fi

if [[ -d "${INSTALL_ROOT}/.git" ]]; then
  git -c safe.directory="${INSTALL_ROOT}" -C "${INSTALL_ROOT}" fetch origin "${BRANCH}"
  git -c safe.directory="${INSTALL_ROOT}" -C "${INSTALL_ROOT}" checkout "${BRANCH}"
  git -c safe.directory="${INSTALL_ROOT}" -C "${INSTALL_ROOT}" pull --ff-only origin "${BRANCH}"
else
  if [[ -d "${INSTALL_ROOT}" ]]; then
    rm -rf "${INSTALL_ROOT}"
  fi
  git clone --depth 1 --branch "${BRANCH}" "${REPO_URL}" "${INSTALL_ROOT}"
fi

bash "${INSTALL_ROOT}/install.sh"

install -d -m 0755 /etc/caddy
install -m 0644 "${INSTALL_ROOT}/deploy/Caddyfile" /etc/caddy/Caddyfile
if command -v systemctl >/dev/null 2>&1; then
  systemctl enable --now caddy
  systemctl reload caddy || systemctl restart caddy
fi

echo
echo "SoundCore is installed."
echo "Open https://<this-host>/login"
echo "Email admin@localhost, password C1nb3rr1."
echo "Change that password now: sudo soundcore-passwd '<newpassword>'"
echo "The browser will warn about Caddy's internal certificate until you trust it or set a public name."
