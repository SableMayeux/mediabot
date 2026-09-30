"""Optional one-way Home Assistant notifications; no HA administrator token."""
import ipaddress
import os
import re
from urllib.parse import urlsplit

import aiohttp


async def notify_home(identity, title, message):
    url = os.environ.get('HOME_ASSISTANT_NOTIFY_WEBHOOK', '')
    if not url:
        return False
    parsed = urlsplit(url)
    try:
        address = ipaddress.ip_address(parsed.hostname or '')
    except ValueError:
        return False
    if (parsed.scheme not in {'http', 'https'} or not address.is_private
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or not parsed.path.startswith('/api/webhook/')
            or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', identity)):
        return False
    # Payloads cannot name a service, change device state, or carry raw magnets.
    body = {'id':identity, 'title':str(title)[:120], 'message':str(message)[:1000]}
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
        async with session.post(url, json=body, allow_redirects=False) as response:
            return response.status == 200
