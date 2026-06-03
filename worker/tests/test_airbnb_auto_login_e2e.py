from __future__ import annotations

import contextlib
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse
from urllib.request import urlopen

import pytest

from worker import airbnb_auto_login


TEST_EMAIL = "qa@example.com"
TEST_CODE = "246810"
RESEND_TEST_CODE = "135790"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _resolve_browser_executable() -> str | None:
    candidates = [
        shutil.which("chrome"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("msedge"),
    ]
    if sys.platform == "win32":
        candidates.extend(
            [
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            ]
        )
    for candidate in candidates:
        if candidate and os.path.exists(candidate):
            return candidate
    return None


def _wait_for_cdp_ready(cdp_url: str, timeout_seconds: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    version_url = f"{cdp_url}/json/version"
    while time.monotonic() < deadline:
        try:
            with urlopen(version_url, timeout=1.0) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError(f"Timed out waiting for CDP browser at {version_url}")


@contextlib.contextmanager
def _run_cdp_browser(profile_dir: Path):
    executable = _resolve_browser_executable()
    if executable is None:
        pytest.skip("No local Chrome or Edge executable found for the auto-login e2e test.")

    port = _find_free_port()
    cdp_url = f"http://127.0.0.1:{port}"
    cmd = [
        executable,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "about:blank",
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_cdp_ready(cdp_url)
        yield cdp_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


class _MockLoginHandler(BaseHTTPRequestHandler):
    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            return

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        route = parsed.path
        params = parse_qs(parsed.query)

        if route == "/login":
            scenario = params.get("scenario", ["email-entry"])[0]
            code_mode = params.get("code_mode", ["button"])[0]
            self._send_html(self._render_login_page(scenario, code_mode))
            return

        if route == "/login/code":
            branch = params.get("branch", ["unknown"])[0]
            code_mode = params.get("code_mode", ["button"])[0]
            state = params.get("state", ["active"])[0]
            self._send_html(self._render_code_page(branch, code_mode, state))
            return

        if route == "/hosting":
            branch = params.get("branch", ["unknown"])[0]
            code = params.get("code", [""])[0]
            result = params.get("result", ["missing"])[0]
            self._send_html(
                f"""
                <!doctype html>
                <html>
                  <body data-branch="{branch}" data-code="{code}" data-result="{result}">
                    <main>LOGIN_OK branch={branch} code={code} result={result}</main>
                  </body>
                </html>
                """
            )
            return

        self.send_error(404)

    def log_message(self, format: str, *args) -> None:
        return

    def _send_html(self, html: str) -> None:
        encoded = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _render_login_page(self, scenario: str, code_mode: str) -> str:
        if scenario == "welcome-back":
            return f"""
            <!doctype html>
            <html>
              <body>
                <main>
                  <h1>Welcome back</h1>
                  <button id="welcome-login" type="button">Log in</button>
                  <button id="not-you" type="button">Not you?</button>
                  <section id="email-form" hidden>
                    <label for="email">Email</label>
                    <input id="email" name="email" type="email" autocomplete="email" />
                    <button id="continue-email" type="button">Continue</button>
                  </section>
                </main>
                <script>
                  const emailInput = document.getElementById("email");
                  const codeMode = "{code_mode}";
                  document.getElementById("welcome-login").addEventListener("click", () => {{
                    window.location.href = `/login/code?branch=welcome-back&code_mode=${{codeMode}}`;
                  }});
                  document.getElementById("not-you").addEventListener("click", () => {{
                    document.getElementById("email-form").hidden = false;
                    emailInput.focus();
                  }});
                  document.getElementById("continue-email").addEventListener("click", () => {{
                    const value = encodeURIComponent(emailInput.value || "");
                    window.location.href = `/login/code?branch=not-you&email=${{value}}&code_mode=${{codeMode}}`;
                  }});
                </script>
              </body>
            </html>
            """

        return f"""
        <!doctype html>
        <html>
          <body>
            <main>
              <label for="email">Email</label>
              <input id="email" name="email" type="email" autocomplete="email" />
              <button id="continue-email" type="button">Continue</button>
            </main>
            <script>
              const emailInput = document.getElementById("email");
              const codeMode = "{code_mode}";
              document.getElementById("continue-email").addEventListener("click", () => {{
                const value = encodeURIComponent(emailInput.value || "");
                window.location.href = `/login/code?branch=email-entry&email=${{value}}&code_mode=${{codeMode}}`;
              }});
            </script>
          </body>
        </html>
        """

    def _render_code_page(self, branch: str, code_mode: str, state: str) -> str:
        encoded_branch = quote(branch, safe="")
        expired_banner = ""
        resend_button = ""
        if code_mode == "expired-then-resend" and state == "expired":
            expired_banner = (
                "<p id=\"code-expired\">Code expired. Please request a new one.</p>"
            )
            resend_button = '<button id="send-new-code" type="button">Send new code</button>'
        return f"""
        <!doctype html>
        <html>
          <body>
            <main>
              {expired_banner}
              <label for="verification-code">Verification code</label>
              <input
                id="verification-code"
                name="verification_code"
                autocomplete="one-time-code"
                inputmode="numeric"
              />
              <button id="submit-code" type="button">Continue</button>
              {resend_button}
            </main>
            <script>
              const branch = "{encoded_branch}";
              const codeMode = "{code_mode}";
              const state = "{state}";
              const input = document.getElementById("verification-code");
              window.__submitted = false;
              const submit = () => {{
                if (window.__submitted) {{
                  return;
                }}
                window.__submitted = true;
                const code = encodeURIComponent(input.value || "");
                if (codeMode === "expired-then-resend" && state !== "resent") {{
                  window.location.href =
                    `/login/code?branch=${{branch}}&code_mode=${{codeMode}}&state=expired`;
                  return;
                }}
                const expectedCode =
                  codeMode === "expired-then-resend" ? "{RESEND_TEST_CODE}" : "{TEST_CODE}";
                const result = input.value === expectedCode ? "ok" : "bad-code";
                window.location.href =
                  `/hosting?branch=${{branch}}&code=${{code}}&result=${{result}}`;
              }};
              document.getElementById("submit-code").addEventListener("click", submit);
              const sendNewCodeButton = document.getElementById("send-new-code");
              if (sendNewCodeButton) {{
                sendNewCodeButton.addEventListener("click", () => {{
                  window.location.href =
                    `/login/code?branch=${{branch}}&code_mode=${{codeMode}}&state=resent`;
                }});
              }}
              if (codeMode === "auto-submit") {{
                input.addEventListener("input", () => {{
                  if ((input.value || "").length >= {len(TEST_CODE)}) {{
                    setTimeout(submit, 20);
                  }}
                }});
              }}
              if (codeMode === "expires-fast") {{
                setTimeout(() => {{
                  if (!window.__submitted) {{
                    window.location.href =
                      `/hosting?branch=${{branch}}&code=expired&result=expired`;
                  }}
                }}, 700);
              }}
            </script>
          </body>
        </html>
        """


@contextlib.contextmanager
def _run_login_test_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _MockLoginHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize(
    ("scenario", "expected_branch", "code_mode", "patch_sleep"),
    [
        ("welcome-back", "welcome-back", "button", True),
        ("email-entry", "email-entry", "button", True),
        ("welcome-back", "welcome-back", "auto-submit", True),
        ("email-entry", "email-entry", "expires-fast", False),
    ],
)
def test_run_login_flow_handles_known_and_first_time_login_states(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    scenario: str,
    expected_branch: str,
    code_mode: str,
    patch_sleep: bool,
) -> None:
    captured_wait_args: dict[str, object] = {}

    def _fake_read_imap_config() -> airbnb_auto_login.ImapConfig:
        return airbnb_auto_login.ImapConfig(
            email_address=TEST_EMAIL,
            app_password="unused",
            host="unused",
            port=993,
            use_ssl=True,
            folder="INBOX",
            from_filter="automated@airbnb.com",
            code_regex=r"(?<!\d)(\d{6})(?!\d)",
            timeout_seconds=5,
            poll_seconds=1,
        )

    def _fake_wait_for_airbnb_code(
        config: airbnb_auto_login.ImapConfig,
        request_time: float = 0.0,
        user_agent: str = "",
        after_message_id: int = 0,
        return_message_id: bool = False,
    ) -> str | tuple[str, int]:
        captured_wait_args["config_email"] = config.email_address
        captured_wait_args["request_time"] = request_time
        captured_wait_args["user_agent"] = user_agent
        captured_wait_args["after_message_id"] = after_message_id
        captured_wait_args["return_message_id"] = return_message_id
        if return_message_id:
            return TEST_CODE, 101
        return TEST_CODE

    with _run_login_test_server() as base_url, _run_cdp_browser(tmp_path / f"cdp-{scenario}-{code_mode}") as cdp_url:
        monkeypatch.setenv("AIRAHOST_EMAIL", TEST_EMAIL)
        monkeypatch.setenv("CDP_URL", cdp_url)
        monkeypatch.setattr(
            airbnb_auto_login,
            "LOGIN_URL",
            f"{base_url}/login?scenario={scenario}&code_mode={code_mode}",
        )
        monkeypatch.setattr(airbnb_auto_login, "_read_imap_config_from_env", _fake_read_imap_config)
        monkeypatch.setattr(airbnb_auto_login, "wait_for_airbnb_code", _fake_wait_for_airbnb_code)
        if patch_sleep:
            monkeypatch.setattr(airbnb_auto_login.time, "sleep", lambda _seconds: None)

        out_dir = tmp_path / f"artifacts-{scenario}-{code_mode}"
        airbnb_auto_login.run_login_flow(out_dir=out_dir, dump_only=False)

    login_html = (out_dir / "01_login_page.html").read_text(encoding="utf-8")
    code_html = (out_dir / "02_code_page.html").read_text(encoding="utf-8")
    final_html = (out_dir / "03_after_submit.html").read_text(encoding="utf-8")

    if scenario == "welcome-back":
        assert "welcome back" in login_html.lower()
    else:
        assert '<label for="email">email</label>' in login_html.lower()
    assert "verification code" in code_html.lower()
    assert "LOGIN_OK" in final_html
    assert f"branch={expected_branch}" in final_html
    assert "result=ok" in final_html
    assert captured_wait_args["config_email"] == TEST_EMAIL
    assert float(captured_wait_args["request_time"]) > 0.0
    assert captured_wait_args["after_message_id"] == 0
    assert captured_wait_args["return_message_id"] is True
    assert str(captured_wait_args["user_agent"]).strip()


def test_run_login_flow_requests_new_code_after_expired_message(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    wait_calls: list[dict[str, object]] = []
    issued_codes = iter([TEST_CODE, RESEND_TEST_CODE])

    def _fake_read_imap_config() -> airbnb_auto_login.ImapConfig:
        return airbnb_auto_login.ImapConfig(
            email_address=TEST_EMAIL,
            app_password="unused",
            host="unused",
            port=993,
            use_ssl=True,
            folder="INBOX",
            from_filter="automated@airbnb.com",
            code_regex=r"(?<!\d)(\d{6})(?!\d)",
            timeout_seconds=5,
            poll_seconds=1,
        )

    def _fake_wait_for_airbnb_code(
        config: airbnb_auto_login.ImapConfig,
        request_time: float = 0.0,
        user_agent: str = "",
        after_message_id: int = 0,
        return_message_id: bool = False,
    ) -> str | tuple[str, int]:
        code = next(issued_codes)
        message_id = 100 + len(wait_calls) + 1
        wait_calls.append(
            {
                "config_email": config.email_address,
                "request_time": request_time,
                "user_agent": user_agent,
                "after_message_id": after_message_id,
                "return_message_id": return_message_id,
            }
        )
        if return_message_id:
            return code, message_id
        return code

    with _run_login_test_server() as base_url, _run_cdp_browser(tmp_path / "cdp-expired-then-resend") as cdp_url:
        monkeypatch.setenv("AIRAHOST_EMAIL", TEST_EMAIL)
        monkeypatch.setenv("CDP_URL", cdp_url)
        monkeypatch.setattr(
            airbnb_auto_login,
            "LOGIN_URL",
            f"{base_url}/login?scenario=email-entry&code_mode=expired-then-resend",
        )
        monkeypatch.setattr(airbnb_auto_login, "_read_imap_config_from_env", _fake_read_imap_config)
        monkeypatch.setattr(airbnb_auto_login, "wait_for_airbnb_code", _fake_wait_for_airbnb_code)
        monkeypatch.setattr(airbnb_auto_login.time, "sleep", lambda _seconds: None)

        out_dir = tmp_path / "artifacts-expired-then-resend"
        airbnb_auto_login.run_login_flow(out_dir=out_dir, dump_only=False)

    final_html = (out_dir / "03_after_submit.html").read_text(encoding="utf-8")

    assert "LOGIN_OK" in final_html
    assert "branch=email-entry" in final_html
    assert f"code={RESEND_TEST_CODE}" in final_html
    assert "result=ok" in final_html
    assert len(wait_calls) == 2
    assert wait_calls[0]["config_email"] == TEST_EMAIL
    assert float(wait_calls[0]["request_time"]) > 0.0
    assert str(wait_calls[0]["user_agent"]).strip()
    assert wait_calls[0]["after_message_id"] == 0
    assert wait_calls[0]["return_message_id"] is True
    assert wait_calls[1]["after_message_id"] == 101
    assert wait_calls[1]["return_message_id"] is True
    assert float(wait_calls[1]["request_time"]) >= float(wait_calls[0]["request_time"])
