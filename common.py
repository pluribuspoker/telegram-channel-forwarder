"""
Shared utilities for forwarder.py and listener.py
"""

import datetime
import io
import re
import sys

from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument


# ─────────────────────────────────────────────────────────────────────────────
#  Logging
# ─────────────────────────────────────────────────────────────────────────────

def _group_summary(group):
    """Return (text_preview, media_tag) for a message group."""
    text = next((m.text for m in group if m.text), "")
    preview = "  ".join(text.split("\n")).strip()   # flatten newlines
    preview = " ".join(preview.split())             # collapse extra whitespace
    if len(preview) > 65:
        preview = preview[:62] + "…"

    has_media = any(m.media for m in group)
    if len(group) > 1:
        media_tag = f"[album ×{len(group)}]"
    elif has_media:
        media_tag = "[photo]"
    else:
        media_tag = ""

    return preview, media_tag


def log_group(group, sent):
    """Print a single log line for a processed message group."""
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    preview, media_tag = _group_summary(group)

    if sent:
        badge = "✦ SENT    "
    else:
        badge = "· filtered"

    body_parts = [p for p in [preview, media_tag] if p]
    body = "  ".join(body_parts) if body_parts else "(no content)"

    print(f" {ts}  {badge}  ┃  {body}")


def passes_filter(group, mapping):
    """Return True if the message group should be forwarded.

    When `filter_pattern` is set on a mapping, at least one message in the
    group must have text matching the pattern.  The default pattern matches
    the picks format:  WORD(S): (anything)  e.g. "STRAIGHT: (1 UNIT)"
    """
    pattern = mapping.get("filter_pattern")
    if not pattern:
        return True
    return any(re.search(pattern, m.text or "") for m in group)


def parse_channel(raw):
    raw = str(raw).strip()
    try:
        return int(raw)
    except ValueError:
        pass
    return raw if raw.startswith("@") else f"@{raw}"


def resolve_dest(mapping, use_test):
    dest_raw = mapping.get("test_dest_channel") if use_test else None
    if not dest_raw:
        dest_raw = mapping["dest_channel"]
    return parse_channel(dest_raw)


async def send_group(client, group, dest_entity, sender=None):
    """Send a list of messages (album or single) to dest_entity, preserving formatting.
    Uses `sender` client for writing if provided, otherwise uses `client`."""
    sender = sender or client
    """Send a list of messages (album or single) to dest_entity, preserving formatting."""
    if len(group) > 1:
        files = []
        caption = ""
        caption_entities = None
        for m in group:
            data = await client.download_media(m.media, file=bytes)
            buf = io.BytesIO(data)
            buf.name = "photo.jpg"
            files.append(buf)
            if m.text:
                caption = m.text
                caption_entities = m.entities
        await sender.send_file(dest_entity, files, caption=caption, formatting_entities=caption_entities, silent=False)
    else:
        msg = group[0]
        if isinstance(msg.media, MessageMediaPhoto):
            photo = await client.download_media(msg.media, file=bytes)
            buf = io.BytesIO(photo)
            buf.name = "photo.jpg"
            await sender.send_file(dest_entity, buf, caption=msg.text or "", formatting_entities=msg.entities, silent=False)

        elif isinstance(msg.media, MessageMediaDocument):
            doc = await client.download_media(msg.media, file=bytes)
            await sender.send_file(dest_entity, doc, caption=msg.text or "", formatting_entities=msg.entities, silent=False)

        elif msg.text:
            await sender.send_message(dest_entity, msg.text, formatting_entities=msg.entities, silent=False)

        else:
            print(f"  Skipped message {msg.id} (unsupported type)")
            return False

    return True
