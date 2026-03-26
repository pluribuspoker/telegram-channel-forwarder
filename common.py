"""
Shared utilities for forwarder.py and listener.py
"""

import io
import sys

from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument


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
