"""3_multi_bot.py - Multi-Bot Listener Example.

Runs several bots in parallel. If credentials are missing for any of
the listed bots, QR login is triggered for each one automatically.

Usage:
  uv run examples/3_multi_bot.py <bot_id_1> [bot_id_2 ...]

Example:
  uv run examples/3_multi_bot.py alice bob charlie
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
    IncomingMessage,
    LoginStatus,
)

CREDS_DIR = Path(__file__).resolve().parent / "credentials"

server = BotServer()


# -- Credential storage ------------------------------------------------------

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


# -- Error handling ----------------------------------------------------------

@server.on_error
def handle_error(bot_id: str, error: Exception) -> None:
    print(f"\n[ERROR] Bot '{bot_id}': {error}")


# -- Login status ------------------------------------------------------------

@server.on_login_status
async def handle_login(status: LoginStatus) -> None:
    if status.status == "scaned":
        print(f"[Login:{status.bot_id}] QR code scanned. Confirm inside WeChat...")
    elif status.status == "confirmed":
        print(f"[Login:{status.bot_id}] Logged in. Bot is starting...")
    elif status.status == "expired":
        print(f"[Login:{status.bot_id}] QR code expired.")
    elif status.status == "timeout":
        print(f"[Login:{status.bot_id}] Login timed out.")
    elif status.status == "error":
        print(f"[Login:{status.bot_id}] Login error: {status.error}")


# -- Message handling --------------------------------------------------------

@server.on_message
async def echo(msg: IncomingMessage) -> None:
    print(f"[{msg.bot_id}] {msg.user_id}: {msg.text!r}")
    await server.reply(msg, f"Echo from {msg.bot_id}:\n{msg.text}")


# -- Helpers -----------------------------------------------------------------

async def monitor_status(bot_ids: list[str]) -> None:
    """Periodically print running status for all bots."""
    while True:
        await asyncio.sleep(30)
        statuses = server.get_status()
        for bot_id in bot_ids:
            if bot_id in statuses:
                s = statuses[bot_id]
                print(
                    f"[Status] {bot_id}: state={s.state.value} "
                    f"msgs={s.message_count}"
                )
            else:
                print(f"[Status] {bot_id}: not running (pending login?)")


# -- Entry point -------------------------------------------------------------

async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: uv run examples/3_multi_bot.py <bot_id_1> [bot_id_2 ...]")
        sys.exit(1)

    bot_ids = sys.argv[1:]
    configs = [BotConfig(bot_id=bid) for bid in bot_ids]

    # Initialise the server. Bots without credentials are skipped silently.
    await server.init(configs)

    # Start QR login for any bots without credentials.
    missing = [bid for bid in bot_ids if not (CREDS_DIR / f"{bid}.json").exists()]
    for bid in missing:
        qr_url = await server.start_login(bid, timeout_s=120.0)
        print(f"\n[Login:{bid}] Open this link in WeChat:")
        print(f"  {qr_url}")

    monitor_task = asyncio.create_task(monitor_status(bot_ids))

    try:
        print(
            f"Server running {len(bot_ids)} bot(s). "
            "Waiting for messages. Press Ctrl+C to exit."
        )
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        monitor_task.cancel()
        await server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())

