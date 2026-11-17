# SoundCore install guide (Raspberry Pi 5)

This is the path from a blank Pi to a signed-in console. The short version is one command. The rest of this page is what that command does, how to sign in, and how to recover a password.

SoundCore has one sign-in. The app does not enforce a separate user role: every route except `/login`, `/healthz`, and `/static/...` requires that session. The tables below still split **user** pages (listen, review, sign in) from **admin** pages (change the appliance). A row in the `users` table can open both.

## 1. Prerequisites

Hardware:

- Raspberry Pi 5, with a power supply that can hold the Pi and the USB audio cards.
- microSD card or SSD for Raspberry Pi OS.
- USB audio capture cards. Each card is one input channel. A 3.5 mm input on a card is another channel.
- Optional USB audio outputs, if you want to relay a channel to a speaker or recorder.
- Ethernet or Wi-Fi so you can open the console from another machine.

OS image:

- Raspberry Pi OS 64-bit (Debian-based). The installer uses `apt` and expects `systemd`.
- Flash the image, boot the Pi, create the user, and connect it to the network.
- Note the hostname or IP address. The examples below call that `<pi-hostname-or-ip>`.

Plug the USB audio cards in **before** the first scan. The `soundcore` user is added to the `audio` group so it can open the devices.

## 2. One-line install

On the Pi, as a user who can `sudo`:

```bash
curl -fsSL https://raw.githubusercontent.com/gargee1608/soundcore-/main/scripts/bootstrap.sh | sudo bash
```

That script:

1. Installs git and Caddy (HTTPS on port 443).
2. Clones or updates `https://github.com/gargee1608/soundcore-.git` into `/opt/soundcore`.
3. Runs `install.sh`, which creates the `soundcore` user, the virtualenv, `/etc/soundcore/soundcore.env`, and the two systemd units, then starts them.
4. Installs `deploy/Caddyfile` as `/etc/caddy/Caddyfile` and reloads Caddy.
5. Installs `soundcore-passwd` on `PATH`.

Flask listens on `127.0.0.1:8741` only. Caddy is the public door. Do not publish port 8741.

The shipped admin password is `C1nb3rr1`. Change it as soon as the services are up:

```bash
sudo soundcore-passwd '<newpassword>'
```

The same program is available as:

```bash
sudo /opt/soundcore/scripts/set_admin_password.sh '<newpassword>'
```

Either form writes `SOUNDCORE_ADMIN_PASSWORD` in `/etc/soundcore/soundcore.env`, sets the password hash for `admin@localhost` in the database, sets `SOUNDCORE_RESET_ADMIN=0`, and restarts `soundcore-worker` and `soundcore`.

## 3. Manual install

Use this when the tree is already on the Pi (a git clone, or a copy you copied yourself).

```bash
sudo apt-get update
sudo apt-get install -y git
git clone --depth 1 --branch main https://github.com/gargee1608/soundcore-.git
cd soundcore-
sudo ./install.sh
```

`install.sh` copies the app to `/opt/soundcore` when you run it from another directory. It stores the database, recordings, and logs in `/var/lib/soundcore`. If `SOUNDCORE_ADMIN_PASSWORD` is empty, it writes `C1nb3rr1`. It then enables and starts `soundcore-worker.service` and `soundcore.service`. No manual edit is required before the first start.

HTTPS is a separate step in the manual path. Install Caddy, then:

```bash
sudo install -m 0644 /opt/soundcore/deploy/Caddyfile /etc/caddy/Caddyfile
sudo systemctl enable --now caddy
sudo systemctl reload caddy
```

Or skip the one-liner and the manual clone and run the bootstrap script from a checkout:

```bash
sudo bash scripts/bootstrap.sh
```

## 4. First login

On another computer on the same network, open:

`https://<pi-hostname-or-ip>/login`

Caddy's shipped file uses `tls internal`, so the browser warns that the certificate is local. Continue past the warning on a trusted LAN, or replace it with a public name (see HTTPS below).

On the Pi itself you can skip Caddy and open:

