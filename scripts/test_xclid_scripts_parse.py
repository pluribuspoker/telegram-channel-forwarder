#!/usr/bin/env python3
"""Regression: XClIdGen bootstrap must parse webpack chunk maps at any hash length.

Pins the 2026-08-24 Trent outage. X redeployed its legacy webpack build with
16-hex chunk hashes (7-hex for years before), so twscrape's hash-map regex
(`[0-9a-f]{7}` exact) matched nothing and every candidate page raised "Failed
to parse scripts" — a total XClIdGen bootstrap failure that took the watcher
down. Also pins the subtle half of the fix: the name map must exclude hash-like
values by the SAME length-agnostic pattern, otherwise the 16-hex hashes leak in
as chunk NAMES and every reconstructed URL doubles the hash
(`{hash}.{hash}a.js`).

Fixture is the verbatim inline <script> block from https://x.com/home captured
during the outage (real bytes, never retyped). Fully offline.

    python scripts/test_xclid_scripts_parse.py
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.x_client import _get_scripts_list, patch_xclid  # noqa: E402
from twscrape import xclid as _xclid  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "x_home_webpack_maps_20260824.html"

# The exact indices-chunk URL the 2026-08-24 build serves (verified live: 200,
# INDICES_REGEX yields [15, 34, 11, 27]).
ONDEMAND_URL = "https://abs.twimg.com/responsive-web/client-web/ondemand.s.d03eda01013904f3a.js"

URL_SHAPE = re.compile(
    r"^https://abs\.twimg\.com/responsive-web/client-web/(?P<name>.+)\.(?P<hash>[0-9a-f]{7,})a\.js$"
)

failures = []


def check(label: str, ok: bool, detail: str = ""):
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))
    if not ok:
        failures.append(label)


text = FIXTURE.read_text()

# 1. Stock twscrape (7-hex only) goes blind on this build — the outage.
stock = _xclid.get_scripts_list
if getattr(stock, "__module__", "").startswith("twscrape"):
    try:
        stock(text)
        check("stock parser fails on 16-hex build (outage repro)", False, "parsed fine?!")
    except Exception:
        check("stock parser fails on 16-hex build (outage repro)", True)
else:
    print("SKIP  stock parser already patched in this process")

# 2. Patched parser reconstructs the chunk list, including the indices chunk.
urls = _get_scripts_list(text)
check("patched parser returns URLs", len(urls) > 500, f"got {len(urls)}")
check("indices chunk URL present", ONDEMAND_URL in urls)

# 3. Every URL is well-formed and no NAME is itself a bare hash (the name-map
#    leak would render {hash}.{hash}a.js).
bad_shape = [u for u in urls if not URL_SHAPE.match(u)]
check("all URLs match {name}.{hash}a.js", not bad_shape, f"e.g. {bad_shape[:2]}")
doubled = [
    u for u in urls
    if (m := URL_SHAPE.match(u)) and re.fullmatch(r"[0-9a-f]{7,}", m.group("name"))
]
check("no hash leaked into the name map", not doubled, f"e.g. {doubled[:2]}")

# 4. Backward compatible with the old 7-hex build.
legacy = '{1:"abc1234",2:"def5678"}...{1:"ondemand.s",2:"vendors~main"}'
legacy_urls = _get_scripts_list(legacy)
check(
    "7-hex legacy build still parses",
    legacy_urls == [
        "https://abs.twimg.com/responsive-web/client-web/ondemand.s.abc1234a.js",
        "https://abs.twimg.com/responsive-web/client-web/vendors~main.def5678a.js",
    ],
    f"got {legacy_urls}",
)

# 5. x-web (Vite) pages: directly-linked assets returned as-is.
xweb = '<link rel="modulepreload" href="https://abs.twimg.com/x-web/x-web/entry-client-logged-out-DCPSu4tq.js">'
check(
    "x-web direct links pass through",
    _get_scripts_list(xweb)
    == ["https://abs.twimg.com/x-web/x-web/entry-client-logged-out-DCPSu4tq.js"],
)

# 6. A page with neither scheme still raises (self-heal moves to the next page).
try:
    _get_scripts_list("<html><body>nothing here</body></html>")
    check("script-less page raises", False, "returned instead of raising")
except Exception:
    check("script-less page raises", True)

# 7. patch_xclid() actually rebinds the module-global twscrape resolves at call
#    time (parse_anim_idx looks it up in module globals on every call).
patch_xclid()
check("patch_xclid rebinds twscrape.xclid.get_scripts_list",
      _xclid.get_scripts_list is _get_scripts_list)

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
