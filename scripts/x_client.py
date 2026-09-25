"""
x_client.py — shared twscrape setup for the X/Twitter scrapers.

Two upstream/library quirks are worked around here so every caller gets them:

  1. XClIdGen fetches the wrong page (twscrape 0.19.1, the latest release).
     twscrape builds X's anti-bot X-Client-Transaction-ID by scraping a page for
     the `ondemand.s-*.js` indices chunk. It hardcodes `https://x.com/tesla`, but
     X keeps migrating pages to a slim single-bundle build with no such chunk —
     so XClIdGen creation fails 3/3 and EVERY request aborts (user_by_login
     returns None, which looks exactly like bad cookies — but the cookieless
     bootstrap failing first means it never was a cookie problem).
     The migration front keeps moving: first profile pages (/tesla, /elonmusk),
     then (2026-07) the logged-out homepage `https://x.com` itself. Rather than
     hardcode one page and re-break on the next migration, we try an ordered list
     (`_XCLID_PAGES`) and keep the first page still served by the full webpack
     build (the one shipping the indices chunk). This SELF-HEALS: as long as one
     candidate still ships the chunk, the scraper recovers with no code change,
     logging a WARNING when it falls through to a non-primary page. Only when
     EVERY candidate fails (a real build change) does it raise — and then it's a
     code fix (add/repoint a page), NOT a cookie refresh. `diagnose_failure()`
     tells the two apart so alerts name the right remedy.
     Upstream: https://github.com/vladkens/twscrape/issues/248

  1b. Webpack chunk hashes grew from 7 to 16 hex chars (2026-08-24 outage).
     twscrape reconstructs chunk URLs from two maps embedded in the page HTML
     (`{chunk_id:"hash"}` + `{chunk_id:"name"}`, URL = `{name}.{hash}a.js`) and
     tells the maps apart by "hash = exactly 7 lowercase hex". When X redeployed
     with 16-hex hashes, every webpack page raised "Failed to parse scripts"
     (and the slim x-web pages never ship the chunk), so ALL candidates failed
     at once — a total outage with no cookie involvement. Still unfixed in
     twscrape 0.20.0 (which instead bootstraps with account cookies and treats
     the logged-out x-web build as a dead end). We monkeypatch
     `get_scripts_list` with length-agnostic hash parsing (`{7,}` hex, and the
     symmetric exclusion when building the name map — without it the 16-hex
     values leak in as chunk NAMES and every URL doubles the hash). Stock
     `parse_anim_idx` then finds `ondemand.s.{hash}a.js` in the URL list by
     itself, so this patch alone also survives a stock reinstall.

  1c. The indices import moved two levels deep (2026-09-24 outage).
     The x-web (Vite) build stopped linking its bundle chunks in the page HTML:
     the page now links ONE entry script, and the transaction-id indices file
     (`sign.o-*.js`, the successor of `ondemand.s-*.js`) is dynamically
     imported from a chunk the ENTRY references (observed carrier:
     `sentry-filter-*.js`) — two hops from the HTML. A scanner that only greps
     the page-linked scripts finds nothing, and since every `_XCLID_PAGES`
     candidate now serves this build, page-hopping no longer self-heals (the
     rollout was staged across X's edge starting 2026-09-23, which is why
     failures were intermittent for a day before going total on 2026-09-24).
     Fix: `_parse_anim_idx`/`_find_indices_url` below walk the chunk graph
     breadth-first from the page-linked scripts (3 deep, fetch-budgeted,
     early-stopping, sentry-filter first since it's the observed carrier),
     monkeypatched over `twscrape.xclid.parse_anim_idx` — which `load_keys`
     resolves from module globals at call time, same mechanism as the
     get_scripts_list rebind. If X renames the file away from both known names,
     a content-signature last resort (clustered `(x[N],16)` sites) picks it —
     loudly, and marked in `bootstrap_report()` so `diagnose_failure()` blames
     the build, not the cookies. This supersedes the hand-patched level-1-only
     `_find_indices_url` in site-packages (out-of-repo state a reinstall would
     lose anyway). Pinned by `scripts/test_xclid_scripts_parse.py` over the
     verbatim 2026-09-24 page/chunk bytes.

  2. add_account_cookies() silently ignores rotated cookies.
     It's a no-op when the account already exists, so twscrape keeps using the
     cookies cached in accounts.db and pasting fresh ones into .env.local has no
     effect. We drop the account first when the stored cookies differ.
"""

