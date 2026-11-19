"""Download and read passive evidence from Feishu Mail attachments."""

from __future__ import annotations

from email.message import Message
from hashlib import sha256
from http.client import HTTPException, HTTPSConnection
from mimetypes import guess_type
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit, urlunsplit

AttachmentKind = Literal["image", "text"]

_DOWNLOAD_HOSTS = {
    "internal-api-drive-stream.feishu.cn",
    "api-drive-stream.feishu.cn",
    "internal-api-drive-stream.larksuite.com",
    "api-drive-stream.larksuite.com",
    "api-drive-stream.blmpb.com",
}


def attachment_kind(filename: str, media_type: str) -> AttachmentKind | None:
    mime = media_type.partition(";")[0].strip().casefold()
    if mime in {"", "application/octet-stream"}:
        mime = guess_type(filename)[0] or ""
    if mime in {"image/png", "image/jpeg", "image/webp"}:
        return "image"
    if mime.startswith("text/") or mime in {"application/json", "application/xml"}:
        return "text"
    return None


def download_mail_attachment(url: str, limit: int) -> bytes | None:
    """Read only the signed Feishu stream without following redirects."""

    try:
        address = urlsplit(url)
        host = address.hostname
        if (
            address.scheme != "https"
            or host is None
            or host not in _DOWNLOAD_HOSTS
            or address.username is not None
            or address.port not in (None, 443)
        ):
            return None
        target = urlunsplit(("", "", address.path or "/", address.query, ""))
        connection = HTTPSConnection(host, timeout=8)
        try:
            connection.request("GET", target)
            response = connection.getresponse()
            if response.status != 200:
                return None
            content = response.read(limit + 1)
            length = response.getheader("Content-Length", "")
            if len(content) > limit or (length.isdecimal() and len(content) != int(length)):
                return None
            return content
        finally:
            connection.close()
    except (OSError, HTTPException, ValueError):
        return None


def attachment_evidence(
    content: bytes, *, kind: AttachmentKind, media_type: str, directory: Path
) -> dict[str, str]:
    """Return decoded text or the name of a turn-scoped image file."""

    if kind == "image":
        suffix = _image_suffix(content)
        if suffix is None:
            return {"reason": "Invalid image data."}
        digest = sha256(content).hexdigest()
        path = directory / f"mail-{digest}{suffix}"
        try:
            if not path.exists():
                path.write_bytes(content)
        except OSError:
            return {"reason": "Could not prepare the image for this turn."}
        return {"image": path.name}

    header = Message()
    header["Content-Type"] = media_type
    charset = header.get_content_charset()
    if charset is None or charset in {"utf-8", "utf8"}:
        charset = "utf-16" if content.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    try:
        text = content.decode(charset)
    except (ValueError, LookupError):
        return {"reason": "Could not decode the attachment as text."}
    if "\x00" in text:
        return {"reason": "The attachment contains binary data."}
    return {"content": text}


def _image_suffix(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpeg"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    return None
