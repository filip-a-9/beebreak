"""Which server URLs the companion may talk to (spec §4 rule 9). Owned by M3-04."""


class EndpointRefused(ValueError):
    """The configured server URL is not allowed. The message never echoes it."""


def validate_server_url(url: str) -> str:
    """Canonical `scheme://host[:port]`; https, or http only for loopback."""
    raise NotImplementedError
