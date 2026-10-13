import pytest

from breakroom_companion.endpoint import EndpointRefused, validate_server_url


@pytest.mark.parametrize(
    ("url", "origin"),
    [
        ("http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("http://localhost", "http://localhost"),
        ("http://LOCALHOST:9/", "http://localhost:9"),
        ("http://[::1]:8000", "http://[::1]:8000"),
        ("https://x.example", "https://x.example"),
        ("HTTPS://X.EXAMPLE:443/", "https://x.example:443"),
    ],
)
def test_accepted(url: str, origin: str) -> None:
    assert validate_server_url(url) == origin


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com",
        "https://u:p@x",
        "https://u@x.example",
        "https://x/path",
        "https://x.example/a/",
        "https://x.example?q",
        "https://x.example/?q=1",
        "https://x.example#f",
        "ftp://x.example",
        "http://0.0.0.0",
        "http://127.0.0.1.nip.io",
        "http://localhost.evil.example",
        "https://x.example:notaport",
        "https://x.example:99999",
        "https://",
        "https://x .example",
        "https://x.example\\@evil",
        "https://exämple.test",
        "x.example",
        "",
    ],
)
def test_refused_without_echoing_input(url: str) -> None:
    with pytest.raises(EndpointRefused) as info:
        validate_server_url(url)

    message = str(info.value)
    assert not url or url not in message
    assert "x.example" not in message
    assert "example.com" not in message
