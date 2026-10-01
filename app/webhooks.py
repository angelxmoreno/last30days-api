"""Signed webhook delivery. Polling stays authoritative; this is best-effort."""

import asyncio
import hashlib
import hmac
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable
from urllib.parse import urlparse

import httpx

log = logging.getLogger("webhooks")
BACKOFF_SECONDS = (1.0, 4.0, 16.0)


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def is_private_target(url: str) -> bool:
    """True if the host resolves to any non-public address (or cannot be resolved)."""
    host = urlparse(url).hostname
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return True
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            return True
    return False


async def deliver(
    url: str,
    body: bytes,
    secret: str,
    *,
    allow_private: bool,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    transport: httpx.AsyncBaseTransport | None = None,
) -> bool:
    """POST body with an HMAC signature; 1 try + 3 retries with backoff. Returns success."""
    headers = {"Content-Type": "application/json", "X-Signature": sign(secret, body)}
    for attempt in range(len(BACKOFF_SECONDS) + 1):
        # Re-check on every attempt so DNS changes cannot sneak in a private target.
        if not allow_private and await asyncio.to_thread(is_private_target, url):
            log.warning("webhook blocked: private target %s", url)
            return False
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False, transport=transport) as client:
                resp = await client.post(url, content=body, headers=headers)
            if resp.status_code < 300:
                return True
            log.warning("webhook %s -> %s", url, resp.status_code)
        except httpx.HTTPError as exc:
            log.warning("webhook %s failed: %s", url, exc)
        if attempt < len(BACKOFF_SECONDS):
            await sleep(BACKOFF_SECONDS[attempt])
    return False
