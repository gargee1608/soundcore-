"""Create channel rows from the detected device list the first time the database is empty."""

from __future__ import annotations

from soundcore.audio.naming import slugify
from soundcore.models import Channel, Route, Stream, utcnow


def _unique_slug(session, name: str) -> str:
    base = slugify(name)
    slug = base
    suffix = 2
    while session.query(Channel).filter_by(slug=slug).first() is not None:
        slug = f"{base}-{suffix}"
        suffix += 1
    return slug


def seed_channels(session, backend, *, enable_demo: bool, cap: int, sample_rate: int) -> int:
    if session.query(Channel).count():
        return 0
    created = 0
    enabled_inputs = 0
    demo_room = min(3, cap) if enable_demo and cap > 0 else 0
    for device in backend.list_devices():
        enabled = False
        if device.direction == "in" and enabled_inputs < demo_room:
            enabled = True
            enabled_inputs += 1
        channel = Channel(
            name=device.name,
            slug=_unique_slug(session, device.name),
            direction=device.direction,
            alsa_device=device.id,
            audio_backend=device.backend or "alsa",
            device_id=device.id,
            usb_id=device.usb_id,
            device_kind=device.kind,
            sample_rate=sample_rate,
            format="s16le",
            gain_db=0.0,
            enabled=enabled,
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        session.add(channel)
        session.flush()
        created += 1
        if device.direction == "in":
            session.add(
                Stream(
                    name=device.name,
                    channel_id=channel.id,
                    status="active" if enabled else "idle",
                    created_by="seed",
                    created_at=utcnow(),
                )
            )
    if enable_demo:
        first_in = session.query(Channel).filter_by(direction="in", enabled=True).order_by(Channel.id).first()
        first_out = session.query(Channel).filter_by(direction="out").order_by(Channel.id).first()
        if first_in and first_out:
            first_out.enabled = True
            session.add(
                Route(
                    input_channel_id=first_in.id,
                    output_channel_id=first_out.id,
                    enabled=True,
                    created_at=utcnow(),
                )
            )
    session.flush()
    return created
