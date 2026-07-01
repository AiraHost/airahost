"""
Automate Airbnb email-login flow and confirmation-code submit.

Flow:
1) Open https://www.airbnb.ca/login
2) Save rendered HTML snapshot (for selector inspection)
3) Fill email and click Continue
4) Poll email inbox via IMAP for Airbnb confirmation code
5) Fill confirmation code and click Continue

Required env vars:
- AIRAHOST_EMAIL
- AIRAHOST_EMAIL_APP_PASSWORD
- AIRAHOST_IMAP_HOST

Optional env vars:
- AIRAHOST_IMAP_PORT (default: 993)
- AIRAHOST_IMAP_USE_SSL (default: 1)
- AIRAHOST_IMAP_FOLDER (default: INBOX)
- AIRAHOST_EMAIL_FROM_FILTER (default: automated@airbnb.com)
- AIRAHOST_CODE_REGEX (default: (?<!\\d)(\\d{6})(?!\\d))
- AIRAHOST_CODE_TIMEOUT_SECONDS (default: 180)
- AIRAHOST_CODE_POLL_SECONDS (default: 5)
- AIRAHOST_HEADLESS (default: 0)
- AIRAHOST_EMAIL_DEBUG (default: 0)
"""

from __future__ import annotations

import argparse
import email
import imaplib
import os
import random
import re
import time
from dataclasses import dataclass
from email.header import decode_header
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from dotenv import load_dotenv
from email.utils import parsedate_to_datetime
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


LOGIN_URL = "https://www.airbnb.ca/login"
# Auth entry point: hitting a profile page redirects logged-out users to /login;
# if it loads without redirect, the session is already authenticated.
PROFILE_URL = "https://www.airbnb.com/users/profile"
DEFAULT_CDP_URL = os.getenv("CDP_URL", "http://127.0.0.1:9222").strip()
EMAIL_INPUT_SELECTORS = (
    'input[name="email"]',
    'input[type="email"]',
    'input[autocomplete="email"]',
    'input[data-testid*="email"]',
    # Airbnb's combined "phone or email" field: type="text",
    # inputmode="email", id="phone-or-email", autocomplete="tel-national".
    # It matches none of the selectors above, so without these the flow could
    # not find the field and never attempted to log in.
    'input[inputmode="email"]',
    'input#phone-or-email',
)
CODE_INPUT_SELECTORS = (
    'input[autocomplete="one-time-code"]',
    'input[inputmode="numeric"]',
    'input[name*="code"]',
    'input[data-testid*="code"]',
)
WELCOME_BACK_HEADING_PATTERN = re.compile(r"welcome back", re.I)
WELCOME_BACK_LOGIN_BUTTON_PATTERN = re.compile(r"^log\s*in$", re.I)
NOT_YOU_BUTTON_PATTERN = re.compile(r"not you", re.I)
# Email-step submit button only. Anchored to exactly "Continue"/"Next" so social
# SSO buttons ("Continue with Google/Apple/Facebook") and the "Continue with
# email" option are NEVER matched as the submit (clicking those was the bug).
CONTINUE_BUTTON_PATTERN = re.compile(r"^\s*(?:continue|next)\s*$", re.I)
# The option that switches the modal from phone/social into email entry.
EMAIL_OPTION_PATTERN = re.compile(r"(?:continue|log in|sign in)\s+with\s+email", re.I)
# Accounts that normally use social login show a "You logged in this way in the
# past / Log in with Google" suggestion; "Try another way" / "More options"
# reveals the email-code path.
TRY_ANOTHER_WAY_PATTERN = re.compile(
    r"try another way|more (?:log ?in |sign ?in )?options|other (?:log ?in |sign ?in )?options|"
    r"more ways to log in|use a different",
    re.I,
)
CODE_SUBMIT_BUTTON_PATTERN = re.compile(r"continue|verify|submit|next", re.I)
CODE_EXPIRED_PATTERN = re.compile(r"code expired|please request a new one", re.I)
SEND_NEW_CODE_PATTERN = re.compile(
    r"send new code|request a new one|resend code|send another code|get a new code",
    re.I,
)


