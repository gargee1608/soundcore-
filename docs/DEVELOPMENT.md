# Development guide — Live Audio Broadcasting (SoundCore)

Use this workflow on your PC first (simulated audio), then deploy the same codebase on a Raspberry Pi with real USB cards and Sanket PTT.

## Prerequisites

| Environment | Requirements |
| --- | --- |
| **Windows / macOS / Linux (dev)** | Python 3.11+ (3.12 recommended), Git |
| **Raspberry Pi 5 (production)** | Raspberry Pi OS 64-bit, 4× USB audio outputs (optional captures), network |

Architecture notes: [`LIVE_BROADCAST_ASSESSMENT.md`](LIVE_BROADCAST_ASSESSMENT.md). Pi install: [`INSTALL_GUIDE.md`](INSTALL_GUIDE.md).

---

## Step 1 — Get the code

You already have the repo at:

`c:\Users\Nandani\OneDrive - sensepart technologies\SoundCore`

To refresh from GitHub:

```powershell
cd "c:\Users\Nandani\OneDrive - sensepart technologies\SoundCore"
git pull origin main
```

---

## Step 2 — Python environment (local dev)

### Windows (PowerShell)

```powershell
cd "c:\Users\Nandani\OneDrive - sensepart technologies\SoundCore"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

If `Activate.ps1` is blocked:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

If corporate policy blocks `pip.exe`, use **`python -m pip`** only (never call `pip.exe` directly).

### Windows Application Control (SQLAlchemy blocked)

On some managed PCs you may see:

`ImportError: DLL load failed while importing _processors_cy: An Application Control policy has blocked this file.`

SoundCore cannot start until SQLAlchemy’s native extensions are allowed. Use one of these:

1. **WSL2 (recommended on Windows)** — install Ubuntu in WSL, clone the repo inside Linux, then follow Step 2–4 using `python3` there.
2. **Develop on the Raspberry Pi** — clone to `/opt/soundcore` or your home directory and run with `simulated` or `hybrid` backend.
3. **Ask IT** — allowlist Python extension modules under `.venv\Lib\site-packages\sqlalchemy\` for your project path.

Editing code on Windows is fine; **run** the app in WSL or on the Pi.

### Linux / macOS

```bash
cd /path/to/SoundCore
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Optional tests:

```bash
pip install -r requirements-dev.txt
pytest
```

---

## Step 3 — Local configuration

```powershell
Copy-Item .env.example .env
```

Edit `.env` for desktop development (minimum):

```ini
SOUNDCORE_ADMIN_EMAIL=admin@localhost
SOUNDCORE_ADMIN_PASSWORD=C1nb3rr1
SOUNDCORE_HOST=127.0.0.1
SOUNDCORE_PORT=8741
SOUNDCORE_AUDIO_BACKEND=simulated
SOUNDCORE_EMBED_WORKER=1
SOUNDCORE_SEED_DEMO=1
SOUNDCORE_DATA_DIR=data
```

| Variable | Dev value | Meaning |
| --- | --- | --- |
| `SOUNDCORE_AUDIO_BACKEND=simulated` | Required on PC | No ALSA/USB; tones exercise routing and meters |
| `SOUNDCORE_EMBED_WORKER=1` | Required on PC | Audio worker runs inside the web process |
| `SOUNDCORE_SEED_DEMO=1` | Optional | Enables first demo inputs on a new database |
| `SOUNDCORE_STREAM_CAP=8` | Default | Max enabled inputs (broadcast spec) |

Boondock, S3, and SMTP can stay empty for routing-only work.

---

## Step 4 — Start the application

```powershell
python -m soundcore
```

Open: **http://127.0.0.1:8741/login**

| Field | Value |
| --- | --- |
| Email | `admin@localhost` |
| Password | `C1nb3rr1` (change after first login) |

### What to verify on the desk

