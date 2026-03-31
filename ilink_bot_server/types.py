from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum, IntEnum
from typing import Literal, NotRequired, TypeAlias, TypedDict


# ---------------------------------------------------------------------------
# Protocol enums
# ---------------------------------------------------------------------------


class MessageType(IntEnum):
    USER = 1
    BOT = 2


class MessageState(IntEnum):
    NEW = 0
    GENERATING = 1
    FINISH = 2


class MessageItemType(IntEnum):
    TEXT = 1
    IMAGE = 2
    VOICE = 3
    FILE = 4
    VIDEO = 5


# ---------------------------------------------------------------------------
# Protocol TypedDicts
# ---------------------------------------------------------------------------


class BaseInfo(TypedDict):
    channel_version: str


class CDNMedia(TypedDict):
    encrypt_query_param: str
    aes_key: str
    encrypt_type: NotRequired[int]


class TextItem(TypedDict):
    text: str


class ImageItem(TypedDict):
    media: CDNMedia
    aeskey: NotRequired[str]
    url: NotRequired[str]
    mid_size: NotRequired[str | int]
    thumb_size: NotRequired[str | int]
    thumb_height: NotRequired[int]
    thumb_width: NotRequired[int]
    hd_size: NotRequired[str | int]


class VoiceItem(TypedDict):
    media: CDNMedia
    encode_type: NotRequired[int]
    text: NotRequired[str]
    playtime: NotRequired[int]


class FileItem(TypedDict):
    media: CDNMedia
    file_name: NotRequired[str]
    md5: NotRequired[str]
    len: NotRequired[str]


class VideoItem(TypedDict):
    media: CDNMedia
    video_size: NotRequired[str | int]
    play_length: NotRequired[int]
    thumb_media: NotRequired[CDNMedia]


class RefMessage(TypedDict):
    title: NotRequired[str]
    message_item: NotRequired[MessageItem]


class MessageItem(TypedDict):
    type: MessageItemType
    text_item: NotRequired[TextItem]
    image_item: NotRequired[ImageItem]
    voice_item: NotRequired[VoiceItem]
    file_item: NotRequired[FileItem]
    video_item: NotRequired[VideoItem]
    ref_msg: NotRequired[RefMessage]


class WeixinMessage(TypedDict):
    message_id: int
    from_user_id: str
    to_user_id: str
    client_id: str
    create_time_ms: int
    message_type: MessageType
    message_state: MessageState
    context_token: str
    item_list: list[MessageItem]


class GetUpdatesRequest(TypedDict):
    get_updates_buf: str
    base_info: BaseInfo


class GetUpdatesResponse(TypedDict):
    ret: int
    msgs: list[WeixinMessage]
    get_updates_buf: str
    longpolling_timeout_ms: NotRequired[int]
    errcode: NotRequired[int]
    errmsg: NotRequired[str]


class SendMessageMessage(TypedDict):
    from_user_id: str
    to_user_id: str
    client_id: str
    message_type: MessageType
    message_state: MessageState
    context_token: str
    item_list: list[MessageItem]


class SendMessageRequest(TypedDict):
    msg: SendMessageMessage
    base_info: BaseInfo


class SendTypingRequest(TypedDict):
    ilink_user_id: str
    typing_ticket: str
    status: Literal[1, 2]
    base_info: BaseInfo


class GetConfigResponse(TypedDict):
    typing_ticket: NotRequired[str]
    ret: NotRequired[int]
    errcode: NotRequired[int]
    errmsg: NotRequired[str]


class QrCodeResponse(TypedDict):
    qrcode: str
    qrcode_img_content: str


class QrStatusResponse(TypedDict):
    status: Literal["wait", "scaned", "confirmed", "expired"]
    bot_token: NotRequired[str]
    ilink_bot_id: NotRequired[str]
    ilink_user_id: NotRequired[str]
    baseurl: NotRequired[str]


# ---------------------------------------------------------------------------
# Server-specific types
# ---------------------------------------------------------------------------

MessageKind: TypeAlias = Literal["text", "image", "voice", "file", "video"]


@dataclass
class MediaInfo:
    """Structured media reference extracted from a CDN-backed message item.

    ``download_url`` is ready to GET directly from the CDN::

        https://novac2c.cdn.weixin.qq.com/c2c/download?encrypted_query_param=...

    ``aes_key`` is the 32-char hex string (16 raw bytes) needed to decrypt the
    response body with AES-128-ECB + PKCS7 padding.  It may be empty when the
    server did not supply a key (treat content as plain-text in that case).

    ``file_name`` is populated only for FILE items.
    ``width`` / ``height`` are populated only when the server supplied thumb
    dimensions (IMAGE / VIDEO).
    """

    download_url: str
    aes_key: str  # 32-char hex, e.g. "00112233445566778899aabbccddeeff"
    file_name: str = ""
    width: int = 0
    height: int = 0


class BotRunState(str, Enum):
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"
    SESSION_EXPIRED = "session_expired"


@dataclass
class BotCredentials:
    """Credentials obtained from QR login, stored/loaded by external callbacks."""

    token: str
    base_url: str
    account_id: str
    user_id: str
    get_updates_buf: str = ""
    """Opaque long-poll cursor.  Persisted so the bot can resume where it
    left off after a restart (see protocol-spec §4.3)."""


LoginStatusKind: TypeAlias = Literal[
    "scaned", "confirmed", "expired", "timeout", "error"
]


@dataclass
class LoginStatus:
    """Status event emitted during the QR login flow for a bot."""

    bot_id: str
    status: LoginStatusKind
    qr_url: str | None = None
    """The QR link to open in WeChat. Populated when status == ``"qr_ready"``."""
    credentials: "BotCredentials | None" = None
    """The obtained credentials. Populated when status == ``"confirmed"``."""
    error: Exception | None = None
    """The exception that caused the failure. Populated when status == ``"error"``."""


@dataclass
class BotConfig:
    """Configuration passed to BotServer.init(). Only bot_id is required;
    credentials are loaded via the credential_loader callback."""

    bot_id: str


@dataclass
class BotStatus:
    """Runtime status of a single bot worker."""

    bot_id: str
    state: BotRunState
    error: Exception | None = None
    last_poll_time: datetime | None = None
    message_count: int = 0


@dataclass
class IncomingMessage:
    """A user message received by a bot worker."""

    bot_id: str
    user_id: str
    text: str
    type: MessageKind
    raw: WeixinMessage
    _context_token: str
    timestamp: datetime
    media: "MediaInfo | None" = None
    """Populated for image / voice / file / video messages.

    ``text`` for those message types is set to ``media.download_url``
    (a ready-to-use CDN URL), so simple handlers only need ``text``.
    Use ``media`` when you also need the AES key, file name, or dimensions.
    """