import asyncio
import logging
import os
import re
from urllib.parse import urljoin

import bs4
from twscrape import API
from twscrape import xclid as _xclid

ACCOUNT_NAME = "me"

_log = logging.getLogger(__name__)

# Ordered pages to try when bootstrapping XClIdGen. X migrates its web build
# page-by-page, so we keep the first still served by the full webpack build (the
# one shipping the transaction-id indices chunk). `/home` is the confirmed-good
# page as of 2026-08-24 (needs the 16-hex hash patch below); the rest are
# app-shell routes kept as self-heal fallbacks for the next migration. A page on
# the slim build just fails load_keys and is skipped — including a dead one is
# harmless. Reorder so the working one is first.
_XCLID_PAGES = (
    "https://x.com/home",
    "https://x.com/explore",
    "https://x.com/notifications",
    "https://x.com/search?q=nba&src=typed_query",
    "https://x.com/tesla",
    "https://x.com/elonmusk",
    "https://x.com",
)


class XClIdBootstrapError(Exception):
    """XClIdGen (anti-bot transaction-ID) could not be built on ANY candidate page.

    Means X changed its web build again — a CODE fix (repoint `_XCLID_PAGES`),
    NOT a cookie refresh. Distinct from a cookie rejection; `diagnose_failure()`
    relies on this type to tell the two apart.
    """


# Webpack-map parsing for the legacy build (quirk 1b). Hash values were 7 hex
# chars for years, 16 since 2026-08 — accept any length ≥7 so the next resize
# doesn't take us down. The name map must exclude by the SAME pattern, or hash
# values count as chunk names and every reconstructed URL doubles the hash.
_WEBPACK_MAP_RE = re.compile(r'(\d+):"([^"]+)"')
_WEBPACK_HASH_RE = re.compile(r"[0-9a-f]{7,}")
# Direct script links in the current builds: x-web (Vite) assets, mirrored from
# twscrape's own ASSET_URL_RE so this module stays self-contained.
_XWEB_ASSET_RE = re.compile(r"https://[\w.-]+/x-web/[\w./-]+\.js")


def _get_scripts_list(text: str) -> list[str]:
    """`twscrape.xclid.get_scripts_list` with length-agnostic webpack hashes."""
    urls = list(dict.fromkeys(_XWEB_ASSET_RE.findall(text)))
    if urls:
        return urls

    hash_map: dict[str, str] = {}
    name_map: dict[str, str] = {}
    for m in _WEBPACK_MAP_RE.finditer(text):
        chunk_id, val = m.group(1), m.group(2)
        if _WEBPACK_HASH_RE.fullmatch(val):
            hash_map[chunk_id] = val
        else:
            name_map[chunk_id] = val

    if not hash_map:
        raise Exception("Failed to parse scripts")

    return [
        f"https://abs.twimg.com/responsive-web/client-web/{name_map.get(cid, cid)}.{h}a.js"
        for cid, h in hash_map.items()
    ]


# The file holding the animation indices: `ondemand.s-*.js` on the legacy
# webpack build, `sign.o-*.js` on x-web. \b guards against substring hits like
# `design.o-*.js`.
_INDICES_FILE_RE = re.compile(r"(?:\.{0,2}/)?[\w./-]*?\b(?:ondemand\.s|sign\.o)[\w.-]*\.js")
# A .js reference inside a chunk body: Vite emits both "./assets/x.js" static
# imports and `./x.js` backticked dynamic imports; absolute URLs are covered by
# _XWEB_ASSET_RE at the call site.
_JS_REF_RE = re.compile(r"""["'`]((?:\.{0,2}/)?[\w./-]+\.js)["'`]""")
# Bound the scan so a future build change degrades to a fast failure (the
# 7-page × 3-attempt loop above this multiplies everything), and batch fetches
# so an early hit stops the scan after at most one batch of waste. Depth 1 is
# the page-linked scripts; sign.o sat at depth 2 on 2026-09-24, so depth 3 is
# one more hop of headroom for the next move.
_SCAN_MAX_DEPTH = 3
_SCAN_FETCH_BUDGET = 400
_SCAN_BATCH = 16
# Content-signature last resort, for when X renames the indices file away from
# both known names. 4+ `(x[N],16)` parseInt sites clustered within a few hundred
# bytes is the observed shape of every real indices file ([15,34,11,27] in the
# 2026-08-24 ondemand.s, [10,5,34,26] across 173 bytes in the 2026-09-24
# sign.o); incidental parseInt-hex code elsewhere is scattered and fails the
# cluster test. A wrong pick is worse than a loud failure UNLESS it is marked —
# hence the bootstrap-report flag, which makes diagnose_failure() blame the
# build instead of the cookies.
_SIG_MIN_SITES = 4
_SIG_MAX_SPAN = 4096

