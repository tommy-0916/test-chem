"""Network guards shared by model-facing web and PDF fetchers."""

from __future__ import annotations

import ipaddress
import socket
import urllib.parse
import urllib.request
from typing import Any, Callable, Iterable


class UnsafeRemoteUrl(ValueError):
    """Raised when a user-controlled URL could reach a non-public address."""


Resolver = Callable[..., Iterable[Any]]
UrlValidator = Callable[[str], None]


def validate_public_http_url(
    url: str,
    *,
    resolver: Resolver = socket.getaddrinfo,
) -> None:
    """Require HTTP(S) and ensure every resolved address is globally routable."""
    cleaned = str(url or "").strip()
    if not cleaned or len(cleaned) > 4096:
        raise UnsafeRemoteUrl("URL is empty or too long")
    parsed = urllib.parse.urlsplit(cleaned)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise UnsafeRemoteUrl("URL must use http or https")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafeRemoteUrl("URLs containing credentials are not allowed")

    hostname = parsed.hostname.rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith((".localhost", ".local")):
        raise UnsafeRemoteUrl("local hostnames are not allowed")

    try:
        literal = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        if not literal.is_global:
            raise UnsafeRemoteUrl("private or local addresses are not allowed")
        return

    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as exc:
        raise UnsafeRemoteUrl("URL contains an invalid port") from exc
    try:
        answers = list(resolver(hostname, port, type=socket.SOCK_STREAM))
    except (OSError, UnicodeError) as exc:
        raise UnsafeRemoteUrl(f"hostname could not be resolved: {exc}") from exc
    if not answers:
        raise UnsafeRemoteUrl("hostname resolved to no addresses")

    addresses = {
        str(answer[4][0]).split("%", 1)[0]
        for answer in answers
        if len(answer) >= 5 and answer[4]
    }
    if not addresses:
        raise UnsafeRemoteUrl("hostname resolved to no addresses")
    for address in addresses:
        try:
            parsed_address = ipaddress.ip_address(address)
        except ValueError as exc:
            raise UnsafeRemoteUrl("resolver returned an invalid address") from exc
        if not parsed_address.is_global:
            raise UnsafeRemoteUrl(
                f"hostname resolves to a non-public address ({address})"
            )


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Revalidate every redirect before urllib follows it."""

    def __init__(self, validator: UrlValidator = validate_public_http_url) -> None:
        super().__init__()
        self.validator = validator

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        resolved = urllib.parse.urljoin(req.full_url, newurl)
        self.validator(resolved)
        redirected = super().redirect_request(req, fp, code, msg, headers, resolved)
        if redirected is not None and self._origin(req.full_url) != self._origin(resolved):
            for name, _ in list(redirected.header_items()):
                if self._is_sensitive_header(name):
                    redirected.remove_header(name)
        return redirected

    @staticmethod
    def _origin(url: str) -> tuple[str, str, int]:
        parsed = urllib.parse.urlsplit(url)
        scheme = parsed.scheme.lower()
        port = parsed.port or (443 if scheme == "https" else 80)
        return scheme, (parsed.hostname or "").lower(), port

    @staticmethod
    def _is_sensitive_header(name: str) -> bool:
        lowered = name.strip().lower()
        return (
            lowered in {
                "authorization",
                "proxy-authorization",
                "cookie",
                "cookie2",
            }
            or "api-key" in lowered
            or "subscription-token" in lowered
            or lowered.endswith("-token")
        )


def build_safe_opener(
    validator: UrlValidator = validate_public_http_url,
) -> Callable[..., Any]:
    """Return an urllib opener that rejects unsafe redirect destinations."""
    return urllib.request.build_opener(SafeRedirectHandler(validator)).open
