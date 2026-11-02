"""Pure durable-ID and WhatsApp identity primitives shared by contracts."""

from __future__ import annotations

import hashlib
import re
from typing import Final

_ID_PREFIX: Final = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
_DIRECT_JID: Final = re.compile(r"^([1-9][0-9]{4,31})@s\.whatsapp\.net$")
# WhatsApp LinkedID (LID): a privacy-protected numeric identifier WhatsApp
# assigns to business-adjacent accounts.  It is NOT a phone number and cannot
# be resolved back to one; real tenants (mariaeduarda/eduarda, genius) address
# 95%+ of their traffic through "@lid" JIDs, which must be handled as
# first-class direct JIDs (persisted and fetched by the full JID, not
# normalized to a phone).  Observed LID local parts are 12-15 digits.
_LID_JID: Final = re.compile(r"^([1-9][0-9]{4,31})@lid$")
_GROUP_JID: Final = re.compile(r"^([1-9][0-9]{4,31}-[0-9]{1,31})@g\.us$")


def stable_id(prefix: str, *parts: object) -> str:
    """Return ``prefix:SHA256`` over UTF-8 parts separated by one NUL byte."""
    if _ID_PREFIX.fullmatch(prefix) is None:
        raise ValueError("invalid durable ID prefix")
    if not parts:
        raise ValueError("stable IDs require at least one identity part")
    normalized_parts = [str(part) for part in parts]
    if any("\0" in part for part in normalized_parts):
        raise ValueError("durable ID parts cannot contain NUL")
    encoded_parts = [part.encode("utf-8") for part in normalized_parts]
    digest = hashlib.sha256(b"\0".join(encoded_parts)).hexdigest()
    return f"{prefix}:{digest}"


def normalize_phone(remote_jid: str) -> str:
    """Validate a supported WhatsApp JID and return its normalized local identity.

    Accepts direct ``<number>@s.whatsapp.net``, LinkedID ``<lid>@lid`` and
    group ``<number>-<id>@g.us`` JIDs.  For a LID the returned local identity
    is the LID itself (stable, PII-free, never a phone number); sending to a
    LID must use the full ``@lid`` JID (see outbox).
    """
    match = (
        _DIRECT_JID.fullmatch(remote_jid)
        or _LID_JID.fullmatch(remote_jid)
        or _GROUP_JID.fullmatch(remote_jid)
    )
    if match is None:
        raise ValueError("malformed or unsupported WhatsApp JID")
    return match.group(1)


def direct_jids_equivalent(requested_jid: str, provider_jid: str) -> bool:
    """Match exact direct JIDs or Evolution's narrow Brazilian phone alias."""
    if requested_jid == provider_jid:
        return True
    # Linked IDs are opaque: only an exact match proves the same recipient.
    if requested_jid.endswith("@lid") or provider_jid.endswith("@lid"):
        return False
    if not (
        requested_jid.endswith("@s.whatsapp.net")
        and provider_jid.endswith("@s.whatsapp.net")
    ):
        return False
    try:
        requested = normalize_phone(requested_jid)
        provider = normalize_phone(provider_jid)
    except ValueError:
        return False
    if not (requested.startswith("55") and provider.startswith("55")):
        return False

    requested_aliases = {requested}
    if len(requested) == 13 and requested[4] == "9":
        requested_aliases.add(requested[:4] + requested[5:])
    elif len(requested) == 12:
        requested_aliases.add(requested[:4] + "9" + requested[4:])
    return provider in requested_aliases


def brazilian_mobile_jid_variants(jid: str) -> tuple[str, ...]:
    """Return ``jid`` plus its Brazilian mobile twin (with/without the ninth digit).

    The same handset shows up as ``55<DDD>9<8 digits>`` (what a person dials, 13
    digits) and as ``55<DDD><8 digits>`` (12 digits, what the Cloud API and some
    providers store).  A twin is produced only for a direct ``@s.whatsapp.net`` JID
    that starts with 55, has a two-digit area code and is a mobile number: the 13
    digit form needs a ``9`` right after the area code, and in both forms the
    eight-digit subscriber number must start in 6-9 (landline-looking numbers,
    starting in 2-5, are never aliased).  Any other JID (other countries, LIDs, groups, landlines) comes back
    alone.  The input form is always first.
    """
    match = _DIRECT_JID.fullmatch(jid)
    if match is None:
        return (jid,)
    digits = match.group(1)
    if not digits.startswith("55") or not (digits[2] in "123456789" and digits[3] in "123456789"):
        return (jid,)
    suffix = "@s.whatsapp.net"
    if len(digits) == 13 and digits[4] == "9" and digits[5] in "6789":
        return (jid, digits[:4] + digits[5:] + suffix)
    if len(digits) == 12 and digits[4] in "6789":
        return (jid, digits[:4] + "9" + digits[4:] + suffix)
    return (jid,)
