"""1_1_login_two_steps.py - Two-Step QR Login Example.

Demonstrates the server-oriented login flow:
  1. Call start_login() — returns the QR URL immediately.
  2. Background polling waits for the user to scan and confirm.
  3. On confirmation, @server.on_credential_update persists the credentials.

Usage:
  uv run examples/1_1_login_two_steps.py <bot_id>
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from ilink_bot_server import (
    BotConfig,
    BotCredentials,
    BotServer,
    LoginStatus,
)

CREDS_DIR = Path(__file__).resolve().parent / "credentials"

server = BotServer()
login_done: asyncio.Event = asyncio.Event()


@server.credential_loader
async def load_credentials(bot_id: str) -> BotCredentials | None:
    path = CREDS_DIR / f"{bot_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return BotCredentials(
        token=data["token"],
        base_url=data["base_url"],
        account_id=data["account_id"],
        user_id=data["user_id"],
        get_updates_buf=data.get("get_updates_buf", ""),
    )


@server.on_credential_update
async def save_credentials(bot_id: str, creds: BotCredentials) -> None:
    CREDS_DIR.mkdir(parents=True, exist_ok=True)
    path = CREDS_DIR / f"{bot_id}.json"
    payload = {
        "token": creds.token,
        "base_url": creds.base_url,
        "account_id": creds.account_id,
        "user_id": creds.user_id,
        "get_updates_buf": creds.get_updates_buf,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


@server.on_login_status
async def handle_login(status: LoginStatus) -> None:
    if status.status == "scaned":
        print("QR code scanned. Confirm inside WeChat...")
    elif status.status == "confirmed":
        print(f"\nLogin successful!")
        assert status.credentials is not None
        print(f"User ID:    {status.credentials.user_id}")
        print(f"Account ID: {status.credentials.account_id}")
        print(f"\nCredentials saved to: {CREDS_DIR / f'{status.bot_id}.json'}")
        print("\nYou can now run the other examples.")
        login_done.set()
    elif status.status == "expired":
        print("QR code expired. Run the script again.")
        login_done.set()
    elif status.status == "timeout":
        print("Login timed out. Run the script again.")
        login_done.set()
    elif status.status == "error":
        print(f"Login error: {status.error}")
        login_done.set()


async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: uv run examples/1_1_login_two_steps.py <bot_id>")
        sys.exit(1)

    bot_id = sys.argv[1]

    await server.init([BotConfig(bot_id=bot_id)])

    # Step 1: start_login() fetches the QR code and returns the URL.
    qr_url = await server.start_login(bot_id, timeout_s=120.0)

    # Step 2: Show the URL to the user — in a real server you'd send this
    # to the frontend via WebSocket / HTTP response / etc.
    print(f"Logging in bot '{bot_id}'...")
    print(f"Open this link in WeChat:\n\n  {qr_url}\n")

    # Wait until login resolves (confirmed / expired / timeout / error).
    await login_done.wait()
    await server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