@dataclass
class ImapConfig:
    email_address: str
    app_password: str
    host: str
    port: int
    use_ssl: bool
    folder: str
    from_filter: str
    code_regex: str
    timeout_seconds: int
    poll_seconds: int


def _env_bool(name: str, default: bool) -> bool:
    raw = str(os.getenv(name, "1" if default else "0")).strip().lower()
    return raw in ("1", "true", "yes", "on")


def _read_imap_config_from_env() -> ImapConfig:
    email_address = os.getenv("AIRAHOST_EMAIL", "").strip()
    app_password = os.getenv("AIRAHOST_EMAIL_APP_PASSWORD", "").strip()
    host = os.getenv("AIRAHOST_IMAP_HOST", "").strip()
    port = int(os.getenv("AIRAHOST_IMAP_PORT", "993"))
    use_ssl = _env_bool("AIRAHOST_IMAP_USE_SSL", True)
    folder = os.getenv("AIRAHOST_IMAP_FOLDER", "INBOX").strip() or "INBOX"
    from_filter = os.getenv("AIRAHOST_EMAIL_FROM_FILTER", "automated@airbnb.com").strip()
    code_regex = os.getenv("AIRAHOST_CODE_REGEX", r"(?<!\d)(\d{6})(?!\d)").strip()
    timeout_seconds = int(os.getenv("AIRAHOST_CODE_TIMEOUT_SECONDS", "180"))
    poll_seconds = int(os.getenv("AIRAHOST_CODE_POLL_SECONDS", "5"))

    missing = []
    if not email_address:
        missing.append("AIRAHOST_EMAIL")
    if not app_password:
        missing.append("AIRAHOST_EMAIL_APP_PASSWORD")
    if not host:
        missing.append("AIRAHOST_IMAP_HOST")
    if missing:
        raise RuntimeError(
            "Missing required env vars for email read: " + ", ".join(missing)
        )

    return ImapConfig(
        email_address=email_address,
        app_password=app_password,
        host=host,
        port=port,
        use_ssl=use_ssl,
        folder=folder,
        from_filter=from_filter,
        code_regex=code_regex,
        timeout_seconds=timeout_seconds,
        poll_seconds=poll_seconds,
    )


def _decode_mime_header(value: str) -> str:
    parts = decode_header(value or "")
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def wait_for_airbnb_code(
    config: ImapConfig,
    request_time: float = 0.0,
    user_agent: str = "",
    after_message_id: int = 0,
    return_message_id: bool = False,
) -> str | tuple[str, int]:
    pattern = re.compile(config.code_regex)
    deadline = time.time() + config.timeout_seconds
    debug_email = _env_bool("AIRAHOST_EMAIL_DEBUG", False)

    while time.time() < deadline:
        if config.use_ssl:
            conn: imaplib.IMAP4 = imaplib.IMAP4_SSL(config.host, config.port)
        else:
            conn = imaplib.IMAP4(config.host, config.port)
        try:
            conn.login(config.email_address, config.app_password)
            conn.select(config.folder)

            status, data = conn.search(None, "ALL")
            if status != "OK" or not data or not data[0]:
                time.sleep(config.poll_seconds)
                continue

            ids = data[0].split()
            if debug_email:
                print(f"[auto_login][email_debug] total messages in folder={len(ids)}")
            # Check latest messages first.
            for msg_id in reversed(ids[-20:]):
                try:
                    numeric_msg_id = int(msg_id.decode(errors="ignore"))
                except ValueError:
                    numeric_msg_id = 0
                if after_message_id > 0 and numeric_msg_id <= after_message_id:
                    continue
                fetch_status, fetched = conn.fetch(msg_id, "(RFC822)")
                if fetch_status != "OK" or not fetched or not fetched[0]:
                    continue
                raw = fetched[0][1]
                if not raw:
                    continue
                msg = email.message_from_bytes(raw)
                from_header = _decode_mime_header(str(msg.get("From", "")))
                subject = _decode_mime_header(str(msg.get("Subject", "")))
                date_header = _decode_mime_header(str(msg.get("Date", "")))
                
                try:
                    msg_time = parsedate_to_datetime(date_header).timestamp()
                    if request_time > 0 and msg_time < request_time - 60:
                        if debug_email:
                            print(f"[auto_login][email_debug] ignoring old email from {date_header}")
                        continue
                except Exception:
                    pass

                if debug_email:
                    print(
                        "[auto_login][email_debug] "
                        f"id={msg_id.decode(errors='ignore')} "
                        f"from={from_header} subject={subject} date={date_header}"
                    )
                if config.from_filter and config.from_filter.lower() not in from_header.lower():
                    continue
                # Airbnb always puts the login code in the SUBJECT, regardless of
                # locale ("Your confirmation code is 123456" / "你的確認碼是
                # 123456"). Match the subject only — never the body. Airbnb also
                # sends a separate "Account activity: New login from <browser>
                # using <os>" notification that (a) carries a stray 6-digit number
                # in its body and (b) often arrives just after the code email;
                # scanning the body newest-first was picking that number instead
                # of the real code. Its subject has no 6-digit run, so a
                # subject-only match skips it.
                m = pattern.search(subject)
                if not m:
                    if debug_email:
                        print(
                            "[auto_login][email_debug] no code in subject; skipping "
                            f"id={msg_id.decode(errors='ignore')} subject={subject}"
                        )
                    continue
                code = m.group(1)
                if return_message_id:
                    return code, numeric_msg_id
                return code
        finally:
            try:
                conn.close()
            except Exception:
                pass
            conn.logout()

        time.sleep(config.poll_seconds)

    raise TimeoutError(
        f"No Airbnb confirmation code found within {config.timeout_seconds} seconds."
    )


