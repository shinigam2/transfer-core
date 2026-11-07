from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from psycopg import Connection
from psycopg.rows import DictRow
from psycopg.types.json import Jsonb

from .money import Currency, Money, round_money
from .quotes_repo import format_iso_utc

TERMINAL_TRANSFER_STATES = frozenset({"COMPLETED", "FAILED"})


@dataclass(frozen=True)
class TransferRecord:
    transfer_id: str
    quote_id: str
    client_id: str
    idempotency_key: str
    request_hash: str
    direction_id: str
    source_amount: Money
    fee: Money
    target_amount: Money
    daily_limit_amount: Money
    recipient: dict[str, Any]
    status: str
    reserve_state: str
    active_provider_id: str | None
    provider_payout_id: str | None
    failure_reason: str | None
    scenario_header: str | None
    next_check_at: datetime | None
    uncertain_since: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    transfer_id: str
    provider_id: str
    idempotency_key: str
    status: str
    payout_id: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "attempt_id": self.attempt_id,
            "provider_id": self.provider_id,
            "status": self.status,
            "payout_id": self.payout_id,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "created_at": format_iso_utc(self.created_at),
            "updated_at": format_iso_utc(self.updated_at),
        }


@dataclass(frozen=True)
class TransitionRecord:
    from_status: str | None
    to_status: str
    initiated_by: str
    reason: str | None
    created_at: datetime

    def to_api_dict(self) -> dict[str, Any]:
        return {
            "from_status": self.from_status,
            "to_status": self.to_status,
            "initiated_by": self.initiated_by,
            "reason": self.reason,
            "created_at": format_iso_utc(self.created_at),
        }