`http://127.0.0.1:8741/login`

Sign in with:

| Field | Value |
| --- | --- |
| Email | `admin@localhost` |
| Password | `C1nb3rr1`, unless you already ran `soundcore-passwd` |

You land on the desk (`/`). Change the password before you put the Pi on a network you do not control:

```bash
sudo soundcore-passwd '<newpassword>'
```

Sign in again with the new password. The old one stops working as soon as the services finish restarting.

## 5. Channels and outputs

1. Plug in the USB cards, then open **Channels** (`/channels`).
2. Rescan. New ALSA devices show up next to any already saved channels. Each input and each output gets a name, a sample rate, a gain, and an enabled switch.
3. Enabling an input starts a stream: continuous WAV chunks, 45 seconds at 16 kHz 16-bit mono unless you change that under Settings. The default cap is 8 enabled inputs. The desk warns as you approach the cap and refuses another enable at the cap.
4. Open **Routing** (`/routing`) to send an input to a USB output. The relay gets the same PCM the recorder writes.
5. On the desk, listen to one channel in the browser. One tab listens to one channel. Other tabs can listen to other channels. Meters stay quiet until a card is actually capturing, or until you are on the simulated desk (`SOUNDCORE_AUDIO_BACKEND=simulated`).

A Pi install uses `SOUNDCORE_AUDIO_BACKEND=alsa` and `SOUNDCORE_EMBED_WORKER=0`. The worker process owns the devices. The web process only serves the console and the live-listen socket.

## 6. S3, Boondock, and SMTP

Secrets are not on the Settings form. They live in `/etc/soundcore/soundcore.env`. Edit that file, then restart:

```bash
sudo systemctl restart soundcore-worker.service soundcore.service
```

S3 offload. Leave the bucket blank to keep files on the Pi.

```bash
SOUNDCORE_S3_BUCKET=your-bucket
SOUNDCORE_S3_PREFIX=soundcore
SOUNDCORE_S3_REGION=us-east-1
SOUNDCORE_S3_ENDPOINT_URL=
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
```

At 95% disk use, and every midnight for files idle 7 days, SoundCore uploads a closed file and deletes the local copy only after the object size and single-part ETag match.

Boondock transcription:

```bash
SOUNDCORE_BOONDOCK_BASE_URL=https://api.boondock.cloud
SOUNDCORE_BOONDOCK_API_KEY=your-customer-key
SOUNDCORE_BOONDOCK_LANGUAGE=en
SOUNDCORE_TRANSCRIBE_ENABLED=1
```

The key is sent as `X-Boondock-Key`. A missing key leaves jobs pending. The Settings page can turn the queue on or off and set the language; it cannot store the key.

SMTP alerts. The log and the Storage page always record alerts. Email is sent only when the host, from-address, and notify address are set.

```bash
SOUNDCORE_SMTP_HOST=smtp.example.com
SOUNDCORE_SMTP_PORT=587
SOUNDCORE_SMTP_USER=
SOUNDCORE_SMTP_PASSWORD=
SOUNDCORE_SMTP_FROM=soundcore@example.com
SOUNDCORE_SMTP_TLS=1
SOUNDCORE_NOTIFY_EMAIL=operator@example.com
SOUNDCORE_NOTIFY_WEBHOOK=
```

Settings you can change in the console (`/settings`) without a restart, except the timezone (the midnight job reads it on the next process start): stream cap, chunk length, idle days, timezone, Boondock language, notify email, webhook, transcription on/off.

## 7. HTTPS and a domain

`deploy/Caddyfile` is copied to `/etc/caddy/Caddyfile`. It listens on 443 for every name, uses an internal certificate, and reverse-proxies to `127.0.0.1:8741`, including the WebSocket upgrade Socket.IO needs.

For a trusted LAN, keep `tls internal` and install Caddy's local CA in the browsers you use.

For a public name, edit `/etc/caddy/Caddyfile` so the site address is that name and delete the `tls internal` line. Caddy will ask Let's Encrypt. The Pi must be reachable on port 443, and the name must point at the Pi. Then:

```bash
sudo systemctl reload caddy
```

Nginx instead of Caddy: start from `deploy/nginx.conf.example`. Keep Flask on `127.0.0.1:8741`.

## 8. Verification

```bash
systemctl status soundcore.service soundcore-worker.service caddy
curl -fsS http://127.0.0.1:8741/healthz
curl -kfsS https://127.0.0.1/healthz
arecord -l
aplay -l
```

`/healthz` returns JSON with `"status": "ok"`. Then sign in and confirm the desk lists your cards. Enable one input and confirm a new file appears under `/var/lib/soundcore/recordings/`.

Logs:

```bash
sudo journalctl -u soundcore -u soundcore-worker -n 100 --no-pager
sudo tail -n 100 /var/lib/soundcore/logs/soundcore.log
```

## 9. URLs the app exposes

Replace `<pi-hostname-or-ip>` with the Pi's hostname or IP. The Caddy column is what you open from another machine. The Flask column is what you open on the Pi, and what Caddy proxies to. POST requests need the session cookie and a CSRF token (`csrf_token` in a form, or the `X-CSRF-Token` header).

### User URLs

These are sign-in, the desk, listening, and read-only JSON. `/login`, `/healthz`, and `/static/...` do not require a session. The others redirect to `/login` until you sign in.

| Method | Path | On the LAN (Caddy) | On the Pi (Flask) | What it is |
| --- | --- | --- | --- | --- |
| GET | `/login` | `https://<pi-hostname-or-ip>/login` | `http://127.0.0.1:8741/login` | Sign-in page |
| POST | `/login` | `https://<pi-hostname-or-ip>/login` | `http://127.0.0.1:8741/login` | Sign in |
| POST | `/logout` | `https://<pi-hostname-or-ip>/logout` | `http://127.0.0.1:8741/logout` | Sign out |
| GET | `/` | `https://<pi-hostname-or-ip>/` | `http://127.0.0.1:8741/` | Desk |
| GET | `/channels` | `https://<pi-hostname-or-ip>/channels` | `http://127.0.0.1:8741/channels` | Channel list |
| GET | `/routing` | `https://<pi-hostname-or-ip>/routing` | `http://127.0.0.1:8741/routing` | Route list |
| GET | `/recordings` | `https://<pi-hostname-or-ip>/recordings` | `http://127.0.0.1:8741/recordings` | Recording list |
| GET | `/healthz` | `https://<pi-hostname-or-ip>/healthz` | `http://127.0.0.1:8741/healthz` | Process health, no login |
| GET | `/static/css/app.css` | `https://<pi-hostname-or-ip>/static/css/app.css` | `http://127.0.0.1:8741/static/css/app.css` | Console CSS |
| GET | `/static/js/app.js` | `https://<pi-hostname-or-ip>/static/js/app.js` | `http://127.0.0.1:8741/static/js/app.js` | Console JavaScript |
| GET | `/static/js/socket.io.min.js` | `https://<pi-hostname-or-ip>/static/js/socket.io.min.js` | `http://127.0.0.1:8741/static/js/socket.io.min.js` | Vendored Socket.IO client |
| GET | `/static/favicon.svg` | `https://<pi-hostname-or-ip>/static/favicon.svg` | `http://127.0.0.1:8741/static/favicon.svg` | Icon |
| GET | `/socket.io/` | `wss://<pi-hostname-or-ip>/socket.io/` | `ws://127.0.0.1:8741/socket.io/` | Live audio. Events: `connect`, `listen`, `unlisten`, `disconnect`. `connect` is rejected without a session. |
| GET | `/api/status` | `https://<pi-hostname-or-ip>/api/status` | `http://127.0.0.1:8741/api/status` | Desk status JSON |
| GET | `/api/streams` | `https://<pi-hostname-or-ip>/api/streams` | `http://127.0.0.1:8741/api/streams` | Active streams |
| GET | `/api/channels` | `https://<pi-hostname-or-ip>/api/channels` | `http://127.0.0.1:8741/api/channels` | Channels and discovered devices |
| GET | `/api/routes` | `https://<pi-hostname-or-ip>/api/routes` | `http://127.0.0.1:8741/api/routes` | Input-to-output routes |
| GET | `/api/recordings` | `https://<pi-hostname-or-ip>/api/recordings` | `http://127.0.0.1:8741/api/recordings` | Recording list JSON |
| GET | `/api/recordings/<id>/audio` | `https://<pi-hostname-or-ip>/api/recordings/<id>/audio` | `http://127.0.0.1:8741/api/recordings/<id>/audio` | WAV bytes for one recording |
| GET | `/api/transcriptions` | `https://<pi-hostname-or-ip>/api/transcriptions` | `http://127.0.0.1:8741/api/transcriptions` | Transcript list |