def dump_rendered_html(page: Page, out_dir: Path, stem: str) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{stem}.html"
    html = ""
    last_err: Optional[Exception] = None
    for _ in range(12):
        try:
            page.wait_for_load_state("domcontentloaded", timeout=3000)
        except PlaywrightTimeoutError:
            pass
        try:
            html = page.content()
            if html:
                break
        except PlaywrightError as exc:
            last_err = exc
            time.sleep(0.4)
    if not html and last_err is not None:
        raise last_err
    path.write_text(html, encoding="utf-8")
    return path


def _locator_is_visible(locator: Locator) -> bool:
    try:
        return locator.count() > 0 and locator.first.is_visible()
    except PlaywrightError:
        return False


def _find_first_visible_locator(
    page: Page,
    selectors: tuple[str, ...],
) -> Optional[tuple[str, Locator]]:
    for sel in selectors:
        matches = page.locator(sel)
        try:
            count = matches.count()
        except PlaywrightError:
            continue
        for idx in range(count):
            candidate = matches.nth(idx)
            try:
                if candidate.is_visible():
                    return sel, candidate
            except PlaywrightError:
                continue
    return None


def _find_first_visible_role_locator(
    page: Page,
    role: str,
    name_pattern: re.Pattern[str],
) -> Optional[Locator]:
    matches = page.get_by_role(role, name=name_pattern)
    try:
        count = matches.count()
    except PlaywrightError:
        return None
    for idx in range(count):
        candidate = matches.nth(idx)
        if _locator_is_visible(candidate):
            return candidate
    return None


def _is_login_url(page: Page) -> bool:
    return "login" in str(page.url or "").lower()


def _wait_for_login_url_to_change(page: Page, timeout_ms: int = 2500) -> bool:
    deadline = time.monotonic() + (max(timeout_ms, 0) / 1000.0)
    while time.monotonic() < deadline:
        if not _is_login_url(page):
            return True
        page.wait_for_timeout(100)
    return not _is_login_url(page)


def _is_welcome_back_login_visible(page: Page) -> bool:
    login_button = page.get_by_role(
        "button",
        name=WELCOME_BACK_LOGIN_BUTTON_PATTERN,
    ).first
    not_you_button = page.get_by_role("button", name=NOT_YOU_BUTTON_PATTERN).first
    not_you_text = page.get_by_text(NOT_YOU_BUTTON_PATTERN).first
    welcome_back_heading = page.get_by_text(WELCOME_BACK_HEADING_PATTERN).first
    return _locator_is_visible(login_button) and (
        _locator_is_visible(not_you_button)
        or _locator_is_visible(not_you_text)
        or _locator_is_visible(welcome_back_heading)
    )


