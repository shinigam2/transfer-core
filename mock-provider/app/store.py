import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone

PROCESSING = "PROCESSING"
COMPLETED = "COMPLETED"
FAILED = "FAILED"


def now_utc():
    return datetime.now(timezone.utc)


@dataclass
class Payout:
    payout_id: str
    provider_id: str
    idempotency_key: str
    reference: str
    amount: str
    currency: str
    recipient: dict
    callback_url: str
    status: str
    failure_code: str | None
    created_at: datetime
    settled_at: datetime | None = None
    visible_at: datetime | None = None
    stale_until: datetime | None = None
    sync_declined: bool = False
    attempts: int = 1

    def fingerprint(self):
        return (self.reference, self.amount, self.currency)

    def visible_status(self, now):
        if self.stale_until and now < self.stale_until:
            return PROCESSING
        return self.status

    def is_visible(self, now):
        return self.visible_at is None or now >= self.visible_at

    def public(self, now):
        status = self.visible_status(now)
        return {
            "payout_id": self.payout_id,
            "reference": self.reference,
            "status": status,
            "failure_code": self.failure_code if status == FAILED else None,
        }

    def ledger_entry(self):
        return {
            "payout_id": self.payout_id,
            "reference": self.reference,
            "idempotency_key": self.idempotency_key,
            "amount": self.amount,
            "currency": self.currency,
            "recipient": self.recipient,
            "status": self.status,
            "failure_code": self.failure_code,
            "created_at": self.created_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "settled_at": self.settled_at.strftime("%Y-%m-%dT%H:%M:%SZ") if self.settled_at else None,
        }


@dataclass
class ProviderState:
    by_key: dict = field(default_factory=dict)
    by_id: dict = field(default_factory=dict)
    by_reference: dict = field(default_factory=dict)
    attempts: dict = field(default_factory=dict)
    scenario: str | None = None


class Store:
    def __init__(self):
        self.providers = {}

    def state(self, provider_id):
        return self.providers.setdefault(provider_id, ProviderState())

    def create(self, provider_id, key, body, status, failure_code=None):
        state = self.state(provider_id)
        payout = Payout(
            payout_id=f"po_{secrets.token_hex(8)}",
            provider_id=provider_id,
            idempotency_key=key,
            reference=body["reference"],
            amount=body["amount"],
            currency=body["currency"],
            recipient=body.get("recipient") or {},
            callback_url=body["callback_url"],
            status=status,
            failure_code=failure_code,
            created_at=now_utc(),
        )
        state.by_key[key] = payout
        state.by_id[payout.payout_id] = payout
        state.by_reference[payout.reference] = payout
        return payout

    def next_attempt(self, provider_id, key):
        state = self.state(provider_id)
        state.attempts[key] = state.attempts.get(key, 0) + 1
        return state.attempts[key]
