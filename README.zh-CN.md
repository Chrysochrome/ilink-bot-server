# ilink-bot-server

用于构建[微信 iLink Bot](https://ilinkai.weixin.qq.com) 服务的异步 Python SDK。

> English docs: [README.md](README.md)

## 特性

- **多 Bot 支持** — 单进程内运行任意数量的 Bot，每个 Bot 拥有独立的凭证和长轮询循环。
- **装饰器 API** — 通过 `@server.*` 装饰器挂载凭证加载、消息处理、登录事件、错误处理等回调，代码结构清晰。
- **两步 QR 登录** — `start_login()` 立即返回二维码 URL，方便传给前端或 HTTP 客户端，后台轮询自动等待用户扫码。
- **游标持久化** — 长轮询游标 `get_updates_buf` 被纳入 `BotCredentials`，每次变更时通过 `@server.on_credential_update` 回调通知，重启后可无缝续接。
- **媒体下载** — `download_media()` 一步完成 CDN 媒体文件的拉取与 AES 解密（图片、语音、文件、视频）。
- **输入状态** — `server.send_typing()` / `server.stop_typing()` 触发微信端"对方正在输入"提示。
- **纯 asyncio + httpx** — 除 `httpx` 和 `cryptography` 外无额外运行时依赖。

## 环境要求

- Python ≥ 3.11
- `httpx >= 0.27`
- `cryptography`

## 安装

```bash
pip install ilink-bot-server
```

## 快速开始

### 1. 首次登录（保存凭证）

```bash
uv run examples/1_1_login_two_steps.py my_bot
```

或以代码方式：

```python
import asyncio
from ilink_bot_server import BotConfig, BotCredentials, BotServer, LoginStatus

server = BotServer()

@server.credential_loader
async def load_creds(bot_id: str) -> BotCredentials | None:
    return None  # 还没有保存的凭证

@server.on_credential_update
async def save_creds(bot_id: str, creds: BotCredentials) -> None:
    # 将 creds 持久化到磁盘或数据库
    ...

@server.on_login_status
async def on_login(status: LoginStatus) -> None:
    if status.status == "confirmed":
        print("登录成功！", status.credentials)

async def main():
    await server.init([BotConfig(bot_id="my_bot")])
    qr_url = await server.start_login("my_bot", timeout_s=120.0)
    print("在微信中扫描：", qr_url)
    await asyncio.sleep(120)
    await server.shutdown()

asyncio.run(main())
```

### 2. 接收并回复消息

```python
import asyncio
from ilink_bot_server import BotConfig, BotCredentials, BotServer, IncomingMessage

server = BotServer()

@server.credential_loader
async def load_creds(bot_id: str) -> BotCredentials | None:
    # 返回已保存的 BotCredentials，或返回 None 触发登录
    ...

@server.on_credential_update
async def save_creds(bot_id: str, creds: BotCredentials) -> None:
    ...

@server.on_message
async def handle(msg: IncomingMessage) -> None:
    await server.send_typing(msg.bot_id, msg.user_id)
    await server.reply(msg, f"你说：{msg.text}")

async def main():
    await server.init([BotConfig(bot_id="my_bot")])
    await server.run_forever()

asyncio.run(main())
```

### 3. 下载媒体文件

```python
from ilink_bot_server import download_media

@server.on_message
async def handle(msg: IncomingMessage) -> None:
    if msg.type == "image" and msg.media:
        data: bytes = await download_media(msg)   # 拉取并解密
        print(f"收到图片：{len(data)} 字节")
```

## API 参考

### `BotServer`

| 方法 / 装饰器 | 说明 |
|---|---|
| `@server.credential_loader` | 注册 `(bot_id) -> BotCredentials \| None` 异步函数 |
| `@server.on_credential_update` | 登录成功或长轮询游标变更时调用 |
| `@server.on_message` | 每条用户消息到达时调用 |
| `@server.on_login_status` | QR 登录流程状态回调（`scaned` / `confirmed` / `expired` / `timeout` / `error`）|
| `@server.on_error` | 轮询循环中发生未处理异常时调用 |
| `server.init(configs)` | 加载凭证、启动所有 Bot 的 Worker |
| `server.start_login(bot_id, ...)` | 获取二维码 URL，启动后台轮询，立即返回 URL 字符串 |
| `server.reply(msg, text)` | 回复一条入站消息 |
| `server.send(bot_id, user_id, text)` | 主动发送消息（需要已有会话上下文）|
| `server.send_typing(bot_id, user_id)` | 显示"正在输入"提示 |
| `server.stop_typing(bot_id, user_id)` | 取消"正在输入"提示 |
| `server.shutdown()` | 优雅停止所有 Worker |
| `server.run_forever()` | 等待 `SIGINT` / `SIGTERM` |

### `BotCredentials`

```python
@dataclass
class BotCredentials:
    token: str             # QR 登录返回的 bot_token
    base_url: str          # API 基座地址
    account_id: str        # ilink_bot_id  (…@im.bot)
    user_id: str           # ilink_user_id (…@im.wechat)
    get_updates_buf: str = ""  # 长轮询游标 — 务必持久化！
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

拉取 CDN 加密媒体文件并返回解密后的 `bytes`。若 `MediaInfo.aes_key` 存在，自动执行 AES-128-ECB + PKCS7 解密。

## 协议文档

完整的微信 iLink Bot HTTP/JSON 协议说明见 [docs/protocol-spec.md](docs/protocol-spec.md)。

主要要点：
- 通过二维码扫码完成认证（`get_bot_qrcode` → `get_qrcode_status`）
- 通过 `getupdates` 长轮询接收消息（服务端持有约 35 秒）
- 回复消息时必须回传入站消息中的 `context_token`
- 媒体文件使用 AES-128-ECB 加密，密钥以 base64 形式内嵌

## 示例

| 文件 | 说明 |
|---|---|
| `examples/1_login.py` | 交互式命令行登录工具 |
| `examples/1_1_login_two_steps.py` | 两步式程序化登录 |
| `examples/2_single_bot.py` | 单 Bot 消息回显 |
| `examples/3_multi_bot.py` | 多 Bot 并行运行 |

## License

MIT
