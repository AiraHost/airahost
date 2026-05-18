from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from supabase import Client, create_client

ROOT = Path(__file__).resolve().parents[1]


def load_environment() -> None:
    """Load local env files for manual CLI runs.

    Next.js API routes already pass the process environment through to the
    spawned Python process, but loading these files keeps the sidecar usable
    from a shell as well.
    """

    for path in (
        ROOT / ".env",
        ROOT / ".env.local",
        ROOT / "worker" / ".env",
        ROOT / "ml_sidecar" / ".env",
    ):
        load_dotenv(path, override=False)


def get_client() -> Client:
    load_environment()

    url = (
        os.environ.get("SUPABASE_URL")
        or os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
        or ""
    ).strip()
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()

    if not url or not key:
        raise RuntimeError(
            "Missing SUPABASE URL or SUPABASE_SERVICE_ROLE_KEY for ml_sidecar."
        )

    hostname = urlparse(url).hostname
    if hostname:
        for key_name in ("NO_PROXY", "no_proxy"):
            existing = os.environ.get(key_name, "")
            entries = [entry.strip() for entry in existing.split(",") if entry.strip()]
            if hostname not in entries:
                entries.append(hostname)
                os.environ[key_name] = ",".join(entries)

    return create_client(url, key)
