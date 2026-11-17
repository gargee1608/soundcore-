# SoundCore

SoundCore is a small audio appliance for a Raspberry Pi 5. Each USB audio card is one channel. A 3.5 mm input is another channel. Enabled inputs record continuously to their own files while you listen in the browser or relay the same audio to a USB output.

Transcription goes to the Boondock API. Long-term copies go to S3, and a local file is removed only after the object’s size and ETag have been checked.

The web process is Flask plus a plain HTML, CSS, and JavaScript console. The database is SQLite through SQLAlchemy, so a later move to Postgres is a `SOUNDCORE_DATABASE_URL` change rather than a rewrite.

## Run it here, without a Pi

Simulated radios, an FM receiver, a handheld, a software-PTT input, a 3.5 mm input, and two USB outputs are built in. No ALSA device is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

- `SOUNDCORE_ADMIN_EMAIL` and `SOUNDCORE_ADMIN_PASSWORD` — required. There is no default password.
- `SOUNDCORE_AUDIO_BACKEND=simulated`
- `SOUNDCORE_EMBED_WORKER=1` so capture runs inside the web process
- `SOUNDCORE_SEED_DEMO=1` to enable the first three inputs on a new database
- `SOUNDCORE_HOST=127.0.0.1` and `SOUNDCORE_PORT=8741`

Then:

```bash
python -m soundcore
```

Open `http://127.0.0.1:8741`, sign in, and use the desk. Listen uses the browser’s audio output. Meters move because the simulated inputs are tones, not silence. The Socket.IO browser client is vendored at `soundcore/web/static/js/socket.io.min.js` (MIT) so the desk does not depend on a CDN.

Tests, including moto for S3:

```bash
pip install -r requirements-dev.txt
pytest
```

`scripts/measure_capacity.py` ramps simulated channels and prints a cap for the machine you run it on. That number is not a Pi 5 result until you run the script on the Pi.

## Run it on a Raspberry Pi 5

Raspberry Pi OS 64-bit. Plug in the USB audio cards before the first scan.

```bash
sudo ./install.sh
sudo edit /etc/soundcore/soundcore.env
sudo systemctl enable --now soundcore-worker.service soundcore.service
```

The install script:

- copies the app to `/opt/soundcore`
- stores the database, recordings, and logs in `/var/lib/soundcore`
- creates a `soundcore` user in the `audio` group
- installs two systemd units

`soundcore-worker` captures, records, relays, offloads, and transcribes. `soundcore` serves the console and the live-listen sockets. On the Pi, `SOUNDCORE_EMBED_WORKER=0` and `SOUNDCORE_AUDIO_BACKEND=alsa`, so the two processes do not both open the same devices. They share SQLite (WAL) and a localhost UDP hop for live PCM.

Flask binds to `127.0.0.1:8741` only. Put Caddy or nginx in front of it:

- `deploy/Caddyfile` — HTTPS on 443, including an internal certificate for a LAN
- `deploy/nginx.conf.example` — TLS plus the WebSocket upgrade headers

Do not publish port 8741.

Set the admin password in `/etc/soundcore/soundcore.env` before starting. To change it later:

```bash
# set SOUNDCORE_ADMIN_PASSWORD and SOUNDCORE_RESET_ADMIN=1, restart once, then set RESET back to 0
```

`sudo ./uninstall.sh` removes the program and leaves recordings. `sudo ./uninstall.sh --purge` removes `/var/lib/soundcore` as well.

## What you can do

- Rescan ALSA (or the simulated desk) and name each input and output. Set sample rate, gain, and whether it is enabled.
- Enabling an input starts a stream: a continuous series of WAV chunks. The default chunk is 45 seconds at 16 kHz 16-bit mono, which stays under the Boondock 2 MB upload limit.
- Filenames look like `recordings/radio-1/2026/11/17/radio-1_2026-11-17T06-30-04_ab12cd34.wav`. The timestamp is the device’s local time. The database stores UTC.
- Listen to one channel in the browser over Socket.IO. Route any input to a USB output at the same time. Both paths get the same PCM the recorder writes.
- The desk warns as you approach the stream cap and refuses another enabled input at the cap. The default cap is 8 until you replace it with a number from `scripts/measure_capacity.py` on the Pi.
- Storage keeps at least 5% of the volume free. At 95% used, the oldest closed files are uploaded. A local file is deleted only when an `s3_objects` row has `verified_at` set after a matching ETag and size. Open files, files played in the last two minutes, and anything that failed verification stay on disk. An alert is written, logged, and optionally emailed or posted to a webhook.
- Every night at midnight in `SOUNDCORE_TIMEZONE` (or the Pi’s zone, if that is empty), files not accessed for 7 days take the same verified-offload path. Playing or downloading a file updates `last_accessed_at`.
- Closed chunks are queued for Boondock `POST /api/v3/transcribe/openai` with `X-Boondock-Key` and an `audio_file` part. HTTP 500 and timeouts retry up to 5 times. HTTP 403 and 413 do not loop. A clip over 2 MB, or a 413, is sent once to `POST /api/v3/s3/transcribe/openai` with `{"s3_url": ...}` when a verified object exists.
- Export your data downloads a zip of the database and local audio. Import your data restores the same ids, so recordings still point at their channel, transcript, and S3 object. Command-line copies: `scripts/export_data.py` and `scripts/import_data.py`.

