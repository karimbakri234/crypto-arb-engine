"""Writes exchange API credentials submitted via the dashboard into `.env`.

Persists them (so a restart doesn't lose the keys) and updates
`os.environ` immediately so `config.settings.get_credentials` and a
subsequent `RestManager.reconnect` pick them up without waiting for a
process restart.

SECURITY NOTE: the dashboard is plain HTTP, not HTTPS (see README "Local
dashboard"). HTTP Basic Auth (dashboard/auth.py) keeps casual/opportunistic
access out, but credentials submitted through this form are not encrypted
in transit. Setting keys directly in `.env` over SSH remains the safer
option -- this exists for convenience once you've accepted that tradeoff,
not as a replacement for it.
"""

from __future__ import annotations

import os

ENV_FILE_PATH = os.getenv("ENV_FILE_PATH", ".env")


def write_credentials(venue_id: str, api_key: str, secret: str, passphrase: str | None = None) -> None:
    """Upsert `{VENUE_ID}_API_KEY`/`_SECRET`/`_PASSPHRASE` in `.env`.

    Rewrites an existing `KEY=...` line in place if present (preserving
    every other line untouched) and appends any key not already there.
    """
    prefix = venue_id.upper()
    updates = {f"{prefix}_API_KEY": api_key, f"{prefix}_SECRET": secret}
    if passphrase:
        updates[f"{prefix}_PASSPHRASE"] = passphrase

    lines: list[str] = []
    if os.path.exists(ENV_FILE_PATH):
        with open(ENV_FILE_PATH, encoding="utf-8") as f:
            lines = f.readlines()

    seen: set[str] = set()
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in updates:
            lines[i] = f"{key}={updates[key]}\n"
            seen.add(key)

    for key, value in updates.items():
        if key not in seen:
            lines.append(f"{key}={value}\n")

    with open(ENV_FILE_PATH, "w", encoding="utf-8") as f:
        f.writelines(lines)

    for key, value in updates.items():
        os.environ[key] = value
