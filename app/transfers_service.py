import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Any

from psycopg import Connection
from psycopg.errors import UniqueViolation
from psycopg.rows import DictRow

from .balances import (
    commit_reserved_funds,
    compute_daily_limit_amount,
    lock_client_for_transfer,
    release_reserved_funds,
    reserve_client_funds,
)
from .config import Settings
from .db import DbPool
from .errors import ApiError
from .provider_client import PayoutCreateResult, ProviderClient
from .quotes_repo import format_iso_utc, get_quote
from .rates import RateSource
from .reference import ReferenceData
from .routing import select_providers
from .transfers_repo import (
    TERMINAL_TRANSFER_STATES,
    AttemptRecord,
    TransferRecord,
    create_attempt,
    format_transfer_response,
    get_transfer_by_id,
    get_transfer_by_idempotency_key,
    insert_transfer,
    insert_webhook_event,
    list_attempts,
    list_due_transfer_ids,
    record_transition,
    update_attempt,
)

log = logging.getLogger("transfer-core")

SAME_PROVIDER_RETRY_DELAYS = (0.15, 0.30, 0.50)


def compute_request_hash(quote_id: str, client_id: str, recipient: dict[str, Any]) -> str:
    canonical = json.dumps(
        {"quote_id": quote_id, "client_id": client_id, "recipient": recipient},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class TransferService:
    """Координирует жизненный цикл перевода, failover провайдеров, вебхуки и фоновую сверку."""

    def __init__(
        self,
        *,
        pool: DbPool,
        ref: ReferenceData,
        rate_source: RateSource,
        provider_client: ProviderClient,
        settings: Settings,
        clock: Callable[[], datetime],
    ) -> None:
        self.pool = pool
        self.ref = ref
        self.rate_source = rate_source
        self.provider_client = provider_client
        self.settings = settings
        self.clock = clock

    def create_or_get_transfer(
        self,
        *,
        idempotency_key: str,
        quote_id: str,
        client_id: str,
        recipient: dict[str, Any],
        scenario_header: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Создаёт перевод с резервированием средств либо возвращает существующий по Idempotency-Key."""
        req_hash = compute_request_hash(quote_id, client_id, recipient)

        with self.pool.connection() as conn:
            existing = get_transfer_by_idempotency_key(conn, idempotency_key, self.ref.currencies)
            if existing is not None:
                self._ensure_same_hash(existing, req_hash)
                return format_transfer_response(conn, existing), False

            quote = get_quote(conn, quote_id, self.ref.currencies)

        if quote is None:
            raise ApiError(
                code="QUOTE_NOT_FOUND",
                message="Котировка не найдена",
                status_code=404,
                details={"quote_id": quote_id},
            )

        now = self.clock()
        if quote.is_expired(now):
            raise ApiError(
                code="QUOTE_EXPIRED",
                message="Срок действия котировки истёк",
                status_code=422,
                details={"quote_id": quote_id, "expires_at": format_iso_utc(quote.expires_at)},
            )

        # Загружаем курсы до открытия транзакции, чтобы не ходить по сети под блокировкой БД.
        client_seed = self.ref.clients.get(client_id)
        need_fx_for_limit = (
            client_seed is not None
            and client_seed.daily_limit.currency != quote.source_amount.currency
        )
        rate_snap = self.rate_source.snapshot() if need_fx_for_limit else None

        with self.pool.connection() as conn, conn.transaction():
            daily_limit = lock_client_for_transfer(conn, client_id, self.ref.currencies)

            # Повторная проверка после блокировки клиента ловит параллельный double-submit.
            existing = get_transfer_by_idempotency_key(conn, idempotency_key, self.ref.currencies)
            if existing is not None:
                self._ensure_same_hash(existing, req_hash)
                return format_transfer_response(conn, existing), False

            if daily_limit.currency != quote.source_amount.currency and rate_snap is None:
                rate_snap = self.rate_source.snapshot()

            limit_amount = compute_daily_limit_amount(
                quote.source_amount,
                daily_limit.currency,
                rate_snap,
            )
            reserve_client_funds(
                conn,
                client_id,
                quote.source_amount,
                daily_limit,
                limit_amount,
                now,
            )

            try:
                with conn.transaction():
                    transfer_id = insert_transfer(
                        conn,
                        quote_id=quote.quote_id,
                        client_id=client_id,
                        idempotency_key=idempotency_key,
                        request_hash=req_hash,
                        direction_id=quote.direction_id,
                        source_amount=quote.source_amount,
                        fee=quote.fee,
                        target_amount=quote.target_amount,
                        daily_limit_amount=limit_amount,
                        recipient=recipient,
                        scenario_header=scenario_header,
                        next_check_at=now + timedelta(seconds=5),
                        now=now,
                    )
            except UniqueViolation:
                # Другой поток успел вставить тот же Idempotency-Key (например, для другого client_id).
                winner = get_transfer_by_idempotency_key(conn, idempotency_key, self.ref.currencies)
                if winner is not None:
                    self._ensure_same_hash(winner, req_hash)
                raise ApiError(
                    code="IDEMPOTENCY_CONFLICT",
                    message="Ключ идемпотентности уже используется другим запросом",
                    status_code=409,
                ) from None

            created = get_transfer_by_id(conn, transfer_id, self.ref.currencies)
            assert created is not None
            return format_transfer_response(conn, created), True

    def get_transfer_details(self, transfer_id: str) -> dict[str, Any]:
        with self.pool.connection() as conn:
            transfer = get_transfer_by_id(conn, transfer_id, self.ref.currencies)
            if transfer is None:
                raise ApiError(
                    code="TRANSFER_NOT_FOUND",
                    message="Перевод не найден",
                    status_code=404,
                    details={"transfer_id": transfer_id},
                )
            return format_transfer_response(conn, transfer)

    def execute_transfer(self, transfer_id: str) -> None:
        """Отправляет выплату провайдеру и переключается на запасных при безопасном отказе."""
        with self.pool.connection() as conn:
            transfer = get_transfer_by_id(conn, transfer_id, self.ref.currencies)
            if transfer is None or transfer.status in TERMINAL_TRANSFER_STATES:
                return
            if transfer.status in ("RECONCILING", "NEEDS_REVIEW"):
                return
            attempts = list_attempts(conn, transfer_id)

        # Если уже есть принятая попытка — ждём вебхук или фоновую сверку.
        if any(a.status == "ACCEPTED" for a in attempts):
            return

        sending_attempt = next((a for a in attempts if a.status == "SENDING"), None)
        excluded = {a.provider_id for a in attempts if a.status == "DECLINED"}
        tariff = self.ref.tariffs[transfer.direction_id]

        while True:
            now = self.clock()
            if sending_attempt is not None:
                attempt = sending_attempt
                sending_attempt = None
            else:
                try:
                    rate_snap = self.rate_source.snapshot()
                except ApiError:
                    self._schedule_recheck(transfer_id, seconds=2.0)
                    return

                decision = select_providers(
                    tariff=tariff,
                    source_amount=transfer.source_amount,
                    client_fee=transfer.fee,
                    target_amount=transfer.target_amount,
                    providers=self.ref.providers.values(),
                    rate_snapshot=rate_snap,
                    now=now,
                    exclude_providers=excluded,
                )
                if not decision.ranked:
                    self._fail_without_provider(transfer_id, "NO_ELIGIBLE_PROVIDER")
                    return

                chosen = decision.ranked[0].provider
                started = self._begin_provider_attempt(transfer_id, chosen.provider_id, now)
                if started is None:
                    return
                attempt, transfer = started

            outcome = self._call_provider_with_same_key_retries(transfer, attempt)
            now = self.clock()

            if outcome.kind == "ACCEPTED":
                self._handle_accepted_attempt(transfer_id, attempt, outcome, now)
                return

            if outcome.kind == "DECLINED":
                self._mark_attempt_declined(transfer_id, attempt, outcome, now)
                excluded.add(attempt.provider_id)
                continue

            if outcome.kind == "RETRYABLE_ERROR":
                # Переключаемся на другого провайдера только если убедились, что выплата не создана.
                lookup = self.provider_client.find_payout_by_reference(
                    attempt.provider_id,
                    transfer.transfer_id,
                )
                if lookup.kind == "NOT_FOUND":
                    self._mark_attempt_declined(transfer_id, attempt, outcome, now)
                    excluded.add(attempt.provider_id)
                    continue
                if lookup.kind == "FOUND":
                    recovered = PayoutCreateResult(
                        kind="ACCEPTED",
                        payout_id=lookup.payout_id,
                        provider_status=lookup.provider_status,
                        error_code=lookup.failure_code,
                    )
                    self._handle_accepted_attempt(transfer_id, attempt, recovered, now)
                    return

            # Исход неизвестен (обрыв связи / таймаут / 504): запрещаем failover и передаём в сверку.
            self._move_to_reconciling(transfer_id, attempt, outcome, now)
            return

    def handle_webhook(
        self,
        *,
        provider_id: str,
        event_id: str,
        payout_id: str,
        reference: str,
        status: str,
        occurred_at: datetime,
    ) -> dict[str, str]:
        now = self.clock()
        with self.pool.connection() as conn, conn.transaction():
            is_new = insert_webhook_event(
                conn,
                provider_id=provider_id,
                event_id=event_id,
                payout_id=payout_id,
                reference=reference,
                status=status,
                occurred_at=occurred_at,
                received_at=now,
            )
            if not is_new:
                return {"status": "duplicate"}

            transfer = get_transfer_by_id(conn, reference, self.ref.currencies, for_update=True)
            if transfer is None:
                return {"status": "ignored", "reason": "unknown_reference"}

            if transfer.status in TERMINAL_TRANSFER_STATES:
                return {"status": "ignored", "reason": "already_terminal"}

            if transfer.active_provider_id and transfer.active_provider_id != provider_id:
                return {"status": "ignored", "reason": "inactive_provider"}

            attempts = list_attempts(conn, transfer.transfer_id)
            matching_attempt = next(
                (a for a in reversed(attempts) if a.provider_id == provider_id),
                None,
            )

            if status == "PROCESSING":
                if matching_attempt is not None and matching_attempt.status in ("SENDING", "UNCERTAIN"):
                    update_attempt(
                        conn,
                        attempt_id=matching_attempt.attempt_id,
                        status="ACCEPTED",
                        payout_id=payout_id,
                        now=now,
                    )
                if transfer.status == "RECONCILING":
                    conn.execute(
                        """
                        UPDATE transfers
                        SET status = 'PROCESSING',
                            provider_payout_id = COALESCE(%s, provider_payout_id),
                            next_check_at = %s,
                            updated_at = %s
                        WHERE transfer_id = %s
                        """,
                        (payout_id, now + timedelta(seconds=3), now, transfer.transfer_id),
                    )
                    record_transition(
                        conn,
                        transfer_id=transfer.transfer_id,
                        from_status="RECONCILING",
                        to_status="PROCESSING",
                        initiated_by="WEBHOOK",
                        reason=f"Получен статус PROCESSING от {provider_id}",
                        now=now,
                    )
                return {"status": "accepted"}

            if status == "COMPLETED":
                if matching_attempt is not None:
                    update_attempt(
                        conn,
                        attempt_id=matching_attempt.attempt_id,
                        status="COMPLETED",
                        payout_id=payout_id,
                        now=now,
                    )
                self._finalize_completed(
                    conn,
                    transfer=transfer,
                    provider_id=provider_id,
                    payout_id=payout_id,
                    initiated_by="WEBHOOK",
                    reason=f"Вебхук COMPLETED от {provider_id}",
                    now=now,
                )
                return {"status": "processed"}

            if status == "FAILED":
                if matching_attempt is not None:
                    update_attempt(
                        conn,
                        attempt_id=matching_attempt.attempt_id,
                        status="FAILED",
                        payout_id=payout_id,
                        error_code="PROVIDER_ASYNC_FAILED",
                        now=now,
                    )
                self._finalize_failed(
                    conn,
                    transfer=transfer,
                    failure_reason=f"PROVIDER_FAILED:{provider_id}",
                    initiated_by="WEBHOOK",
                    reason=f"Вебхук FAILED от {provider_id}",
                    payout_id=payout_id,
                    now=now,
                )
                return {"status": "processed"}

            return {"status": "ignored", "reason": "unknown_status"}

    def reconcile_due_transfers(self, dispatch_fn: Callable[[str], None] | None = None) -> int:
        """Проверяет зависшие и неопределённые переводы через GET у провайдера."""
        now = self.clock()
        with self.pool.connection() as conn:
            due_ids = list_due_transfer_ids(conn, now)

        processed = 0
        for transfer_id in due_ids:
            processed += 1
            self._reconcile_one(transfer_id, dispatch_fn)
        return processed

    def _reconcile_one(
        self,
        transfer_id: str,
        dispatch_fn: Callable[[str], None] | None,
    ) -> None:
        with self.pool.connection() as conn:
            transfer = get_transfer_by_id(conn, transfer_id, self.ref.currencies)
            if transfer is None or transfer.status in TERMINAL_TRANSFER_STATES or transfer.status == "NEEDS_REVIEW":
                return
            attempts = list_attempts(conn, transfer_id)

        # Перевод ещё ни разу не отправлялся (например, после рестарта процесса).
        if not transfer.active_provider_id and not any(a.status in ("SENDING", "ACCEPTED", "UNCERTAIN") for a in attempts):
            if dispatch_fn is not None:
                dispatch_fn(transfer_id)
            else:
                self.execute_transfer(transfer_id)
            return

        provider_id = transfer.active_provider_id or attempts[-1].provider_id
        lookup = self.provider_client.find_payout_by_reference(provider_id, transfer_id)
        now = self.clock()

        with self.pool.connection() as conn, conn.transaction():
            locked = get_transfer_by_id(conn, transfer_id, self.ref.currencies, for_update=True)
            if locked is None or locked.status in TERMINAL_TRANSFER_STATES or locked.status == "NEEDS_REVIEW":
                return

            latest_attempt = next(
                (a for a in reversed(list_attempts(conn, transfer_id)) if a.provider_id == provider_id),
                None,
            )

            if lookup.kind == "FOUND":
                if lookup.provider_status == "COMPLETED":
                    if latest_attempt is not None:
                        update_attempt(
                            conn,
                            attempt_id=latest_attempt.attempt_id,
                            status="COMPLETED",
                            payout_id=lookup.payout_id,
                            now=now,
                        )
                    self._finalize_completed(
                        conn,
                        transfer=locked,
                        provider_id=provider_id,
                        payout_id=lookup.payout_id,
                        initiated_by="RECONCILER",
                        reason=f"Сверка подтвердила выплату у {provider_id}",
                        now=now,
                    )
                    return

                if lookup.provider_status == "FAILED":
                    if latest_attempt is not None:
                        update_attempt(
                            conn,
                            attempt_id=latest_attempt.attempt_id,
                            status="FAILED",
                            payout_id=lookup.payout_id,
                            error_code=lookup.failure_code or "PROVIDER_FAILED",
                            now=now,
                        )
                    self._finalize_failed(
                        conn,
                        transfer=locked,
                        failure_reason=lookup.failure_code or f"PROVIDER_FAILED:{provider_id}",
                        initiated_by="RECONCILER",
                        reason=f"Сверка зафиксировала отказ у {provider_id}",
                        payout_id=lookup.payout_id,
                        now=now,
                    )
                    return

                # Статус у провайдера всё ещё PROCESSING — ждём дальше.
                if latest_attempt is not None and latest_attempt.status != "ACCEPTED":
                    update_attempt(
                        conn,
                        attempt_id=latest_attempt.attempt_id,
                        status="ACCEPTED",
                        payout_id=lookup.payout_id,
                        now=now,
                    )
                new_status = "PROCESSING"
                conn.execute(
                    """
                    UPDATE transfers
                    SET status = %s,
                        provider_payout_id = COALESCE(%s, provider_payout_id),
                        next_check_at = %s,
                        updated_at = %s
                    WHERE transfer_id = %s
                    """,
                    (new_status, lookup.payout_id, now + timedelta(seconds=2), now, transfer_id),
                )
                if locked.status != new_status:
                    record_transition(
                        conn,
                        transfer_id=transfer_id,
                        from_status=locked.status,
                        to_status=new_status,
                        initiated_by="RECONCILER",
                        reason=f"Сверка нашла активную выплату у {provider_id}",
                        now=now,
                    )
                return

            # Выплата не видна (visibility_lag) или провайдер недоступен.
            since = locked.uncertain_since or locked.created_at
            elapsed = (now - since).total_seconds()
            if elapsed >= self.settings.max_uncertainty_sec:
                conn.execute(
                    """
                    UPDATE transfers
                    SET status = 'NEEDS_REVIEW',
                        failure_reason = 'PAYOUT_OUTCOME_UNKNOWN',
                        next_check_at = NULL,
                        updated_at = %s
                    WHERE transfer_id = %s
                    """,
                    (now, transfer_id),
                )
                record_transition(
                    conn,
                    transfer_id=transfer_id,
                    from_status=locked.status,
                    to_status="NEEDS_REVIEW",
                    initiated_by="RECONCILER",
                    reason=f"Статус выплаты у {provider_id} неизвестен после {int(elapsed)}с; резерв сохранён",
                    now=now,
                )
            else:
                conn.execute(
                    """
                    UPDATE transfers
                    SET next_check_at = %s,
                        updated_at = %s
                    WHERE transfer_id = %s
                    """,
                    (now + timedelta(seconds=2), now, transfer_id),
                )

    def _call_provider_with_same_key_retries(
        self,
        transfer: TransferRecord,
        attempt: AttemptRecord,
    ) -> PayoutCreateResult:
        callback_url = f"{self.settings.callback_base_url}/webhooks/{attempt.provider_id}"
        saw_uncertain = False

        outcome = self.provider_client.create_payout(
            provider_id=attempt.provider_id,
            idempotency_key=attempt.idempotency_key,
            reference=transfer.transfer_id,
            amount=transfer.target_amount.format(),
            currency=transfer.target_amount.currency.code,
            recipient=transfer.recipient,
            callback_url=callback_url,
            scenario=transfer.scenario_header,
        )
        if outcome.kind in ("ACCEPTED", "DECLINED"):
            return outcome
        if outcome.kind == "UNCERTAIN":
            saw_uncertain = True

        # Повторяем запрос к тому же провайдеру с тем же Idempotency-Key.
        for delay in SAME_PROVIDER_RETRY_DELAYS:
            time.sleep(delay)
            outcome = self.provider_client.create_payout(
                provider_id=attempt.provider_id,
                idempotency_key=attempt.idempotency_key,
                reference=transfer.transfer_id,
                amount=transfer.target_amount.format(),
                currency=transfer.target_amount.currency.code,
                recipient=transfer.recipient,
                callback_url=callback_url,
                scenario=transfer.scenario_header,
            )
            if outcome.kind in ("ACCEPTED", "DECLINED"):
                return outcome
            if outcome.kind == "UNCERTAIN":
                saw_uncertain = True

        # Если хотя бы одна попытка оборвалась с неизвестным результатом — нельзя считать ошибку чистой 500.
        if saw_uncertain and outcome.kind != "UNCERTAIN":
            return PayoutCreateResult(
                kind="UNCERTAIN",
                error_code=outcome.error_code,
                error_message=outcome.error_message,
            )
        return outcome

    def _begin_provider_attempt(
        self,
        transfer_id: str,
        provider_id: str,
        now: datetime,
    ) -> tuple[AttemptRecord, TransferRecord] | None:
        with self.pool.connection() as conn, conn.transaction():
            locked = get_transfer_by_id(conn, transfer_id, self.ref.currencies, for_update=True)
            if locked is None or locked.status in TERMINAL_TRANSFER_STATES or locked.status == "NEEDS_REVIEW":
                return None

            attempt = create_attempt(
                conn,
                transfer_id=transfer_id,
                provider_id=provider_id,
                now=now,
            )
            conn.execute(
                """
                UPDATE transfers
                SET status = 'PROCESSING',
                    active_provider_id = %s,
                    next_check_at = %s,
                    updated_at = %s
                WHERE transfer_id = %s
                """,
                (provider_id, now + timedelta(seconds=10), now, transfer_id),
            )
            if locked.status != "PROCESSING":
                record_transition(
                    conn,
                    transfer_id=transfer_id,
                    from_status=locked.status,
                    to_status="PROCESSING",
                    initiated_by="WORKER",
                    reason=f"Отправка выплаты через {provider_id}",
                    now=now,
                )
            updated = get_transfer_by_id(conn, transfer_id, self.ref.currencies)
            assert updated is not None
            return attempt, updated

    def _handle_accepted_attempt(
        self,
        transfer_id: str,
        attempt: AttemptRecord,
        outcome: PayoutCreateResult,
        now: datetime,
    ) -> None:
        with self.pool.connection() as conn, conn.transaction():
            locked = get_transfer_by_id(conn, transfer_id, self.ref.currencies, for_update=True)
            if locked is None:
                return

            if locked.status in TERMINAL_TRANSFER_STATES:
                # Быстрый вебхук мог закрыть перевод, пока шёл медленный ответ POST /payouts.
                update_attempt(
                    conn,
                    attempt_id=attempt.attempt_id,
                    status=locked.status,
                    payout_id=outcome.payout_id,
                    now=now,
                )
                return

            if outcome.provider_status == "COMPLETED":
                update_attempt(
                    conn,
                    attempt_id=attempt.attempt_id,
                    status="COMPLETED",
                    payout_id=outcome.payout_id,
                    now=now,
                )
                self._finalize_completed(
                    conn,
                    transfer=locked,
                    provider_id=attempt.provider_id,
                    payout_id=outcome.payout_id,
                    initiated_by="WORKER",
                    reason=f"Провайдер {attempt.provider_id} сразу вернул COMPLETED",
                    now=now,
                )
                return

            if outcome.provider_status == "FAILED":
                update_attempt(
                    conn,
                    attempt_id=attempt.attempt_id,
                    status="FAILED",
                    payout_id=outcome.payout_id,
                    error_code=outcome.error_code or "PROVIDER_FAILED",
                    now=now,
                )
                self._finalize_failed(
                    conn,
                    transfer=locked,
                    failure_reason=outcome.error_code or f"PROVIDER_FAILED:{attempt.provider_id}",
                    initiated_by="WORKER",
                    reason=f"Провайдер {attempt.provider_id} вернул FAILED",
                    payout_id=outcome.payout_id,
                    now=now,
                )
                return

            update_attempt(
                conn,
                attempt_id=attempt.attempt_id,
                status="ACCEPTED",
                payout_id=outcome.payout_id,
                now=now,
            )
            conn.execute(
                """
                UPDATE transfers
                SET status = 'PROCESSING',
                    active_provider_id = %s,
                    provider_payout_id = COALESCE(%s, provider_payout_id),
                    next_check_at = %s,
                    updated_at = %s
                WHERE transfer_id = %s
                """,
                (attempt.provider_id, outcome.payout_id, now + timedelta(seconds=3), now, transfer_id),
            )

    def _mark_attempt_declined(
        self,
        transfer_id: str,
        attempt: AttemptRecord,
        outcome: PayoutCreateResult,
        now: datetime,
    ) -> None:
        with self.pool.connection() as conn, conn.transaction():
            locked = get_transfer_by_id(conn, transfer_id, self.ref.currencies, for_update=True)
            if locked is None or locked.status in TERMINAL_TRANSFER_STATES:
                return
            update_attempt(
                conn,
                attempt_id=attempt.attempt_id,
                status="DECLINED",
                error_code=outcome.error_code,
                error_message=outcome.error_message,
                now=now,
            )
            conn.execute(
                """
                UPDATE transfers
                SET active_provider_id = NULL,
                    updated_at = %s
                WHERE transfer_id = %s
                """,
                (now, transfer_id),
            )

    def _move_to_reconciling(
        self,
        transfer_id: str,
        attempt: AttemptRecord,
        outcome: PayoutCreateResult,
        now: datetime,
    ) -> None:
        with self.pool.connection() as conn, conn.transaction():
            locked = get_transfer_by_id(conn, transfer_id, self.ref.currencies, for_update=True)
            if locked is None or locked.status in TERMINAL_TRANSFER_STATES:
                return
            update_attempt(
                conn,
                attempt_id=attempt.attempt_id,
                status="UNCERTAIN",
                error_code=outcome.error_code,
                error_message=outcome.error_message,
                now=now,
            )
            conn.execute(
                """
                UPDATE transfers
                SET status = 'RECONCILING',
                    uncertain_since = COALESCE(uncertain_since, %s),
                    next_check_at = %s,
                    updated_at = %s
                WHERE transfer_id = %s
                """,
                (now, now + timedelta(seconds=2), now, transfer_id),
            )
            if locked.status != "RECONCILING":
                record_transition(
                    conn,
                    transfer_id=transfer_id,
                    from_status=locked.status,
                    to_status="RECONCILING",
                    initiated_by="WORKER",
                    reason=f"Неопределённый ответ {attempt.provider_id} ({outcome.error_code}); ждём сверку",
                    now=now,
                )

    def _fail_without_provider(self, transfer_id: str, reason_code: str) -> None:
        now = self.clock()
        with self.pool.connection() as conn, conn.transaction():
            locked = get_transfer_by_id(conn, transfer_id, self.ref.currencies, for_update=True)
            if locked is None or locked.status in TERMINAL_TRANSFER_STATES:
                return
            self._finalize_failed(
                conn,
                transfer=locked,
                failure_reason=reason_code,
                initiated_by="WORKER",
                reason="Нет доступных подходящих провайдеров (или все отказали)",
                payout_id=None,
                now=now,
            )

    def _schedule_recheck(self, transfer_id: str, seconds: float) -> None:
        now = self.clock()
        with self.pool.connection() as conn, conn.transaction():
            conn.execute(
                "UPDATE transfers SET next_check_at = %s, updated_at = %s WHERE transfer_id = %s",
                (now + timedelta(seconds=seconds), now, transfer_id),
            )

    def _finalize_completed(
        self,
        conn: Connection[DictRow],
        *,
        transfer: TransferRecord,
        provider_id: str,
        payout_id: str | None,
        initiated_by: str,
        reason: str,
        now: datetime,
    ) -> None:
        if transfer.reserve_state == "HELD":
            commit_reserved_funds(conn, transfer.client_id, transfer.source_amount)
        conn.execute(
            """
            UPDATE transfers
            SET status = 'COMPLETED',
                reserve_state = 'COMMITTED',
                active_provider_id = %s,
                provider_payout_id = COALESCE(%s, provider_payout_id),
                next_check_at = NULL,
                updated_at = %s
            WHERE transfer_id = %s
            """,
            (provider_id, payout_id, now, transfer.transfer_id),
        )
        record_transition(
            conn,
            transfer_id=transfer.transfer_id,
            from_status=transfer.status,
            to_status="COMPLETED",
            initiated_by=initiated_by,
            reason=reason,
            now=now,
        )

    def _finalize_failed(
        self,
        conn: Connection[DictRow],
        *,
        transfer: TransferRecord,
        failure_reason: str,
        initiated_by: str,
        reason: str,
        payout_id: str | None,
        now: datetime,
    ) -> None:
        if transfer.reserve_state == "HELD":
            release_reserved_funds(conn, transfer.client_id, transfer.source_amount)
        conn.execute(
            """
            UPDATE transfers
            SET status = 'FAILED',
                reserve_state = 'RELEASED',
                provider_payout_id = COALESCE(%s, provider_payout_id),
                failure_reason = %s,
                next_check_at = NULL,
                updated_at = %s
            WHERE transfer_id = %s
            """,
            (payout_id, failure_reason, now, transfer.transfer_id),
        )
        record_transition(
            conn,
            transfer_id=transfer.transfer_id,
            from_status=transfer.status,
            to_status="FAILED",
            initiated_by=initiated_by,
            reason=reason,
            now=now,
        )

    @staticmethod
    def _ensure_same_hash(existing: TransferRecord, req_hash: str) -> None:
        if existing.request_hash != req_hash:
            raise ApiError(
                code="IDEMPOTENCY_CONFLICT",
                message="Ключ идемпотентности уже использован с другими параметрами",
                status_code=409,
                details={"transfer_id": existing.transfer_id},
            )


class TransferRuntime:
    """Фоновый пул отправки выплат и периодический поток сверки."""

    def __init__(
        self,
        service: TransferService,
        reconcile_interval_sec: float = 1.5,
        auto_dispatch: bool = True,
    ) -> None:
        self.service = service
        self._interval = reconcile_interval_sec
        self.auto_dispatch = auto_dispatch
        self._executor = ThreadPoolExecutor(max_workers=16, thread_name_prefix="transfer-worker")
        self._stop_event = threading.Event()
        self._reconciler_thread: threading.Thread | None = None

    def start(self) -> None:
        if self.auto_dispatch:
            self.recover_incomplete()
        if self._interval > 0:
            self._reconciler_thread = threading.Thread(
                target=self._reconcile_loop,
                name="transfer-reconciler",
                daemon=True,
            )
            self._reconciler_thread.start()

    def recover_incomplete(self) -> int:
        """После рестарта досылает переводы в RESERVED и прерванные на этапе SENDING."""
        with self.service.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT t.transfer_id
                FROM transfers t
                WHERE t.status = 'RESERVED'
                   OR (
                       t.status = 'PROCESSING'
                       AND EXISTS (
                           SELECT 1 FROM provider_attempts a
                           WHERE a.transfer_id = t.transfer_id AND a.status = 'SENDING'
                       )
                   )
                ORDER BY t.created_at ASC
                """
            ).fetchall()
        for row in rows:
            self.submit(row["transfer_id"])
        return len(rows)

    def submit(self, transfer_id: str) -> None:
        if self.auto_dispatch:
            self._executor.submit(self._safe_execute, transfer_id)

    def stop(self) -> None:
        self._stop_event.set()
        if self._reconciler_thread is not None:
            self._reconciler_thread.join(timeout=3.0)
        self._executor.shutdown(wait=True, cancel_futures=False)

    def _safe_execute(self, transfer_id: str) -> None:
        try:
            self.service.execute_transfer(transfer_id)
        except Exception:
            log.exception("Unhandled error while executing transfer %s", transfer_id)

    def _reconcile_loop(self) -> None:
        while not self._stop_event.wait(self._interval):
            try:
                self.service.reconcile_due_transfers(dispatch_fn=self.submit)
            except Exception:
                log.exception("Unhandled error in reconciliation loop")
