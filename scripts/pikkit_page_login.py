"""
scripts/pikkit_page_login.py -- page-driven Pikkit login (fallback for pikkit_auth.py).

`pikkit_auth.py` aborts the login form's request and replays the API calls
itself, so it breaks whenever Pikkit changes a payload (2026-09-24: the phone
field switched to national digits and the stock flow got INVALID_PHONE_NUMBER).
This script lets the PAGE do the whole login in real Chrome and only watches:

  1. types the phone, clicks Continue, lets the page's own /login/phone request
     through, and prints the exact body it sent (the diagnosis for the next
     drift) plus the response;
  2. waits for the SMS code -- either written to the code file by the operator
     (`--code-file`, default scripts/output/pikkit_code.txt) or typed straight
     into the Chrome window by the operator (the /login/code response is
     harvested either way);
  3. reads session_id from the page's /login/code response, validates it and
     saves PIKKIT_TOKEN to the local .env.local via pikkit_auth._save_token.

The session lives only in memory/cookies (Local Storage stays empty), so the
Chrome window must stay open until the token is printed.  auth_id is a JWT
with a 300 s TTL: run this only when the operator is at their phone.
Run locally (real Chrome + display; Turnstile rejects headless).  Exercised
live 2026-09-24.

Usage:
    python scripts/pikkit_page_login.py                 # phone from PIKKIT_PHONE or the default
    python scripts/pikkit_page_login.py --no-save       # print the token instead of writing .env.local
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from pikkit_auth import _phone_digits, _save_token, validate_token  # noqa: E402

DEFAULT_CODE_FILE = ROOT / "scripts" / "output" / "pikkit_code.txt"


def _redact(obj):
    if isinstance(obj, dict):
        return {k: _redact(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    if isinstance(obj, str) and len(obj) > 40:
        return obj[:12] + f"...({len(obj)} chars)"
    return obj


async def _find_empty_input(page):
    """First visible, empty input on the page (the code field on the code screen)."""
    for sel in ('input[autocomplete="one-time-code"]', 'input[inputmode="numeric"]',
                'input[type="tel"]', 'input[type="number"]', 'input[type="text"]', 'input'):
        loc = page.locator(sel)
        for i in range(await loc.count()):
            el = loc.nth(i)
            try:
                if await el.is_visible() and (await el.input_value()) == "":
                    return el
            except Exception:  # noqa: BLE001 -- detached/odd elements: keep looking
                continue
    return None


async def run(phone: str, code_file: Path, wait_s: int) -> str:
    from playwright.async_api import async_playwright

    reqs, resps = [], []

    def code_resp():
        return next((r for r in resps if "/login/code" in r.url), None)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, channel="chrome")
        page = await (await browser.new_context()).new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        page.on("request", lambda r: reqs.append(r) if "/login/" in r.url else None)
        page.on("response", lambda r: resps.append(r) if "/login/" in r.url else None)

        print("[page-login] loading app.pikkit.com (real Chrome)...", flush=True)
        # "networkidle" is flaky on this SPA -- wait for the DOM, then the field.
        await page.goto("https://app.pikkit.com", wait_until="domcontentloaded", timeout=60000)
        phone_input = page.locator('input[type="tel"]')
        await phone_input.wait_for(timeout=45000)
        await phone_input.press_sequentially(_phone_digits(phone), delay=80)
        await page.get_by_role("button", name="Continue").click()

        phone_resp = None
        for _ in range(60):
            phone_resp = next((r for r in resps if "/login/phone" in r.url), None)
            if phone_resp:
                break
            await page.wait_for_timeout(1000)
        if not phone_resp:
            raise RuntimeError("no /login/phone response within 60s (Turnstile?)")
        sent = [r for r in reqs if "/login/phone" in r.url]
        print(f"[page-login] page sent: {json.dumps(_redact(sent[-1].post_data_json))}", flush=True)
        body = await phone_resp.json()
        print(f"[page-login] /login/phone -> {phone_resp.status} "
              f"{json.dumps(_redact(body))[:200]}", flush=True)
        if phone_resp.status != 200 or not body.get("auth_id"):
            raise RuntimeError(f"phone step failed: {body}")
        code_file.parent.mkdir(parents=True, exist_ok=True)
        code_file.unlink(missing_ok=True)
        print(f"[page-login] SMS sent {time.strftime('%H:%M:%S')} (code valid ~5 min). Either type "
              f"the code into the Chrome window, or write it to {code_file}", flush=True)

        code, deadline = None, time.time() + wait_s
        while time.time() < deadline and not code_resp():
            if code_file.exists():
                code = re.sub(r"\D", "", code_file.read_text())
                if code:
                    break
            await page.wait_for_timeout(1500)

        if code and not code_resp():
            target = await _find_empty_input(page)
            if target is None:
                raise RuntimeError("no empty visible input to type the code into")
            await target.click()
            await target.press_sequentially(code, delay=90)
            for attempt in range(3):
                for _ in range(6):
                    if code_resp():
                        break
                    await page.wait_for_timeout(1000)
                if code_resp():
                    break
                if attempt == 0:
                    await page.keyboard.press("Enter")
                else:
                    btn = page.get_by_role("button", name=re.compile(
                        r"continue|verify|submit|log ?in|sign ?in|confirm|next", re.I))
                    if await btn.count():
                        await btn.first.click()

        cr = code_resp()
        if not cr:
            raise RuntimeError("no /login/code response (code never submitted, or timed out)")
        cbody = await cr.json()
        print(f"[page-login] /login/code -> {cr.status} success={cbody.get('success')}", flush=True)
        session_id = (cbody.get("data") or {}).get("session_id") or cbody.get("session_id")
        if not session_id:
            raise RuntimeError(f"no session_id in /login/code response: {_redact(cbody)}")
        await browser.close()

    ok = await validate_token(session_id)
    print(f"[page-login] token {'validated' if ok else 'WARNING: failed validation'} "
          f"({len(session_id)} chars)", flush=True)
    return session_id


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phone", default=os.getenv("PIKKIT_PHONE", "+19545361686"),
                    help="registered number, +1XXXXXXXXXX (the national digits are what gets sent)")
    ap.add_argument("--code-file", type=Path, default=DEFAULT_CODE_FILE,
                    help="file to drop the SMS code into (alternative: type it in the window)")
    ap.add_argument("--wait", type=int, default=420, help="seconds to wait for the code")
    ap.add_argument("--no-save", action="store_true", help="print the token; don't write .env.local")
    args = ap.parse_args()

    session_id = asyncio.run(run(args.phone, args.code_file, args.wait))
    if args.no_save:
        print(f"[page-login] PIKKIT_TOKEN={session_id}")
    else:
        _save_token(session_id)
        print("[page-login] now on the VPS: python3 scripts/set_env_local.py PIKKIT_TOKEN=<token>")


if __name__ == "__main__":
    main()
