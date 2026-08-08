"""Regression test: the Trent watcher's parlay veto catches Pikkit's multi-leg
tickets whatever the book calls them in the header.

    ~/venv/bin/python scripts/test_parlay_veto.py

UNLIKE every other `scripts/test_*.py` here, this one is NOT offline — the veto
IS a Claude call, so pinning it without one would only pin a string. It runs the
real `is_parlay_image()` over committed slips and costs ~$0.02 (8 image calls,
2 per fixture). Run it whenever `_IMAGE_IS_PARLAY_PROMPT` changes.

2026-08-08 is the case this pins. Pikkit draws every multi-leg ticket the same
way — a header, then one card per leg — and only the wording of that header
differs. The prompt listed the labels it had seen ("N-LEG PARLAY", "PARLAY",
"SGP", …), so a 6-leg same-game ticket headed "6-Pick Entry" (six players Under
0.5 HR, one $2000 stake, one +130 price) was forwarded to the singles-only
channel as msg 53.

The old prompt did not fail cleanly, and that is the reason both headers are
fixtures rather than just the one that leaked. Replayed on these same slips it
vetoed the "6-Pick Entry" 1 time in 5 and the "2-Leg Parlay" it supposedly did
know only 3 in 5 — a coin-flip near the decision boundary, not a gap with a
label on one side of it. Two of its own rules pushed a same-layout ticket
toward false: per-leg cards look like the "several separate slips" exclusion,
and "anything you are not sure about → false" turns any hesitation into a
forward. So the veto's own default-to-pass — which is correct, since a false
positive silently drops real picks — is what makes a label whitelist dangerous
rather than merely incomplete: an unrecognised header doesn't fail loudly, it
ships the parlay. Adding "N-Pick Entry" to the list would have left the ticket
one Pikkit rename away from leaking again.

Hence the rule the prompt now states, and the reason both headers are fixtures:
describe the SHAPE of a multi-leg ticket (a header that counts selections, or
2+ selections sharing one stake/price/payout, including same-game and including
one-card-per-leg), never the names a book happens to print.

The two negatives are here because tightening this prompt is what breaks real
picks, and nothing downstream would report it: a wrongly vetoed tweet leaves no
artifact at all, just a pick that never arrives.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import trent_watcher as tw  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "parlay_veto"

# (fixture, tweet text as X served it, is_parlay, what the image actually shows)
CASES = [
    (
        "parlay_6pick_entry.jpg",
        "BRAVES/YANKS NO HR LAY: ✋\U0001f6ab\n\nNONE OF THESE BOZOS ARE GOING YARD "
        "AT PIKKIT AT THE PARK. https://t.co/Rk0Yt4vHqA",
        True,
        'Pikkit "6-Pick Entry" — 6x Under 0.5 HR, same game, $2000 @ +130 (the miss)',
    ),
    (
        "parlay_2leg_pikkit.jpg",
        "HAPPY SALE DAY\n\nBRAVES ML + SALE 7+ FOR A MEGA\n\nSEE YOU AT @pikkitsports "
        "AT THE PARK https://t.co/Vn7D9xER94",
        True,
        'Pikkit "2-Leg Parlay" — same layout, header the old prompt did know',
    ),
    (
        "single_slip.jpg",
        "gimmie the backs for 5 racks https://t.co/9Iodjx756H",
        False,
        "single selection (Arizona game winner), one price, one payout",
    ),
    (
        "promo_graphic.jpg",
        "DODGERS ML IS A 10U MORTAL MEGA MAX https://t.co/0rGDbhx22v",
        False,
        "promo art, no slip anywhere in the image",
    ),
]

RUNS = 2  # the veto is temperature=0, but a borderline prompt still wobbles


async def main() -> int:
    print(f"module under test: {tw.__file__}\nmodel: {tw.CLASSIFY_MODEL}\n")
    failures = []

    for fixture, text, want, desc in CASES:
        path = FIX / fixture
        if not path.exists():
            print(f"FAIL {fixture}: fixture missing")
            failures.append(fixture)
            continue

        blob = path.read_bytes()

        async def _local(url, _blob=blob):  # only the network fetch is stubbed
            return ("image/jpeg", _blob)

        real_download, tw.download_image = tw.download_image, _local
        try:
            got = [
                await tw.is_parlay_image({"id": fixture, "text": text, "photos": "local://" + fixture})
                for _ in range(RUNS)
            ]
        finally:
            tw.download_image = real_download

        ok = all(g == want for g in got)
        label = "veto" if want else "forward"
        print(f"{'ok  ' if ok else 'FAIL'} {fixture:26} expect {label:7} got {sum(got)}/{RUNS} veto   {desc}")
        if not ok:
            failures.append(fixture)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        print("A false negative forwards a parlay into a singles-only channel;")
        print("a false positive silently drops real picks and leaves no trace.")
        return 1
    print(f"all {len(CASES)} fixtures pinned ({RUNS} runs each)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
