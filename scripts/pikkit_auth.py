"""
scripts/pikkit_auth.py -- Pikkit token generation.

Solves Turnstile via Playwright (real Chrome, headed mode), then makes
direct API calls for login.  Writes PIKKIT_TOKEN to .env.local.

Turnstile rejects headless/bundled Chromium — must use real Chrome
(`channel="chrome"`) in headed mode.  This means the script must run
on a machine with Chrome + a display (not the VPS).

The session_id is NOT IP-bound: tokens generated locally work from
the VPS (tested 2026-07-24).

Prerequisites:
    pip install playwright
    # Chrome must be installed (Playwright uses it via channel="chrome")

Env vars (in .env.local, for full Twilio automation):
    PIKKIT_PHONE         -- Phone registered with Pikkit (+1XXXXXXXXXX)
    TWILIO_ACCOUNT_SID   -- (optional) for automated SMS code retrieval
    TWILIO_AUTH_TOKEN     -- (optional) for automated SMS code retrieval

Usage:
    # Two-step manual flow (run locally, you read the SMS yourself):
    python scripts/pikkit_auth.py --send-sms --phone +19545361686
    python scripts/pikkit_auth.py --submit-code 67348185 --auth-id <from step 1>

    # Full automated flow (Twilio receives the SMS):
    python scripts/pikkit_auth.py --phone +19545361686

    # Validate existing token:
    python scripts/pikkit_auth.py --validate
"""

import argparse
import asyncio
import email.utils
import os
import re
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.local", override=True)

PIKKIT_BASE = "https://prod-website.pikkit.app"
ENV_LOCAL = ROOT / ".env.local"


# -- Turnstile solving --------------------------------------------------------


async def _solve_turnstile(phone_digits: str) -> str:
    """Solve Turnstile by opening Pikkit login in real Chrome.

    Fills the phone input, clicks Continue, intercepts the outgoing
    /login/phone request to capture the Turnstile token, then aborts
    the request so the token remains unconsumed.
    """
    from playwright.async_api import async_playwright

    captured = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False, channel="chrome")
        ctx = await browser.new_context()
        page = await ctx.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        async def intercept(route):
            body = route.request.post_data_json
            if body and "turnstileToken" in body:
                captured["token"] = body["turnstileToken"]
            await route.abort()

        await page.route("**/login/phone", intercept)

        print("[pikkit-auth] Loading app.pikkit.com (real Chrome)...")
        await page.goto("https://app.pikkit.com", wait_until="networkidle")

        phone_input = page.locator('input[type="tel"]')
        await phone_input.wait_for(timeout=10000)
        await phone_input.press_sequentially(phone_digits, delay=80)

        await page.get_by_role("button", name="Continue").click()
        print("[pikkit-auth] Clicked Continue, solving Turnstile...")

        for _ in range(45):
            if "token" in captured:
                break
            await page.wait_for_timeout(2000)

        await browser.close()

    if "token" not in captured:
        raise RuntimeError("Turnstile failed — ensure Chrome is installed and display is available")

    print(f"[pikkit-auth] Turnstile solved ({len(captured['token'])} chars)")
    return captured["token"]


# -- Twilio SMS polling -------------------------------------------------------


async def _poll_sms_code(
    sid: str,
    auth: str,
    phone: str,
    after_ts: float,
    timeout: int = 120,
) -> str:
    """Poll Twilio for a verification code sent to *phone* after *after_ts*."""
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    deadline = time.time() + timeout

    async with httpx.AsyncClient(timeout=15) as client:
        while time.time() < deadline:
            resp = await client.get(
                url,
                params={"To": phone, "PageSize": "5"},
                auth=(sid, auth),
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Twilio API {resp.status_code}: {resp.text[:200]}")

            for msg in resp.json().get("messages", []):
                sent = msg.get("date_sent") or msg.get("date_created", "")
                if sent:
                    parsed = email.utils.parsedate_tz(sent)
                    if parsed and email.utils.mktime_tz(parsed) < after_ts:
                        continue
                m = re.search(r"\b(\d{6,8})\b", msg.get("body", ""))
                if m:
                    return m.group(1)

            await asyncio.sleep(3)

    raise TimeoutError(f"No verification SMS received within {timeout}s")


# -- Pikkit API ---------------------------------------------------------------


async def _request_code(phone: str, turnstile_token: str) -> str:
    """POST /login/phone -> auth_id.  SMS is sent to *phone*."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{PIKKIT_BASE}/login/phone",
            json={"phoneNumber": phone, "turnstileToken": turnstile_token},
            headers={"Origin": "https://app.pikkit.com", "Referer": "https://app.pikkit.com/"},
        )
    data = resp.json()
    if data.get("new_user"):
        raise RuntimeError(
            "Pikkit says this is a new number -- register at app.pikkit.com first"
        )
    auth_id = data.get("auth_id")
    if not auth_id:
        raise RuntimeError(f"Unexpected /login/phone response: {data}")
    return auth_id


async def _submit_code(code: str, auth_id: str, turnstile_token: str) -> str:
    """POST /login/code -> session_id."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{PIKKIT_BASE}/login/code",
            json={"code": code, "auth_id": auth_id, "turnstileToken": turnstile_token},
            headers={"Origin": "https://app.pikkit.com", "Referer": "https://app.pikkit.com/"},
        )
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Login failed: {data}")
    session_id = data.get("data", {}).get("session_id")
    if not session_id:
        raise RuntimeError(f"No session_id in response: {data}")
    return session_id


