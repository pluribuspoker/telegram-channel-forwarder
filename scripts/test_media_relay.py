"""Media relay: a fan-out downloads + uploads each photo ONCE, not once per dest.

The first successful send caches the SENT message's media (listener._media_relay);
every other dest for the same source message re-sends that by file reference —
send_group(media_override=...) skips both client.download_media and the upload.
A rejected/stale reference falls back to the normal path and drops the cache entry.

Run:  ~/venv/bin/python scripts/test_media_relay.py
"""
import asyncio
import os
import sys
import tempfile
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

for k, v in {"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "x",
             "TELEGRAM_SESSION": "", "BOT_TOKEN": "x", "MAPPINGS_CONFIG": "[]"}.items():
    os.environ.setdefault(k, v)

from telethon.extensions import markdown as tl_markdown  # noqa: E402
from telethon.tl.types import MessageEntityBold, MessageMediaPhoto  # noqa: E402

import listener  # noqa: E402

RAW = "FCS Falcons / Saints over 47\n\n42-13 L30 days"
ENTS = [MessageEntityBold(offset=0, length=3)]


class FakeMsg:
    _next_id = 1000

    def __init__(self, raw=RAW, ents=None, media=None, grouped_id=None):
        self.raw_text = raw
        self.entities = ents if ents is not None else list(ENTS)
        self.media = media
        self.grouped_id = grouped_id
        self.sender_id = 7
        FakeMsg._next_id += 1
        self.id = FakeMsg._next_id
        self.peer_id = types.SimpleNamespace(channel_id=1910823870)

    @property
    def text(self):
        return tl_markdown.unparse(self.raw_text, self.entities)


class FakeUserClient:
    def __init__(self):
        self.downloads = 0

    async def download_media(self, media, file=None):
        self.downloads += 1
        return b"\xff\xd8\xff\xe0jpegbytes"


class FakeSender:
    """Sender client: records every send; sent photos come back as real media objects."""

    def __init__(self, reject_media_once=False):
        self.sends = []            # (kind, file_arg, caption, entities)
        self.reject_media_once = reject_media_once
        self._next_id = 1

    def _sent(self, with_media):
        self._next_id += 1
        return types.SimpleNamespace(id=self._next_id,
                                     media=MessageMediaPhoto() if with_media else None)

    async def send_message(self, dest, text, **kw):
        self.sends.append(("message", None, text, kw.get("formatting_entities")))
        return self._sent(False)

    async def send_file(self, dest, files, caption=None, **kw):
        is_media_obj = (isinstance(files, MessageMediaPhoto)
                        or (isinstance(files, list) and files
                            and isinstance(files[0], MessageMediaPhoto)))
        if is_media_obj and self.reject_media_once:
            self.reject_media_once = False
            raise RuntimeError("FILE_REFERENCE_EXPIRED")
        self.sends.append(("file", files, caption, kw.get("formatting_entities")))
        if isinstance(files, list):
            return [self._sent(True) for _ in files]
        return self._sent(True)


def _mapping(mid, dest):
    return {"id": mid, "source_channel": -1001910823870, "source_topic_id": 380160,
            "dest_channel": dest, "test_dest_channel": dest}


async def run() -> int:
    failures = 0

    def check(label, ok):
        nonlocal failures
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        failures += not ok

    td = tempfile.mkdtemp()
    listener._DB_PATH = os.path.join(td, "picks.db")
    listener._forwarded_init()

    # ── Single photo fanned out to two dests ─────────────────────────────────
    listener._media_relay.clear()
    user, bot = FakeUserClient(), FakeSender()
    group = [FakeMsg(media=MessageMediaPhoto())]
    ok1 = await listener._forward_group(group, _mapping("a", -101), user, bot, -101, use_test=True)
    ok2 = await listener._forward_group(group, _mapping("b", -102), user, bot, -102, use_test=True)
    check("both dests forwarded", ok1 and ok2)
    check("source downloaded exactly once", user.downloads == 1)
    check("first send uploads bytes", not isinstance(bot.sends[0][1], MessageMediaPhoto))
    check("second send reuses sent media by reference",
          isinstance(bot.sends[1][1], MessageMediaPhoto))
    check("reuse path keeps raw_text + entities",
          bot.sends[1][2] == RAW and bot.sends[1][3] == ENTS)

    # ── Album of two photos ──────────────────────────────────────────────────
    listener._media_relay.clear()
    user, bot = FakeUserClient(), FakeSender()
    gid = 555
    album = [FakeMsg(media=MessageMediaPhoto(), grouped_id=gid),
             FakeMsg(raw="", ents=[], media=MessageMediaPhoto(), grouped_id=gid)]
    await listener._forward_group(album, _mapping("a", -103), user, bot, -103, use_test=True)
    await listener._forward_group(album, _mapping("b", -104), user, bot, -104, use_test=True)
    check("album: each photo downloaded once", user.downloads == 2)
    reused = bot.sends[1][1]
    check("album: second send reuses the sent media list",
          isinstance(reused, list) and len(reused) == 2
          and all(isinstance(m, MessageMediaPhoto) for m in reused))

    # ── Stale file reference falls back to the slow path ─────────────────────
    listener._media_relay.clear()
    user, bot = FakeUserClient(), FakeSender()
    group = [FakeMsg(media=MessageMediaPhoto())]
    await listener._forward_group(group, _mapping("a", -105), user, bot, -105, use_test=True)
    bot.reject_media_once = True
    ok = await listener._forward_group(group, _mapping("b", -106), user, bot, -106, use_test=True)
    check("rejected reference still forwards", bool(ok))
    check("fallback re-downloads", user.downloads == 2)
    check("fallback uploads bytes", not isinstance(bot.sends[-1][1], MessageMediaPhoto))

    # ── Text-only posts never touch the relay ────────────────────────────────
    listener._media_relay.clear()
    user, bot = FakeUserClient(), FakeSender()
    group = [FakeMsg(media=None)]
    await listener._forward_group(group, _mapping("a", -107), user, bot, -107, use_test=True)
    check("text post sends as message", bot.sends[0][0] == "message")
    check("text post caches nothing", not listener._media_relay)

    print("FAILURES:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
