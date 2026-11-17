"""Channel and route changes used by the HTML forms."""

from __future__ import annotations

import secrets

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
) -> Channel:
    name = (name or "").strip()
    alsa_device = (alsa_device or "").strip()
    if not name:
        raise OpsError("A channel needs a name.")
    if not alsa_device:
        raise OpsError("Pick an input or output device.")
    if direction not in {"in", "out"}:
        raise OpsError("Direction must be input or output.")
    if sample_rate not in settings.allowed_sample_rates:
        raise OpsError("Sample rate is not one of the supported rates.")
    if gain_db < -24 or gain_db > 24:
        raise OpsError("Gain must stay between -24 and +24 dB.")

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


def save_route(session, input_id: int, output_id: int, enabled: bool = True) -> Route:
    src = session.get(Channel, input_id)
    dst = session.get(Channel, output_id)
    if src is None or src.direction != "in":
        raise OpsError("Pick an input channel.")
    if dst is None or dst.direction != "out":
        raise OpsError("Pick an output channel.")
    if input_id == output_id:
        raise OpsError("A channel cannot route to itself.")
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
    session.flush()
    return route