# Per-process XClIdGen bootstrap health (see bootstrap_report). Never reset:
# the watcher is a oneshot, so one process = one run.
_BOOTSTRAP_REPORT: dict = {"create_failures": 0, "fallback_page": None, "heuristic": False}


def bootstrap_report() -> dict:
    """This process's XClIdGen bootstrap health, as a copy.

    create_failures — create() calls that failed on EVERY candidate page (each
                      is one twscrape "XClIdGen creation attempt N/3 failed")
    fallback_page   — the non-primary _XCLID_PAGES entry that last succeeded
    heuristic       — the indices file was picked by content signature, not name

    All zero/None/False on a clean run. Anything else while requests still
    succeed is X's build shifting under us (trent_watcher's early warning).
    """
    return dict(_BOOTSTRAP_REPORT)


async def _fetch_text(url: str, clt) -> str:
    try:
        return (await clt.get(url)).text
    except Exception:
        return ""


def _is_carrier(url: str) -> bool:
    return url.rsplit("/", 1)[-1].startswith("sentry-filter")


# What the scan needs from one chunk body, keyed by its content-hashed URL:
# (indices-file URL it names, resolved x-web/responsive-web refs, content
# signature (span, size, sites)). Process-scoped so repeat scans of one graph
# download nothing: a failed run scans it ~28 times (7 pages × 3 twscrape create
# attempts + diagnose_failure) inside the watcher's 90s fetch timeout, and a
# full miss is _SCAN_FETCH_BUDGET fetches — re-downloading that per scan would
# turn a loud bootstrap failure into the silent "Rate-limited" timeout. Failed
# (empty) fetches are never cached, so the next scan retries them.
_CHUNK_DIGESTS: dict[str, tuple[str | None, tuple[str, ...], tuple[int, int, int] | None]] = {}


def _digest_chunk(url: str, body: str):
    m = _INDICES_FILE_RE.search(body)
    if m:
        return urljoin(url, m.group(0)), (), None
    sig = None
    sites = list(_xclid.INDICES_REGEX.finditer(body))
    if len(sites) >= _SIG_MIN_SITES:
        span = sites[-1].end() - sites[0].start()
        if span <= _SIG_MAX_SPAN:
            sig = (span, len(body), len(sites))
    refs: dict[str, None] = {}
    for ref in _JS_REF_RE.findall(body) + _XWEB_ASSET_RE.findall(body):
        full = urljoin(url, ref)
        if "/x-web/" in full or "/responsive-web/" in full:
            refs[full] = None
    return None, tuple(refs), sig


