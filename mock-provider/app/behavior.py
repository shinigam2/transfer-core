import random
from dataclasses import dataclass, field
from decimal import Decimal

ACCEPT = "accept"
DECLINE = "decline"
ERROR_5XX = "error_5xx"
SLOW = "slow"
DROP_LOST = "drop_lost"
DROP_CREATED = "drop_created"
HANG_CREATED = "hang_created"
ERROR_CREATED = "error_created"

SCENARIOS = {"ok": ACCEPT, "decline": DECLINE, "error_5xx": ERROR_5XX, "slow": SLOW}

ASYNC_FAILURES = ["BENEFICIARY_BANK_REJECTED", "ACCOUNT_CLOSED", "COMPLIANCE_HOLD_REJECTED"]


@dataclass
class SettlePlan:
    delay: float
    final_status: str
    failure_code: str | None = None
    visibility_lag: float = 0.0
    stale_read: float = 0.0
    lost_webhook: bool = False
    duplicates: int = 0
    late_processing: bool = False
    contradiction: bool = False
    forged: str | None = None
    unknown_event: bool = False
    events: list = field(default_factory=list)


def seeded(seed, *parts):
    return random.Random(":".join([str(seed), *map(str, parts)]))


def first_attempt_action(rng, success_rate):
    decline = min(max((1 - success_rate) * Decimal("0.6"), Decimal("0.01")), Decimal("0.25"))
    table = [
        (float(decline), DECLINE),
        (0.04, DROP_LOST),
        (0.06, DROP_CREATED),
        (0.05, HANG_CREATED),
        (0.05, ERROR_5XX),
        (0.04, ERROR_CREATED),
        (0.08, SLOW),
    ]
    point = rng.random()
    edge = 0.0
    for weight, action in table:
        edge += weight
        if point < edge:
            return action
    return ACCEPT


def chaos_action(seed, provider, key, attempt, exists):
    rng = seeded(seed, "post", provider["provider_id"], key, attempt)
    rate = Decimal(provider["success_rate_24h"])
    if exists:
        if attempt <= 3 and rng.random() < 0.15:
            return ERROR_5XX
        return ACCEPT
    if attempt == 1:
        return first_attempt_action(rng, rate)
    if attempt <= 4 and rng.random() < 0.2:
        return ERROR_5XX
    return ACCEPT


def chaos_settle(seed, provider, key):
    rng = seeded(seed, "settle", provider["provider_id"], key)
    rate = Decimal(provider["success_rate_24h"])
    failure = min(max((1 - rate), Decimal("0.02")), Decimal("0.30"))
    fails = rng.random() < float(failure)
    plan = SettlePlan(
        delay=rng.uniform(2.0, 9.0) if rng.random() < 0.9 else rng.uniform(12.0, 25.0),
        final_status="FAILED" if fails else "COMPLETED",
        failure_code=rng.choice(ASYNC_FAILURES) if fails else None,
    )
    if rng.random() < 0.25:
        plan.visibility_lag = rng.uniform(3.0, 15.0)
    if rng.random() < 0.20:
        plan.stale_read = rng.uniform(1.0, 3.0)
    plan.lost_webhook = rng.random() < 0.12
    if not plan.lost_webhook:
        if rng.random() < 0.30:
            plan.duplicates = rng.randint(1, 2)
        plan.late_processing = rng.random() < 0.20
        plan.contradiction = rng.random() < 0.04
    if rng.random() < 0.06:
        plan.forged = rng.choice(["missing", "invalid"])
    plan.unknown_event = rng.random() < 0.05
    return plan


def normal_settle(settle_sec):
    return SettlePlan(delay=settle_sec, final_status="COMPLETED")
