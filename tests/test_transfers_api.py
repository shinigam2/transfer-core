import hashlib
import hmac
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.db import DbPool
from app.main import create_app
from app.provider_client import PayoutCreateResult, PayoutLookupResult
from app.rates import FileRateSource

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
WEBHOOK_SECRET = "test-webhook-secret"


@dataclass
class ManualClock:
    current: datetime = field(default_factory=lambda: datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC))

    def __call__(self) -> datetime:
        return self.current

    def advance(self, seconds: float) -> None:
        self.current = self.current + timedelta(seconds=seconds)


class FakeProviderClient:
    """Управляемый стаб провайдеров для проверки всех сценариев Level 2 и Level 3."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.create_behaviors: dict[str, list[PayoutCreateResult]] = {}
        self.lookup_behaviors: dict[tuple[str, str], PayoutLookupResult] = {}
        self.calls: list[tuple[str, str, str, str]] = []
        self.on_create_hook: Any = None
        self._by_key: dict[tuple[str, str], PayoutCreateResult] = {}

    def set_provider_results(self, provider_id: str, results: list[PayoutCreateResult]) -> None:
        with self._lock:
            self.create_behaviors[provider_id] = list(results)

    def set_lookup(self, provider_id: str, reference: str, result: PayoutLookupResult) -> None:
        with self._lock:
            self.lookup_behaviors[(provider_id, reference)] = result

    def create_payout(
        self,
        provider_id: str,
        idempotency_key: str,
        reference: str,
        amount: str,
        currency: str,
        recipient: dict[str, Any],
        callback_url: str,
        scenario: str | None = None,
    ) -> PayoutCreateResult:
        del recipient, callback_url, scenario
        with self._lock:
            self.calls.append((provider_id, idempotency_key, reference, f"{amount} {currency}"))
            queue = self.create_behaviors.get(provider_id)
            if queue:
                outcome = queue.pop(0)
                if outcome.kind == "ACCEPTED":
                    self._by_key[(provider_id, idempotency_key)] = outcome
            elif (provider_id, idempotency_key) in self._by_key:
                outcome = self._by_key[(provider_id, idempotency_key)]
            else:
                payout_id = f"po_{provider_id}_{len(self.calls)}"
                outcome = PayoutCreateResult(
                    kind="ACCEPTED",
                    payout_id=payout_id,
                    provider_status="PROCESSING",
                )
                self._by_key[(provider_id, idempotency_key)] = outcome

        if self.on_create_hook is not None:
            self.on_create_hook(provider_id, reference, outcome)
        return outcome

    def find_payout_by_reference(self, provider_id: str, reference: str) -> PayoutLookupResult:
        with self._lock:
            return self.lookup_behaviors.get(
                (provider_id, reference),
                PayoutLookupResult(kind="NOT_FOUND"),
            )


def _signed_webhook(client: TestClient, provider_id: str, event: dict[str, Any], secret: str = WEBHOOK_SECRET):
    raw = json.dumps(event).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return client.post(
        f"/webhooks/{provider_id}",
        content=raw,
        headers={"Content-Type": "application/json", "X-Signature": sig},
    )


def _create_quote(client: TestClient, amount: str = "10000.00") -> str:
    resp = client.post(
        "/quotes",
        json={
            "source_country": "RU",
            "source_currency": "RUB",
            "target_country": "TH",
            "target_currency": "THB",
            "payout_method": "BANK_ACCOUNT",
            "amount": amount,
            "amount_type": "SOURCE",
        },
    )
    assert resp.status_code == 201
    return resp.json()["quote_id"]


@pytest.fixture
def transfer_env(test_db_url: str):
    clock = ManualClock()
    fake_provider = FakeProviderClient()
    settings = Settings(
        database_url=test_db_url,
        mock_provider_url="http://mock-provider:9000",
        webhook_secret=WEBHOOK_SECRET,
        data_dir=DATA_DIR,
        reconcile_interval_sec=0.0,  # В тестах вызываем воркер и сверку явно без гонок таймера
        max_uncertainty_sec=30.0,
    )
    rate_source = FileRateSource(DATA_DIR / "rates.json", max_age_sec=3600)
    app = create_app(
        settings=settings,
        rate_source=rate_source,
        provider_client=fake_provider,
        clock=clock,
        auto_dispatch=False,
    )
    with TestClient(app) as client:
        pool: DbPool = app.state.pool
        with pool.connection() as conn, conn.transaction():
            conn.execute("TRUNCATE webhook_events, provider_attempts, transfer_transitions, transfers, quotes RESTART IDENTITY")
            conn.execute(
                """
                UPDATE client_balances
                SET available = initial_balance,
                    reserved = initial_balance - initial_balance,
                    debited = initial_balance - initial_balance
                """
            )
        yield client, app.state.transfer_service, app.state.transfer_runtime, fake_provider, clock


def test_scenario_ok_reserves_executes_and_commits_on_webhook(transfer_env) -> None:
    client, service, _, fake_provider, _ = transfer_env
    quote_id = _create_quote(client, "10000.00")

    create_resp = client.post(
        "/transfers",
        headers={"Idempotency-Key": "idem-ok-1"},
        json={
            "quote_id": quote_id,
            "client_id": "c_001",
            "recipient": {"name": "Somchai Jaidee", "account": "1234567890", "bank_code": "BKKB"},
        },
    )
    assert create_resp.status_code == 202
    transfer_id = create_resp.json()["transfer_id"]
    assert create_resp.json()["status"] == "RESERVED"

    # Средства заблокированы в резерве
    bal_held = client.get("/clients/c_001/balance").json()["balances"]["RUB"]
    assert bal_held["available"] == "490000.00"
    assert bal_held["reserved"] == "10000.00"
    assert bal_held["debited"] == "0.00"

    # Воркер отправляет выплату лучшему провайдеру (prov_alpha)
    service.execute_transfer(transfer_id)
    mid = client.get(f"/transfers/{transfer_id}").json()
    assert mid["status"] == "PROCESSING"
    assert mid["provider_id"] == "prov_alpha"
    assert len(fake_provider.calls) == 1
    assert fake_provider.calls[0][3] == "4137.00 THB"

    # Вебхук COMPLETED окончательно списывает резерв
    wh = _signed_webhook(
        client,
        "prov_alpha",
        {
            "event_id": "ev_ok_1",
            "payout_id": mid["provider_payout_id"],
            "reference": transfer_id,
            "status": "COMPLETED",
            "occurred_at": "2026-11-07T12:00:05Z",
        },
    )
    assert wh.status_code == 200

    final = client.get(f"/transfers/{transfer_id}").json()
    assert final["status"] == "COMPLETED"
    assert final["reserve_state"] == "COMMITTED"
    assert [t["to_status"] for t in final["transitions"]] == ["RESERVED", "PROCESSING", "COMPLETED"]
    assert [t["initiated_by"] for t in final["transitions"]] == ["CLIENT", "WORKER", "WEBHOOK"]

    bal_after = client.get("/clients/c_001/balance").json()["balances"]["RUB"]
    assert bal_after == {
        "currency": "RUB",
        "available": "490000.00",
        "reserved": "0.00",
        "debited": "10000.00",
    }


def test_scenario_decline_and_error_5xx_failover_and_release_when_all_fail(transfer_env) -> None:
    client, service, _, fake_provider, _ = transfer_env

    # 1. prov_alpha даёт 422 DECLINE, prov_charlie даёт 500 (выплаты нет), prov_bravo принимает 201!
    fake_provider.set_provider_results(
        "prov_alpha",
        [PayoutCreateResult(kind="DECLINED", error_code="RECIPIENT_ACCOUNT_INVALID", error_message="Declined")],
    )
    fake_provider.set_provider_results(
        "prov_charlie",
        [PayoutCreateResult(kind="RETRYABLE_ERROR", error_code="INTERNAL_ERROR", error_message="500")] * 4,
    )

    q1 = _create_quote(client, "10000.00")
    t1 = client.post(
        "/transfers",
        headers={"Idempotency-Key": "idem-failover-1"},
        json={"quote_id": q1, "client_id": "c_001", "recipient": {"name": "A", "account": "111"}},
    ).json()["transfer_id"]

    service.execute_transfer(t1)
    details = client.get(f"/transfers/{t1}").json()
    assert details["status"] == "PROCESSING"
    assert details["provider_id"] == "prov_bravo"
    assert [a["provider_id"] for a in details["attempts"]] == ["prov_alpha", "prov_charlie", "prov_bravo"]
    assert [a["status"] for a in details["attempts"]] == ["DECLINED", "DECLINED", "ACCEPTED"]

    _signed_webhook(
        client,
        "prov_bravo",
        {
            "event_id": "ev_bravo_1",
            "payout_id": details["provider_payout_id"],
            "reference": t1,
            "status": "COMPLETED",
            "occurred_at": "2026-11-07T12:00:10Z",
        },
    )
    assert client.get(f"/transfers/{t1}").json()["status"] == "COMPLETED"

    # 2. Все три провайдера отказывают -> FAILED и полный возврат резерва клиенту
    for pid in ("prov_alpha", "prov_charlie", "prov_bravo"):
        fake_provider.set_provider_results(
            pid,
            [PayoutCreateResult(kind="DECLINED", error_code="DECLINED", error_message="No")],
        )

    q2 = _create_quote(client, "5000.00")
    t2 = client.post(
        "/transfers",
        headers={"Idempotency-Key": "idem-all-decline"},
        json={"quote_id": q2, "client_id": "c_002", "recipient": {"name": "B", "account": "222"}},
    ).json()["transfer_id"]

    service.execute_transfer(t2)
    failed_body = client.get(f"/transfers/{t2}").json()
    assert failed_body["status"] == "FAILED"
    assert failed_body["reserve_state"] == "RELEASED"

    bal_c002 = client.get("/clients/c_002/balance").json()["balances"]["RUB"]
    assert bal_c002 == {
        "currency": "RUB",
        "available": "50000.00",
        "reserved": "0.00",
        "debited": "0.00",
    }


def test_scenario_slow_webhook_arrives_before_post_payout_returns(transfer_env) -> None:
    client, service, _, fake_provider, _ = transfer_env
    quote_id = _create_quote(client, "10000.00")
    tid = client.post(
        "/transfers",
        headers={"Idempotency-Key": "idem-slow-race"},
        json={"quote_id": quote_id, "client_id": "c_001", "recipient": {"name": "Slow", "account": "999"}},
    ).json()["transfer_id"]

    # Симулируем ситуацию slow: вебхук COMPLETED приходит до возврата create_payout.
    def deliver_early_webhook(provider_id: str, reference: str, outcome: PayoutCreateResult) -> None:
        _signed_webhook(
            client,
            provider_id,
            {
                "event_id": "ev_early_1",
                "payout_id": outcome.payout_id,
                "reference": reference,
                "status": "COMPLETED",
                "occurred_at": "2026-11-07T12:00:02Z",
            },
        )

    fake_provider.on_create_hook = deliver_early_webhook
    try:
        service.execute_transfer(tid)
    finally:
        fake_provider.on_create_hook = None

    final = client.get(f"/transfers/{tid}").json()
    assert final["status"] == "COMPLETED"
    assert final["reserve_state"] == "COMMITTED"


def test_idempotency_replay_parallel_double_submit_and_conflict(transfer_env) -> None:
    client, service, _, _, _ = transfer_env
    quote_id = _create_quote(client, "10000.00")
    payload = {
        "quote_id": quote_id,
        "client_id": "c_001",
        "recipient": {"name": "Ivan", "account": "777"},
    }

    # Параллельная отправка с одним и тем же Idempotency-Key
    def post_once():
        return client.post("/transfers", headers={"Idempotency-Key": "idem-parallel"}, json=payload)

    with ThreadPoolExecutor(max_workers=2) as pool:
        r1, r2 = list(pool.map(lambda _: post_once(), range(2)))

    assert r1.status_code == 202
    assert r2.status_code == 202
    tid = r1.json()["transfer_id"]
    assert r2.json()["transfer_id"] == tid

    # Резерв списался ровно один раз (10000, а не 20000)
    bal = client.get("/clients/c_001/balance").json()["balances"]["RUB"]
    assert bal["reserved"] == "10000.00"
    assert bal["available"] == "490000.00"

    # Завершаем перевод и проверяем идемпотентный повтор после завершения (HTTP 200)
    service.execute_transfer(tid)
    _signed_webhook(
        client,
        "prov_alpha",
        {
            "event_id": "ev_idem_done",
            "payout_id": "po_done",
            "reference": tid,
            "status": "COMPLETED",
            "occurred_at": "2026-11-07T12:00:10Z",
        },
    )
    after_done = client.post("/transfers", headers={"Idempotency-Key": "idem-parallel"}, json=payload)
    assert after_done.status_code == 200
    assert after_done.json()["transfer_id"] == tid
    assert after_done.json()["status"] == "COMPLETED"

    # Тот же Idempotency-Key, но другое тело -> 409 IDEMPOTENCY_CONFLICT
    conflict = client.post(
        "/transfers",
        headers={"Idempotency-Key": "idem-parallel"},
        json={**payload, "recipient": {"name": "Other", "account": "888"}},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"


def test_webhook_forged_duplicate_out_of_order_contradiction_and_unknown(transfer_env) -> None:
    client, service, _, _, _ = transfer_env
    quote_id = _create_quote(client, "10000.00")
    tid = client.post(
        "/transfers",
        headers={"Idempotency-Key": "idem-wh-chaos"},
        json={"quote_id": quote_id, "client_id": "c_001", "recipient": {"name": "Test", "account": "555"}},
    ).json()["transfer_id"]
    service.execute_transfer(tid)

    # 1. Поддельный вебхук (неверная подпись и отсутствующая подпись) -> 401
    forged_event = {
        "event_id": "ev_forged",
        "payout_id": "po_1",
        "reference": tid,
        "status": "FAILED",
        "occurred_at": "2026-11-07T12:00:01Z",
    }
    bad_sig = client.post(
        "/webhooks/prov_alpha",
        content=json.dumps(forged_event).encode(),
        headers={"Content-Type": "application/json", "X-Signature": "deadbeef"},
    )
    no_sig = client.post("/webhooks/prov_alpha", json=forged_event)
    assert bad_sig.status_code == 401
    assert no_sig.status_code == 401
    assert client.get(f"/transfers/{tid}").json()["status"] == "PROCESSING"

    # 2. Вебхук по неизвестной выплате -> 200 ignored
    unknown = _signed_webhook(
        client,
        "prov_alpha",
        {
            "event_id": "ev_unknown",
            "payout_id": "po_unknown",
            "reference": "tr_does_not_exist",
            "status": "COMPLETED",
            "occurred_at": "2026-11-07T12:00:02Z",
        },
    )
    assert unknown.status_code == 200
    assert unknown.json()["status"] == "ignored"

    # 3. Валидный COMPLETED + его дубль + запоздавший PROCESSING + противоречивый FAILED
    valid_completed = {
        "event_id": "ev_real_1",
        "payout_id": "po_1",
        "reference": tid,
        "status": "COMPLETED",
        "occurred_at": "2026-11-07T12:00:05Z",
    }
    assert _signed_webhook(client, "prov_alpha", valid_completed).json()["status"] == "processed"
    assert _signed_webhook(client, "prov_alpha", valid_completed).json()["status"] == "duplicate"

    late_proc = _signed_webhook(
        client,
        "prov_alpha",
        {
            "event_id": "ev_late_proc",
            "payout_id": "po_1",
            "reference": tid,
            "status": "PROCESSING",
            "occurred_at": "2026-11-07T12:00:01Z",
        },
    )
    assert late_proc.json()["status"] == "ignored"

    contradiction = _signed_webhook(
        client,
        "prov_alpha",
        {
            "event_id": "ev_contradict",
            "payout_id": "po_1",
            "reference": tid,
            "status": "FAILED",
            "occurred_at": "2026-11-07T12:00:04Z",
        },
    )
    assert contradiction.json()["status"] == "ignored"

    # Перевод остаётся COMPLETED, баланс списан ровно один раз
    assert client.get(f"/transfers/{tid}").json()["status"] == "COMPLETED"
    bal = client.get("/clients/c_001/balance").json()["balances"]["RUB"]
    assert bal["debited"] == "10000.00"
    assert bal["reserved"] == "0.00"


def test_uncertain_response_never_fails_over_and_reconciles_or_marks_needs_review(transfer_env) -> None:
    client, service, _, fake_provider, clock = transfer_env

    # Кейс 1: обрыв связи у prov_alpha (UNCERTAIN на всех попытках) -> НЕ идём в prov_charlie,
    # переходим в RECONCILING, затем фоновая сверка находит выплату COMPLETED (при потерянном вебхуке).
    fake_provider.set_provider_results(
        "prov_alpha",
        [PayoutCreateResult(kind="UNCERTAIN", error_code="TRANSPORT_ERROR", error_message="Connection reset")] * 4,
    )

    q1 = _create_quote(client, "10000.00")
    t1 = client.post(
        "/transfers",
        headers={"Idempotency-Key": "idem-uncertain-1"},
        json={"quote_id": q1, "client_id": "c_001", "recipient": {"name": "U1", "account": "101"}},
    ).json()["transfer_id"]

    service.execute_transfer(t1)
    mid = client.get(f"/transfers/{t1}").json()
    assert mid["status"] == "RECONCILING"
    # Вызывался ТОЛЬКО prov_alpha (4 попытки с одним и тем же ключом идемпотентности), failover запрещён!
    assert {call[0] for call in fake_provider.calls} == {"prov_alpha"}
    assert len({call[1] for call in fake_provider.calls}) == 1

    # Спустя 5с visibility_lag прошёл, сверка видит COMPLETED
    fake_provider.set_lookup(
        "prov_alpha",
        t1,
        PayoutLookupResult(kind="FOUND", payout_id="po_recovered", provider_status="COMPLETED"),
    )
    clock.advance(5)
    service.reconcile_due_transfers()

    resolved = client.get(f"/transfers/{t1}").json()
    assert resolved["status"] == "COMPLETED"
    assert resolved["reserve_state"] == "COMMITTED"

    # Кейс 2: провайдер так и не ответил за всё окно max_uncertainty_sec -> NEEDS_REVIEW с сохранением резерва
    fake_provider.calls.clear()
    fake_provider.set_provider_results(
        "prov_alpha",
        [PayoutCreateResult(kind="UNCERTAIN", error_code="TRANSPORT_ERROR", error_message="Timeout")] * 4,
    )
    q2 = _create_quote(client, "5000.00")
    t2 = client.post(
        "/transfers",
        headers={"Idempotency-Key": "idem-uncertain-review"},
        json={"quote_id": q2, "client_id": "c_002", "recipient": {"name": "U2", "account": "102"}},
    ).json()["transfer_id"]

    service.execute_transfer(t2)
    assert client.get(f"/transfers/{t2}").json()["status"] == "RECONCILING"

    clock.advance(35)
    service.reconcile_due_transfers()

    review = client.get(f"/transfers/{t2}").json()
    assert review["status"] == "NEEDS_REVIEW"
    assert review["reserve_state"] == "HELD"
    # У c_002 5000 RUB остаются надёжно в резерве до ручной проверки
    bal_c002 = client.get("/clients/c_002/balance").json()["balances"]["RUB"]
    assert bal_c002 == {
        "currency": "RUB",
        "available": "45000.00",
        "reserved": "5000.00",
        "debited": "0.00",
    }


def test_crash_between_sending_and_recording_recovers_with_same_idempotency_key(transfer_env) -> None:
    client, service, runtime, fake_provider, clock = transfer_env
    quote_id = _create_quote(client, "10000.00")
    tid = client.post(
        "/transfers",
        headers={"Idempotency-Key": "idem-crash-1"},
        json={"quote_id": quote_id, "client_id": "c_001", "recipient": {"name": "Crash", "account": "404"}},
    ).json()["transfer_id"]

    # Имитируем падение процесса сразу после записи намерения SENDING в БД (до получения ответа)
    started = service._begin_provider_attempt(tid, "prov_alpha", clock())
    assert started is not None
    attempt, _ = started

    # Провайдер на самом деле успел создать выплату под этим ключом до падения нашего процесса
    fake_provider._by_key[("prov_alpha", attempt.idempotency_key)] = PayoutCreateResult(
        kind="ACCEPTED",
        payout_id="po_before_crash",
        provider_status="PROCESSING",
    )

    runtime.auto_dispatch = True
    try:
        recovered_count = runtime.recover_incomplete()
        assert recovered_count == 1
        time.sleep(0.1)
    finally:
        runtime.auto_dispatch = False

    after = client.get(f"/transfers/{tid}").json()
    assert after["status"] == "PROCESSING"
    assert after["provider_payout_id"] == "po_before_crash"
    assert len(after["attempts"]) == 1
    assert after["attempts"][0]["status"] == "ACCEPTED"
