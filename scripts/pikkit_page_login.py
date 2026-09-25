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


async def _dump_debug(page, debug_dir: Path, console: list, net: list, note: str) -> None:
    """Screenshot + page state for a Turnstile post-mortem (only with --debug-dir)."""
    debug_dir.mkdir(parents=True, exist_ok=True)
    try:
        await page.screenshot(path=str(debug_dir / "page.png"), full_page=True)
    except Exception as e:  # noqa: BLE001
        console.append(f"screenshot failed: {e}")
    try:
        body = await page.evaluate("document.body ? document.body.innerText : ''")
    except Exception as e:  # noqa: BLE001
        body = f"innerText failed: {e}"
    info = {
        "note": note,
        "url": page.url,
        "title": await page.title(),
        "frames": [f.url for f in page.frames],
        "body_text": body[:2000],
        "console": console[-60:],
        "network": net[-80:],
    }
    (debug_dir / "debug.json").write_text(json.dumps(info, indent=2))
    print(f"[page-login] debug written to {debug_dir}", flush=True)


async def _try_interactive_challenge(page, console: list) -> bool:
    """If Turnstile rendered an interactive widget, click its checkbox once."""
    for frame in page.frames:
        if "challenges.cloudflare.com" not in frame.url:
            continue
        for sel in ('input[type="checkbox"]', "label", "body"):
            try:
                loc = frame.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=5000)
                    console.append(f"clicked turnstile {sel} in {frame.url[:60]}")
                    return True
            except Exception as e:  # noqa: BLE001
                console.append(f"turnstile click {sel} failed: {e}")
    return False


async def run(phone: str, code_file: Path, wait_s: int, debug_dir: Path | None = None) -> str:
    from playwright.async_api import async_playwright

    reqs, resps = [], []
    console: list[str] = []
    net: list[str] = []

    def code_resp():
        return next((r for r in resps if "/login/code" in r.url), None)

    async with async_playwright() as pw:
        extra_args = [a for a in os.getenv("PIKKIT_CHROME_ARGS", "").split() if a]
        ctx_kw = dict(locale="en-US", timezone_id="America/New_York",
                      viewport={"width": 1280, "height": 800})
        profile_dir = os.getenv("PIKKIT_PROFILE_DIR", "")
        if profile_dir:
            # A persistent profile looks like a real install; the ephemeral one is a tell.
            context = await pw.chromium.launch_persistent_context(
                profile_dir, headless=False, channel="chrome", args=extra_args, **ctx_kw
            )
            browser = context
        else:
            browser = await pw.chromium.launch(headless=False, channel="chrome", args=extra_args)
            context = await browser.new_context(**ctx_kw)
        page = context.pages[0] if context.pages else await context.new_page()
        if not os.getenv("PIKKIT_KEEP_WEBDRIVER"):
            await page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
            )
        page.on("request", lambda r: reqs.append(r) if "/login/" in r.url else None)
        page.on("response", lambda r: resps.append(r) if "/login/" in r.url else None)
        if debug_dir is not None:
            page.on("console", lambda m: console.append(f"{m.type}: {m.text[:200]}"))
            page.on("response", lambda r: net.append(f"{r.status} {r.url[:140]}")
                    if any(k in r.url for k in ("cloudflare", "turnstile", "pikkit")) else None)
            page.on("requestfailed", lambda r: net.append(f"FAILED {r.failure} {r.url[:140]}"))

        print("[page-login] loading app.pikkit.com (real Chrome)...", flush=True)
        # "networkidle" is flaky on this SPA -- wait for the DOM, then the field.
        await page.goto("https://app.pikkit.com", wait_until="domcontentloaded", timeout=60000)
        phone_input = page.locator('input[type="tel"]')
        await phone_input.wait_for(timeout=45000)
        await phone_input.press_sequentially(_phone_digits(phone), delay=80)
        await page.get_by_role("button", name="Continue").click()

        phone_resp = None
        clicked = False
        for tick in range(60):
            phone_resp = next((r for r in resps if "/login/phone" in r.url), None)
            if phone_resp:
                break
            if tick == 15 and not clicked:
                # A datacenter IP often gets the interactive widget instead of
                # the invisible check: give its checkbox one click.
                clicked = await _try_interactive_challenge(page, console)
                if clicked:
                    print("[page-login] clicked the interactive Turnstile widget", flush=True)
            await page.wait_for_timeout(1000)
        if not phone_resp:
            if debug_dir is not None:
                await _dump_debug(page, debug_dir, console, net, "no /login/phone response")
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
    ap.add_argument("--token-file", type=Path, default=None,
                    help="write the token here (for the desktop agent) instead of .env.local")
    ap.add_argument("--debug-dir", type=Path, default=None,
                    help="write a screenshot + page state here when Turnstile never resolves")
    args = ap.parse_args()

    session_id = asyncio.run(run(args.phone, args.code_file, args.wait, args.debug_dir))
    if args.token_file is not None:
        args.token_file.parent.mkdir(parents=True, exist_ok=True)
        args.token_file.write_text(session_id)
        try:
            os.chmod(args.token_file, 0o600)
        except OSError:
            pass
        print(f"[page-login] token written to {args.token_file}")
    elif args.no_save:
        print(f"[page-login] PIKKIT_TOKEN={session_id}")
    else:
        _save_token(session_id)
        print("[page-login] now on the VPS: python3 scripts/set_env_local.py PIKKIT_TOKEN=<token>")


if __name__ == "__main__":
    main()