def _is_code_expired_visible(page: Page) -> bool:
    expired_text = page.get_by_text(CODE_EXPIRED_PATTERN).first
    if _locator_is_visible(expired_text):
        return True
    try:
        body_text = page.locator("body").inner_text(timeout=1000)
    except PlaywrightError:
        return False
    return bool(CODE_EXPIRED_PATTERN.search(body_text or ""))


def _click_send_new_code(page: Page) -> float:
    request_time = time.time()
    for role in ("button", "link"):
        control = _find_first_visible_role_locator(page, role, SEND_NEW_CODE_PATTERN)
        if control is not None:
            control.click(timeout=2000)
            return request_time

    control = page.get_by_text(SEND_NEW_CODE_PATTERN).first
    if _locator_is_visible(control):
        control.click(timeout=2000)
        return request_time

    raise RuntimeError(
        "Confirmation code expired, but no visible 'Send new code' or resend control was found."
    )


def _wait_for_post_code_submit_state(page: Page, timeout_ms: int = 5000) -> str:
    deadline = time.monotonic() + (max(timeout_ms, 0) / 1000.0)
    while time.monotonic() < deadline:
        if not _is_login_url(page):
            return "advanced"
        if _is_code_expired_visible(page):
            return "expired"
        page.wait_for_timeout(100)
    if not _is_login_url(page):
        return "advanced"
    if _is_code_expired_visible(page):
        return "expired"
    return "pending"


def detect_login_page_variant(page: Page, timeout_ms: int = 12000) -> str:
    deadline = time.monotonic() + (max(timeout_ms, 0) / 1000.0)
    while True:
        current_url = str(page.url or "").lower()
        if "login" not in current_url:
            return "already_logged_in"
        if _is_welcome_back_login_visible(page):
            return "welcome_back"
        if _find_first_visible_locator(page, EMAIL_INPUT_SELECTORS) is not None:
            return "email_entry"
        # Default view may show phone + social buttons with no email field yet;
        # a "Continue with email" option still means we can do email login.
        if _find_first_visible_role_locator(page, "button", EMAIL_OPTION_PATTERN) is not None:
            return "email_entry"
        if time.monotonic() >= deadline:
            return "unknown"
        page.wait_for_timeout(250)


