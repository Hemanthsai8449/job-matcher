import hashlib
import ipaddress
import re
import secrets
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import phonenumbers

TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def normalize_phone(phone: str, default_region: str = "IN") -> str:
    raw = phone.strip()
    try:
        parsed = phonenumbers.parse(raw, None if raw.startswith("+") else default_region)
    except phonenumbers.NumberParseException as exc:
        raise ValueError("Enter a valid mobile number with country code.") from exc
    if not phonenumbers.is_valid_number(parsed):
        raise ValueError("Enter a valid mobile number with country code.")
    return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)


def generate_numeric_code(length: int = 6) -> str:
    return "".join(secrets.choice("0123456789") for _ in range(length))


def generate_token(bytes_count: int = 24) -> str:
    return secrets.token_urlsafe(bytes_count)


def hash_token(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.netloc:
        raise ValueError("Only valid HTTP or HTTPS job links are allowed.")
    host = parts.hostname.lower() if parts.hostname else ""
    if not host or host == "localhost" or host.endswith((".local", ".internal")):
        raise ValueError("Job links must use a public website.")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Job links must use a public website.")
    port = parts.port
    netloc = host
    default_port = (parts.scheme.lower() == "http" and port == 80) or (
        parts.scheme.lower() == "https" and port == 443
    )
    if port and not default_port:
        netloc = f"{host}:{port}"
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lowered = key.lower()
        if lowered.startswith("utm_") or lowered in TRACKING_PARAMETERS:
            continue
        query.append((key, value))
    path = re.sub(r"/{2,}", "/", parts.path or "/")
    return urlunsplit((parts.scheme.lower(), netloc, path.rstrip("/") or "/", urlencode(query), ""))


def is_safe_relative_url(target: str | None) -> bool:
    """Accept only local absolute paths for post-action redirects."""

    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return False
    if any(ord(character) < 32 for character in target):
        return False
    parts = urlsplit(target)
    return not parts.scheme and not parts.netloc


def safe_initials(value: str, fallback: str = "JM") -> str:
    words = [part for part in re.split(r"\s+", value.strip()) if part]
    return "".join(word[0].upper() for word in words[:2]) or fallback
