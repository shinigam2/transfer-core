import asyncio
import hashlib
import hmac
import json
import secrets
from datetime import timedelta

import aiohttp

from .reference_data import format_time
from .store import FAILED, PROCESSING, now_utc

DELIVERY_ATTEMPTS = 5


def sign(secret, body):
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


class WebhookSender:
    def __init__(self, secret):
        self.secret = secret
        self.session = None

    async def start(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))

    async def stop(self):
        if self.session:
            await self.session.close()

    def event(self, payout, status, occurred_at=None, event_id=None):
        return {
            "event_id": event_id or f"ev_{secrets.token_hex(8)}",
            "payout_id": payout.payout_id,
            "reference": payout.reference,
            "status": status,
            "occurred_at": format_time(occurred_at or now_utc()),
        }

    async def post(self, url, body, headers):
        for attempt in range(DELIVERY_ATTEMPTS):
            try:
                async with self.session.post(url, data=body, headers=headers) as response:
                    await response.read()
                    if response.status < 500:
                        return
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
                pass
            await asyncio.sleep(0.5 * (2**attempt))

    async def deliver(self, payout, event):
        body = json.dumps(event).encode()
        headers = {"Content-Type": "application/json", "X-Signature": sign(self.secret, body)}
        await self.post(payout.callback_url, body, headers)

    async def deliver_forged(self, payout, kind, status):
        event = self.event(payout, status)
        body = json.dumps(event).encode()
        headers = {"Content-Type": "application/json"}
        if kind == "invalid":
            headers["X-Signature"] = sign("wrong-" + self.secret, body)
        try:
            async with self.session.post(payout.callback_url, data=body, headers=headers) as response:
                await response.read()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            pass

    async def deliver_unknown(self, payout):
        event = {
            "event_id": f"ev_{secrets.token_hex(8)}",
            "payout_id": f"po_{secrets.token_hex(8)}",
            "reference": f"unknown-{secrets.token_hex(6)}",
            "status": "COMPLETED",
            "occurred_at": format_time(now_utc()),
        }
        await self.deliver(payout, event)

    async def deliver_final(self, payout, plan):
        settled = payout.settled_at or now_utc()
        event = self.event(payout, payout.status, settled)
        await self.deliver(payout, event)
        for _ in range(plan.duplicates):
            await asyncio.sleep(0.4)
            await self.deliver(payout, event)
        if plan.late_processing:
            await asyncio.sleep(0.6)
            earlier = settled - timedelta(seconds=plan.delay)
            await self.deliver(payout, self.event(payout, PROCESSING, earlier))
        if plan.contradiction:
            await asyncio.sleep(0.8)
            earlier = settled - timedelta(seconds=1)
            other = "COMPLETED" if payout.status == FAILED else FAILED
            await self.deliver(payout, self.event(payout, other, earlier))
