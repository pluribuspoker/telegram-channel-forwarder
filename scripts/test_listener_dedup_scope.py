"""Content-dedup is scoped by source topic (2026-10-10).

The dedup key was (dest, text_hash): a byte-identical pick posted in BOTH the CICL
and CILT topics (different cappers, same dest channels) within the 15-min window
dropped the second copy permanently — the probe advances last_seen past the message
on the same cycle that declines it, so nothing ever retried. The key is now scoped
by the mapping's source_topic_id; delete-and-repost (what the dedup is for) always
happens within one topic, so the suppression it exists for still fires.

Run:  ~/venv/bin/python scripts/test_listener_dedup_scope.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# listener.py reads these at import; the sandbox .env provides them, but keep the
# test runnable standalone too.
for k, v in {"TELEGRAM_API_ID": "1", "TELEGRAM_API_HASH": "x",
             "TELEGRAM_SESSION": "", "BOT_TOKEN": "x", "MAPPINGS_CONFIG": "[]"}.items():
    os.environ.setdefault(k, v)

import listener  # noqa: E402


class FakeMsg:
    def __init__(self, text):
        self.text = text


def main() -> int:
    failures = 0

    def check(label, ok):
        nonlocal failures
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        failures += not ok

    cicl = {"id": "cicl-to-fc", "source_topic_id": 380160}
    cilt = {"id": "cilt-to-fc", "source_topic_id": 380157}
    dagger = {"id": "dagger-to-fc", "source_topic_id": None}

    group = [FakeMsg("❗️MAIN PLAY❗️ Ravens -3 (-110) 2U")]
    sig = listener._content_sig(group)
    check("signature exists for text post", bool(sig))
    check("media-only post has no signature", listener._content_sig([FakeMsg("")]) is None)
    check("None sig stays None when scoped", listener._scoped_sig(cicl, None) is None)

    a, b = listener._scoped_sig(cicl, sig), listener._scoped_sig(cilt, sig)
    check("identical text in different topics gets different keys", a != b)
    check("same topic, same text → same key (repost still suppressed)",
          listener._scoped_sig(cicl, sig) == a)
    check("topic id prefixes the hash", a == f"380160:{sig}")
    check("topic-less mapping scopes to 0", listener._scoped_sig(dagger, sig) == f"0:{sig}")

    # Round-trip through the sqlite window: a CILT post must not be suppressed by a
    # CICL record for the same dest + text, while a CICL repost must be.
    with tempfile.TemporaryDirectory() as td:
        listener._DB_PATH = os.path.join(td, "picks.db")
        listener._forwarded_init()
        dest = -1002486251914
        listener._content_save(dest, a)
        check("same topic repost is recent", listener._content_recent(dest, a))
        check("other topic same text is NOT recent", not listener._content_recent(dest, b))
        check("other dest same key is NOT recent", not listener._content_recent(dest + 1, a))

    print("FAILURES:", failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
