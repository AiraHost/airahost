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
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


LOGIN_URL = "https://www.airbnb.ca/login"
DEFAULT_CDP_URL = os.getenv("CDP_URL", "http://127.0.0.1:9222").strip()


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


def wait_for_airbnb_code(config: ImapConfig) -> str:
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
                fetch_status, fetched = conn.fetch(msg_id, "(RFC822)")
                if fetch_status != "OK" or not fetched or not fetched[0]:
                    continue
                raw = fetched[0][1]
                if not raw:
                    continue
                msg = email.message_from_bytes(raw)
                from_header = _decode_mime_header(str(msg.get("From", "")))
                subject = _decode_mime_header(str(msg.get("Subject", "")))
                if debug_email:
                    date_header = _decode_mime_header(str(msg.get("Date", "")))
                    print(
                        "[auto_login][email_debug] "
                        f"id={msg_id.decode(errors='ignore')} "
                        f"from={from_header} subject={subject} date={date_header}"
                    )
                if config.from_filter and config.from_filter.lower() not in from_header.lower():
                    continue
                body = _extract_text_from_message(msg)
                source = f"{subject}\n{body}"
                m = pattern.search(source)
                if m:
                    return m.group(1)
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


def fill_email_and_continue(page: Page, email_value: str) -> None:
    def _human_pause(min_s: float = 0.25, max_s: float = 1.1) -> None:
        time.sleep(random.uniform(min_s, max_s))

    # Candidate selectors observed on Airbnb login flows.
    selectors = [
        'input[name="email"]',
        'input[type="email"]',
        'input[autocomplete="email"]',
        'input[data-testid*="email"]',
    ]
    for sel in selectors:
        loc = page.locator(sel).first
        if loc.count() > 0:
            print(f"[auto_login] filling email with selector: {sel}")
            _human_pause()
            loc.fill(email_value)
            break
    else:
        # Label fallback.
        print("[auto_login] filling email with label fallback")
        _human_pause()
        page.get_by_label("Email", exact=False).first.fill(email_value)

    # Continue button candidates.
    print("[auto_login] clicking Continue/Next")
    _human_pause(0.35, 1.4)
    btn = page.get_by_role("button", name=re.compile(r"continue|next", re.I)).first
    btn.click()


def fill_code_and_continue(page: Page, code_value: str) -> None:
    def _human_pause(min_s: float = 0.25, max_s: float = 1.1) -> None:
        time.sleep(random.uniform(min_s, max_s))

    selectors = [
        'input[autocomplete="one-time-code"]',
        'input[inputmode="numeric"]',
        'input[name*="code"]',
        'input[data-testid*="code"]',
    ]
    for sel in selectors:
        loc = page.locator(sel).first
        if loc.count() > 0:
            print(f"[auto_login] filling code with selector: {sel}")
            _human_pause()
            loc.fill(code_value)
            break
    else:
        print("[auto_login] filling code with label fallback")
        _human_pause()
        page.get_by_label(re.compile(r"code|verification", re.I)).first.fill(code_value)

    print("[auto_login] clicking code Continue/Verify/Submit")
    _human_pause(0.35, 1.4)
    btn = page.get_by_role("button", name=re.compile(r"continue|verify|submit|next", re.I)).first
    btn.click()


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

        print("[auto_login] step: submit email")
        fill_email_and_continue(page, email_value)
        # Wait for either OTP/code input or a stable post-submit state.
        code_ready_selectors = [
            'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"]',
            'input[name*="code"]',
            'input[data-testid*="code"]',
        ]
        code_ready = False
        for sel in code_ready_selectors:
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
        print("[auto_login] waiting for confirmation code email")
        code = wait_for_airbnb_code(cfg)
        print("[auto_login] confirmation code received")

        fill_code_and_continue(page, code)
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
