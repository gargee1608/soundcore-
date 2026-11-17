"""Admin warnings: always logged and stored. Email and webhook run when configured."""

from __future__ import annotations

import json
import logging
import smtplib
from email.message import EmailMessage

import requests

from soundcore.models import Event, utcnow

logger = logging.getLogger("soundcore.notify")


def record_event(session, level: str, kind: str, message: str, payload: dict | None = None) -> Event:
    event = Event(
        level=level,
        kind=kind,
        message=message,
        payload_json=json.dumps(payload or {}, default=str),
        created_at=utcnow(),
    )
    session.add(event)
    session.flush()
    log = logger.warning if level in {"warning", "error"} else logger.info
    log("%s: %s", kind, message)
    return event


def send_webhook(url: str, payload: dict, *, http=None) -> None:
    client = http or requests
    response = client.post(url, json=payload, timeout=5)
    response.raise_for_status()


def send_email(settings, subject: str, body: str) -> None:
    if not (settings.smtp_host and settings.notify_email and settings.smtp_from):
        return
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.smtp_from
    message["To"] = settings.notify_email
    message.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
        if settings.smtp_tls:
            smtp.starttls()
        if settings.smtp_user:
            smtp.login(settings.smtp_user, settings.smtp_password)
        smtp.send_message(message)


def notify(session, settings, level: str, kind: str, message: str, payload: dict | None = None, *, http=None) -> Event:
    event = record_event(session, level, kind, message, payload)
    body = {
        "level": level,
        "kind": kind,
        "message": message,
        "payload": payload or {},
        "created_at": event.created_at.isoformat(),
    }
    webhook = getattr(settings, "notify_webhook", "") or ""
    if webhook:
        try:
            send_webhook(webhook, body, http=http)
        except Exception:
            logger.exception("webhook notification failed")
    if getattr(settings, "notify_email", "") and getattr(settings, "smtp_host", ""):
        try:
            send_email(settings, f"SoundCore {level}: {kind}", message)
        except Exception:
            logger.exception("email notification failed")
    return event