def _verify_logged_in(page: Page, timeout_ms: int = 20000) -> bool:
    """
    Confirm the session is authenticated by visiting a host-only page. Airbnb
    redirects logged-out users to /login, so staying off /login means logged in.
    The origin is taken from the current page so this works against the real
    site and a test mock server alike.
    """
    parsed = urlparse(str(page.url or ""))
    origin = (
        f"{parsed.scheme}://{parsed.netloc}"
        if parsed.scheme and parsed.netloc
        else "https://www.airbnb.com"
    )
    try:
        page.goto(
            f"{origin}/hosting",
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
    except PlaywrightError:
        return False
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except PlaywrightTimeoutError:
        pass
    return "/login" not in str(page.url or "").lower()


def click_welcome_back_login(page: Page) -> None:
    def _human_pause(min_s: float = 0.35, max_s: float = 1.4) -> None:
        time.sleep(random.uniform(min_s, max_s))

    btn = page.get_by_role(
        "button",
        name=WELCOME_BACK_LOGIN_BUTTON_PATTERN,
    ).first
    if not _locator_is_visible(btn):
        raise RuntimeError(
            "Welcome-back login state detected, but the primary 'Log in' button is not visible."
        )
    print("[auto_login] welcome-back flow detected. Clicking Log in.")
    _human_pause()
    btn.click()


def fill_email_and_continue(page: Page, email_value: str) -> None:
    def _human_pause(min_s: float = 0.25, max_s: float = 1.1) -> None:
        time.sleep(random.uniform(min_s, max_s))

    visible_email_input = _find_first_visible_locator(page, EMAIL_INPUT_SELECTORS)
    if visible_email_input is None:
        # The default login view often shows a phone field plus social SSO
        # buttons and no email input. Choose the "Continue with email" option to
        # switch to email entry — never click a social ("…with Google/Apple/
        # Facebook") button.
        email_option = _find_first_visible_role_locator(page, "button", EMAIL_OPTION_PATTERN)
        if email_option is not None:
            print("[auto_login] selecting 'Continue with email' option")
            _human_pause()
            email_option.click()
            for sel in EMAIL_INPUT_SELECTORS:
                try:
                    page.locator(sel).first.wait_for(state="visible", timeout=6000)
                    break
                except PlaywrightTimeoutError:
                    continue
            visible_email_input = _find_first_visible_locator(page, EMAIL_INPUT_SELECTORS)

    if visible_email_input is not None:
        sel, loc = visible_email_input
        print(f"[auto_login] filling email with selector: {sel}")
        _human_pause()
        loc.fill(email_value)
    else:
        # Label fallback.
        print("[auto_login] filling email with label fallback")
        _human_pause()
        loc = page.get_by_label("Email", exact=False).first
        loc.fill(email_value)

    # Submit the email. CONTINUE_BUTTON_PATTERN is anchored to "Continue"/"Next",
    # so social SSO buttons are never selected here.
    print("[auto_login] clicking Continue (email submit)")
    _human_pause(0.35, 1.4)
    btn = _find_first_visible_role_locator(page, "button", CONTINUE_BUTTON_PATTERN)
    if btn is None:
        # Fall back to submitting via Enter on the email field.
        try:
            loc.press("Enter", timeout=500)
            return
        except PlaywrightError:
            pass
        raise RuntimeError("Could not find a visible Continue/Next submit button on the email step.")
    btn.click()


def fill_code_and_continue(page: Page, code_value: str) -> None:
    visible_code_input = _find_first_visible_locator(page, CODE_INPUT_SELECTORS)
    if visible_code_input is not None:
        sel, loc = visible_code_input
        print(f"[auto_login] filling code with selector: {sel}")
        loc.fill(code_value)
    else:
        print("[auto_login] filling code with label fallback")
        loc = page.get_by_label(re.compile(r"code|verification", re.I)).first
        loc.fill(code_value)

    print("[auto_login] submitting code")
    # Click an explicit submit button if present; otherwise Airbnb's one-time-code
    # input auto-submits once all digits are entered — nudge it with Enter. Do
    # NOT raise here: the caller (submit_confirmation_code_with_retry) classifies
    # the outcome (advanced / expired / pending) and retries as needed. The
    # expired-code page legitimately stays on /login.
    btn = _find_first_visible_role_locator(page, "button", CODE_SUBMIT_BUTTON_PATTERN)
    if btn is not None:
        try:
            btn.click(timeout=2000)
        except PlaywrightError:
            try:
                btn.click(timeout=2000, force=True)
            except PlaywrightError:
                pass
    else:
        try:
            loc.press("Enter", timeout=500)
        except PlaywrightError:
            pass
    # Brief settle so an immediate redirect is observed; the caller waits longer
    # and classifies the result.
    _wait_for_login_url_to_change(page, timeout_ms=1500)


def submit_confirmation_code_with_retry(
    page: Page,
    cfg: ImapConfig,
    request_time: float,
    user_agent: str,
    max_expired_retries: int = 1,
) -> None:
    current_request_time = request_time
    latest_message_id = 0

    for attempt in range(max_expired_retries + 1):
        print("[auto_login] waiting for confirmation code email")
        wait_result = wait_for_airbnb_code(
            cfg,
            request_time=current_request_time,
            user_agent=user_agent,
            after_message_id=latest_message_id,
            return_message_id=True,
        )
        code, latest_message_id = wait_result
        print("[auto_login] confirmation code received")
        fill_code_and_continue(page, code)

        # Allow time for Airbnb to verify the code server-side and redirect.
        post_submit_state = _wait_for_post_code_submit_state(page, timeout_ms=15000)
        if post_submit_state != "expired":
            return
        if attempt >= max_expired_retries:
            print("[auto_login] confirmation code expired after the final retry attempt.")
            return

        print("[auto_login] confirmation code expired. Requesting a new code.")
        current_request_time = _click_send_new_code(page)
        for sel in CODE_INPUT_SELECTORS:
            try:
                page.locator(sel).first.wait_for(state="visible", timeout=5000)
                break
            except PlaywrightTimeoutError:
                continue


def _advance_to_code_input(page: Page, email_value: str, max_steps: int = 6) -> bool:
    """
    After the initial login action, walk through any interstitials until the OTP
    code input appears. Handles, in priority order per step:
      - code input visible                      -> done
      - email input visible                     -> fill email + Continue
      - "Continue with email" option            -> click it
      - "Try another way" / "More options"       -> click it (social-SSO accounts
        show a "Log in with Google / you logged in this way in the past" page)
      - welcome-back "Log in" button            -> click it

    Never clicks a social SSO button. Returns True once a code input is visible.
    """
    def _code_input_visible() -> bool:
        return _find_first_visible_locator(page, CODE_INPUT_SELECTORS) is not None

    def _wait_code_input(timeout_ms: int) -> bool:
        for sel in CODE_INPUT_SELECTORS:
            try:
                page.locator(sel).first.wait_for(state="visible", timeout=timeout_ms)
                return True
            except PlaywrightTimeoutError:
                continue
        return False

    for _ in range(max_steps):
        if _code_input_visible():
            return True

        # Email entry form -> fill and submit, then wait for the code input to
        # appear (codes can be short-lived, so submit promptly — no blind sleep).
        if _find_first_visible_locator(page, EMAIL_INPUT_SELECTORS) is not None:
            fill_email_and_continue(page, email_value)
            if _wait_code_input(6000):
                return True
            continue

        # Explicit "Continue with email" option.
        email_option = _find_first_visible_role_locator(page, "button", EMAIL_OPTION_PATTERN)
        if email_option is not None:
            print("[auto_login] selecting 'Continue with email'")
            email_option.click()
            page.wait_for_timeout(600)
            continue

        # Social-SSO suggestion -> reveal alternate methods.
        alt = _find_first_visible_role_locator(page, "button", TRY_ANOTHER_WAY_PATTERN)
        if alt is None:
            alt = _find_first_visible_role_locator(page, "link", TRY_ANOTHER_WAY_PATTERN)
        if alt is None:
            alt_text = page.get_by_text(TRY_ANOTHER_WAY_PATTERN).first
            if _locator_is_visible(alt_text):
                alt = alt_text
        if alt is not None:
            print("[auto_login] SSO suggestion detected; clicking 'Try another way'")
            alt.click()
            page.wait_for_timeout(1200)
            continue

        # Remembered welcome-back "Log in".
        if _is_welcome_back_login_visible(page):
            print("[auto_login] welcome-back 'Log in' detected")
            click_welcome_back_login(page)
            # Wait for the welcome-back view to transition before re-evaluating,
            # so we don't click "Log in" again mid-navigation.
            _deadline = time.monotonic() + 5.0
            while time.monotonic() < _deadline and _is_welcome_back_login_visible(page):
                page.wait_for_timeout(150)
            continue

        # Nothing actionable yet — let the page settle and retry.
        page.wait_for_timeout(900)

    return _code_input_visible()


def run_login_flow(out_dir: Path, dump_only: bool) -> None:
    email_value = os.getenv("AIRAHOST_EMAIL", "").strip()
    if not email_value:
        raise RuntimeError("Missing AIRAHOST_EMAIL")

    headless = _env_bool("AIRAHOST_HEADLESS", False)
    cdp_url = os.getenv("CDP_URL", DEFAULT_CDP_URL).strip() or DEFAULT_CDP_URL

    with sync_playwright() as pw:
        browser = None
        created_new_browser = False
        try:
            browser = pw.chromium.connect_over_cdp(cdp_url, timeout=15000)
            print(f"[auto_login] connected to existing browser via CDP: {cdp_url}")
        except Exception:
            browser = pw.chromium.launch(headless=headless)
            created_new_browser = True
            print("[auto_login] no attachable existing browser; launched a new browser")

        if browser.contexts:
            context = browser.contexts[0]
        else:
            context = browser.new_context()
        page = context.new_page()

        # New login method: navigate straight to the profile page. A logged-out
        # session is redirected to /login; if the profile loads without a redirect
        # we are already logged in and no sign-in is needed.
        page.goto(PROFILE_URL, wait_until="domcontentloaded", timeout=45000)
        try:
            page.wait_for_load_state("networkidle", timeout=8000)
        except PlaywrightTimeoutError:
            pass
        # Allow a late client-side redirect to /login to settle.
        _redirect_deadline = time.monotonic() + 2.5
        while time.monotonic() < _redirect_deadline and not _is_login_url(page):
            page.wait_for_timeout(150)

        landed_html = dump_rendered_html(page, out_dir, "01_login_page")
        print(f"[auto_login] landed on {page.url}; saved rendered html: {landed_html}")

        if not _is_login_url(page):
            print(
                "[auto_login] /users/profile loaded without redirect — "
                "already logged in. Login status verified."
            )
            if created_new_browser:
                browser.close()
            return

        if dump_only:
            if created_new_browser:
                browser.close()
            return

        user_agent = page.evaluate("navigator.userAgent")

        login_variant = detect_login_page_variant(page)
        print(f"[auto_login] detected login page variant: {login_variant}")
        if login_variant == "unknown":
            # Slow hydration can leave the page in an indeterminate state — reload
            # once and re-detect before giving up.
            print("[auto_login] login variant unknown; reloading once and re-detecting")
            try:
                page.reload(wait_until="domcontentloaded", timeout=30000)
                page.wait_for_load_state("networkidle", timeout=4000)
            except PlaywrightError:
                pass
            login_variant = detect_login_page_variant(page)
            print(f"[auto_login] re-detected login page variant: {login_variant}")

        if login_variant == "already_logged_in":
            print(f"[auto_login] URL is now {page.url} - assuming already logged in.")
            if created_new_browser:
                browser.close()
            return

        request_time = time.time()
        # Drive through the login states until the OTP code input appears —
        # welcome-back 'Log in', the social-SSO 'Try another way' interstitial,
        # 'Continue with email', and email entry are all handled in one loop.
        print(f"[auto_login] advancing to the code step (variant={login_variant})")
        code_ready = _advance_to_code_input(page, email_value)
        code_html = dump_rendered_html(page, out_dir, "02_code_page")
        print(f"[auto_login] saved rendered html: {code_html}")
        if not code_ready:
            raise RuntimeError(
                "Could not reach the email verification code step "
                f"(final url: {page.url}). The login page state was not recognized."
            )

        cfg = _read_imap_config_from_env()
        submit_confirmation_code_with_retry(
            page,
            cfg,
            request_time=request_time,
            user_agent=user_agent,
        )
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
        except PlaywrightTimeoutError:
            pass
        final_html = dump_rendered_html(page, out_dir, "03_after_submit")
        print(f"[auto_login] saved rendered html: {final_html}")
        print(f"[auto_login] final url: {page.url}")

        # Verify the session is actually authenticated before reporting success,
        # so callers can retry when the flow finished without logging in.
        pre_verify_url = str(page.url)
        logged_in = _verify_logged_in(page)
        print(f"[auto_login] post-login verification: logged_in={logged_in}")
        if created_new_browser:
            browser.close()
        if not logged_in:
            raise RuntimeError(
                "Auto-login flow finished but the session is not logged in "
                f"(url before verify: {pre_verify_url})."
            )


def main() -> None:
    load_dotenv()

    parser = argparse.ArgumentParser(
        description="Airbnb auto-login with email code retrieval."
    )
    parser.add_argument(
        "--out-dir",
        default="worker/outputs/airbnb_login_debug",
        help="Directory for rendered HTML snapshots.",
    )
    parser.add_argument(
        "--dump-only",
        action="store_true",
        help="Only open login page and dump rendered HTML.",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    run_login_flow(out_dir=out_dir, dump_only=args.dump_only)


if __name__ == "__main__":
    main()
