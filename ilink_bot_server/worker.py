from __future__ import annotations

import asyncio
import base64
import contextlib
import inspect
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any, cast
from urllib.parse import quote

import httpx

try:
    from opentelemetry.context import suppress_instrumentation
except ImportError:
    @contextlib.contextmanager
    def suppress_instrumentation():  # type: ignore[misc]
        yield

from .api import (
    ApiError,
    build_text_message,
    get_config,
    get_updates,
    send_message,
    send_typing as api_send_typing,
)
from .types import (
    BotCredentials,
    BotRunState,
    BotStatus,
    IncomingMessage,
    MediaInfo,
    MessageItem,
    MessageItemType,
    MessageKind,
    MessageType,
    WeixinMessage,
)


class BotWorker:
    """Manages the long-polling loop and message handling for a single bot."""

    def __init__(
        self,
        bot_id: str,
        credentials: BotCredentials,
        session: httpx.AsyncClient,
        message_callback: Callable[[IncomingMessage], Awaitable[None]],
        error_callback: Callable[[str, Exception], Awaitable[None]],
        cursor_update_callback: Callable[[str, str], Awaitable[None]] | None = None,
    ) -> None:
        self._bot_id = bot_id
        self._credentials = credentials
        self._session = session
        self._message_callback = message_callback
        self._error_callback = error_callback
        self._cursor_update_callback = cursor_update_callback

        self._cursor = credentials.get_updates_buf
        self._context_tokens: dict[str, str] = {}
        self._state = BotRunState.STOPPED
        self._last_error: Exception | None = None
        self._last_poll_time: datetime | None = None
        self._message_count = 0
        self._task: asyncio.Task[None] | None = None

    # -- public properties ---------------------------------------------------

    @property
    def credentials(self) -> BotCredentials:
        return self._credentials

    @property
    def status(self) -> BotStatus:
        return BotStatus(
            bot_id=self._bot_id,
            state=self._state,
            error=self._last_error,
            last_poll_time=self._last_poll_time,
            message_count=self._message_count,
        )

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._state = BotRunState.RUNNING
        self._last_error = None
        self._task = asyncio.create_task(self._poll_loop())
        self._task.add_done_callback(self._on_task_done)

    async def stop(self) -> None:
        task = self._task
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self._task = None
        if self._state not in (BotRunState.SESSION_EXPIRED, BotRunState.ERROR):
            self._state = BotRunState.STOPPED

    # -- messaging -----------------------------------------------------------

    async def reply(self, message: IncomingMessage, text: str) -> None:
        self._context_tokens[message.user_id] = message._context_token
        await self._send_text(message.user_id, text, message._context_token)
        try:
            await self._stop_typing_for(message.user_id)
        except Exception:
            pass

    async def send(self, user_id: str, text: str) -> None:
        context_token = self._context_tokens.get(user_id)
        if context_token is None:
            raise RuntimeError(
                f"No cached context_token for user {user_id}. "
                "Reply to an incoming message first."
            )
        await self._send_text(user_id, text, context_token)

    async def send_typing(self, user_id: str) -> None:
        context_token = self._context_tokens.get(user_id)
        if context_token is None:
            raise RuntimeError(f"No cached context_token for user {user_id}.")

        config = await get_config(
            self._session,
            self._credentials.base_url,
            self._credentials.token,
            user_id,
            context_token,
        )
        ticket = config.get("typing_ticket")
        if not isinstance(ticket, str):
            return
        await api_send_typing(
            self._session,
            self._credentials.base_url,
            self._credentials.token,
            user_id,
            ticket,
            1,
        )

    async def stop_typing(self, user_id: str) -> None:
        await self._stop_typing_for(user_id)

    # -- internal ------------------------------------------------------------

    async def _stop_typing_for(self, user_id: str) -> None:
        context_token = self._context_tokens.get(user_id)
        if context_token is None:
            return
        config = await get_config(
            self._session,
            self._credentials.base_url,
            self._credentials.token,
            user_id,
            context_token,
        )
        ticket = config.get("typing_ticket")
        if not isinstance(ticket, str):
            return
        await api_send_typing(
            self._session,
            self._credentials.base_url,
            self._credentials.token,
            user_id,
            ticket,
            2,
        )

    async def _send_text(
        self, user_id: str, text: str, context_token: str
    ) -> None:
        if not text:
            raise ValueError("Message text cannot be empty.")
        for chunk in _chunk_text(text, 2000):
            await send_message(
                self._session,
                self._credentials.base_url,
                self._credentials.token,
                build_text_message(user_id, context_token, chunk),
            )

    async def _poll_loop(self) -> None:
        retry_delay = 1.0
        self._log("Long-poll loop started.")
        try:
            while True:
                try:
                    with suppress_instrumentation():
                        updates = await get_updates(
                            self._session,
                            self._credentials.base_url,
                            self._credentials.token,
                            self._cursor,
                            40.0,
                        )
                    new_cursor = updates.get("get_updates_buf") or self._cursor
                    cursor_changed = new_cursor != self._cursor
                    self._cursor = new_cursor
                    self._last_poll_time = datetime.now(timezone.utc)
                    retry_delay = 1.0

                    if cursor_changed and self._cursor_update_callback is not None:
                        try:
                            await self._cursor_update_callback(
                                self._bot_id, self._cursor
                            )
                        except Exception:
                            pass  # best-effort; don't break the poll loop

                    for raw in updates.get("msgs", []):
                        self._remember_context(raw)
                        incoming = self._to_incoming_message(raw)
                        if incoming is not None:
                            self._message_count += 1
                            try:
                                await self._message_callback(incoming)
                            except Exception as handler_err:
                                await self._report_error(handler_err)

                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    if _is_session_expired(err):
                        self._state = BotRunState.SESSION_EXPIRED
                        self._last_error = err
                        self._cursor = ""
                        self._context_tokens.clear()
                        self._log("Session expired.")
                        await self._report_error(err)
                        return

                    if _is_timeout(err):
                        # Timeouts are normal for long-polling; immediately retry.
                        continue

                    self._state = BotRunState.ERROR
                    self._last_error = err
                    await self._report_error(err)
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 30.0)
                    self._state = BotRunState.RUNNING
        finally:
            self._log("Long-poll loop ended.")

    def _remember_context(self, message: WeixinMessage) -> None:
        user_id = (
            message["from_user_id"]
            if message["message_type"] == MessageType.USER
            else message["to_user_id"]
        )
        context_token = message.get("context_token")
        if user_id and context_token:
            self._context_tokens[user_id] = context_token

    def _to_incoming_message(
        self, message: WeixinMessage
    ) -> IncomingMessage | None:
        if message["message_type"] != MessageType.USER:
            return None

        create_time_ms = message.get("create_time_ms", 0)
        timestamp = datetime.fromtimestamp(
            create_time_ms / 1000, tz=timezone.utc
        ).astimezone()

        return IncomingMessage(
            bot_id=self._bot_id,
            user_id=message["from_user_id"],
            text=_extract_text(message["item_list"]),
            type=_detect_type(message["item_list"]),
            raw=message,
            _context_token=message["context_token"],
            timestamp=timestamp,
            media=_extract_media_info(message["item_list"]),
        )

    async def _report_error(self, error: Exception) -> None:
        try:
            await self._error_callback(self._bot_id, error)
        except Exception:
            pass

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None and self._state == BotRunState.RUNNING:
            self._state = BotRunState.ERROR
            self._last_error = exc

    def _log(self, message: str) -> None:
        sys.stderr.write(f"[ilink-bot-server:{self._bot_id}] {message}\n")


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _detect_type(items: list[MessageItem]) -> MessageKind:
    first = items[0] if items else None
    item_type = first["type"] if first is not None else None

    if item_type == MessageItemType.IMAGE:
        return "image"
    if item_type == MessageItemType.VOICE:
        return "voice"
    if item_type == MessageItemType.FILE:
        return "file"
    if item_type == MessageItemType.VIDEO:
        return "video"
    return "text"


_CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"


def _build_cdn_url(encrypt_query_param: str) -> str:
    """Build a CDN download URL from an encrypt_query_param value."""
    return f"{_CDN_BASE}/download?encrypted_query_param={quote(encrypt_query_param)}"


def _decode_aes_key(raw_key: str) -> str:
    """Normalise an AES key to a 32-char lowercase hex string.

    The protocol delivers ``aes_key`` in two formats (§8.4):
    - Format A: base64(raw 16 bytes)  → base64-decode gives 16 bytes directly
    - Format B: base64(hex string)    → base64-decode gives 32 ASCII hex chars
    A bare 32-char hex string (``image_item.aeskey``) is also accepted.
    Returns empty string if the key cannot be decoded.
    """
    if not raw_key:
        return ""
    # Already a plain 32-char hex string (image_item.aeskey style)
    if len(raw_key) == 32 and all(c in "0123456789abcdefABCDEF" for c in raw_key):
        return raw_key.lower()
    try:
        decoded = base64.b64decode(raw_key)
    except Exception:
        return ""
    if len(decoded) == 16:
        # Format A: raw 16-byte key
        return decoded.hex()
    if len(decoded) == 32:
        # Format B: the 32 bytes are ASCII hex chars
        candidate = decoded.decode("ascii", errors="replace")
        if all(c in "0123456789abcdefABCDEF" for c in candidate):
            return candidate.lower()
    return ""


