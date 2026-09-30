"""One optional fixed Home Assistant action, without an HA administrator token."""
import ipaddress
import os
import re
from urllib.parse import urlsplit

import aiohttp


def cast_webhook_url():
    url = os.environ.get("HOME_ASSISTANT_CAST_WEBHOOK", "")
    try:
        parsed = urlsplit(url)
        address = ipaddress.ip_address(parsed.hostname or "")
        port = parsed.port
    except ValueError:
        return None
    local_ranges = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")
    if (parsed.scheme not in {"http", "https"}
            or not any(address in ipaddress.ip_network(block) for block in local_ranges)
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or port == 0
            or not re.fullmatch(r"/api/webhook/[A-Za-z0-9_-]{48,96}", parsed.path)):
        return None
    return url


async def restore_home():
    """Send exactly one request. Never retry an ambiguously delivered action."""
    url = cast_webhook_url()
    if not url:
        return "unconfigured"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as session:
            async with session.post(url, json={}, allow_redirects=False) as response:
                return "accepted" if response.status == 200 else "rejected"
    except (aiohttp.ClientError, TimeoutError):
        return "unconfirmed"
