"""3_multi_bot.py - Multi-Bot Orchestration Example.

This script demonstrates how to start and manage multiple bots simultaneously
within a single node process. It also shows how to dynamically add bots at runtime.

Usage:
  1. Generate credentials for multiple bots:
     uv run examples/1_login.py bot1
     uv run examples/1_login.py bot2

  2. Run this script:
     uv run examples/multi_bot.py bot1 bot2
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
)

CREDS_DIR = Path(__file__).resolve().parent / "credentials"


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
    )


async def save_credentials(bot_id: str, creds: BotCredentials) -> None:
    CREDS_DIR.mkdir(parents=True, exist_ok=True)
    path = CREDS_DIR / f"{bot_id}.json"
    payload = {
        "token": creds.token,
        "base_url": creds.base_url,
        "account_id": creds.account_id,
        "user_id": creds.user_id,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


server = BotServer(
    credential_loader=load_credentials,
    credential_saver=save_credentials,
    on_error=lambda bot_id, err: print(f"[ERROR][bot_id={bot_id}] {err}"),
)


@server.on_message
async def multi_router(msg: IncomingMessage) -> None:
    """A single handler receives messages from ALL active bots."""
    print(f"[RECV] [{msg.bot_id}] from {msg.user_id}: {msg.text}")

    # You can route logic differently depending on which bot received it
    if msg.bot_id.startswith("test"):
        await server.reply(msg, "Test bot handling your request...")
    else:
        await server.reply(msg, f"[{msg.bot_id}] Echo: {msg.text}")


async def display_status_loop() -> None:
    """Print the status of all running bots every 5 seconds."""
    while True:
        await asyncio.sleep(5)
        print("\n--- Bot Status ---")
        for bot_id, status in server.get_status().items():
            print(f"  {bot_id}: state={status.state.value} messages={status.message_count}")
        print("------------------\n")


async def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: uv run examples/multi_bot.py <bot_id_1> [bot_id_2] ...")
        print("Note: Make sure each bot has credentials created via 1_login.py")
        sys.exit(1)

    bot_ids = sys.argv[1:]

    # Verify all bot credentials exist before starting
    for bid in bot_ids:
        path = CREDS_DIR / f"{bid}.json"
        if not path.exists():
            print(f"Error: Credentials not found for '{bid}'. Please run 1_login.py first.")
            sys.exit(1)

    configs = [BotConfig(bot_id=bid) for bid in bot_ids]

    print(f"Initializing BotServer with bots: {', '.join(bot_ids)}")
    await server.init(configs)

    # Start the periodic status print
    monitor_task = asyncio.create_task(display_status_loop())

    try:
        print("Server running. Press Ctrl+C to stop.")
        # Wait forever
        while True:
            await asyncio.sleep(3600)
    except KeyboardInterrupt:
        print("\nInitiating shutdown...")
    finally:
        monitor_task.cancel()
        await server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