def _extract_media_info(items: list[MessageItem]) -> MediaInfo | None:
    """Extract a ``MediaInfo`` from the first CDN-backed item, or return None."""
    for item in items:
        item_type = item["type"]

        if item_type == MessageItemType.IMAGE:
            image_item = item.get("image_item") or {}
            media = image_item.get("media") or {}
            encrypt_param = media.get("encrypt_query_param", "")
            if not encrypt_param:
                continue
            # Prefer image_item.aeskey (plain hex) over media.aes_key
            raw_key = image_item.get("aeskey") or media.get("aes_key", "")
            return MediaInfo(
                download_url=_build_cdn_url(encrypt_param),
                aes_key=_decode_aes_key(raw_key),
                width=image_item.get("thumb_width", 0),
                height=image_item.get("thumb_height", 0),
            )

        if item_type == MessageItemType.VOICE:
            voice_item = item.get("voice_item") or {}
            media = voice_item.get("media") or {}
            encrypt_param = media.get("encrypt_query_param", "")
            if not encrypt_param:
                continue
            return MediaInfo(
                download_url=_build_cdn_url(encrypt_param),
                aes_key=_decode_aes_key(media.get("aes_key", "")),
            )

        if item_type == MessageItemType.FILE:
            file_item = item.get("file_item") or {}
            media = file_item.get("media") or {}
            encrypt_param = media.get("encrypt_query_param", "")
            if not encrypt_param:
                continue
            return MediaInfo(
                download_url=_build_cdn_url(encrypt_param),
                aes_key=_decode_aes_key(media.get("aes_key", "")),
                file_name=file_item.get("file_name", ""),
            )

        if item_type == MessageItemType.VIDEO:
            video_item = item.get("video_item") or {}
            media = video_item.get("media") or {}
            encrypt_param = media.get("encrypt_query_param", "")
            if not encrypt_param:
                continue
            return MediaInfo(
                download_url=_build_cdn_url(encrypt_param),
                aes_key=_decode_aes_key(media.get("aes_key", "")),
                width=video_item.get("thumb_width", 0),
                height=video_item.get("thumb_height", 0),
            )

    return None


def _extract_text(items: list[MessageItem]) -> str:
    parts: list[str] = []
    for item in items:
        item_type = item["type"]
        if item_type == MessageItemType.TEXT:
            text = item.get("text_item", {}).get("text", "")
        elif item_type == MessageItemType.IMAGE:
            image_item = item.get("image_item") or {}
            encrypt_param = (image_item.get("media") or {}).get(
                "encrypt_query_param", ""
            )
            text = _build_cdn_url(encrypt_param) if encrypt_param else "[image]"
        elif item_type == MessageItemType.VOICE:
            # Prefer transcription text; fall back to CDN URL
            voice_item = item.get("voice_item") or {}
            transcript = voice_item.get("text", "")
            if transcript:
                text = transcript
            else:
                encrypt_param = (voice_item.get("media") or {}).get(
                    "encrypt_query_param", ""
                )
                text = _build_cdn_url(encrypt_param) if encrypt_param else "[voice]"
        elif item_type == MessageItemType.FILE:
            file_item = item.get("file_item") or {}
            file_name = file_item.get("file_name", "")
            encrypt_param = (file_item.get("media") or {}).get(
                "encrypt_query_param", ""
            )
            if file_name and encrypt_param:
                text = f"[file: {file_name}] {_build_cdn_url(encrypt_param)}"
            elif encrypt_param:
                text = _build_cdn_url(encrypt_param)
            else:
                text = f"[file: {file_name}]" if file_name else "[file]"
        elif item_type == MessageItemType.VIDEO:
            encrypt_param = (
                (item.get("video_item") or {}).get("media") or {}
            ).get("encrypt_query_param", "")
            text = _build_cdn_url(encrypt_param) if encrypt_param else "[video]"
        else:
            text = ""

        if text:
            parts.append(text)

    return "\n".join(parts)


def _chunk_text(text: str, limit: int) -> list[str]:
    chunks = [text[i : i + limit] for i in range(0, len(text), limit)]
    return chunks or [""]


def _is_timeout(error: object) -> bool:
    return isinstance(error, httpx.TimeoutException)


def _is_session_expired(error: object) -> bool:
    return isinstance(error, ApiError) and error.is_session_expired
