# ilink-bot-server

An async Python SDK for building [WeChat iLink Bot](https://ilinkai.weixin.qq.com) services.

> 中文文档：[README.zh-CN.md](README.zh-CN.md)

## Features

- **Multi-bot support** — run any number of bots in a single process, each with its own credential and long-poll loop.
- **Decorator API** — wire up credential loading, message handling, login events, and error handling with simple `@server.*` decorators.
- **Two-step QR login** — `start_login()` returns the QR URL immediately so you can hand it to a frontend or HTTP client, while polling continues in the background.
- **Cursor persistence** — the `get_updates_buf` long-poll cursor is included in `BotCredentials` and passed to `@server.on_credential_update` on every change, so restarts resume exactly where they left off.
- **Media download** — `download_media()` fetches and AES-decrypts CDN media (images, voice, files, video) in one call.
- **Typing indicator** — `server.send_typing()` / `server.stop_typing()` trigger the "typing…" bubble in WeChat.
- **Pure asyncio + httpx** — no extra runtime dependencies beyond `httpx` and `cryptography`.

## Requirements

- Python ≥ 3.11
- `httpx >= 0.27`
- `cryptography`

## Installation

```bash
pip install ilink-bot-server
```

## Quick start

### 1. First-time login (save credentials)

```bash
uv run examples/1_1_login_two_steps.py my_bot
```

Or programmatically:

```python
import asyncio
from ilink_bot_server import BotConfig, BotCredentials, BotServer, LoginStatus

server = BotServer()

@server.credential_loader
async def load_creds(bot_id: str) -> BotCredentials | None:
    return None  # no stored credentials yet

@server.on_credential_update
async def save_creds(bot_id: str, creds: BotCredentials) -> None:
    # persist creds to disk / database
    ...

@server.on_login_status
async def on_login(status: LoginStatus) -> None:
    if status.status == "confirmed":
        print("Logged in!", status.credentials)

async def main():
    await server.init([BotConfig(bot_id="my_bot")])
    qr_url = await server.start_login("my_bot", timeout_s=120.0)
    print("Scan in WeChat:", qr_url)
    await asyncio.sleep(120)      # wait for user to scan
    await server.shutdown()

asyncio.run(main())
```

### 2. Receiving and replying to messages

```python
import asyncio
from ilink_bot_server import BotConfig, BotCredentials, BotServer, IncomingMessage

server = BotServer()

@server.credential_loader
async def load_creds(bot_id: str) -> BotCredentials | None:
    # return saved BotCredentials or None to trigger login
    ...

@server.on_credential_update
async def save_creds(bot_id: str, creds: BotCredentials) -> None:
    ...

@server.on_message
async def handle(msg: IncomingMessage) -> None:
    await server.send_typing(msg.bot_id, msg.user_id)
    await server.reply(msg, f"You said: {msg.text}")

async def main():
    await server.init([BotConfig(bot_id="my_bot")])
    await server.run_forever()

asyncio.run(main())
```

### 3. Downloading media

```python
from ilink_bot_server import download_media

@server.on_message
async def handle(msg: IncomingMessage) -> None:
    if msg.type == "image" and msg.media:
        data: bytes = await download_media(msg)   # fetches + decrypts
        print(f"Received image: {len(data)} bytes")
```

## API reference

### `BotServer`

| Method / Decorator | Description |
|---|---|
| `@server.credential_loader` | Register an async function `(bot_id) -> BotCredentials \| None` |
| `@server.on_credential_update` | Called after login and whenever the long-poll cursor changes |
| `@server.on_message` | Called for every incoming user message |
| `@server.on_login_status` | Called during QR login flow (`scaned`, `confirmed`, `expired`, `timeout`, `error`) |
| `@server.on_error` | Called when an unhandled error occurs in a poll loop |
| `server.init(configs)` | Load credentials and start workers for all configured bots |
| `server.start_login(bot_id, ...)` | Fetch a QR URL, start background polling, return the URL string |
| `server.reply(msg, text)` | Reply to an incoming message |
| `server.send(bot_id, user_id, text)` | Send a proactive message (requires a prior conversation) |
| `server.send_typing(bot_id, user_id)` | Show "typing…" indicator |
| `server.stop_typing(bot_id, user_id)` | Clear "typing…" indicator |
| `server.shutdown()` | Gracefully stop all workers |
| `server.run_forever()` | `await` until `SIGINT` / `SIGTERM` |

### `BotCredentials`

```python
@dataclass
class BotCredentials:
    token: str             # bot_token from QR login
    base_url: str          # API base URL
    account_id: str        # ilink_bot_id  (…@im.bot)
    user_id: str           # ilink_user_id (…@im.wechat)
    get_updates_buf: str = ""  # long-poll cursor — persist this!
```

### `IncomingMessage`

```python
@dataclass
class IncomingMessage:
    bot_id: str
    user_id: str
    text: str
    type: MessageKind      # "text" | "image" | "voice" | "file" | "video"
    timestamp: datetime
    media: MediaInfo | None
    raw: WeixinMessage
```

### `download_media(msg)` / `download_media_info(info, client?)`

Fetches a CDN-encrypted media file and returns the decrypted `bytes`. AES-128-ECB + PKCS7 decryption is applied automatically when `MediaInfo.aes_key` is present.

## Protocol documentation

The full WeChat iLink Bot HTTP/JSON protocol is documented in
[docs/protocol-spec.md](docs/protocol-spec.md).

Highlights:
- Authentication via QR code scan (`get_bot_qrcode` → `get_qrcode_status`)
- Message delivery via `getupdates` long-polling (~35 s server hold)
- `context_token` is required for all replies and must be echoed from the incoming message
- Media encrypted with AES-128-ECB; key delivered inline as base64

## Examples

| File | Description |
|---|---|
| `examples/1_login.py` | Interactive CLI login helper |
| `examples/1_1_login_two_steps.py` | Two-step programmatic login |
| `examples/2_single_bot.py` | Single bot with message echo |
| `examples/3_multi_bot.py` | Multiple bots running in parallel |

## License

MIT
