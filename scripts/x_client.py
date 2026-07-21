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

  2. add_account_cookies() silently ignores rotated cookies.
     It's a no-op when the account already exists, so twscrape keeps using the
     cookies cached in accounts.db and pasting fresh ones into .env.local has no
     effect. We drop the account first when the stored cookies differ.
"""

import logging
import os

import bs4
from twscrape import API
from twscrape import xclid as _xclid

ACCOUNT_NAME = "me"

_log = logging.getLogger(__name__)

# Ordered pages to try when bootstrapping XClIdGen. X migrates its web build
# page-by-page, so we keep the first still served by the full webpack build (the
# one shipping the transaction-id indices chunk). `/home` is the confirmed-good
# page as of 2026-07; the rest are app-shell routes kept as self-heal fallbacks
# for the next migration. A page on the slim build just fails load_keys and is
# skipped — including a dead one is harmless. Reorder so the working one is first.
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


def patch_xclid() -> None:
    """Bootstrap XClIdGen from the first `_XCLID_PAGES` entry on the full build."""
    if getattr(_xclid.XClIdGen, "_home_patched", False):
        return

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
                    _log.warning(
                        "XClIdGen recovered via fallback page %s; page(s) ahead of "
                        "it no longer ship the indices chunk — move it to the front "
                        "of _XCLID_PAGES. Failures: %s",
                        url, " | ".join(errors),
                    )
                return _xclid.XClIdGen(vk_bytes, anim_key)
        finally:
            await clt.aclose()
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
    """
    patch_xclid()
    try:
        await _xclid.XClIdGen.create()
    except Exception as e:
        return ("bootstrap", f"{type(e).__name__}: {e}")
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
