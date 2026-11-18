# Phase 1 assessment

Inspection only. No application code was changed in this document. Base is `gargee1608/soundcore-` at `09d9566` (main). The owner confirmed this repository is also **Gargi**.

Phase 2 later moved Sanket broker logins out of Python and into env and the channels page. The broker findings below describe the tree at inspection time.

**Vivesh** is [vibhvesh/voicestudio](https://github.com/vibhvesh/voicestudio). [gargee1608/voicestudio](https://github.com/gargee1608/voicestudio) is a fork of it, one commit ahead (`aba0389`, adds `apps/anon-clone`). The Sanket PTT repository URL is still unconfirmed. A public candidate is noted below and is not treated as the source of truth.

## 1. Current architecture

Two processes, SQLite (WAL), and a localhost UDP hop.

| Piece | Where | What it does |
| --- | --- | --- |
| Web | `soundcore/server.py`, `app.py`, Flask-SocketIO (threading) | Console on `127.0.0.1:8741`. Caddy or nginx is the public door. One admin session. |
| Worker | `soundcore/worker.py` | Capture, record, relay, S3 offload, transcription. `SOUNDCORE_EMBED_WORKER=1` folds this into the web process for laptops. |
| DB | `models.py`, SQLAlchemy | Channels, streams, routes, recordings, settings, users, `activity_events` (1,000-row ring), `events` (alerts), S3 and transcription rows. |
| Audio interface | `audio/detect.py`, `audio/capture.py` | `simulated`, `alsa` (ffmpeg to `plughw:N,M`), `pipewire` (paplay/ffmpeg to a Pulse socket), `hybrid` (ALSA capture + PipeWire playback). Pi install forces `hybrid` and `EMBED_WORKER=0`. |
| Supervisor | `audio/service.py` | One thread per enabled input. Reconciles DB every 2 s. 20 ms frames. |
| Router | `routes` table, `plan_routes`, `fanout` | Each input fans the same PCM to its WAV, every routed playback, and the live bus. |
| Recorder | `audio/recorder.py` | Mono s16le WAV chunks (default 45 s, 16 kHz). Sanket inputs open a file only while the floor is active. |
| Live bus | `audio/livestream.py` | `SC1` UDP from worker to web (`127.0.0.1:8742`), then Socket.IO `pcm` to one browser listener. |
| Deploy | `deploy/*.service`, `install.sh` | `soundcore` and `soundcore-worker` are `WantedBy=multi-user.target`, plus a Pulse socket helper for user `sensepart`. |

**Reuse.** Process split, SQLite models, channel and route APIs, ffmpeg ALSA capture and playback, WAV chunks, storage-reserve checks, activity ring, systemd units, and the existing Sanket receive path.

**Change.** Routing is copy-to-N, not a mix. Device identity is the ALSA card index. The console is seven pages with a 1 s HTTP poll. Meters are real RMS of captured PCM, but CPU, RAM, temperature, and engine health are not measured. The README still says software PTT is simulated and UDP ingest is absent; `audio/sanketptt.py` already receives live MQTT audio.

## 2. Network ingest that exists

**Sanket PTT, receive-only, implemented.** One shared MQTT-over-WebSocket connection. Topic prefix `ptt-radio-66ab9178/<channel>/{presence,floor,audio,sstv}`. Audio payload is `[speaker-id length][id bytes][Int16LE PCM at 12 kHz]`, linearly resampled in Python to the channel rate. Floor JSON is `claim` / `hb` / `release`, with a 2.5 s timeout. Four public broker URLs are hardcoded, with rotate-on-failure, a 15 s handshake limit, and a 60 s stale watchdog. The client publishes presence heartbeats when powered on. It refuses to publish floor, audio, or SSTV. Discovery can insert disabled `sanketptt:` channels. Status is written to `logs/sanket_status.json`.

**Not network ingest.** The UDP hop is worker-to-web PCM on localhost. `sim:ptt` is a tone generator. There is no RTSP, Icecast, HLS, RTP, or generic URL input. Boondock UDP repositories were not used as a protocol source.

## 3. Gaps

| Requirement | Now | Gap |
| --- | --- | --- |
| ≥8 live online streams | Sanket channels share one MQTT client. Cap default is 8 enabled inputs. | No second online protocol. Public-broker latency is not a local low-latency path. README and code disagree. |
| Pi mic / line input | ALSA capture, including a guessed 3.5 mm kind. | Works as another input. Not a separate “local mic” type. |
| 4 dedicated USB outputs | Any number of `direction=out` channels. | No reservation of four sinks. Two inputs aimed at one card each open their own ffmpeg writer. |
| One-to-many | Supported. One PCM buffer is written to every playback on that input. | Playback processes are torn down and reopened when the route list changes. |
| Many-to-one mix, per-input volume, clip protection | `gain_db` is per input and is applied before record, relay, and listen. `apply_gain` clamps a single buffer to int16. | No summer. No per-route gain. No mix limiter. Two writers to one ALSA device will contend. |
| Live routing rules | Route rows are reread every 2 s without a process restart. | Not instantaneous, and a change restarts playback. No rule language beyond enabled input→output pairs. |
| Feedback-loop prevention | SoundCore does not transmit on Sanket. | No check that a USB output is the capture side of the same card, or that a route would close a loop. |
| Low latency | 20 ms frames, blocking ALSA reads, browser listen adds its own jitter buffer. | MQTT-over-public-WSS dominates for Sanket. Python resample is fine at 12–16 kHz mono. No measured Pi 5 figure in-tree. |
| Per-stream recording, retention, storage warnings | Continuous WAV chunks; Sanket is floor-gated. Disk reserve 5%; 95% used triggers S3 offload; 7-day idle offload. Alerts if S3 is required and missing. | Retention is “upload then delete,” not a local keep policy. Recording start is not an activity event (`file_closed` is). |
| Auto-reconnect | Sanket rotates brokers and counts reconnects. Worker and web systemd units restart on failure. ffmpeg playback restarts if the process dies. | Reconnect is not an activity-log event. A capture that fails `start()` exits the thread and waits for the 2 s supervisor. |
| Stable USB identity | `device_id` is `plughw:<card>,<device>`. `usb_id` is `card<N>:<short name>`. | Card index follows plug order. Parser does not read `lsusb`, `/dev/snd/by-id`, or USB serial. PipeWire names are Pulse sink names, not serials. |
| Hot-plug recovery | Supervisor rescans every 2 s and restarts dead threads. Disconnected routes stay in the plan and are reported. | No udev hook. Identity is still the card number, so a replug can bind the wrong row. |
| Per-output test tone | `pcm.tone` drives the simulated desk only. | No output test control. |
| Dark single-screen cockpit, real meters, health over WebSockets | Desk, Activity, Channels, Routing, Recordings, Storage, Settings. Meters poll `GET /api/status` every 1 s from real RMS (tones only on the simulated backend). Socket.IO carries one listened channel, not meters. | Not one screen, not dark-as-the-product, no CPU / RAM / SoC temperature, no engine-health rollup. |
| System logs and Healthy / Warning / Critical | `activity_events` plus alert `events`. Disk and stream-cap banners. | Missing event kinds: stream connect/disconnect/reconnect, USB connect/disconnect, recording start, and a single health lamp. Engine errors exist as `error` rows and Python logs. |
| Systemd autostart | Present. | Keep. Pulse helper is only for the hybrid Bluetooth/PipeWire path. |

## 4. Recommended engine

Keep the worker as the clock owner. Do not replace it with GStreamer or a PipeWire filter graph for this scale.

Target: 8 network PCM inputs + 1 mic, mixed to 4 USB outputs, on a Pi 5.

- **Capture edge stays ffmpeg/ALSA** for the mic and USB inputs (`s16le`, one channel, device rate or a chosen internal rate). One ffmpeg per device is already how this repo opens hardware.
- **Sanket stays a Python MQTT ingest** that pushes PCM into the same input slots. Move the broker list to the env file. Do not add a second decoder until the owner names another source.
- **Add one mixer thread per output**, not one playback process per route. Each mixer sums the inputs routed to that output, applies per-route gain, then a limiter (look-ahead or a simple peak hold with makeup limited to 0 dBFS), then one ffmpeg/ALSA writer. At 16 kHz or 48 kHz mono, 9 inputs and 20 ms frames, the arithmetic is a few million adds per second. That is a small fraction of one Pi 5 core. The expensive parts are USB period size, MQTT RTT, and waking ffmpeg per buffer, not the sum.
- **Internal format:** 48 kHz s16le mono if the USB cards are 48 kHz class-compliant; otherwise lock record and mix to 16 kHz, which matches today’s files and the Boondock size limit. Do not let each route pick a rate. Today `fanout` writes the input buffer into a playback opened at the output rate, so a mismatch plays at the wrong speed.
- **Why not GStreamer.** `audiomixer` plus `alsasink` would do this, and it is the right tool if a later source is RTSP, HLS, or Opus. It is a second media stack beside ffmpeg, and no such source is specified.
- **Why not a PipeWire graph.** PipeWire is already used for optional playback through another user’s session socket. A headless router should not depend on that session. Use PipeWire or `/dev/snd/by-id` only to **name** devices. Keep the mixer in the worker so systemd does not need a desktop.
- **Latency budget to design for:** 20 ms frames + one ALSA period (typically 10–40 ms) on the local path. Sanket cannot meet a local budget while it rides public WSS brokers. Measure both on the Pi; do not quote a laptop `measure_capacity.py` number as a Pi result.
- **Feedback rule:** refuse a route whose output’s USB identity equals an enabled input, and refuse any future network source that would subscribe to audio this box transmits. Sanket already does not transmit.

## 5. Out of scope — keep, leave off

| Feature | Dependency if removed | Recommendation |
| --- | --- | --- |
| S3 offload | `s3_objects`, storage page, midnight scheduler, transcription’s oversized `s3_url` path, export/import | Leave the bucket empty. Offload no-ops. Do not delete the tables. |
| Boondock transcription | Job queue, recordings “transcribe” action, dashboard pending count, worker thread | `SOUNDCORE_TRANSCRIBE_ENABLED` defaults off and a missing key does not retry forever. Leave it off. |
| Simulated backend | pytest and the laptop desk | Keep. Production stays `hybrid` or `alsa`. |
| PipeWire / Bluetooth playback and the `sensepart` socket | Existing Pi installs that already play to a Bluetooth sink | Keep the code. Four USB outputs do not need it. |
| Export / import, SMTP, webhook | Settings and ops scripts | Keep. They are not new product surface. |
| VoiceStudio (Vivesh) | AGPL-3.0 tree, multi-GB models | Do not vendor it into SoundCore. See below. |

SoundCore has no `LICENSE` file. VoiceStudio is AGPL-3.0. Copying its engine into this process would put that license on the appliance. Call it only as an optional separate HTTP service if the owner asks for generated speech later.

## 6. Vivesh (VoiceStudio)

[vibhvesh/voicestudio](https://github.com/vibhvesh/voicestudio) is a source copy of [debpalash/VoiceStudio](https://github.com/debpalash/VoiceStudio), pinned in the README at `4d4cb0b` (2026-10-29), plus later commits (CanberraAI Voice branding and a Voice Training page, tip `9a705b4`). It is a self-hosted TTS and ASR studio, not a live radio.

**Generate.** FastAPI on `127.0.0.1:3900`. Speech is `POST /v1/audio/speech` (OpenAI-compatible: `model`, `voice`, `input`, `response_format=wav`) and `POST /generate` (form: `text`, `voice`, `speed`, `wav_bits=16`), which returns a WAV body. Default weights are Hugging Face `k2-fsa/OmniVoice` (~2.4 GB). First synthesis loads the model. CPU inference is described as roughly an order of magnitude slower than a GPU and wants about 8–16 GB RAM. `POST /voices` stores a reference clip. This is request/response audio, not eight continuous streams.

**Authenticate.** Loopback is open. Remote clients need `OMNIVOICE_API_KEY` as `Authorization: Bearer`. The UI exchanges that once for an HttpOnly `ov_session` or a bearer session (`POST /api/auth/session`). A 6-digit share PIN (`X-OmniVoice-Pin`) covers non-loopback HTTP consumption, not admin and not WebSockets. WebSockets use a 30 s single-use ticket from `POST /api/auth/ws-ticket`. Admin routes (`/system/*`, `/api/settings/*`) need loopback or the API key. The VPS installer writes the key to `/etc/voicestudio/voicestudio.env` and does not print it again.

**Transmit.** There is no PTT or multicast audio. Live sockets are ASR (`/v1/audio/transcriptions/stream`, `/ws/transcribe`: WebM/Opus or `?pcm=1&sr=16000`) and a dub preview (`/ws/tts`). Dictation capture stays on the client.

**Reuse.** None of the mixer, USB, or cockpit work. The only future hook is “fetch a WAV from `POST /generate` and play it as a file,” on a machine that is not this Pi. Do not put OmniVoice on the Pi 5 that is mixing USB audio.

**gargee1608/voicestudio.** Fork parent is `vibhvesh/voicestudio`. It is 1 commit ahead and 0 behind: `aba0389` adds `apps/anon-clone`, a Flask proxy that holds `VOICESTUDIO_API_KEY` server-side and exposes unauthenticated `GET /api/voices`, `POST /api/voices`, `POST /api/generate` (WAV), and `DELETE /api/voices/<slug>`. The proxy’s own README says anyone who can reach it can spend the server’s key and CPU. That app is not an audio source for SoundCore.

### Other repos on `vibhvesh` (relevant, not confirmed as “the” Sanket repo)

The owner has not named the Sanket PTT URL. These are public and match the client already in this tree. Treat them as candidates.

| Repo | What it is | Relation |
| --- | --- | --- |
| [vibhvesh/sanketptt](https://github.com/vibhvesh/sanketptt) | Static PTT page, Pages at `https://vibhvesh.github.io/sanketptt/`, Pi “Sanket Edge” under `pi/`. README: MQTT over WSS, 12 kHz 16-bit PCM, floor control, topic family `presence/floor/audio/sstv`. | **Best candidate.** `sanketptt.py` says it matches that page and `pi/sanket/mqtt_relay.py`. Same prefix and codec this repo already decodes. |
| [vibhvesh/ptt-radio](https://github.com/vibhvesh/ptt-radio) | Older single-channel page, same prefix and 12 kHz PCM. | Same family, narrower. |
| [vibhvesh/hyderabad-police-ptt](https://github.com/vibhvesh/hyderabad-police-ptt) | Talk UI plus a control-room server, WebSocket `/ws`, line tokens, optional Whisper. README still allows the public MQTT relays and names demo hosts. | Different control plane. Do not assume SoundCore should speak this API. |
| [vibhvesh/Super-PTT](https://github.com/vibhvesh/Super-PTT) | Empty repository. | Ignore. |
| `radiocloud`, `chitrakoot-sanket-radio`, `sanket-story-radio` | Browser players / news pages. | Not inspected as ingest protocols. |

Boondock-Echo `BoondockEdgev2_udp`, `UDP-Audio-Recorder-Service`, and `Boondock-Airband-UDP-Receiver` are separate UDP products. They were not read as the Sanket contract.

## 7. Open questions

1. Confirm or replace `vibhvesh/sanketptt` as the Sanket PTT source. Until then, do not add brokers, topics, or codecs.
2. Is one MQTT talk-group one SoundCore input, and is 8 the number of groups or the number of talkers?
3. Should SoundCore stay receive-only? It is receive-only today.
4. Which broker should production use, and who operates it? The four public URLs in source, one of them with embedded public credentials, should move to the env file and out of git.
5. What are the “other compatible sources,” with a sample URL or packet? Do not guess RTSP, Icecast, or the Boondock UDP framing.
6. Are the four USB outputs playback-only, and may the Pi mic be the onboard jack, a USB capture card, or both?
7. Local retention: how many days or gigabytes, and is S3 still allowed to delete local files?
8. Latency target for local routes versus Sanket routes, in milliseconds.
9. May the cockpit replace the seven-page console, or does it sit in front of it?
10. Should transcription and S3 stay installed-but-off for this product?
11. Is generated speech from VoiceStudio in scope at all? If yes, it runs on another host.

## 8. Phases 2–10

Each phase keeps current capture, recording, and login working. No new product surface beyond the list below.

2. **Contract.** Owner confirms the Sanket repo. Diff it against `sanketptt.py`. Move broker URLs to env. Correct the README sentence that says PTT is simulated. No new codecs.
3. **Identity.** Resolve USB outputs and the mic by `/dev/snd/by-id` or USB serial, store that as `usb_id`, and rebind `plughw` only at scan time. Hot-plug updates availability without renumbering rows.
4. **Mixer.** One writer per output. Sum, per-route gain, limiter. Block feedback routes. Same PCM still feeds the WAV and the live bus.
5. **Live rules.** Route and gain edits apply on the next frame without reopening a healthy playback. Persist in the existing `routes` table (gain column only if needed).
6. **Ingest slot.** Keep Sanket as the first network input. Add a small source interface so a later URL or UDP source can push PCM. Do not implement a second protocol in this phase.
7. **Recording policy.** Per-input record on/off, local retention, and a storage warning that does not require S3. Log recording start and stop.
8. **Health and logs.** Real CPU, RAM, temperature, disk, and engine state. One Healthy / Warning / Critical value derived from those. Activity rows for stream connect, disconnect, reconnect, USB connect and disconnect, route changes, recording, and engine errors.
9. **Cockpit.** One dark page. WebSocket push of the same status the worker already writes (levels, routes, health, log tail). No simulated meters on a Pi backend. Per-output test tone into the mixer.
10. **Pi 5 soak.** Eight Sanket inputs plus the mic, four USB outputs, hot-plug, broker drop, and disk-full. Record latency and CPU on the device. Set `SOUNDCORE_STREAM_CAP` from that run.

Systemd units from phase 1 stay. S3 and Boondock stay off unless the owner answers question 10 the other way.
