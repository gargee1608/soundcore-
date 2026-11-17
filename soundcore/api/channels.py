from flask import Blueprint, current_app, g, jsonify, request

from soundcore.auth import login_required
from soundcore.models import Channel
from soundcore.ops import OpsError, delete_channel, save_channel

bp = Blueprint("channels_api", __name__)


def _devices():
    backend = current_app.extensions["audio_backend"]
    return [
        {"id": device.id, "name": device.name, "direction": device.direction, "kind": device.kind, "usb_id": device.usb_id}
        for device in backend.list_devices()
    ]


@bp.get("/api/channels")
@login_required
def list_channels():
    rows = g.db.query(Channel).order_by(Channel.direction, Channel.id).all()
    return jsonify(
        {
            "devices": _devices(),
            "channels": [
                {
                    "id": row.id,
                    "name": row.name,
                    "slug": row.slug,
                    "direction": row.direction,
                    "alsa_device": row.alsa_device,
                    "usb_id": row.usb_id,
                    "kind": row.device_kind,
                    "sample_rate": row.sample_rate,
                    "gain_db": row.gain_db,
                    "enabled": row.enabled,
                }
                for row in rows
            ],
        }
    )


@bp.post("/api/channels")
@login_required
def create_channel():
    payload = request.get_json(silent=True) or {}
    try:
        channel = save_channel(g.db, current_app.config["SETTINGS"], channel_id=None, actor=g.current_user.email, **_fields(payload))
    except OpsError as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify({"id": channel.id, "slug": channel.slug}), 201


@bp.post("/api/channels/<int:channel_id>")
@login_required
def update_channel(channel_id: int):
    payload = request.get_json(silent=True) or {}
    try:
        channel = save_channel(
            g.db,
            current_app.config["SETTINGS"],
            channel_id=channel_id,
            actor=g.current_user.email,
            **_fields(payload),
        )
    except OpsError as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify({"id": channel.id, "enabled": channel.enabled})


@bp.post("/api/channels/<int:channel_id>/delete")
@login_required
def remove_channel(channel_id: int):
    try:
        result = delete_channel(g.db, channel_id)
    except OpsError as exc:
        return jsonify({"error": str(exc)}), 404
    return jsonify({"result": result})


def _fields(payload: dict) -> dict:
    return {
        "name": payload.get("name", ""),
        "alsa_device": payload.get("alsa_device") or payload.get("device") or "",
        "sample_rate": int(payload.get("sample_rate") or 16000),
        "gain_db": float(payload.get("gain_db") or 0),
        "enabled": bool(payload.get("enabled")),
        "direction": payload.get("direction") or "in",
        "device_kind": payload.get("device_kind") or payload.get("kind") or "usb",
        "usb_id": payload.get("usb_id"),
    }
