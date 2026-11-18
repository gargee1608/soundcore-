"""Channel and route changes used by the HTML forms."""

from __future__ import annotations

import secrets

from soundcore.audio.detect import infer_backend
from soundcore.audio.naming import slugify
from soundcore.capacity import can_start_another
from soundcore.models import Channel, Route, Stream, utcnow
from soundcore.settings_store import load_runtime
from soundcore.storage.guard import admission, measure_disk


class OpsError(Exception):
    pass


def _unique_slug(session, name: str, ignore_id: int | None = None) -> str:
    base = slugify(name)
    slug = base
    suffix = 2
    while True:
        query = session.query(Channel).filter_by(slug=slug)
        if ignore_id is not None:
            query = query.filter(Channel.id != ignore_id)
        if query.first() is None:
            return slug
        slug = f"{base}-{suffix}"[:64].strip("-")
        suffix += 1


def active_inputs(session) -> int:
    return session.query(Channel).filter_by(direction="in", enabled=True).count()


def channel_endpoint(channel) -> tuple[str, str]:
    """Return (device_id, backend), falling back to a legacy alsa_device."""
    device_id = (getattr(channel, "device_id", None) or "").strip()
    legacy = (getattr(channel, "alsa_device", None) or "").strip()
    if not device_id:
        device_id = legacy
    backend = (getattr(channel, "audio_backend", None) or "").strip() or infer_backend(device_id)
    return device_id, backend


