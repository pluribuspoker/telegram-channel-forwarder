"""
x_client.py — shared twscrape setup for the X/Twitter scrapers.

Two upstream/library quirks are worked around here so every caller gets them:

  1. XClIdGen fetches the wrong page (twscrape 0.19.1, the latest release).
     twscrape builds X's anti-bot X-Client-Transaction-ID by scraping a page for
     the `ondemand.s-*.js` indices chunk. It hardcodes `https://x.com/tesla`, but
     X migrated profile pages to a new build that ships a single bundle and no
     such chunk — so XClIdGen creation fails 3/3 and EVERY request aborts
     (user_by_login returns None, which looks exactly like bad cookies).
     The logged-out homepage still serves the legacy webpack build with the full
     chunk map, so we point XClIdGen at `https://x.com` instead.
     Upstream: https://github.com/vladkens/twscrape/issues/248

  2. add_account_cookies() silently ignores rotated cookies.
     It's a no-op when the account already exists, so twscrape keeps using the
     cookies cached in accounts.db and pasting fresh ones into .env.local has no
     effect. We drop the account first when the stored cookies differ.
"""

import os

import bs4
from twscrape import API
from twscrape import xclid as _xclid

ACCOUNT_NAME = "me"


def patch_xclid() -> None:
    """Point XClIdGen at the homepage instead of the (now-migrated) /tesla page."""
    if getattr(_xclid.XClIdGen, "_home_patched", False):
        return

    async def _create_from_home() -> "_xclid.XClIdGen":
        clt = _xclid._make_client()
        try:
            text = await _xclid.get_tw_page_text("https://x.com", clt)
            soup = bs4.BeautifulSoup(text, "html.parser")
            vk_bytes, anim_key = await _xclid.load_keys(soup, clt)
            return _xclid.XClIdGen(vk_bytes, anim_key)
        finally:
            await clt.aclose()

    _xclid.XClIdGen.create = staticmethod(_create_from_home)
    _xclid.XClIdGen._home_patched = True


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
