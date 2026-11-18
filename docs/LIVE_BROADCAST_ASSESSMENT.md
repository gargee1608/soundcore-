# Phase 1 — Technical assessment (Live Audio Broadcasting)

## Repositories inspected

| Source | Role |
| --- | --- |
| [gargee1608/soundcore-](https://github.com/gargee1608/soundcore-) | **Primary codebase** (cloned into this workspace). Pi 5 appliance: capture, record, route, Sanket PTT, Flask console. |
| [vibhvesh/sanketptt](https://github.com/vibhvesh/sanketptt) | PTT web client + Pi `mqtt_relay.py`. Defines MQTT topics, brokers, and 12 kHz PCM framing. |
| [vibhvesh/boondock-echo-live](https://github.com/vibhvesh/boondock-echo-live) | Reference browser PTT (MQTT `audio` / `floor` / `presence`). Same protocol family as Sanket. |

**Vivesh** maps to the `vibhvesh` GitHub org (Sanket PTT, `radio-toolkit`, `radiocloud`, `superptt`, etc.). **Gargi** maps to `gargee1608/soundcore-`.

The local workspace was empty; development continues from the cloned **SoundCore** tree (not a greenfield rewrite).

## Sanket PTT — verified stream access

SoundCore already implements receive-only integration in `soundcore/audio/sanketptt.py`, aligned with Sanket Edge (`pi/sanket/mqtt_relay.py`):

- **Prefix:** `ptt-radio-66ab9178/`
- **Topics:** `<prefix><channel>/{presence/<id>,floor,audio,sstv}`
- **Audio payload:** `[n][id bytes][Int16LE PCM @ 12 kHz]`
- **Transport:** MQTT over WebSocket (`wss://`), with the same public broker fallbacks as the web client (not hardcoded private credentials in app code).
- **Admin:** Paste `https://vibhvesh.github.io/sanketptt/?ch=<name>` or channel name on **Channels**.

Other “online” sources in Sanket’s website demo use **HTTP MP3 / HLS** in the browser only; SoundCore today does **not** ingest arbitrary HTTP/HLS URLs as inputs (gap).

## Existing SoundCore architecture

```text
Inputs (ALSA / PipeWire / SanketPTT / simulated)
        │
        ▼
AudioService (worker) — per-input threads
        │
        ├── WavRecorder → SQLite + files
        ├── Router → USB outputs (per-input playbacks)
        └── Live bus (UDP / in-process) → Flask-SocketIO → browser
```

- **Stack:** Python 3, **Flask** (not FastAPI), SQLAlchemy + SQLite, Socket.IO, ffmpeg for ALSA I/O, optional PipeWire playback.
- **Processes:** `soundcore` (web) + `soundcore-worker` (audio) on Pi; embedded worker in dev.
- **Stream cap:** Default 8 enabled inputs (`settings.stream_cap`).
- **Routing:** `routes` table — many rows allowed (one input → many outputs). UI historically showed only one relay label per input.
- **Many-to-one:** Multiple inputs → same USB output previously opened **separate** ffmpeg playbacks on one device (unsafe). **Output mixer** (this pass) fixes that.
- **Recording:** Continuous chunked WAV; Sanket gates files to active transmissions.
- **Out of scope in upstream README:** React UI, GStreamer pipeline, generic HTTP stream ingest, local Whisper, multi-role RBAC.

## Gap analysis vs master prompt

| Requirement | Status |
| --- | --- |
| ≥8 simultaneous inputs | Supported (cap configurable); Sanket shares one MQTT hub |
| Sanket PTT | Implemented |
| Other live sources (HTTP/HLS/ICEcast) | **Not implemented** — add ffmpeg URL capture backend |
| 4 USB outputs, stable IDs | `detect.py` + `usb_id`; PipeWire/ALSA `device_id` |
| One-to-many routing | Supported via multiple `Route` rows |
| Many-to-one mixing + per-source gain | **Added:** `OutputMixer` + channel `gain_db` |
| Custom routing matrix | Same as multiple routes; UI is pairwise forms |
| Cockpit dashboard (10 cards, real metrics) | **Partial → enhanced** desk + `/api/status` health block |
| React SPA | Not used; Flask templates + polling (matches “where practical”) |
| CPU/RAM/temp/disk | **Added:** `system_health.py` (Linux/Pi) |
| Feedback loop prevention | **Added:** block route when input/output share `device_id` |
| Boot services | `deploy/soundcore*.service` + `install.sh` |

## Recommended implementation order (phases 2–10)

1. **Phase 2:** HTTP/HLS/ffmpeg URL input type (`audio_backend=url`).
2. **Phase 3:** Harden USB discovery UX (four dedicated outputs, test tone API).
3. **Phase 4–5:** Route matrix UI + per-route mix level column (optional DB field).
4. **Phase 6:** Mic input = existing ALSA/PipeWire capture paths on Pi.
5. **Phase 7:** On-demand recording sessions (today: always-on chunked record).
6. **Phase 8–9:** Cockpit polish, activity kinds, WebSocket push for status (optional).
7. **Phase 10:** Soak test on Pi 5 with `scripts/measure_capacity.py`.

## Deployment

Unchanged from [INSTALL_GUIDE.md](INSTALL_GUIDE.md): Raspberry Pi OS 64-bit, `bootstrap.sh`, Caddy on 443, data under `/var/lib/soundcore`.

## Judgment

Extend **SoundCore** rather than rebuilding with FastAPI + React unless the team explicitly wants a separate frontend repo. The worker/web split, Sanket hub, and ALSA detection are production-shaped; the master prompt’s “broadcasting” focus maps to strengthening **routing + monitoring**, not replacing transcription/S3 unless you later strip those modules.