def save_channel(
    session,
    settings,
    *,
    channel_id: int | None,
    name: str,
    alsa_device: str,
    sample_rate: int,
    gain_db: float,
    enabled: bool,
    direction: str,
    device_kind: str = "usb",
    usb_id: str | None = None,
    actor: str = "",
    audio_backend: str = "",
    device_id: str = "",
) -> Channel:
    name = (name or "").strip()
    device_id = (device_id or alsa_device or "").strip()
    alsa_device = device_id
    inferred = infer_backend(device_id)
    if inferred in {"pipewire", "simulated", "sanketptt"}:
        audio_backend = inferred
    else:
        audio_backend = (audio_backend or inferred).strip() or "alsa"
    if audio_backend == "sanketptt" and direction != "in":
        raise OpsError("SanketPTT channels are inputs only.")
    if not name:
        raise OpsError("A channel needs a name.")
    if not device_id:
        raise OpsError("Pick an input or output device.")
    if direction not in {"in", "out"}:
        raise OpsError("Direction must be input or output.")
    if sample_rate not in settings.allowed_sample_rates:
        raise OpsError("Sample rate is not one of the supported rates.")
    if gain_db < -24 or gain_db > 24:
        raise OpsError("Gain must stay between -24 and +24 dB.")
    if audio_backend == "sanketptt":
        taken = session.query(Channel).filter(
            Channel.audio_backend == "sanketptt",
            Channel.device_id == device_id,
        )
        if channel_id:
            taken = taken.filter(Channel.id != int(channel_id))
        if taken.first() is not None:
            raise OpsError("That SanketPTT channel is already on this appliance.")

    if channel_id:
        channel = session.get(Channel, channel_id)
        if channel is None:
            raise OpsError("That channel is gone.")
    else:
        channel = Channel(
            direction=direction,
            device_kind=device_kind or "usb",
            usb_id=usb_id,
            format="s16le",
            enabled=False,
            gain_db=0.0,
            sample_rate=sample_rate,
            name=name or "channel",
            slug=f"new-{secrets.token_hex(4)}",
            alsa_device=alsa_device or "pending",
            audio_backend=audio_backend,
            device_id=device_id,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        session.add(channel)

    turning_on = direction == "in" and enabled and not channel.enabled
    if turning_on:
        runtime = load_runtime(session, settings)
        if not can_start_another(active_inputs(session), runtime.stream_cap):
            decision_cap = runtime.stream_cap
            raise OpsError(
                f"Stream cap is {decision_cap}. Stop another channel before enabling this one."
            )
        usage = measure_disk(settings.data_dir)
        if admission(usage, reserve_ratio=runtime.free_reserve_ratio) == "refuse":
            raise OpsError("Less than 5% of the disk is free. New streams are refused until space is verified off to S3.")

    channel.name = name
    channel.slug = _unique_slug(session, name, ignore_id=channel.id)
    channel.alsa_device = alsa_device
    channel.device_id = device_id
    channel.audio_backend = audio_backend
    channel.sample_rate = sample_rate
    channel.gain_db = gain_db
    channel.enabled = bool(enabled)
    channel.direction = direction
    if device_kind:
        channel.device_kind = device_kind
    if usb_id:
        channel.usb_id = usb_id
    channel.updated_at = utcnow()
    session.flush()

    if direction == "in":
        stream = session.query(Stream).filter_by(channel_id=channel.id).one_or_none()
        if stream is None:
            stream = Stream(name=name, channel_id=channel.id, created_by=actor or None, created_at=utcnow())
            session.add(stream)
        stream.name = name
        stream.status = "active" if channel.enabled else "idle"
    session.flush()
    return channel


def sync_usb_output_slots(session, devices) -> list[dict]:
    """Keep USB Audio 1-4 pinned to stable ids. Card numbers are not stored.

    Returns the playback devices that were seen, so the caller can log
    connect and disconnect from the difference. Unplugging does not clear
    a pin. HDMI, the onboard jack, Bluetooth, and ambiguous adapters are
    not given a slot.
    """
    from soundcore.audio.identity import USB_OUTPUT_SLOTS

    usb_outs = []
    for device in devices:
        token = device.usb_id or ""
        if device.direction != "out" or device.kind != "usb" or (device.backend or "alsa") != "alsa":
            continue
        if not token.startswith("usb-") or token.startswith("usb-ambiguous"):
            continue
        usb_outs.append(device)
    usb_outs.sort(key=lambda device: device.usb_id or "")

    slots = []
    for index, name in enumerate(USB_OUTPUT_SLOTS, start=1):
        slug = f"usb-audio-{index}"
        row = session.query(Channel).filter_by(slug=slug).one_or_none()
        if row is None:
            row = Channel(
                name=name,
                slug=slug,
                direction="out",
                alsa_device=f"alsa:unassigned:{index}",
                audio_backend="alsa",
                device_id=f"alsa:unassigned:{index}",
                usb_id="",
                device_kind="usb",
                sample_rate=48000,
                format="s16le",
                gain_db=0.0,
                enabled=False,
                created_at=utcnow(),
                updated_at=utcnow(),
            )
            session.add(row)
            session.flush()
        slots.append(row)

    by_usb = {row.usb_id: row for row in slots if row.usb_id}
    claimed: set[int] = set()
    for device in usb_outs:
        row = by_usb.get(device.usb_id)
        if row is None or row.id in claimed:
            row = next((slot for slot in slots if not slot.usb_id and slot.id not in claimed), None)
        if row is None:
            continue
        claimed.add(row.id)
        first_pin = not row.usb_id
        row.usb_id = device.usb_id
        row.device_id = device.id
        row.alsa_device = device.hw or device.id
        row.audio_backend = "alsa"
        row.device_kind = "usb"
        row.direction = "out"
        if first_pin:
            row.enabled = True
        row.updated_at = utcnow()
        by_usb[device.usb_id] = row
    session.flush()
    return [
        {"usb_id": device.usb_id, "name": device.name, "hw": device.hw, "id": device.id}
        for device in usb_outs
    ]


def delete_channel(session, channel_id: int) -> str:
    from soundcore.models import Recording

    channel = session.get(Channel, channel_id)
    if channel is None:
        raise OpsError("That channel is gone.")
    if session.query(Recording).filter_by(channel_id=channel.id).count():
        channel.enabled = False
        stream = session.query(Stream).filter_by(channel_id=channel.id).one_or_none()
        if stream is not None:
            stream.status = "idle"
        session.flush()
        return "disabled"
    session.query(Route).filter(
        (Route.input_channel_id == channel.id) | (Route.output_channel_id == channel.id)
    ).delete()
    session.query(Stream).filter_by(channel_id=channel.id).delete()
    session.delete(channel)
    session.flush()
    return "deleted"


def save_route(session, input_id: int, output_id: int, enabled: bool = True, gain_db: float = 0.0) -> Route:
    src = session.get(Channel, input_id)
    dst = session.get(Channel, output_id)
    if src is None or src.direction != "in":
        raise OpsError("Pick an input channel.")
    if dst is None or dst.direction != "out":
        raise OpsError("Pick an output channel.")
    if input_id == output_id:
        raise OpsError("A channel cannot route to itself.")
    from soundcore.audio.service import same_device

    if same_device(src, dst):
        raise OpsError("That route would feed this output back into the same device.")
    if gain_db < -24 or gain_db > 24:
        raise OpsError("Route gain must stay between -24 and +24 dB.")
    route = (
        session.query(Route)
        .filter_by(input_channel_id=input_id, output_channel_id=output_id)
        .one_or_none()
    )
    if route is None:
        route = Route(
            input_channel_id=input_id,
            output_channel_id=output_id,
            enabled=enabled,
            created_at=utcnow(),
        )
        session.add(route)
    else:
        route.enabled = enabled
    route.gain_db = gain_db
    session.flush()
    return route