## Configuration

Secrets are read from the environment or from the optional JSON file in `SOUNDCORE_CONFIG`. The environment wins. Nothing in the repository is a usable password, API key, or cloud credential.

If `SOUNDCORE_SECRET_KEY` is empty, a key is generated into the data directory and kept there. It is not a stand-in for the admin password.

Operator settings that are safe to edit in the console (stream cap, chunk length, idle days, timezone, transcription on/off, notify email, webhook, language) live in the `settings` table. Changing the timezone is picked up by the midnight job on the next process start.

A missing Boondock key leaves transcription jobs pending with “API key is not configured” instead of burning retries. A missing S3 bucket leaves files local and raises an alert if the midnight job had work to do.

## Architecture

```text
USB cards and 3.5 mm inputs
        │
        ▼
Capture (ALSA via ffmpeg, or the simulated backend)
        │
        ├── Recorder  →  WAV chunks + SQLite
        ├── Router    →  USB outputs
        └── Live bus  →  Flask-SocketIO  →  browser
                │
                ▼
        Storage guard (continuous) and midnight APScheduler job
                │
                ▼
        S3 put, head, compare size and single-part MD5 ETag, then mark the row
                │
                ▼
        Local delete only after that mark is committed

Transcription worker  →  Boondock API v3
Caddy or nginx        →  Flask on localhost
```

Layout:

```text
soundcore/            Flask app, models, audio, storage, transcription
soundcore/web/        HTML, CSS, and JavaScript
deploy/               systemd units, Caddyfile, nginx example
scripts/              capacity estimate, export, import
tests/                pytest, no Pi required
```

The capture path is an interface. `SimulatedBackend` is what tests and a laptop use. `AlsaBackend` shells out to `arecord -l`, `aplay -l`, and `ffmpeg` on the Pi. `auto` uses ALSA when a capture card is present and otherwise falls back to the simulated desk.

## Tests

| Area | What is covered |
| --- | --- |
| File names | Channel slug, timestamp, 8-character id, path layout |
| Storage guard | 5% reserve, 95% cleanup, unverified and open files kept |
| S3 | moto upload, ETag and size match, local delete; a mismatched head keeps the file |
| Scheduler | Cron at 00:00 in the configured zone; 7-day selection; open files skipped |
| Transcription | 500 retry then success, attempt cap, 403, 413, timeout, S3 URL path, Boondock request shape |
| Export / import | Ids and channel, transcript, and S3 links survive a restore, audio bytes included |
| Capacity | Warning under the cap, refusal at the cap |
| Console | Login required, simulated desk renders |

## Judgment calls

These are the places the plan left a choice, or where a Pi-only detail could not be proven on this machine.

- Default format is 16 kHz, 16-bit, mono WAV. Chunks default to 45 seconds so a normal file stays under the 2 MB transcribe limit. 8 kHz and compressed archive copies are not generated.
- The stream cap counts enabled input channels. Listeners and relays ride on that channel; they do not add another slot. The shipped default is 8 and is an estimate, not a soak-test result.
- One browser tab listens to one channel at a time. Other tabs and other people can listen to other channels.
- ETag verification treats a multipart ETag (`md5-parts`) as a failure and keeps the local file. `put_object` is a single part for the chunk sizes this app writes.
- “Currently playing” is approximated by refusing to delete a file whose `last_accessed_at` is inside the last two minutes, plus any chunk still in `writing`.
- Notifications always hit the log and the alerts list. Email and webhooks run only when configured.
- Midnight uses the configured IANA timezone, or the host’s local zone when that setting is empty.
- Live audio between the worker and the web process is localhost UDP. A single-process dev run keeps it in memory instead.
- Flask-SocketIO uses threading mode so the app runs without eventlet. That is the right shape for one appliance process; it is not a multi-worker deployment.
- The `recordings.s3_object_id` column is a logical link. The foreign key lives on `s3_objects.recording_id` so the two rows can be inserted without a circular constraint.
- Software PTT is a simulated virtual input for now. A real PTT app that shows up as an ALSA device can be added like any other card. UDP packet ingest is not in this version.
- Canva Vision, Postgres as the default database, multi-user roles, WebRTC, and local Whisper are out of scope, on purpose.

## Open questions

- How many channels should a Pi 5 be sized for? Run `scripts/measure_capacity.py` on the device and set the cap from that.
- Who issues the per-customer `X-Boondock-Key`, and should `language` be pinned to `en`?
- Which S3 bucket, region, and prefix should the fleet use?
- Should software PTT arrive as an ALSA device or as UDP later?
- Is the midnight zone the Pi’s local zone, or a fixed zone for the whole fleet?
- Where should admin warnings go besides the desk, email, and a webhook?
- What is Canva Vision? It is not in this version.
