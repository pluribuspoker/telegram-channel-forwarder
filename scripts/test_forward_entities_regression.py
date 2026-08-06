"""Regression: forwarded messages must keep the source's formatting exactly.

The bug this pins down: `send_group` used Telethon's `msg.text` — which is
`parse_mode.unparse(raw_text, entities)`, i.e. the *markdown render* with `**`
inserted around bold — while passing `msg.entities`, whose offsets index
`raw_text`. Because `formatting_entities=` also makes Telethon skip parsing, the
delimiters survived as literal characters AND every entity from the first one on
landed N chars early.

Real case (fc:3539, 2026-08-06), source -1001910823870:461333:
    want:  CFL <strong><u>SATURDAY</u></strong> MAX PLAY …  <blockquote>YTD CFL: 25-9</blockquote>
    got:   CFL <strong><u>**SATURD</u></strong>AY** MAX PLAY …  (3.9u<blockquote>) …YTD CFL: </blockquote>25-9

Run:  ~/venv/bin/python scripts/test_forward_entities_regression.py
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telethon.extensions import html as tl_html
from telethon.extensions import markdown as tl_markdown
from telethon.tl.types import (
    MessageEntityBlockquote,
    MessageEntityBold,
    MessageEntityUnderline,
    MessageMediaPhoto,
)

from common import send_group

# Captured verbatim from the source message that triggered the report.
RAW = ("Andrew Cunningham\n\nCFL SATURDAY MAX PLAY‼️ \n\n"
       "• Alouettes 1Q ML (-130) / (3.9u) \n\nYTD CFL: 25-9")
ENTS = [
    MessageEntityBold(offset=23, length=8),
    MessageEntityUnderline(offset=23, length=8),
    MessageEntityBlockquote(offset=81, length=13),
]
WANT_HTML = tl_html.unparse(RAW, ENTS)


class FakeMsg:
    def __init__(self, raw, ents, media=None):
        self.raw_text = raw
        self.entities = ents
        self.media = media
        self.id = 1

    @property
    def text(self):
        """Mirrors Telethon's Message.text: the markdown render of raw_text."""
        return tl_markdown.unparse(self.raw_text, self.entities)


class FakeClient:
    async def download_media(self, media, file=None):
        return b"\xff\xd8\xff\xe0jpegbytes"


class FakeSender(FakeClient):
    def __init__(self):
        self.calls = []

    async def send_message(self, dest, text, **kw):
        self.calls.append((text, kw.get("formatting_entities")))
        return type("S", (), {"id": 99})()

    async def send_file(self, dest, files, caption=None, **kw):
        self.calls.append((caption, kw.get("formatting_entities")))
        return type("S", (), {"id": 99})()


def check(label, sent_text, sent_ents, want_html=WANT_HTML, want_text=RAW):
    got_html = tl_html.unparse(sent_text, sent_ents or [])
    ok = sent_text == want_text and got_html == want_html
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
    if not ok:
        print(f"        want text: {want_text!r}")
        print(f"        got  text: {sent_text!r}")
        print(f"        want html: {want_html!r}")
        print(f"        got  html: {got_html!r}")
    return ok


async def main():
    failures = 0

    # Guard (lesson #22): the fixture must actually exercise the mangling condition,
    # otherwise this test passes against the broken code too.
    m = FakeMsg(RAW, ENTS)
    assert m.text != m.raw_text, "fixture has no markdown-materialising entity — test is vacuous"
    assert "**SATURD" in tl_html.unparse(m.text, ENTS), "fixture does not reproduce the bug"
    print("fixture reproduces the pre-fix mangling: ok")

    print("send_group:")
    for label, msg in (
        ("plain text message", FakeMsg(RAW, ENTS)),
        ("photo + caption", FakeMsg(RAW, ENTS, media=MessageMediaPhoto())),
    ):
        s = FakeSender()
        await send_group(FakeClient(), [msg], dest_entity=-100, sender=s)
        failures += not check(label, *s.calls[-1])

    # Album path (>1 message) carries its own copy of the same pairing.
    s = FakeSender()
    album = [FakeMsg(RAW, ENTS, media=MessageMediaPhoto()),
             FakeMsg("", [], media=MessageMediaPhoto())]
    await send_group(FakeClient(), album, dest_entity=-100, sender=s)
    failures += not check("album caption", *s.calls[-1])

    # text_suffix appends at the end, so no existing offset may move.
    s = FakeSender()
    await send_group(FakeClient(), [FakeMsg(RAW, ENTS)], dest_entity=-100,
                     sender=s, text_suffix="— Kelly")
    failures += not check("text_suffix appended", *s.calls[-1],
                          want_text=RAW + "\n\n— Kelly",
                          want_html=WANT_HTML + "\n\n— Kelly")

    print("FAILURES:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