def _row_to_transfer(row: DictRow, currencies: dict[str, Currency]) -> TransferRecord:
    src_curr = currencies[row["source_currency"]]
    fee_curr = currencies[row["fee_currency"]]
    dst_curr = currencies[row["target_currency"]]
    lim_curr = currencies[row["daily_limit_currency"]]
    return TransferRecord(
        transfer_id=row["transfer_id"],
        quote_id=row["quote_id"],
        client_id=row["client_id"],
        idempotency_key=row["idempotency_key"],
        request_hash=row["request_hash"],
        direction_id=row["direction_id"],
        source_amount=round_money(row["source_amount"], src_curr),
        fee=round_money(row["fee_amount"], fee_curr),
        target_amount=round_money(row["target_amount"], dst_curr),
        daily_limit_amount=round_money(row["daily_limit_amount"], lim_curr),
        recipient=row["recipient"],
        status=row["status"],
        reserve_state=row["reserve_state"],
        active_provider_id=row["active_provider_id"],
        provider_payout_id=row["provider_payout_id"],
        failure_reason=row["failure_reason"],
        scenario_header=row["scenario_header"],
        next_check_at=row["next_check_at"],
        uncertain_since=row["uncertain_since"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_transfer_by_id(
    conn: Connection[DictRow],
    transfer_id: str,
    currencies: dict[str, Currency],
    *,
    for_update: bool = False,
) -> TransferRecord | None:
    query = (
        "SELECT * FROM transfers WHERE transfer_id = %s FOR UPDATE"
        if for_update
        else "SELECT * FROM transfers WHERE transfer_id = %s"
    )
    row = conn.execute(query, (transfer_id,)).fetchone()
    return _row_to_transfer(row, currencies) if row is not None else None


def get_transfer_by_idempotency_key(
    conn: Connection[DictRow],
    idempotency_key: str,
    currencies: dict[str, Currency],
) -> TransferRecord | None:
    row = conn.execute(
        "SELECT * FROM transfers WHERE idempotency_key = %s",
        (idempotency_key,),
    ).fetchone()
    return _row_to_transfer(row, currencies) if row is not None else None


def insert_transfer(
    conn: Connection[DictRow],
    *,
    quote_id: str,
    client_id: str,
    idempotency_key: str,
    request_hash: str,
    direction_id: str,
    source_amount: Money,
    fee: Money,
    target_amount: Money,
    daily_limit_amount: Money,
    recipient: dict[str, Any],
    scenario_header: str | None,
    next_check_at: datetime,
    now: datetime,
) -> str:
    transfer_id = f"tr_{uuid4().hex[:16]}"
    conn.execute(
        """
        INSERT INTO transfers (
            transfer_id, quote_id, client_id, idempotency_key, request_hash,
            direction_id, source_amount, source_currency, fee_amount, fee_currency,
            target_amount, target_currency, daily_limit_amount, daily_limit_currency,
            recipient, status, reserve_state, scenario_header, next_check_at,
            created_at, updated_at
        ) VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s,
            %s, 'RESERVED', 'HELD', %s, %s,
            %s, %s
        )
        """,
        (
            transfer_id,
            quote_id,
            client_id,
            idempotency_key,
            request_hash,
            direction_id,
            source_amount.amount,
            source_amount.currency.code,
            fee.amount,
            fee.currency.code,
            target_amount.amount,
            target_amount.currency.code,
            daily_limit_amount.amount,
            daily_limit_amount.currency.code,
            Jsonb(recipient),
            scenario_header,
            next_check_at,
            now,
            now,
        ),
    )
    record_transition(
        conn,
        transfer_id=transfer_id,
        from_status=None,
        to_status="RESERVED",
        initiated_by="CLIENT",
        reason="Средства зарезервированы по котировке",
        now=now,
    )
    return transfer_id


def record_transition(
    conn: Connection[DictRow],
    *,
    transfer_id: str,
    from_status: str | None,
    to_status: str,
    initiated_by: str,
    reason: str | None,
    now: datetime,
) -> None:
    conn.execute(
        """
        INSERT INTO transfer_transitions (
            transfer_id, from_status, to_status, initiated_by, reason, created_at
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (transfer_id, from_status, to_status, initiated_by, reason, now),
    )


def list_transitions(conn: Connection[DictRow], transfer_id: str) -> list[TransitionRecord]:
    rows = conn.execute(
        """
        SELECT from_status, to_status, initiated_by, reason, created_at
        FROM transfer_transitions
        WHERE transfer_id = %s
        ORDER BY id ASC
        """,
        (transfer_id,),
    ).fetchall()
    return [
        TransitionRecord(
            from_status=r["from_status"],
            to_status=r["to_status"],
            initiated_by=r["initiated_by"],
            reason=r["reason"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


def list_attempts(conn: Connection[DictRow], transfer_id: str) -> list[AttemptRecord]:
    rows = conn.execute(
        """
        SELECT attempt_id, transfer_id, provider_id, idempotency_key, status,
               payout_id, error_code, error_message, created_at, updated_at
        FROM provider_attempts
        WHERE transfer_id = %s
        ORDER BY seq ASC
        """,
        (transfer_id,),
    ).fetchall()
    return [
        AttemptRecord(
            attempt_id=r["attempt_id"],
            transfer_id=r["transfer_id"],
            provider_id=r["provider_id"],
            idempotency_key=r["idempotency_key"],
            status=r["status"],
            payout_id=r["payout_id"],
            error_code=r["error_code"],
            error_message=r["error_message"],
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )
        for r in rows
    ]


def create_attempt(
    conn: Connection[DictRow],
    *,
    transfer_id: str,
    provider_id: str,
    now: datetime,
) -> AttemptRecord:
    attempt_id = f"att_{uuid4().hex[:16]}"
    idem_key = f"{transfer_id}:{provider_id}"
    conn.execute(
        """
        INSERT INTO provider_attempts (
            attempt_id, transfer_id, provider_id, idempotency_key,
            status, created_at, updated_at
        ) VALUES (%s, %s, %s, %s, 'SENDING', %s, %s)
        """,
        (attempt_id, transfer_id, provider_id, idem_key, now, now),
    )
    return AttemptRecord(
        attempt_id=attempt_id,
        transfer_id=transfer_id,
        provider_id=provider_id,
        idempotency_key=idem_key,
        status="SENDING",
        payout_id=None,
        error_code=None,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


def update_attempt(
    conn: Connection[DictRow],
    *,
    attempt_id: str,
    status: str,
    payout_id: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    now: datetime,
) -> None:
    conn.execute(
        """
        UPDATE provider_attempts
        SET status = %s,
            payout_id = COALESCE(%s, payout_id),
            error_code = %s,
            error_message = %s,
            updated_at = %s
        WHERE attempt_id = %s
        """,
        (status, payout_id, error_code, error_message, now, attempt_id),
    )


def list_due_transfer_ids(conn: Connection[DictRow], now: datetime, limit: int = 50) -> list[str]:
    rows = conn.execute(
        """
        SELECT transfer_id
        FROM transfers
        WHERE status IN ('RESERVED', 'PROCESSING', 'RECONCILING')
          AND next_check_at IS NOT NULL
          AND next_check_at <= %s
        ORDER BY next_check_at ASC
        LIMIT %s
        """,
        (now, limit),
    ).fetchall()
    return [r["transfer_id"] for r in rows]


def insert_webhook_event(
    conn: Connection[DictRow],
    *,
    provider_id: str,
    event_id: str,
    payout_id: str,
    reference: str,
    status: str,
    occurred_at: datetime,
    received_at: datetime,
) -> bool:
    """Возвращает True для нового события и False для дубля."""
    row = conn.execute(
        """
        INSERT INTO webhook_events (
            provider_id, event_id, payout_id, reference, status, occurred_at, received_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (provider_id, event_id) DO NOTHING
        RETURNING event_id
        """,
        (provider_id, event_id, payout_id, reference, status, occurred_at, received_at),
    ).fetchone()
    return row is not None


def format_transfer_response(
    conn: Connection[DictRow],
    transfer: TransferRecord,
) -> dict[str, Any]:
    transitions = list_transitions(conn, transfer.transfer_id)
    attempts = list_attempts(conn, transfer.transfer_id)
    return {
        "transfer_id": transfer.transfer_id,
        "quote_id": transfer.quote_id,
        "client_id": transfer.client_id,
        "direction_id": transfer.direction_id,
        "status": transfer.status,
        "reserve_state": transfer.reserve_state,
        "source_amount": transfer.source_amount.to_dict(),
        "fee": transfer.fee.to_dict(),
        "target_amount": transfer.target_amount.to_dict(),
        "recipient": transfer.recipient,
        "provider_id": transfer.active_provider_id,
        "provider_payout_id": transfer.provider_payout_id,
        "failure_reason": transfer.failure_reason,
        "created_at": format_iso_utc(transfer.created_at),
        "updated_at": format_iso_utc(transfer.updated_at),
        "transitions": [t.to_api_dict() for t in transitions],
        "attempts": [a.to_api_dict() for a in attempts],
    }
