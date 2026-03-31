"""1_login.py - Interactive QR Login Example.

Use this script once per bot account to obtain and persist credentials.
The credentials are saved to credentials/<bot_id>.json and used by the
other examples.

Usage:
  uv run examples/1_login.py <bot_id>
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from ilink_bot_server import login


async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: uv run examples/1_login.py <bot_id>")
        print("Example: uv run examples/1_login.py my_first_bot")
        sys.exit(1)

    bot_id = sys.argv[1]

    # credentials/ directory next to this script
    creds_dir = Path(__file__).resolve().parent / "credentials"
    creds_path = creds_dir / f"{bot_id}.json"

    print(f"Logging in bot '{bot_id}'...")
    print("A QR link will be printed below — open it in WeChat to complete login.\n")

    try:
        credentials = await login()
    except KeyboardInterrupt:
        print("\nLogin cancelled.")
        sys.exit(1)
    except Exception as e:
        print(f"\nLogin failed: {e}")
        sys.exit(1)

    print("\nLogin successful!")
    print(f"User ID:    {credentials.user_id}")
    print(f"Account ID: {credentials.account_id}")

    # Ensure directory exists and save credentials
    creds_dir.mkdir(parents=True, exist_ok=True)

    payload = {
        "token": credentials.token,
        "base_url": credentials.base_url,
        "account_id": credentials.account_id,
        "user_id": credentials.user_id,
        "get_updates_buf": "",
    }
    creds_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nCredentials saved to: {creds_path}")
    print("\nYou can now run the next examples.")


if __name__ == "__main__":
    asyncio.run(main())