async def _find_indices_url(scripts: list[str], clt) -> str:
    """Locate the indices file behind the page's script graph (quirk 1c).

    Breadth-first: depth 1 is the page-linked scripts, each next frontier the
    x-web/responsive-web .js files the previous one references, up to
    _SCAN_MAX_DEPTH deep and _SCAN_FETCH_BUDGET chunks in total (cached ones
    count too, so every call walks the same graph). The first body naming
    sign.o/ondemand.s wins immediately. `sentry-filter-*` chunks go first in
    every frontier — the observed carrier of the sign.o import in the 2026-09
    build — so the common case costs ~17 fetches, while a moved carrier is still
    found by the full scan. Only when NO body names the file does the content
    signature (_SIG_MIN_SITES) decide: tightest cluster wins, logged loudly and
    marked in the bootstrap report.
    """
    frontier = list(dict.fromkeys(scripts))
    seen = set(frontier)
    candidates: list[tuple[int, int, str, int]] = []  # (span, body size, url, sites)
    scanned: list[int] = []  # chunks per depth, for the summary
    fetched = 0
    for depth in range(1, _SCAN_MAX_DEPTH + 1):
        if not frontier or fetched >= _SCAN_FETCH_BUDGET:
            break
        # Stable sort: carrier candidates first, otherwise first-seen order.
        frontier.sort(key=lambda u: 0 if _is_carrier(u) else 1)
        frontier = frontier[: _SCAN_FETCH_BUDGET - fetched]
        expand = depth < _SCAN_MAX_DEPTH and fetched + len(frontier) < _SCAN_FETCH_BUDGET
        nxt: list[str] = []
        for i in range(0, len(frontier), _SCAN_BATCH):
            batch = frontier[i : i + _SCAN_BATCH]
            todo = [u for u in batch if u not in _CHUNK_DIGESTS]
            bodies = dict(zip(todo, await asyncio.gather(*(_fetch_text(u, clt) for u in todo))))
            fetched += len(batch)
            for url in batch:
                digest = _CHUNK_DIGESTS.get(url)
                if digest is None:
                    if not bodies.get(url):
                        continue  # failed fetch: nothing to learn, retried next scan
                    digest = _CHUNK_DIGESTS[url] = _digest_chunk(url, bodies[url])
                named, refs, sig = digest
                if named:
                    return named
                if sig:
                    candidates.append((sig[0], sig[1], url, sig[2]))
                if expand:
                    for full in refs:
                        if full not in seen:
                            seen.add(full)
                            nxt.append(full)
        scanned.append(len(frontier))
        frontier = nxt

    summary = (
        f"scanned {'+'.join(map(str, scanned)) or 0} chunks by depth, "
        f"limits {_SCAN_MAX_DEPTH} deep / {_SCAN_FETCH_BUDGET} fetches"
    )
    if candidates:
        span, size, url, sites = min(candidates)
        _BOOTSTRAP_REPORT["heuristic"] = True
        _log.warning(
            "XClIdGen indices file picked by CONTENT HEURISTIC: %s (%d clustered "
            "(x[N],16) sites within %d bytes; %d candidate(s); %s). No chunk names "
            "sign.o/ondemand.s any more — X renamed the file: update "
            "_INDICES_FILE_RE in scripts/x_client.py.",
            url, sites, span, len(candidates), summary,
        )
        return url
    raise Exception(
        f"Couldn't get XClientTxId indices script ({summary}; no name or "
        "content-signature match)"
    )


async def _parse_anim_idx(text: str, clt) -> list[int]:
    """`twscrape.xclid.parse_anim_idx` with the breadth-first chunk scan (quirk 1c)."""
    scripts = _get_scripts_list(text)
    direct = [u for u in scripts if _INDICES_FILE_RE.search(u)]
    url = direct[0] if direct else await _find_indices_url(scripts, clt)

    body = await _xclid.get_tw_page_text(url, clt)
    items = [int(m.group(2)) for m in _xclid.INDICES_REGEX.finditer(body)]
    if not items:
        raise Exception("Couldn't get XClientTxId indices")
    return items