async def validate_token(token: str) -> bool:
    """GET /login/validate -- True if the token is still valid."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{PIKKIT_BASE}/login/validate",
            headers={"Authorization": token},
        )
    return resp.status_code == 200


# -- Token persistence --------------------------------------------------------


def _save_token(token: str) -> None:
    """Write PIKKIT_TOKEN to .env.local, preserving other values."""
    lines = ENV_LOCAL.read_text().splitlines() if ENV_LOCAL.exists() else []
    lines = [l for l in lines if not l.startswith("PIKKIT_TOKEN=")]
    lines.append(f"PIKKIT_TOKEN={token}")
    ENV_LOCAL.write_text("\n".join(lines) + "\n")
    print(f"[pikkit-auth] Saved PIKKIT_TOKEN to {ENV_LOCAL}")


def _phone_digits(phone: str) -> str:
    """Strip +1 prefix for the Pikkit login form (it shows +1 already)."""
    return phone.lstrip("+").lstrip("1") if phone.startswith("+1") else phone


# -- CLI -----------------------------------------------------------------------


async def main() -> None:
    parser = argparse.ArgumentParser(description="Pikkit auth automation")
    parser.add_argument(
        "--validate", action="store_true",
        help="Just validate the existing PIKKIT_TOKEN",
    )
    parser.add_argument("--phone", help="Phone number (+1XXXXXXXXXX)")
    parser.add_argument(
        "--send-sms", action="store_true",
        help="Step 1: solve Turnstile and send SMS, print auth_id",
    )
    parser.add_argument(
        "--submit-code", metavar="CODE",
        help="Step 2: submit CODE with --auth-id to complete login",
    )
    parser.add_argument("--auth-id", help="auth_id from --send-sms step")
    args = parser.parse_args()

    if args.validate:
        token = os.getenv("PIKKIT_TOKEN", "")
        if not token:
            print("[pikkit-auth] PIKKIT_TOKEN not set")
            raise SystemExit(1)
        ok = await validate_token(token)
        print(f"[pikkit-auth] Token {'valid' if ok else 'EXPIRED / INVALID'}")
        if not ok:
            raise SystemExit(1)
        return

    phone = args.phone or os.getenv("PIKKIT_PHONE", "")

    # -- Two-step manual flow --------------------------------------------------
    if args.send_sms:
        if not phone:
            print("[pikkit-auth] --phone required")
            raise SystemExit(1)
        ts_token = await _solve_turnstile(_phone_digits(phone))
        auth_id = await _request_code(phone, ts_token)
        print(f"[pikkit-auth] SMS sent!  auth_id={auth_id}")
        print(f"[pikkit-auth] Next: --submit-code <CODE> --auth-id {auth_id}")
        return

    if args.submit_code:
        if not args.auth_id:
            print("[pikkit-auth] --auth-id required with --submit-code")
            raise SystemExit(1)
        if not phone:
            print("[pikkit-auth] --phone required")
            raise SystemExit(1)
        ts_token = await _solve_turnstile(_phone_digits(phone))
        session_id = await _submit_code(args.submit_code, args.auth_id, ts_token)
        print("[pikkit-auth] Login successful!")

        ok = await validate_token(session_id)
        print(f"[pikkit-auth] Token {'validated' if ok else 'WARNING: failed validation'}")

        _save_token(session_id)
        return

    # -- Full automated flow (Twilio) ------------------------------------------
    if not phone:
        print("[pikkit-auth] --phone required (or set PIKKIT_PHONE)")
        raise SystemExit(1)
    twilio_sid = os.getenv("TWILIO_ACCOUNT_SID", "")
    twilio_auth = os.getenv("TWILIO_AUTH_TOKEN", "")
    if not twilio_sid or not twilio_auth:
        print("[pikkit-auth] TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN not set")
        print("[pikkit-auth] Use --send-sms / --submit-code for manual two-step flow")
        raise SystemExit(1)

    digits = _phone_digits(phone)

    # Step 1: Turnstile + send SMS
    ts1 = await _solve_turnstile(digits)
    sms_after = time.time() - 5
    auth_id = await _request_code(phone, ts1)
    print(f"[pikkit-auth] SMS requested (auth_id={auth_id[:20]}...)")

    # Step 2: poll Twilio
    print("[pikkit-auth] Polling Twilio for code...")
    code = await _poll_sms_code(twilio_sid, twilio_auth, phone, sms_after)
    print(f"[pikkit-auth] Got code: {code}")

    # Step 3: Turnstile + submit code
    ts2 = await _solve_turnstile(digits)
    session_id = await _submit_code(code, auth_id, ts2)
    print("[pikkit-auth] Login successful!")

    ok = await validate_token(session_id)
    print(f"[pikkit-auth] Token {'validated' if ok else 'WARNING: failed validation'}")

    _save_token(session_id)


if __name__ == "__main__":
    asyncio.run(main())