1. **Desk (cockpit)** — stream counts, meters, Pi health (Linux/Pi shows CPU/RAM/temp; Windows shows memory only where available).
2. **Channels** — rescan (simulated devices), enable inputs, add Sanket PTT via `https://vibhvesh.github.io/sanketptt/?ch=YOUR_CHANNEL`.
3. **Routing** — one input → multiple outputs (one-to-many); multiple inputs → same USB output (many-to-one via **OutputMixer**).
4. **Activity** — connection and routing events.
5. **Listen** — browser playback on one channel (Socket.IO).

Stop the server: `Ctrl+C`.

---

## Step 5 — Development phases (what to build next)

Work in this order (matches the master prompt):

| Phase | Task | Where in codebase |
| --- | --- | --- |
| **2** | HTTP/HLS/MP3 stream inputs | `soundcore/audio/capture.py`, `ops.py`, Channels UI |
| **3** | USB output discovery + test tone | `soundcore/audio/detect.py`, new API route |
| **4–5** | Routing matrix UI + per-route gain | `models.Route`, `routing.html` |
| **6** | Microphone / line-in capture | ALSA `arecord` paths in `capture.py` |
| **7** | Start/stop recording sessions | `audio/service.py`, recordings API |
| **8–9** | Cockpit polish, push status over Socket.IO | `status.py`, `web/` |
| **10** | Pi soak + `measure_capacity.py` | On hardware only |

Recent work already in tree:

- `soundcore/audio/mixer.py` — many-to-one USB mixing
- `soundcore/system_health.py` — real host metrics
- Cockpit cards on **Desk** dashboard

---

## Step 6 — Raspberry Pi deployment

On the Pi (not on Windows):

```bash
curl -fsSL https://raw.githubusercontent.com/gargee1608/soundcore-/main/scripts/bootstrap.sh | sudo bash
```

Then:

1. Open `https://<pi-ip-or-hostname>/login` (Caddy TLS).
2. Change password: `sudo soundcore-passwd 'YourStrongPassword'`.
3. Plug **four USB audio outputs** before **Channels → Rescan**.
4. Name each output; create inputs (Sanket + ALSA).
5. **Routing** — map streams to USB devices.

Pi-specific `.env` lives at `/etc/soundcore/soundcore.env`:

```ini
SOUNDCORE_AUDIO_BACKEND=hybrid
SOUNDCORE_EMBED_WORKER=0
SOUNDCORE_DATA_DIR=/var/lib/soundcore
```

Services:

```bash
sudo systemctl status soundcore soundcore-worker
sudo journalctl -u soundcore-worker -f
```

---

## Step 7 — Sanket PTT (live streams)

No extra credentials in the repo. In **Channels → Add Sanket PTT**:

- Paste: `https://vibhvesh.github.io/sanketptt/?ch=channel_name`
- Or type the channel name only.

Protocol (verified from [vibhvesh/sanketptt](https://github.com/vibhvesh/sanketptt)):

- MQTT over WebSocket, prefix `ptt-radio-66ab9178/`
- Audio: 12 kHz mono PCM on topic `.../audio`

Enable the channel and assign routes to USB outputs.

---

## Step 8 — Troubleshooting

| Problem | Action |
| --- | --- |
| `ModuleNotFoundError` | Activate `.venv`, `python -m pip install -r requirements.txt` |
| SQLAlchemy DLL blocked (Windows policy) | Develop on WSL2/Linux VM, or use the Pi |
| No meters on desk | Enable at least one input; wait ~1s for `/api/status` poll |
| Sanket silent | Confirm channel name, PTT activity on that channel, worker logs |
| USB missing after reboot | Rescan; check `device_id` in Channels (ALSA/PipeWire, not raw USB index) |
| Port in use | Change `SOUNDCORE_PORT` in `.env` |

---

## Step 9 — Git workflow (your machine)

Commit only when you intend to:

```powershell
git status
git add -A
git commit -m "Describe your change"
```

Push to your fork when remote is configured.

---

## Quick reference — one command dev start

```powershell
cd "c:\Users\Nandani\OneDrive - sensepart technologies\SoundCore"
.\.venv\Scripts\Activate.ps1
python -m soundcore
```

Login: **http://127.0.0.1:8741** · `admin@localhost` / `C1nb3rr1`