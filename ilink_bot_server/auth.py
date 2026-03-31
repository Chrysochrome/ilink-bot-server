from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable

import httpx

from .api import DEFAULT_BASE_URL, fetch_qr_code, poll_qr_status
from .types import BotCredentials, LoginStatus

QR_POLL_INTERVAL_S = 2.0


def _log(message: str) -> None:
    sys.stderr.write(f"[ilink-bot-server] {message}\n")


async def fetch_qr_url(
    client: httpx.AsyncClient,
    base_url: str = DEFAULT_BASE_URL,
) -> tuple[str, str]:
    """Fetch a QR code and return ``(qr_url, qrcode_token)``.

    *qr_url* is the link the user should open in WeChat.
    *qrcode_token* is passed to :func:`poll_login` for polling.
    """
    qr = await fetch_qr_code(client, base_url)
    return qr["qrcode_img_content"], qr["qrcode"]


async def poll_login(
    client: httpx.AsyncClient,
    bot_id: str,
    qrcode_token: str,
    on_status: Callable[[LoginStatus], Awaitable[None]],
    base_url: str = DEFAULT_BASE_URL,
    timeout_s: float = 120.0,
) -> None:
    """Poll the QR login status, firing *on_status* callbacks throughout.

    Called as a background task by :meth:`BotServer.start_login`.
    Fires events: (``scaned``) → ``confirmed`` | ``expired`` | ``timeout`` | ``error``.
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout_s

    last_status: str | None = None

    while True:
        if asyncio.get_event_loop().time() >= deadline:
            await on_status(LoginStatus(bot_id=bot_id, status="timeout"))
            return

        try:
            qr_status = await poll_qr_status(client, base_url, qrcode_token)
        except httpx.TimeoutException:
            # Long-poll timeout is normal; treat like {"status": "wait"}.
            continue
        except Exception as exc:
            await on_status(LoginStatus(bot_id=bot_id, status="error", error=exc))
            return

        current = qr_status["status"]

        if current != last_status:
            last_status = current
            if current == "scaned":
                await on_status(LoginStatus(bot_id=bot_id, status="scaned"))
            elif current == "expired":
                await on_status(LoginStatus(bot_id=bot_id, status="expired"))
                return

        if current == "confirmed":
            token = qr_status.get("bot_token")
            account_id = qr_status.get("ilink_bot_id")
            user_id = qr_status.get("ilink_user_id")
            if (
                not isinstance(token, str)
                or not isinstance(account_id, str)
                or not isinstance(user_id, str)
            ):
                await on_status(
                    LoginStatus(
                        bot_id=bot_id,
                        status="error",
                        error=RuntimeError(
                            "QR login confirmed but credentials were not returned"
                        ),
                    )
                )
                return
            credentials = BotCredentials(
                token=token,
                base_url=qr_status.get("baseurl") or base_url,
                account_id=account_id,
                user_id=user_id,
            )
            await on_status(
                LoginStatus(bot_id=bot_id, status="confirmed", credentials=credentials)
            )
            return

        await asyncio.sleep(QR_POLL_INTERVAL_S)


async def login(base_url: str = DEFAULT_BASE_URL) -> BotCredentials:
    """Interactive QR login utility for standalone credential setup.

    Prints the QR URL to *stderr* and polls until the user scans and confirms.
    Returns :class:`BotCredentials` on success.

    This is a standalone helper intended for initial credential setup (see
    ``examples/1_login.py``).  For in-app login use :meth:`BotServer.start_login`.
    """
    async with httpx.AsyncClient() as client:
        while True:
            qr = await fetch_qr_code(client, base_url)
            _log("在微信中打开以下链接完成登录:")
            sys.stderr.write(f"{qr['qrcode_img_content']}\n")

            last_status: str | None = None

            while True:
                try:
                    status = await poll_qr_status(client, base_url, qr["qrcode"])
                except httpx.TimeoutException:
                    continue

                if status["status"] != last_status:
                    if status["status"] == "scaned":
                        _log("QR code scanned. Confirm the login inside WeChat.")
                    elif status["status"] == "confirmed":
                        _log("Login confirmed.")
                    elif status["status"] == "expired":
                        _log("QR code expired. Requesting a new one...")
                    last_status = status["status"]

                if status["status"] == "confirmed":
                    token = status.get("bot_token")
                    account_id = status.get("ilink_bot_id")
                    user_id = status.get("ilink_user_id")
                    if (
                        not isinstance(token, str)
                        or not isinstance(account_id, str)
                        or not isinstance(user_id, str)
                    ):
                        raise RuntimeError(
                            "QR login confirmed, but the API did not return bot credentials"
                        )
                    return BotCredentials(
                        token=token,
                        base_url=status.get("baseurl") or base_url,
                        account_id=account_id,
                        user_id=user_id,
                    )

                if status["status"] == "expired":
                    break

                await asyncio.sleep(QR_POLL_INTERVAL_S)