def patch_xclid() -> None:
    """Bootstrap XClIdGen from the first `_XCLID_PAGES` entry on the full build."""
    if getattr(_xclid.XClIdGen, "_home_patched", False):
        return

    # load_keys/parse_anim_idx resolve these from module globals at call time,
    # so rebinding the attributes is enough.
    _xclid.get_scripts_list = _get_scripts_list
    _xclid.parse_anim_idx = _parse_anim_idx

    async def _create_from_candidates() -> "_xclid.XClIdGen":
        clt = _xclid._make_client()
        errors: list[str] = []
        try:
            for idx, url in enumerate(_XCLID_PAGES):
                try:
                    text = await _xclid.get_tw_page_text(url, clt)
                    soup = bs4.BeautifulSoup(text, "html.parser")
                    vk_bytes, anim_key = await _xclid.load_keys(soup, clt)
                except Exception as e:  # this page is on the slim build / errored
                    errors.append(f"{url} -> {type(e).__name__}: {e}")
                    continue
                if idx > 0:
                    _BOOTSTRAP_REPORT["fallback_page"] = url
                    _log.warning(
                        "XClIdGen recovered via fallback page %s; page(s) ahead of "
                        "it no longer ship the indices chunk — move it to the front "
                        "of _XCLID_PAGES. Failures: %s",
                        url, " | ".join(errors),
                    )
                return _xclid.XClIdGen(vk_bytes, anim_key)
        finally:
            await clt.aclose()
        _BOOTSTRAP_REPORT["create_failures"] += 1
        raise XClIdBootstrapError(
            "XClIdGen bootstrap failed on every candidate page — X likely changed "
            "its web build again; none still ship the transaction-id indices chunk. "
            "Repoint _XCLID_PAGES in scripts/x_client.py (code fix, NOT a cookie "
            "refresh). Failures: " + " | ".join(errors)
        )

    _xclid.XClIdGen.create = staticmethod(_create_from_candidates)
    _xclid.XClIdGen._home_patched = True


async def diagnose_failure() -> tuple[str, str]:
    """Classify why an X fetch failed, so alerts point at the real remedy.

    twscrape signals every failure the same way (None / aborted request), which
    conflates two very different causes. We disambiguate by bootstrapping XClIdGen
    directly — it is cookieless, touching only public pages:

      ("bootstrap", detail) — the anti-bot transaction-ID generator can't be built
          because X changed its web build. CODE fix (repoint _XCLID_PAGES), not a
          cookie refresh. This is what masqueraded as "bad cookies" on 2026-07-21.
      ("auth", detail)      — XClIdGen builds fine (anti-bot layer OK), so an
          authenticated request being rejected points at the cookies
          (expired/revoked) — refresh X_AUTH_TOKEN / X_CT0.

    A bootstrap that only succeeded via the content heuristic counts as
    "bootstrap": a wrong indices pick yields rejected requests that look exactly
    like dead cookies, the historical wrong-way alert.
    """
    patch_xclid()
    try:
        await _xclid.XClIdGen.create()
    except Exception as e:
        return ("bootstrap", f"{type(e).__name__}: {e}")
    if bootstrap_report()["heuristic"]:
        return (
            "bootstrap",
            "XClIdGen bootstrap succeeded only via the content heuristic (no chunk "
            "names sign.o/ondemand.s any more). If authenticated requests still "
            "fail, the heuristic likely picked the wrong indices file — update "
            "_INDICES_FILE_RE / the scan in scripts/x_client.py (code fix); the "
            "cookies are probably fine.",
        )
    return (
        "auth",
        "XClIdGen bootstrap succeeded (anti-bot layer OK), so an authenticated "
        "request being rejected points at expired/revoked cookies.",
    )


class XCredentialsError(Exception):
    """X cookies are missing. Fatal — a human has to paste fresh ones."""


async def build_api(auth_token: str = "", ct0: str = "") -> API:
    """Return a twscrape API authenticated with the current cookies.

    Reads X_AUTH_TOKEN / X_CT0 from the environment when not passed explicitly.
    Keep them in .env.local, NOT .env — syncenv overwrites .env.
    """
    auth_token = auth_token or os.environ.get("X_AUTH_TOKEN", "")
    ct0 = ct0 or os.environ.get("X_CT0", "")
    if not auth_token or not ct0:
        raise XCredentialsError(
            "X_AUTH_TOKEN / X_CT0 missing — set them in .env.local "
            "(NOT .env, which syncenv overwrites)"
        )

    patch_xclid()

    api = API()
    # Rotated cookies are ignored unless we drop the cached account first.
    existing = await api.pool.get_account(ACCOUNT_NAME)
    if existing is not None:
        stored = existing.cookies or {}
        if stored.get("auth_token") != auth_token or stored.get("ct0") != ct0:
            await api.pool.delete_accounts([ACCOUNT_NAME])

    await api.pool.add_account_cookies(ACCOUNT_NAME, f"auth_token={auth_token}; ct0={ct0}")
    return api
