#!/usr/bin/env python3
"""Regression: XClIdGen bootstrap must survive X's web-build changes.

Pins two total Trent outages, both "every candidate page failed at once":

2026-08-24 — X redeployed its legacy webpack build with 16-hex chunk hashes
(7-hex for years before), so twscrape's hash-map regex (`[0-9a-f]{7}` exact)
matched nothing and every candidate page raised "Failed to parse scripts".
Also pins the subtle half of the fix: the name map must exclude hash-like
values by the SAME length-agnostic pattern, otherwise the 16-hex hashes leak
in as chunk NAMES and every reconstructed URL doubles the hash
(`{hash}.{hash}a.js`).

2026-09-24 — the x-web build stopped linking its chunks in the page HTML (one
entry script only) and the `sign.o-*.js` indices file moved behind a dynamic
import in a chunk the entry references (`sentry-filter-*.js`): two hops from
the HTML, so a level-1 scan found nothing on any page. Pinned over the
verbatim capture from that night: page HTML -> entry chunk -> sentry-filter
slice (byte-exact window [288000:304000] of the 538KB chunk, containing the
sign.o import) -> sign.o file. `_parse_anim_idx` must walk that chain, fetch
the carrier FIRST in wave 2 (priority), and stop early.

Fixtures are verbatim captured bytes, never retyped. Fully offline.

    python scripts/test_xclid_scripts_parse.py
"""
import asyncio
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.x_client import (  # noqa: E402
    _JS_REF_RE,
    _SCAN_BATCH,
    _find_indices_url,
    _get_scripts_list,
    _parse_anim_idx,
    patch_xclid,
)
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

# 7. patch_xclid() actually rebinds the module-globals twscrape resolves at
#    call time (load_keys/parse_anim_idx look them up on every call).
patch_xclid()
check("patch_xclid rebinds twscrape.xclid.get_scripts_list",
      _xclid.get_scripts_list is _get_scripts_list)
check("patch_xclid rebinds twscrape.xclid.parse_anim_idx",
      _xclid.parse_anim_idx is _parse_anim_idx)

# --- 2026-09-24: two-level indices scan ------------------------------------

FIXDIR = Path(__file__).resolve().parent / "fixtures"
PAGE_0924 = (FIXDIR / "x_home_xweb_20260924.html").read_text()
ENTRY_0924 = (FIXDIR / "x_entry_chunk_20260924.js").read_text()
SENTRY_0924 = (FIXDIR / "x_sentry_filter_slice_20260924.js").read_text()
SIGN_0924 = (FIXDIR / "x_sign_o_20260924.js").read_text()


class _FakeResp:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        pass


class _FakeClient:
    """Serves mapped URLs; anything else raises (-> _fetch_text yields "")."""

    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping
        self.log: list[str] = []

    async def get(self, url: str, *a, **k):
        self.log.append(url)
        if url in self.mapping:
            return _FakeResp(self.mapping[url])
        raise Exception(f"unmapped {url}")


entry_url = _get_scripts_list(PAGE_0924)[0]
sentry_url = urljoin(entry_url, "./assets/sentry-filter-DA8h2Jwu.js")
sign_url = urljoin(sentry_url, "./sign.o-DjU_k1xX.js")

# 8. The 2026-09-24 page links exactly one script and names nothing directly —
#    the shape that blinded the level-1 scanner.
check("2026-09 page links a single entry script",
      len(_get_scripts_list(PAGE_0924)) == 1, f"got {len(_get_scripts_list(PAGE_0924))}")
check("entry references the carrier chunk", "sentry-filter-DA8h2Jwu.js" in ENTRY_0924)
check("carrier slice holds the sign.o import", "`./sign.o-DjU_k1xX.js`" in SENTRY_0924)

# 9. Full chain over the real bytes: page -> entry -> sentry-filter -> sign.o.
clt = _FakeClient({entry_url: ENTRY_0924, sentry_url: SENTRY_0924, sign_url: SIGN_0924})
items = asyncio.run(_parse_anim_idx(PAGE_0924, clt))
check("indices parsed from sign.o via two-level scan",
      items == [10, 5, 34, 26], f"got {items}")
check("entry fetched first", clt.log[0] == entry_url)
check("carrier prioritized to front of wave 2", clt.log[1] == sentry_url)
check("indices file fetched last", clt.log[-1] == sign_url)
check("early stop bounds fetch count",
      len(clt.log) <= 2 + _SCAN_BATCH, f"{len(clt.log)} fetches")

# 10. No indices reference anywhere in the graph -> raises so the page-level
#     self-heal (next _XCLID_PAGES candidate) still engages.
clt_miss = _FakeClient({entry_url: ENTRY_0924.replace("sentry-filter", "sentry-flitre")})
try:
    asyncio.run(_parse_anim_idx(PAGE_0924, clt_miss))
    check("graph without indices file raises", False, "returned instead of raising")
except Exception as e:
    check("graph without indices file raises", "indices script" in str(e), str(e)[:80])

# 11. Ref collector sees quoted AND backticked imports (Vite emits both).
refs = _JS_REF_RE.findall('import("./assets/a.js");x=`./b.o-C1.js`;"no.txt"')
check("ref regex matches quoted and backticked .js", refs == ["./assets/a.js", "./b.o-C1.js"],
      f"got {refs}")

print()
if failures:
    print(f"{len(failures)} FAILED: {failures}")
    sys.exit(1)
print("all checks passed")
