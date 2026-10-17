"""Outbound request target policy (SSRF boundary).

Every tool that can make a server-side network request must validate its
target through this module before the request leaves the process. The policy
is deny-by-default for non-global addresses: loopback, private, link-local,
reserved, multicast and IPv4-mapped forms are rejected after literal parsing
or DNS resolution.

This is a trusted-layer check: it does not depend on the Agent or on the
model honouring a prompt.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import urlsplit

DNS_TIMEOUT_SECONDS = 5.0

_NUMERIC_HOST_RE = re.compile(r"^(?:0[xX][0-9a-fA-F]+|[0-9]+)$")


class EgressBlockedError(PermissionError):
    """Raised when a tool target violates the egress policy."""


def is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True when ``ip`` is not a globally routable unicast address."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return is_blocked_ip(ip.ipv4_mapped)
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return True
    return not ip.is_global


def _literal_ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """Parse IP literals, including decimal/hex/octal/short forms.

    ``ipaddress`` alone rejects forms such as ``2130706433`` or ``127.1`` that
    sockets still accept, so those are normalised here before DNS resolution.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass

    if _NUMERIC_HOST_RE.match(host):
        try:
            return ipaddress.IPv4Address(int(host, 0))
        except ValueError:
            pass

    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except OSError:
        return None


async def _resolve_ips(
    host: str, port: int
) -> set[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    infos = await asyncio.wait_for(
        asyncio.to_thread(socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM),
        timeout=DNS_TIMEOUT_SECONDS,
    )
    addresses: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        raw = str(sockaddr[0]).split("%", 1)[0]
        try:
            addresses.add(ipaddress.ip_address(raw))
        except ValueError:
            continue
    return addresses


async def validate_egress_url(url: str) -> None:
    """Raise :class:`EgressBlockedError` when ``url`` must not be requested."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise EgressBlockedError(f"Egress target scheme '{parts.scheme or '(missing)'}' is not allowed")
    if parts.username or parts.password:
        raise EgressBlockedError("Egress target must not embed credentials")

    host = parts.hostname
    if not host:
        raise EgressBlockedError("Egress target is missing a hostname")

    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise EgressBlockedError("Egress target has an invalid port") from exc

    literal = _literal_ip(host)
    if literal is not None:
        if is_blocked_ip(literal):
            raise EgressBlockedError(f"Egress target address {literal} is not allowed")
        return

    try:
        addresses = await _resolve_ips(host, port)
    except socket.gaierror:
        # Unresolvable names cannot reach a network target; let the HTTP client
        # report its own connection error instead of misclassifying it as SSRF.
        return
    except TimeoutError as exc:
        raise EgressBlockedError(f"Egress target '{host}' DNS resolution timed out") from exc
    except OSError as exc:
        raise EgressBlockedError(f"Egress target '{host}' could not be resolved") from exc

    if not addresses:
        return

    blocked = sorted(str(ip) for ip in addresses if is_blocked_ip(ip))
    if blocked:
        raise EgressBlockedError(f"Egress target '{host}' resolves to a disallowed address: {blocked[0]}")
