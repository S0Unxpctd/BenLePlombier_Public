"""Souffl.AI V3 — WhatsApp Cloud API media I/O.

Inbound (two-step, per Meta docs):
    1. ``GET {base}/{media_id}`` → returns ``{"url": "https://lookaside.fbsbx.com/…"}``
    2. ``GET {url}`` with ``Authorization: Bearer {token}`` → returns binary bytes.
We write the bytes to a ``NamedTemporaryFile`` and return its path; callers
must ``os.unlink`` it when done (or use ``download_media`` as a context manager).

Outbound (two-step for anything other than text):
    1. ``POST {base}/{phone_number_id}/media`` (multipart) → returns ``{"id": …}``
    2. Caller sends a message referencing that id (done by ``send.py``).

Design notes
------------
- The HTTP client is ``httpx`` (already in ``requirements.txt``) using a short
  timeout budget and a small exponential backoff on 429/5xx.
- Mime types are passed through from the webhook payload so WhatsApp's
  codec/container negotiation is preserved (OGG Opus for voice notes, etc.).
- All logs redact the token.

Public API
----------
- ``download_media(media_id, *, client=None)`` — context manager yielding a path
- ``download_media_to_path(media_id, dest_path, *, client=None)`` — writes and returns bytes_written
- ``upload_media(file_path, mime_type, *, client=None)`` — returns the uploaded media_id
"""

from __future__ import annotations

import contextlib
import logging
import mimetypes
import os
import tempfile
from pathlib import Path
from typing import Iterator, Optional

import httpx

from .config import WhatsAppConfig, get_config


logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Error taxonomy
# ─────────────────────────────────────────────────────────────────────────────


class MediaError(RuntimeError):
    """Base for all media-layer errors."""


class MediaMetadataError(MediaError):
    """``GET /{media_id}`` failed (non-2xx or no ``url`` in payload)."""


class MediaDownloadError(MediaError):
    """``GET {media_url}`` failed after retries."""


class MediaUploadError(MediaError):
    """``POST /{phone_number_id}/media`` failed."""


# ─────────────────────────────────────────────────────────────────────────────
# Inbound
# ─────────────────────────────────────────────────────────────────────────────


_RETRY_STATUS = {429, 500, 502, 503, 504}


def _sleep_backoff(attempt: int) -> None:
    """Tiny exponential backoff. Kept synchronous so tests can monkeypatch time."""
    import time

    time.sleep(min(2 ** attempt, 5))


def download_media_to_path(
    media_id: str,
    dest_path: str,
    *,
    client: Optional[httpx.Client] = None,
    cfg: Optional[WhatsAppConfig] = None,
    max_attempts: int = 3,
) -> int:
    """Download an inbound media by id, writing it to ``dest_path``.

    Returns the number of bytes written. Raises on failure after ``max_attempts``.
    """
    cfg = cfg or get_config()
    cfg.require("token")
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        headers = {"Authorization": f"Bearer {cfg.token}"}
        # Step 1 — resolve the temporary URL
        meta_url = cfg.media_metadata_url(media_id)
        last_exc: Optional[Exception] = None
        url: Optional[str] = None
        for attempt in range(max_attempts):
            try:
                r = client.get(meta_url, headers=headers)
                if r.status_code == 200:
                    data = r.json()
                    url = data.get("url")
                    if not url:
                        raise MediaMetadataError(
                            f"Media metadata missing 'url': {data!r}"
                        )
                    break
                if r.status_code in _RETRY_STATUS and attempt < max_attempts - 1:
                    _sleep_backoff(attempt)
                    continue
                raise MediaMetadataError(
                    f"GET {media_id} returned HTTP {r.status_code}: {r.text[:200]}"
                )
            except httpx.HTTPError as exc:  # network error
                last_exc = exc
                if attempt < max_attempts - 1:
                    _sleep_backoff(attempt)
                    continue
                raise MediaMetadataError(f"HTTP error fetching metadata: {exc}") from exc
        if url is None:
            raise MediaMetadataError(
                f"Media {media_id!r}: failed to resolve URL after {max_attempts} attempts"
                + (f" ({last_exc})" if last_exc else "")
            )

        # Step 2 — fetch the binary (same retry policy)
        bytes_written = 0
        for attempt in range(max_attempts):
            try:
                r = client.get(url, headers=headers)
                if r.status_code == 200:
                    with open(dest_path, "wb") as f:
                        f.write(r.content)
                        bytes_written = len(r.content)
                    return bytes_written
                if r.status_code in _RETRY_STATUS and attempt < max_attempts - 1:
                    _sleep_backoff(attempt)
                    continue
                raise MediaDownloadError(
                    f"GET {url!s} returned HTTP {r.status_code}"
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                if attempt < max_attempts - 1:
                    _sleep_backoff(attempt)
                    continue
                raise MediaDownloadError(
                    f"HTTP error downloading media: {exc}"
                ) from exc
        raise MediaDownloadError(
            f"Media {media_id!r}: download failed after {max_attempts} attempts"
        )
    finally:
        if owns_client:
            client.close()


@contextlib.contextmanager
def download_media(
    media_id: str,
    *,
    client: Optional[httpx.Client] = None,
    cfg: Optional[WhatsAppConfig] = None,
    suffix: str = "",
) -> Iterator[str]:
    """Context manager downloading ``media_id`` to a temp file.

    Usage::

        with download_media(mid, suffix=".ogg") as path:
            transcription = transcribe_audio(path)
    """
    fd, path = tempfile.mkstemp(prefix="wa_media_", suffix=suffix)
    os.close(fd)
    try:
        download_media_to_path(media_id, path, client=client, cfg=cfg)
        yield path
    finally:
        try:
            os.unlink(path)
        except OSError:
            # Best-effort cleanup; log and continue.
            logger.warning("[media] failed to unlink %s", path)


# ─────────────────────────────────────────────────────────────────────────────
# Outbound
# ─────────────────────────────────────────────────────────────────────────────


def upload_media(
    file_path: str,
    mime_type: Optional[str] = None,
    *,
    client: Optional[httpx.Client] = None,
    cfg: Optional[WhatsAppConfig] = None,
) -> str:
    """Upload ``file_path`` to Meta and return the resulting ``media_id``.

    The caller (typically ``send.send_document``) then references that id in
    the ``messages`` payload.
    """
    cfg = cfg or get_config()
    cfg.require("token", "phone_number_id")
    if not os.path.exists(file_path):
        raise MediaUploadError(f"File not found: {file_path}")
    if mime_type is None:
        mime_type, _ = mimetypes.guess_type(file_path)
        if mime_type is None:
            mime_type = "application/octet-stream"

    owns_client = client is None
    client = client or httpx.Client(timeout=60.0)
    try:
        headers = {"Authorization": f"Bearer {cfg.token}"}
        with open(file_path, "rb") as f:
            files = {"file": (Path(file_path).name, f, mime_type)}
            data = {"messaging_product": "whatsapp", "type": mime_type}
            r = client.post(
                cfg.media_upload_url(), headers=headers, files=files, data=data
            )
        if r.status_code != 200:
            raise MediaUploadError(
                f"Upload failed: HTTP {r.status_code} — {r.text[:200]}"
            )
        payload = r.json()
        media_id = payload.get("id")
        if not media_id:
            raise MediaUploadError(f"Upload response missing 'id': {payload!r}")
        return media_id
    finally:
        if owns_client:
            client.close()


__all__ = [
    "MediaError",
    "MediaMetadataError",
    "MediaDownloadError",
    "MediaUploadError",
    "download_media",
    "download_media_to_path",
    "upload_media",
]
