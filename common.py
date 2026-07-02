"""
Shared utilities for the Telegram forwarder and pick tracker.
"""

import base64
import io
import copy
import re
import sys

from telethon.tl.types import MessageEntityBlockquote, MessageMediaDocument, MessageMediaPhoto


def parlay_combined_odds(leg_odds: list[int | None]) -> int | None:
    """Multiply individual American leg odds into a combined parlay price.
    Returns None if any leg is missing odds."""
    valid = [o for o in leg_odds if o is not None]
    if not valid or len(valid) != len(leg_odds):
        return None
    dec = 1.0
    for o in valid:
        dec *= (o / 100 + 1) if o > 0 else (100 / abs(o) + 1)
    return round((dec - 1) * 100) if dec >= 2.0 else round(-100 / (dec - 1))

VERDICT_EMOJI = {
    "WIN":     "✅",
    "LOSS":    "❌",
    "PUSH":    "♻️",
    "UNKNOWN": "❓",
    "PENDING": "⏳",
}

# Matches any phrasing that means "must win in regulation / 60 min only"
# e.g. "3-way", "3 way", "3way", "60 min", "60-min", "60 minutes",
#      "regulation", "reg ML", "reg moneyline", "to win in regulation"
_REGULATION_ML_RE = re.compile(
    r"\b(3.?way|60.?min(utes?)?|regulation|reg\s+(ml|moneyline)|to win in reg)\b",
    re.IGNORECASE,
)


def is_regulation_ml(description: str) -> bool:
    """Return True if the pick description indicates a 3-way / regulation moneyline."""
    return bool(_REGULATION_ML_RE.search(description or ""))


def _anthropic():
    from ai import claude
    return claude()


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


def log_group(group, sent, ocr_odds=None, catchup=False):
    """Print a single log line for a processed message group.

    ocr_odds=None  → no OCR attempted
    ocr_odds=""    → OCR attempted but failed (falling back to image)
    ocr_odds="-146"→ OCR succeeded; image dropped
    """
    preview, media_tag = _group_summary(group)

    if sent:
        badge = "✦ CATCH-UP" if catchup else "✦ SENT    "
    else:
        badge = "· filtered"

    if ocr_odds is None:
        tag = media_tag                          # no OCR — show [photo] / [album] as normal
    elif ocr_odds:
        tag = f"[ocr: {ocr_odds}]"               # success — image dropped
    else:
        tag = f"[ocr: failed → {media_tag}]"    # failed — image kept as fallback

    body_parts = [p for p in [preview, tag] if p]
    body = "  ".join(body_parts) if body_parts else "(no content)"

    print(f" {badge}  ┃  {body}")


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
                        "Return only the odds/juice/vig — a whole integer like -161, +220, or -110. "
                        "Do NOT return the point spread or total (e.g. +7.5, -3, 224.5 are spreads/totals, not odds). "
                        "The odds are always a whole number with no decimal point. "
                        "Just the number, nothing else. If you cannot find odds, return nothing."
                    ),
                },
            ],
        }],
    )
    result = resp.content[0].text.strip()
    return result if re.match(r"^[+-]\d+$", result) else ""


async def enrich_caption(group, mapping, client):
    """If ocr_odds is enabled, download the image and extract odds via Haiku.

    Returns (caption, odds_str):
      - odds_str non-empty → OCR succeeded; caller should send text-only, no image
      - odds_str empty     → OCR attempted but failed; caller should send with image as normal
      - odds_str None      → OCR not attempted (disabled or no media)
      - caption is None when OCR is disabled or there is no media to read from
    """
    if not mapping.get("ocr_odds"):
        return None, None
    text = next((m.text for m in group if m.text), "")
    media_msg = next((m for m in group if m.media), None)
    if not media_msg:
        return None, None
    image_bytes = await client.download_media(media_msg.media, file=bytes)
    if not image_bytes:
        return None, None
    odds = await extract_odds(image_bytes)
    if odds:
        return f"{text} {odds}".strip(), odds  # success → text only
    return None, ""                             # failed → keep image


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


def strip_collapsed_blockquotes(text, entities):
    """Remove collapsed blockquote ranges from text and adjust remaining entity offsets."""
    if not entities:
        return text, entities
    collapsed = sorted(
        [e for e in entities if isinstance(e, MessageEntityBlockquote) and e.collapsed],
        key=lambda e: e.offset,
        reverse=True,  # remove from end so offsets stay valid
    )
    if not collapsed:
        return text, entities
    text = list(text)
    for e in collapsed:
        del text[e.offset:e.offset + e.length]
    text = "".join(text)
    removed_ranges = [(e.offset, e.offset + e.length) for e in collapsed]
    collapsed_ids = frozenset(id(e) for e in collapsed)
    surviving = []
    for e in entities:
        if id(e) in collapsed_ids:
            continue
        shift = sum(end - start for start, end in removed_ranges if start < e.offset)
        e = copy.copy(e)
        e.offset -= shift
        surviving.append(e)
    return text, surviving


async def send_group(client, group, dest_entity, sender=None, caption_override=None, text_only=False, reply_to=None, text_suffix=None):
    """Send a list of messages (album or single) to dest_entity, preserving formatting.
    Uses `sender` client for writing if provided, otherwise uses `client`.
    caption_override replaces the message text (e.g. after OCR enrichment).
    text_only=True skips all media and sends just the caption as a plain message.
    text_suffix appends text (e.g. source label) without discarding original entities."""
    sender = sender or client
    if text_only and caption_override:
        sent = await sender.send_message(dest_entity, caption_override, silent=False, reply_to=reply_to)
        return sent
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
        if text_suffix:
            caption = f"{caption}\n\n{text_suffix}" if caption else text_suffix
        # Telegram enforces a 1024-char limit for album captions (SendMultiMediaRequest).
        if len(caption) > 1024:
            caption, caption_entities = strip_collapsed_blockquotes(caption, caption_entities)
        if len(caption) > 1024:
            sent = await sender.send_file(dest_entity, files[0], caption=caption, formatting_entities=caption_entities, silent=False, reply_to=reply_to)
        else:
            sent = await sender.send_file(dest_entity, files, caption=caption, formatting_entities=caption_entities, silent=False, reply_to=reply_to)
    else:
        msg = group[0]
        cap = caption_override if caption_override is not None else (msg.text or "")
        ents = None if caption_override is not None else msg.entities
        if text_suffix:
            cap = f"{cap}\n\n{text_suffix}" if cap else text_suffix
        if isinstance(msg.media, MessageMediaPhoto):
            photo = await client.download_media(msg.media, file=bytes)
            buf = io.BytesIO(photo)
            buf.name = "photo.jpg"
            sent = await sender.send_file(dest_entity, buf, caption=cap, formatting_entities=ents, silent=False, reply_to=reply_to)

        elif isinstance(msg.media, MessageMediaDocument):
            doc = await client.download_media(msg.media, file=bytes)
            sent = await sender.send_file(dest_entity, doc, caption=cap, formatting_entities=ents, silent=False, reply_to=reply_to)

        elif msg.text:
            sent = await sender.send_message(dest_entity, cap, formatting_entities=ents, silent=False, reply_to=reply_to)

        else:
            print(f"  Skipped message {msg.id} (unsupported type)")
            return False

    return sent
