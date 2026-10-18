"""Which server URLs the companion may talk to (spec §4 rule 9). Owned by M3-04."""

import re
from urllib.parse import urlsplit

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
_IPV6_RE = re.compile(r"^[0-9a-f:]+$")
_REFUSED = "server URL is not allowed"


class EndpointRefused(ValueError):
    """The configured server URL is not allowed. The message never echoes it."""


def validate_server_url(url: str) -> str:
    """Canonical `scheme://host[:port]`; https, or http only for loopback."""
    try:
        return _validate(url)
    except ValueError:
        # Fixed message: parser errors can quote the input.
        raise EndpointRefused(_REFUSED) from None


def _validate(url: str) -> str:
    if not url or not url.isascii() or not url.isprintable() or " " in url:
        raise EndpointRefused(_REFUSED)
    if any(c in url for c in "\\?#@"):
        raise EndpointRefused(_REFUSED)
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise EndpointRefused(_REFUSED)
    if parts.path not in ("", "/"):
        raise EndpointRefused(_REFUSED)
    host = parts.hostname
    port = parts.port  # a bad port raises ValueError, mapped by the caller
    if not host:
        raise EndpointRefused(_REFUSED)
    is_v6 = ":" in host
    pattern = _IPV6_RE if is_v6 else _HOST_RE
    if not pattern.match(host):
        raise EndpointRefused(_REFUSED)
    if scheme == "http" and host not in _LOOPBACK_HOSTS:
        raise EndpointRefused(_REFUSED)
    shown = f"[{host}]" if is_v6 else host
    return f"{scheme}://{shown}" + (f":{port}" if port is not None else "")
