#!/usr/bin/env bash
# Set the SoundCore admin password, update the database hash, and restart services.
# Usage:
#   sudo soundcore-passwd <newpassword>
#   sudo /opt/soundcore/scripts/set_admin_password.sh <newpassword>
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo soundcore-passwd <newpassword>" >&2
  exit 1
fi

if [[ $# -ne 1 || -z "${1}" ]]; then
  echo "Usage: sudo soundcore-passwd <newpassword>" >&2
  exit 1
fi

if [[ "${1}" == *$'\n'* ]]; then
  echo "Password must be a single line." >&2
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -f "${SCRIPT_DIR}/../deploy/install.conf" ]]; then
  # shellcheck disable=SC1091
  source "${SCRIPT_DIR}/../deploy/install.conf"
elif [[ -f /opt/soundcore/deploy/install.conf ]]; then
  # shellcheck disable=SC1091
  source /opt/soundcore/deploy/install.conf
else
  INSTALL_ROOT=/opt/soundcore
  ENV_FILE=/etc/soundcore/soundcore.env
  DATA_DIR=/var/lib/soundcore
fi

if [[ ! -f "${ENV_FILE}" ]]; then
  echo "Env file not found: ${ENV_FILE}" >&2
  exit 1
fi

export SOUNDCORE_NEW_ADMIN_PASSWORD="${1}"
export SOUNDCORE_ENV_FILE="${ENV_FILE}"
export SOUNDCORE_INSTALL_ROOT="${INSTALL_ROOT}"

PYTHON="${INSTALL_ROOT}/.venv/bin/python"
if [[ ! -x "${PYTHON}" ]]; then
  PYTHON="$(command -v python3)"
fi

"${PYTHON}" - << 'PY'
import os
import sys
from pathlib import Path

env_path = Path(os.environ["SOUNDCORE_ENV_FILE"])
password = os.environ["SOUNDCORE_NEW_ADMIN_PASSWORD"]
install_root = Path(os.environ["SOUNDCORE_INSTALL_ROOT"])


def format_value(value: str) -> str:
    if value == "" or any(ch.isspace() or ch in "\\\"$'`#" for ch in value):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


text = env_path.read_text(encoding="utf-8")
lines = text.splitlines()
updates = {
    "SOUNDCORE_ADMIN_PASSWORD": format_value(password),
    "SOUNDCORE_RESET_ADMIN": "0",
}
seen: set[str] = set()
out: list[str] = []
for line in lines:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in line:
        out.append(line)
        continue
    key = line.split("=", 1)[0].strip()
    if key in updates:
        out.append(f"{key}={updates[key]}")
        seen.add(key)
    else:
        out.append(line)
for key, value in updates.items():
    if key not in seen:
        out.append(f"{key}={value}")
env_path.write_text("\n".join(out) + "\n", encoding="utf-8")

loaded: dict[str, str] = {}
for line in out:
    stripped = line.strip()
    if not stripped or stripped.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    loaded[key] = value

sys.path.insert(0, str(install_root))
loaded["SOUNDCORE_ADMIN_PASSWORD"] = password
loaded["SOUNDCORE_RESET_ADMIN"] = "0"

from werkzeug.security import generate_password_hash

from soundcore.config import load_settings
from soundcore.db import create_all, init_engine, session_scope
from soundcore.models import User

settings = load_settings(environ=loaded)
init_engine(settings.database_url, force=True)
create_all()
email = (settings.admin_email or "admin@localhost").strip().lower()
with session_scope() as db:
    user = db.query(User).filter_by(email=email).one_or_none()
    if user is None:
        user = User(email=email, password_hash=generate_password_hash(password), role="admin")
        db.add(user)
    else:
        user.password_hash = generate_password_hash(password)
    db.flush()
print(f"Updated password hash for {email}.")
PY

if command -v systemctl >/dev/null 2>&1 && [[ -f /etc/systemd/system/soundcore.service ]]; then
  systemctl restart soundcore-worker.service soundcore.service
  echo "Restarted soundcore-worker and soundcore."
else
  echo "Service units are not installed. Start SoundCore after install.sh."
fi
