"""HMAC signature verification helpers."""

import hashlib
import hmac
import time


def verify_signature(body: bytes, header_value: str | None, secret: str) -> bool:
    """Verify Evolution webhook signature (signs body only, no timestamp)."""
    if header_value is None or not header_value.startswith("sha256="):
        return False

    expected = "sha256=" + hmac.new(
        secret.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, header_value)


def verify_control_signature(
    body: bytes,
    timestamp: str | None,
    signature: str | None,
    secret: str,
    *,
    now: int | None = None,
    max_age_seconds: int = 300,
) -> bool:
    """Verify a timestamp-prefixed control/webapp signature.

    Signs ``timestamp.body`` — the body is empty (b"") for GET requests.
    """
    if timestamp is None or signature is None:
        return False
    try:
        issued_at = int(timestamp)
    except ValueError:
        return False
    current_time = int(time.time()) if now is None else now
    if (
        str(issued_at) != timestamp
        or issued_at > current_time
        or current_time - issued_at > max_age_seconds
    ):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(signature, expected)


def verify_zernio_signature(body: bytes, header_value: str | None, secret: str) -> bool:
    """Verify ``X-Zernio-Signature``: lowercase hex HMAC-SHA256 of the raw body."""
    if not header_value or not secret:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(
        expected.encode("ascii"), header_value.strip().encode("utf-8")
    )