### Admin URLs

These change channels, routes, storage, settings, or the export archive. They require the same sign-in as the user URLs.

| Method | Path | On the LAN (Caddy) | On the Pi (Flask) | What it is |
| --- | --- | --- | --- | --- |
| POST | `/channels` | `https://<pi-hostname-or-ip>/channels` | `http://127.0.0.1:8741/channels` | Save a channel |
| POST | `/channels/<id>/delete` | `https://<pi-hostname-or-ip>/channels/<id>/delete` | `http://127.0.0.1:8741/channels/<id>/delete` | Delete a channel |
| POST | `/routing` | `https://<pi-hostname-or-ip>/routing` | `http://127.0.0.1:8741/routing` | Save a route |
| POST | `/routing/<id>/delete` | `https://<pi-hostname-or-ip>/routing/<id>/delete` | `http://127.0.0.1:8741/routing/<id>/delete` | Delete a route |
| POST | `/recordings/<id>/transcribe` | `https://<pi-hostname-or-ip>/recordings/<id>/transcribe` | `http://127.0.0.1:8741/recordings/<id>/transcribe` | Queue transcription for one file |
| GET | `/storage` | `https://<pi-hostname-or-ip>/storage` | `http://127.0.0.1:8741/storage` | Disk, offload, alerts |
| POST | `/storage/check` | `https://<pi-hostname-or-ip>/storage/check` | `http://127.0.0.1:8741/storage/check` | Run the storage guard once |
| POST | `/storage/events/<id>/ack` | `https://<pi-hostname-or-ip>/storage/events/<id>/ack` | `http://127.0.0.1:8741/storage/events/<id>/ack` | Acknowledge an alert |
| GET | `/settings` | `https://<pi-hostname-or-ip>/settings` | `http://127.0.0.1:8741/settings` | Settings form, export, import |
| POST | `/settings` | `https://<pi-hostname-or-ip>/settings` | `http://127.0.0.1:8741/settings` | Save console settings |
| GET | `/export` | `https://<pi-hostname-or-ip>/export` | `http://127.0.0.1:8741/export` | Download the export zip |
| POST | `/import` | `https://<pi-hostname-or-ip>/import` | `http://127.0.0.1:8741/import` | Restore an export zip |
| POST | `/api/channels` | `https://<pi-hostname-or-ip>/api/channels` | `http://127.0.0.1:8741/api/channels` | Create a channel |
| POST | `/api/channels/<id>` | `https://<pi-hostname-or-ip>/api/channels/<id>` | `http://127.0.0.1:8741/api/channels/<id>` | Update a channel |
| POST | `/api/channels/<id>/delete` | `https://<pi-hostname-or-ip>/api/channels/<id>/delete` | `http://127.0.0.1:8741/api/channels/<id>/delete` | Delete a channel |
| POST | `/api/routes` | `https://<pi-hostname-or-ip>/api/routes` | `http://127.0.0.1:8741/api/routes` | Create a route |
| POST | `/api/routes/<id>/delete` | `https://<pi-hostname-or-ip>/api/routes/<id>/delete` | `http://127.0.0.1:8741/api/routes/<id>/delete` | Delete a route |
| POST | `/api/recordings/<id>/transcribe` | `https://<pi-hostname-or-ip>/api/recordings/<id>/transcribe` | `http://127.0.0.1:8741/api/recordings/<id>/transcribe` | Queue transcription |
| GET | `/api/storage` | `https://<pi-hostname-or-ip>/api/storage` | `http://127.0.0.1:8741/api/storage` | Storage JSON |
| POST | `/api/storage/check` | `https://<pi-hostname-or-ip>/api/storage/check` | `http://127.0.0.1:8741/api/storage/check` | Run the storage guard |
| POST | `/api/storage/events/<id>/ack` | `https://<pi-hostname-or-ip>/api/storage/events/<id>/ack` | `http://127.0.0.1:8741/api/storage/events/<id>/ack` | Acknowledge an alert |
| GET | `/api/admin/settings` | `https://<pi-hostname-or-ip>/api/admin/settings` | `http://127.0.0.1:8741/api/admin/settings` | Read console settings |
| POST | `/api/admin/settings` | `https://<pi-hostname-or-ip>/api/admin/settings` | `http://127.0.0.1:8741/api/admin/settings` | Update console settings |
| GET | `/api/export` | `https://<pi-hostname-or-ip>/api/export` | `http://127.0.0.1:8741/api/export` | Export zip |
| POST | `/api/import` | `https://<pi-hostname-or-ip>/api/import` | `http://127.0.0.1:8741/api/import` | Import zip |

