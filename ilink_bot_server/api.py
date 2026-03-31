from __future__ import annotations

import base64
import json
import os
from typing import Any, Literal, cast
from urllib.parse import quote, urljoin
from uuid import uuid4

import httpx

from .types import (
    BaseInfo,
    GetConfigResponse,
    GetUpdatesRequest,
    GetUpdatesResponse,
    MessageItemType,
    MessageState,
    MessageType,
    QrCodeResponse,
    QrStatusResponse,
    SendMessageMessage,
    SendTypingRequest,
)

DEFAULT_BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "1.0.0"


class ApiError(Exception):
    """Raised when an API call returns an error status or business error code."""

    def __init__(
        self,
        message: str,
        *,
        status: int,
        code: int | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.payload = payload

    @property
    def is_session_expired(self) -> bool:
        return self.code == -14


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalize_base_url(base_url: str) -> str:
    return base_url.rstrip("/")


def _build_base_info() -> BaseInfo:
    return {"channel_version": CHANNEL_VERSION}


def random_wechat_uin() -> str:
    value = int.from_bytes(os.urandom(4), "big")
    return base64.b64encode(str(value).encode("utf-8")).decode("ascii")


def build_headers(token: str) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "Authorization": f"Bearer {token}",
        "X-WECHAT-UIN": random_wechat_uin(),
    }


def _parse_json_response(
    response: httpx.Response, label: str
) -> dict[str, Any]:
    text = response.text
    payload = cast(dict[str, Any], json.loads(text) if text else {})

    if response.status_code < 200 or response.status_code >= 300:
        message = payload.get("errmsg") or f"{label} failed with HTTP {response.status_code}"
        raise ApiError(
            message,
            status=response.status_code,
            code=payload.get("errcode"),
            payload=payload,
        )

    if isinstance(payload.get("ret"), int) and payload["ret"] != 0:
        raise ApiError(
            payload.get("errmsg") or f"{label} failed",
            status=response.status_code,
            code=cast(int | None, payload.get("errcode", payload["ret"])),
            payload=payload,
        )

    return payload


async def api_post(
    client: httpx.AsyncClient,
    base_url: str,
    endpoint: str,
    body: object,
    token: str,
    timeout_s: float = 40.0,
) -> dict[str, Any]:
    url = urljoin(f"{_normalize_base_url(base_url)}/", endpoint.lstrip("/"))
    response = await client.post(
        url, headers=build_headers(token), json=body, timeout=timeout_s
    )
    return _parse_json_response(response, endpoint)


async def api_get(
    client: httpx.AsyncClient,
    base_url: str,
    path: str,
    headers: dict[str, str] | None = None,
    timeout_s: float = 15.0,
) -> dict[str, Any]:
    url = urljoin(f"{_normalize_base_url(base_url)}/", path.lstrip("/"))
    response = await client.get(url, headers=headers or {}, timeout=timeout_s)
    return _parse_json_response(response, path)


# ---------------------------------------------------------------------------
# Business API functions
# ---------------------------------------------------------------------------


async def get_updates(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    buf: str,
    timeout_s: float = 40.0,
) -> GetUpdatesResponse:
    body: GetUpdatesRequest = {
        "get_updates_buf": buf,
        "base_info": _build_base_info(),
    }
    payload = await api_post(
        client, base_url, "/ilink/bot/getupdates", body, token, timeout_s
    )
    return cast(GetUpdatesResponse, payload)


async def send_message(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    msg: SendMessageMessage,
) -> dict[str, Any]:
    return await api_post(
        client,
        base_url,
        "/ilink/bot/sendmessage",
        {"msg": msg, "base_info": _build_base_info()},
        token,
        15.0,
    )


async def get_config(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    user_id: str,
    context_token: str,
) -> GetConfigResponse:
    payload = await api_post(
        client,
        base_url,
        "/ilink/bot/getconfig",
        {
            "ilink_user_id": user_id,
            "context_token": context_token,
            "base_info": _build_base_info(),
        },
        token,
        15.0,
    )
    return cast(GetConfigResponse, payload)


async def send_typing(
    client: httpx.AsyncClient,
    base_url: str,
    token: str,
    user_id: str,
    ticket: str,
    status: Literal[1, 2],
) -> dict[str, Any]:
    body: SendTypingRequest = {
        "ilink_user_id": user_id,
        "typing_ticket": ticket,
        "status": status,
        "base_info": _build_base_info(),
    }
    return await api_post(
        client, base_url, "/ilink/bot/sendtyping", body, token, 15.0
    )


async def fetch_qr_code(
    client: httpx.AsyncClient, base_url: str
) -> QrCodeResponse:
    payload = await api_get(
        client, base_url, "/ilink/bot/get_bot_qrcode?bot_type=3"
    )
    return cast(QrCodeResponse, payload)


async def poll_qr_status(
    client: httpx.AsyncClient, base_url: str, qrcode: str
) -> QrStatusResponse:
    payload = await api_get(
        client,
        base_url,
        f"/ilink/bot/get_qrcode_status?qrcode={quote(qrcode, safe='')}",
        {"iLink-App-ClientVersion": "1"},
        timeout_s=40.0,
    )
    return cast(QrStatusResponse, payload)


# ---------------------------------------------------------------------------
# Message construction helpers
# ---------------------------------------------------------------------------


def build_text_message(
    user_id: str, context_token: str, text: str
) -> SendMessageMessage:
    return {
        "from_user_id": "",
        "to_user_id": user_id,
        "client_id": str(uuid4()),
        "message_type": MessageType.BOT,
        "message_state": MessageState.FINISH,
        "context_token": context_token,
        "item_list": [
            {
                "type": MessageItemType.TEXT,
                "text_item": {"text": text},
            }
        ],
    }
