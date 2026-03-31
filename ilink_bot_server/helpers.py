"""helpers.py - Utility helpers for working with incoming media messages."""

from __future__ import annotations

import binascii

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from .types import IncomingMessage, MediaInfo


async def download_media(
    msg: IncomingMessage,
    client: httpx.AsyncClient | None = None,
) -> bytes:
    """Download and decrypt the media attached to *msg*.

    Parameters
    ----------
    msg:
        An incoming message whose ``type`` is one of ``"image"``,
        ``"voice"``, ``"file"``, or ``"video"``.
    client:
        An existing ``httpx.AsyncClient`` to reuse.  When *None* a
        temporary client is created and closed automatically.

    Returns
    -------
    bytes
        The decrypted (plaintext) media content.

    Raises
    ------
    ValueError
        If *msg* does not carry media (``msg.media`` is ``None``).
    httpx.HTTPStatusError
        If the CDN returns a non-2xx status.
    """
    if msg.media is None:
        raise ValueError(
            f"Message from {msg.user_id} has no media "
            f"(type={msg.type!r}).  Only image/voice/file/video messages "
            "carry a CDN attachment."
        )
    return await download_media_info(msg.media, client=client)


async def download_media_info(
    media: MediaInfo,
    client: httpx.AsyncClient | None = None,
) -> bytes:
    """Download and decrypt a ``MediaInfo`` object directly.

    This is the lower-level variant used by :func:`download_media`.
    """
    own_client = client is None
    if own_client:
        client = httpx.AsyncClient()

    try:
        resp = await client.get(media.download_url)
        resp.raise_for_status()
        ciphertext = resp.content
    finally:
        if own_client:
            await client.aclose()

    if not media.aes_key:
        # No key supplied — return raw bytes as-is
        return ciphertext

    return _decrypt_aes_ecb(ciphertext, media.aes_key)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _decrypt_aes_ecb(ciphertext: bytes, aes_key_hex: str) -> bytes:
    """Decrypt *ciphertext* with AES-128-ECB + PKCS7 padding.

    Parameters
    ----------
    ciphertext:
        Raw encrypted bytes from the CDN.
    aes_key_hex:
        32-character lowercase hex string representing the 16-byte AES key.
    """
    try:
        key = binascii.unhexlify(aes_key_hex)
    except (ValueError, binascii.Error) as exc:
        raise ValueError(f"Invalid AES key hex string: {aes_key_hex!r}") from exc

    if len(key) != 16:
        raise ValueError(
            f"Expected a 16-byte AES key, got {len(key)} bytes "
            f"(hex: {aes_key_hex!r})"
        )

    cipher = Cipher(algorithms.AES128(key), modes.ECB())
    decryptor = cipher.decryptor()
    padded = decryptor.update(ciphertext) + decryptor.finalize()

    unpadder = PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()