## 10. Lost password

### Admin password

Preferred. This updates the env file, the hash for the admin email in that file (`admin@localhost` on a default install), and restarts both services:

```bash
sudo soundcore-passwd '<newpassword>'
```

```bash
sudo /opt/soundcore/scripts/set_admin_password.sh '<newpassword>'
```

Manual fallback, if that command is missing. Edit `/etc/soundcore/soundcore.env`:

```bash
SOUNDCORE_ADMIN_PASSWORD=<newpassword>
SOUNDCORE_RESET_ADMIN=1
```

Restart so the web and worker processes rewrite the hash, then turn the reset flag off and restart again so later boots do not keep rewriting it:

```bash
sudo systemctl restart soundcore-worker.service soundcore.service
sudo sed -i 's|^SOUNDCORE_RESET_ADMIN=.*|SOUNDCORE_RESET_ADMIN=0|' /etc/soundcore/soundcore.env
sudo systemctl restart soundcore-worker.service soundcore.service
```

Database-only fallback. Use this when you want to set the hash yourself and the env file already contains the new password. Stop the services first so SQLite is not mid-write:

```bash
sudo systemctl stop soundcore.service soundcore-worker.service
sudo SOUNDCORE_NEW_PASSWORD='<newpassword>' /opt/soundcore/.venv/bin/python - << 'PY'
import os
import sqlite3
from pathlib import Path

from werkzeug.security import generate_password_hash

env = {}
for line in Path("/etc/soundcore/soundcore.env").read_text(encoding="utf-8").splitlines():
    if not line.strip() or line.strip().startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    env[key.strip()] = value.strip().strip('"').strip("'")
email = (env.get("SOUNDCORE_ADMIN_EMAIL") or "admin@localhost").strip().lower()
db_path = env.get("SOUNDCORE_DATABASE_URL") or ""
if db_path.startswith("sqlite:///"):
    path = db_path[len("sqlite:///"):]
else:
    data = env.get("SOUNDCORE_DATA_DIR") or "/var/lib/soundcore"
    path = str(Path(data) / "db" / "soundcore.db")
digest = generate_password_hash(os.environ["SOUNDCORE_NEW_PASSWORD"])
conn = sqlite3.connect(path)
conn.execute("UPDATE users SET password_hash = ? WHERE email = ?", (digest, email))
print("rows", conn.total_changes)
conn.commit()
conn.close()
PY
sudo systemctl start soundcore-worker.service soundcore.service
```

Put the same `<newpassword>` in `SOUNDCORE_ADMIN_PASSWORD` as well. If the env password and the hash disagree, the next restart with `SOUNDCORE_RESET_ADMIN=1` will make the env file win.

### Any other user

