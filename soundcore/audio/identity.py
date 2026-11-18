"""Stable USB audio identity. Card indexes are only a join key during one scan."""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

USB_OUTPUT_SLOTS = ("USB Audio 1", "USB Audio 2", "USB Audio 3", "USB Audio 4")
_CONTROL_RE = re.compile(r"controlC(\d+)$")
_PCM_RE = re.compile(r"pcmC(\d+)D(\d+)[pc]$")
_BY_ID_INDEX_RE = re.compile(r"-\d{2}$")


@dataclass(frozen=True)
class UsbIdentity:
    """Facts collected for one ALSA card. `card` is not part of the stored id."""

    by_id: str = ""
    by_path: str = ""
    serial: str = ""
    vendor: str = ""
    product: str = ""


def card_from_snd_node(target: str) -> int | None:
    name = os.path.basename(target.rstrip("/"))
    match = _CONTROL_RE.search(name) or _PCM_RE.search(name)
    if not match:
        return None
    return int(match.group(1))


def normalize_by_id(name: str) -> str:
    """Drop the ALSA node suffix (`-00`) from a by-id symlink."""
    base = os.path.basename(name)
    return _BY_ID_INDEX_RE.sub("", base)


def stable_token(
    alsa_id: str,
    pcm: int,
    ident: UsbIdentity | None,
    *,
    serial_counts: Counter,
    by_id_counts: Counter,
) -> str:
    """Pick an id that survives reboot. Never includes the card number.

    Order: unique USB serial, unique by-id name, physical port (by-path),
    then the ALSA card id. Identical adapters with no serial and no port
    stay `usb-ambiguous` until a port is pinned.
    """
    ident = ident or UsbIdentity()
    serial = (ident.serial or "").strip()
    by_id = (ident.by_id or "").strip()
    by_path = (ident.by_path or "").strip()
    if serial and serial_counts[serial] == 1:
        return f"usb-serial:{serial}:pcm{pcm}"
    if by_id and by_id_counts[by_id] == 1:
        return f"usb-id:{by_id}:pcm{pcm}"
    if by_path:
        return f"usb-path:{by_path}:pcm{pcm}"
    short = re.sub(r"[^A-Za-z0-9._-]", "", alsa_id or "") or "card"
    rough = f"usb-cardid:{short}:pcm{pcm}"
    return rough


def assign_tokens(rows: list[tuple[int, str, int, UsbIdentity | None]]) -> dict[tuple[int, int], str]:
    """rows are (card, alsa short name, pcm device, identity)."""
    serial_counts: Counter = Counter()
    by_id_counts: Counter = Counter()
    for _card, _short, _pcm, ident in rows:
        if ident and ident.serial:
            serial_counts[ident.serial] += 1
        if ident and ident.by_id:
            by_id_counts[ident.by_id] += 1
    chosen: dict[tuple[int, int], str] = {}
    seen: Counter = Counter()
    for card, short, pcm, ident in rows:
        token = stable_token(short, pcm, ident, serial_counts=serial_counts, by_id_counts=by_id_counts)
        seen[token] += 1
        chosen[(card, pcm)] = token
    for card, short, pcm, ident in rows:
        token = chosen[(card, pcm)]
        if seen[token] > 1 and not (ident and ident.by_path) and not token.startswith("usb-serial:"):
            chosen[(card, pcm)] = f"usb-ambiguous:{token}"
    return chosen


def parse_snd_links(
    by_id_links: list[tuple[str, str]],
    by_path_links: list[tuple[str, str]],
    *,
    serial_by_card: dict[int, str] | None = None,
    vendor_by_card: dict[int, str] | None = None,
    product_by_card: dict[int, str] | None = None,
) -> dict[int, UsbIdentity]:
    """Build identities from symlink listings. Targets are node names like controlC2."""
    found: dict[int, dict] = {}

    def slot(card: int) -> dict:
        return found.setdefault(card, {"by_id": "", "by_path": ""})

    for name, target in by_id_links:
        card = card_from_snd_node(target)
        if card is None:
            continue
        slot(card)["by_id"] = slot(card)["by_id"] or normalize_by_id(name)
    for name, target in by_path_links:
        card = card_from_snd_node(target)
        if card is None:
            continue
        slot(card)["by_path"] = slot(card)["by_path"] or os.path.basename(name)
    serial_by_card = serial_by_card or {}
    vendor_by_card = vendor_by_card or {}
    product_by_card = product_by_card or {}
    identities: dict[int, UsbIdentity] = {}
    cards = set(found) | set(serial_by_card) | set(vendor_by_card) | set(product_by_card)
    for card in cards:
        row = found.get(card, {})
        identities[card] = UsbIdentity(
            by_id=row.get("by_id", ""),
            by_path=row.get("by_path", ""),
            serial=(serial_by_card.get(card) or "").strip(),
            vendor=(vendor_by_card.get(card) or "").strip(),
            product=(product_by_card.get(card) or "").strip(),
        )
    return identities


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""


def _symlink_targets(directory: Path) -> list[tuple[str, str]]:
    if not directory.is_dir():
        return []
    links = []
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    for name in names:
        path = directory / name
        try:
            target = os.readlink(path)
        except OSError:
            continue
        links.append((name, target))
    return links


def _sysfs_value(card: int, name: str) -> str:
    device = Path(f"/sys/class/sound/card{card}/device")
    if not device.exists():
        return ""
    # The sound device node is the USB interface. Serial lives on the parent device.
    for candidate in (device / name, device / ".." / name):
        text = _read_text(candidate)
        if text:
            return text
    return ""


def read_usb_identities() -> dict[int, UsbIdentity]:
    """Read udev by-id, by-path, and sysfs serial. Missing files yield an empty map."""
    serials: dict[int, str] = {}
    vendors: dict[int, str] = {}
    products: dict[int, str] = {}
    for card in range(32):
        if not Path(f"/sys/class/sound/card{card}").exists():
            continue
        serial = _sysfs_value(card, "serial")
        vendor = _sysfs_value(card, "idVendor")
        product = _sysfs_value(card, "idProduct")
        if serial:
            serials[card] = serial
        if vendor:
            vendors[card] = vendor
        if product:
            products[card] = product
    return parse_snd_links(
        _symlink_targets(Path("/dev/snd/by-id")),
        _symlink_targets(Path("/dev/snd/by-path")),
        serial_by_card=serials,
        vendor_by_card=vendors,
        product_by_card=products,
    )
