"""2_single_bot.py - Single Bot Listener Example.

This script demonstrates how to start the BotServer with just one bot,
listen for messages, and echo them back. It also reports bot status and errors.

If no credentials exist for the bot, QR login is triggered automatically.

Usage:
  uv run examples/2_single_bot.py <bot_id>
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
    download_media,
)

CREDS_DIR = Path(__file__).resolve().parent / "credentials"

server = BotServer()


# -- Credential storage ------------------------------------------------------

@server.credential_loader
async def load_credentials(bot_id: str) -> BotCredentials | None:
    print(f"\n[Credential Loader] Loading credentials for bot '{bot_id}'...")
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
    print(f"\n[Credential Update] Bot '{bot_id}' credentials updated.")
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
        print(f"[Login] QR code scanned. Confirm inside WeChat...")
    elif status.status == "confirmed":
        print(f"[Login] '{status.bot_id}' logged in. Bot is starting...")
    elif status.status == "expired":
        print(f"[Login] QR code expired for '{status.bot_id}'. Run the script again.")
    elif status.status == "timeout":
        print(f"[Login] Login timed out for '{status.bot_id}'. Run the script again.")
    elif status.status == "error":
        print(f"[Login] Error during login for '{status.bot_id}': {status.error}")


# -- Message handling --------------------------------------------------------

@server.on_message
async def echo_handler(msg: IncomingMessage) -> None:
    print(f"\n[Message Received]")
    print(f"Bot ID:   {msg.bot_id}")
    print(f"User ID:  {msg.user_id}")
    print(f"Type:     {msg.type}")
    print(f"Text:     {msg.text}")
    print(f"Time:     {msg.timestamp}")

    if msg.media:
        print(f"  Download URL: {msg.media.download_url}")
        if msg.media.aes_key:
            print(f"  AES key (hex): {msg.media.aes_key}")
        if msg.media.file_name:
            print(f"  File name: {msg.media.file_name}")
        if msg.media.width and msg.media.height:
            print(f"  Dimensions: {msg.media.width}x{msg.media.height}")

        try:
            data = await download_media(msg)
            print(f"  Downloaded & decrypted: {len(data)} bytes")
        except Exception as e:
            print(f"  [WARN] Failed to download media: {e}")

    print(f"-> Replying to {msg.user_id}...")
    await server.send_typing(msg.bot_id, msg.user_id)
    await asyncio.sleep(1)  # simulate some work
    await server.reply(msg, f"Echo from {msg.bot_id}:\n{msg.text}")


# -- Helpers -----------------------------------------------------------------

async def monitor_status(bot_id: str) -> None:
    """Periodically print the status of the bot."""
    while True:
        await asyncio.sleep(10)
        try:
            status = server.get_bot_status(bot_id)
            print(
                f"[Status] {bot_id}: state={status.state.value} "
                f"messages_received={status.message_count}"
            )
        except KeyError:
            pass  # Bot not started yet (e.g. pending login)


# -- Entry point -------------------------------------------------------------

async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: uv run examples/2_single_bot.py <bot_id>")
        print("Example: uv run examples/2_single_bot.py my_first_bot")
        sys.exit(1)

    bot_id = sys.argv[1]

    # Initialise the server. Bots without credentials are skipped silently.
    await server.init([BotConfig(bot_id=bot_id)])

    # If no credentials yet, start QR login (timeout: 120 s).
    # start_login() returns the QR URL directly; the bot starts
    # automatically in the background when login is confirmed.
    if not (CREDS_DIR / f"{bot_id}.json").exists():
        print(f"No credentials found for '{bot_id}'. Starting QR login...")
        qr_url = await server.start_login(bot_id, timeout_s=120.0)
        print(f"\nOpen this link in WeChat to log in:")
        print(f"  {qr_url}\n")

    monitor_task = asyncio.create_task(monitor_status(bot_id))

    try:
        print("Server is running. Waiting for messages. Press Ctrl+C to exit.")
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        print("\nShutting down...")
    finally:
        monitor_task.cancel()
        await server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