The console does not create extra accounts. `authenticate` will accept any email that already has a row in `users`, and that session can open every URL in both tables. To reset one of those passwords, stop the services and set that row's hash. This does not change `SOUNDCORE_ADMIN_PASSWORD`.

```bash
sudo systemctl stop soundcore.service soundcore-worker.service
sudo SOUNDCORE_USER_EMAIL='other@example.com' SOUNDCORE_NEW_PASSWORD='<newpassword>' \
  /opt/soundcore/.venv/bin/python - << 'PY'
import os
import sqlite3
from pathlib import Path

from werkzeug.security import generate_password_hash

env = {}
for line in Path("/etc/soundcore/soundcore.env").read_text(encoding="utf-8").splitlines():
    if not line.strip() or line.strip().startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    env[key.strip()] = value.strip().strip('"').strip("'")
db_url = env.get("SOUNDCORE_DATABASE_URL") or ""
if db_url.startswith("sqlite:///"):
    path = db_url[len("sqlite:///"):]
else:
    data = env.get("SOUNDCORE_DATA_DIR") or "/var/lib/soundcore"
    path = str(Path(data) / "db" / "soundcore.db")
email = os.environ["SOUNDCORE_USER_EMAIL"].strip().lower()
digest = generate_password_hash(os.environ["SOUNDCORE_NEW_PASSWORD"])
conn = sqlite3.connect(path)
cur = conn.execute("UPDATE users SET password_hash = ? WHERE email = ?", (digest, email))
if cur.rowcount != 1:
    raise SystemExit(f"No user named {email}")
conn.commit()
conn.close()
print(f"Updated {email}")
PY
sudo systemctl start soundcore-worker.service soundcore.service
```

List the accounts first if you are not sure of the email:

```bash
sudo sqlite3 /var/lib/soundcore/db/soundcore.db "SELECT id, email, role FROM users;"
```

## 11. Troubleshooting

- **Services did not stay up.** `systemctl status soundcore soundcore-worker` and `journalctl -u soundcore -u soundcore-worker`. The usual cause is a Python dependency or a typo in the env file.
- **Sign-in says the password does not match.** Run `sudo soundcore-passwd '<newpassword>'`. An empty `SOUNDCORE_ADMIN_EMAIL` skips account creation; a default install sets it to `admin@localhost`.
- **No USB cards on the Channels page.** `arecord -l` and `aplay -l` should list them. Replug, then reload Channels. The `soundcore` user must be in the `audio` group (`groups soundcore`).
- **Worker and web both grab the same device.** On the Pi, `/etc/soundcore/soundcore.env` must have `SOUNDCORE_EMBED_WORKER=0` and `SOUNDCORE_AUDIO_BACKEND=alsa`. `install.sh` sets both.
- **Browser cannot connect on 8741 from another machine.** That is intentional. Use `https://<pi-hostname-or-ip>/`. Check `systemctl status caddy`.
- **Certificate warning.** Expected with `tls internal`. Trust Caddy's local CA, or switch the Caddyfile to a public name.
- **Disk filled and recording stopped.** Open Storage. Closed files offload only when S3 is configured and the upload verifies. Open files and files played in the last two minutes stay on disk.
- **Transcription stays pending.** `SOUNDCORE_BOONDOCK_API_KEY` is empty, or the Settings checkbox is off. A missing key does not burn retries.

## 12. Uninstall

Keep recordings and the database:

```bash
sudo /opt/soundcore/uninstall.sh
```

That stops the two units, removes them, removes `/opt/soundcore`, and removes `/usr/local/bin/soundcore-passwd`. It leaves `/var/lib/soundcore` and `/etc/soundcore/soundcore.env`.

Remove recordings, the database, and logs as well:

```bash
sudo /opt/soundcore/uninstall.sh --purge
```

Run that while `/opt/soundcore` is still present, because the script lives there. Caddy is not removed. To turn HTTPS off:

```bash
sudo systemctl disable --now caddy
```

Delete `/etc/soundcore/soundcore.env` yourself when you want the admin password and cloud keys gone.
