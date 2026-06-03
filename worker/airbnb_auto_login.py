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
from email.message import Message
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from email.utils import parsedate_to_datetime
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


LOGIN_URL = "https://www.airbnb.ca/login"
DEFAULT_CDP_URL = os.getenv("CDP_URL", "http://127.0.0.1:9222").strip()
EMAIL_INPUT_SELECTORS = (
    'input[name="email"]',
    'input[type="email"]',
    'input[autocomplete="email"]',
    'input[data-testid*="email"]',
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
CONTINUE_BUTTON_PATTERN = re.compile(r"continue|next", re.I)
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


def _extract_text_from_message(msg: Message) -> str:
    chunks = []
    if msg.is_multipart():
        for part in msg.walk():
            content_type = (part.get_content_type() or "").lower()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            text = payload.decode(charset, errors="replace")
            if content_type in ("text/plain", "text/html"):
                chunks.append(text)
    else:
        payload = msg.get_payload(decode=True)
        if payload is not None:
            charset = msg.get_content_charset() or "utf-8"
            chunks.append(payload.decode(charset, errors="replace"))
    return "\n".join(chunks)


def get_device_identifiers(ua: str) -> list[str]:
    ua = (ua or "").lower()
    identifiers = []
    if "firefox" in ua:
        identifiers.append("firefox")
    elif "edg" in ua or "edge" in ua:
        identifiers.append("edge")
    elif "chrome" in ua:
        identifiers.append("chrome")
    elif "safari" in ua:
        identifiers.append("safari")

    if "windows" in ua:
        identifiers.append("windows")
    elif "mac os" in ua or "macos" in ua or "macintosh" in ua:
        identifiers.append("mac")
    elif "linux" in ua:
        identifiers.append("linux")
    
    return identifiers


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

    device_ids = get_device_identifiers(user_agent)

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
                body = _extract_text_from_message(msg)
                source = f"{subject}\n{body}"
                
                source_lower = source.lower()
                has_any_browser = any(b in source_lower for b in ["chrome", "firefox", "safari", "edge", "mac", "windows", "linux"])
                if has_any_browser and device_ids:
                    if not all(ident in source_lower for ident in device_ids):
                        if debug_email:
                            print(f"[auto_login][email_debug] ignoring email due to device mismatch. Expected: {device_ids}")
                        continue

                m = pattern.search(source)
                if m:
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
        if time.monotonic() >= deadline:
            return "unknown"
        page.wait_for_timeout(250)


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
    if visible_email_input is not None:
        sel, loc = visible_email_input
        print(f"[auto_login] filling email with selector: {sel}")
        _human_pause()
        loc.fill(email_value)
    else:
        # Label fallback.
        print("[auto_login] filling email with label fallback")
        _human_pause()
        page.get_by_label("Email", exact=False).first.fill(email_value)

    # Continue button candidates.
    print("[auto_login] clicking Continue/Next")
    _human_pause(0.35, 1.4)
    btn = _find_first_visible_role_locator(page, "button", CONTINUE_BUTTON_PATTERN)
    if btn is None:
        raise RuntimeError("Could not find a visible Continue/Next button on the email step.")
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

    print("[auto_login] clicking code Continue/Verify/Submit")
    # OTPs are short-lived. Avoid randomized delays on this step and try the
    # fastest visible submit control immediately after filling the input.
    btn = _find_first_visible_role_locator(page, "button", CODE_SUBMIT_BUTTON_PATTERN)
    if btn is None:
        try:
            loc.press("Enter", timeout=250)
        except PlaywrightError:
            pass
        if _wait_for_login_url_to_change(page, timeout_ms=350):
            print("[auto_login] code entry advanced login flow without an explicit submit click.")
            return
        btn = _find_first_visible_role_locator(page, "button", CODE_SUBMIT_BUTTON_PATTERN)
        if btn is None:
            raise RuntimeError("Could not find a visible Continue/Verify/Submit/Next button on the code step.")

    try:
        btn.click(timeout=1500)
        if _wait_for_login_url_to_change(page, timeout_ms=350):
            return
    except PlaywrightTimeoutError:
        if _wait_for_login_url_to_change(page, timeout_ms=500):
            print("[auto_login] login flow advanced during the code submit click.")
            return
        print("[auto_login] code submit click timed out; retrying with force=True.")
    except PlaywrightError:
        if _wait_for_login_url_to_change(page, timeout_ms=500):
            print("[auto_login] login flow advanced during the code submit click.")
            return
        print("[auto_login] code submit click failed; retrying with force=True.")

    btn = _find_first_visible_role_locator(page, "button", CODE_SUBMIT_BUTTON_PATTERN)
    if btn is None:
        if _wait_for_login_url_to_change(page, timeout_ms=500):
            print("[auto_login] login flow advanced before the forced code submit retry.")
            return
        raise RuntimeError("Code submit button disappeared before the forced retry completed.")

    try:
        btn.click(timeout=2000, force=True)
        if _wait_for_login_url_to_change(page, timeout_ms=500):
            return
    except PlaywrightError:
        if _wait_for_login_url_to_change(page, timeout_ms=500):
            print("[auto_login] login flow advanced during the forced code submit click.")
            return
        raise


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

        post_submit_state = _wait_for_post_code_submit_state(page)
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

        page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=45000)
        login_html = dump_rendered_html(page, out_dir, "01_login_page")
        print(f"[auto_login] saved rendered html: {login_html}")

        if dump_only:
            if created_new_browser:
                browser.close()
            return

        user_agent = page.evaluate("navigator.userAgent")

        # Let the page settle in case of redirect
        try:
            page.wait_for_load_state("networkidle", timeout=3000)
        except PlaywrightTimeoutError:
            pass

        login_variant = detect_login_page_variant(page)
        print(f"[auto_login] detected login page variant: {login_variant}")

        if login_variant == "already_logged_in":
            print(f"[auto_login] URL is now {page.url} - assuming already logged in.")
            if created_new_browser:
                browser.close()
            return

        request_time = time.time()
        if login_variant == "welcome_back":
            print("[auto_login] step: submit welcome-back login")
            click_welcome_back_login(page)
        elif login_variant == "email_entry":
            print("[auto_login] step: submit email")
            fill_email_and_continue(page, email_value)
        else:
            raise RuntimeError(
                f"Airbnb login page state not recognized at {page.url}. "
                "Expected a welcome-back prompt or visible email input."
            )
        # Wait for either OTP/code input or a stable post-submit state.
        code_ready = False
        for sel in CODE_INPUT_SELECTORS:
            try:
                print(f"[auto_login] waiting for code input selector: {sel}")
                page.locator(sel).first.wait_for(state="visible", timeout=8000)
                code_ready = True
                print(f"[auto_login] code input detected: {sel}")
                break
            except PlaywrightTimeoutError:
                continue
        if not code_ready:
            print("[auto_login] code input not detected yet, waiting for domcontentloaded fallback")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=15000)
            except PlaywrightTimeoutError:
                pass
        code_html = dump_rendered_html(page, out_dir, "02_code_page")
        print(f"[auto_login] saved rendered html: {code_html}")

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
        if created_new_browser:
            browser.close()


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
