"""
Shared utilities for forwarder.py and listener.py
"""

import base64
import datetime
import io
import re
import sys

import anthropic
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

_anth_client = None


def _anthropic():
    global _anth_client
    if _anth_client is None:
        _anth_client = anthropic.AsyncAnthropic()  # reads ANTHROPIC_API_KEY from env
    return _anth_client


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


# ─────────────────────────────────────────────────────────────────────────────
#  OCR odds extraction
# ─────────────────────────────────────────────────────────────────────────────

async def extract_odds(image_bytes):
    """Ask Claude Haiku to read the American odds from a bet slip image.
    Returns a string like '-146' or '+220', or '' if not found."""
    b64 = base64.standard_b64encode(image_bytes).decode()
    resp = await _anthropic().messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=16,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
                },
                {
                    "type": "text",
                    "text": (
                        "This is a sports betting slip screenshot. "
                        "Return only the American odds number shown (e.g. -146, +220, -110). "
                        "Just the number, nothing else. If you cannot find odds, return nothing."
                    ),
                },
            ],
        }],
    )
    result = resp.content[0].text.strip()
    return result if re.match(r"^[+-]\d+$", result) else ""


async def enrich_caption(group, mapping, client):
    """If ocr_odds is enabled, download the image and append odds to the caption.
    Returns the enriched caption string, or None to leave text unchanged."""
    if not mapping.get("ocr_odds"):
        return None
    text = next((m.text for m in group if m.text), "")
    media_msg = next((m for m in group if m.media), None)
    if not media_msg:
        return None
    image_bytes = await client.download_media(media_msg.media, file=bytes)
    if not image_bytes:
        return None
    odds = await extract_odds(image_bytes)
    if odds:
        return f"{text} {odds}".strip()
    return None  # couldn't extract — leave original text alone


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


async def send_group(client, group, dest_entity, sender=None, caption_override=None):
    """Send a list of messages (album or single) to dest_entity, preserving formatting.
    Uses `sender` client for writing if provided, otherwise uses `client`.
    Pass `caption_override` to replace the message text (e.g. after OCR enrichment)."""
    sender = sender or client
    if len(group) > 1:
        files = []
        caption = ""
        caption_entities = None
        for m in group:
            data = await client.download_media(m.media, file=bytes)
            buf = io.BytesIO(data)
            buf.name = "photo.jpg"
            files.append(buf)
            if m.text and caption_override is None:
                caption = m.text
                caption_entities = m.entities
        if caption_override is not None:
            caption = caption_override
            caption_entities = None
        await sender.send_file(dest_entity, files, caption=caption, formatting_entities=caption_entities, silent=False)
    else:
        msg = group[0]
        cap = caption_override if caption_override is not None else (msg.text or "")
        ents = None if caption_override is not None else msg.entities
        if isinstance(msg.media, MessageMediaPhoto):
            photo = await client.download_media(msg.media, file=bytes)
            buf = io.BytesIO(photo)
            buf.name = "photo.jpg"
            await sender.send_file(dest_entity, buf, caption=cap, formatting_entities=ents, silent=False)

        elif isinstance(msg.media, MessageMediaDocument):
            doc = await client.download_media(msg.media, file=bytes)
            await sender.send_file(dest_entity, doc, caption=cap, formatting_entities=ents, silent=False)

        elif msg.text:
            await sender.send_message(dest_entity, cap, formatting_entities=ents, silent=False)

        else:
            print(f"  Skipped message {msg.id} (unsupported type)")
            return False

    return True
