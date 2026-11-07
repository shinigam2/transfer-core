CREATE TABLE clients (
    client_id            TEXT PRIMARY KEY,
    daily_limit_amount   NUMERIC NOT NULL CHECK (daily_limit_amount >= 0),
    daily_limit_currency TEXT NOT NULL
);

CREATE TABLE client_balances (
    client_id       TEXT NOT NULL REFERENCES clients(client_id),
    currency        TEXT NOT NULL,
    initial_balance NUMERIC NOT NULL CHECK (initial_balance >= 0),
    available       NUMERIC NOT NULL CHECK (available >= 0),
    reserved        NUMERIC NOT NULL CHECK (reserved >= 0),
    debited         NUMERIC NOT NULL CHECK (debited >= 0),
    PRIMARY KEY (client_id, currency),
    CHECK (available + reserved + debited = initial_balance)
);

CREATE TABLE transfers (
    transfer_id          TEXT PRIMARY KEY,
    quote_id             TEXT NOT NULL REFERENCES quotes(quote_id),
    client_id            TEXT NOT NULL REFERENCES clients(client_id),
    idempotency_key      TEXT NOT NULL UNIQUE,
    request_hash         TEXT NOT NULL,
    direction_id         TEXT NOT NULL,
    source_amount        NUMERIC NOT NULL CHECK (source_amount > 0),
    source_currency      TEXT NOT NULL,
    fee_amount           NUMERIC NOT NULL CHECK (fee_amount >= 0),
    fee_currency         TEXT NOT NULL,
    target_amount        NUMERIC NOT NULL CHECK (target_amount > 0),
    target_currency      TEXT NOT NULL,
    daily_limit_amount   NUMERIC NOT NULL CHECK (daily_limit_amount > 0),
    daily_limit_currency TEXT NOT NULL,
    recipient            JSONB NOT NULL,
    status               TEXT NOT NULL,
    reserve_state        TEXT NOT NULL CHECK (reserve_state IN ('HELD', 'COMMITTED', 'RELEASED')),
    active_provider_id   TEXT,
    provider_payout_id   TEXT,
    failure_reason       TEXT,
    next_check_at        TIMESTAMPTZ,
    uncertain_since      TIMESTAMPTZ,
    created_at           TIMESTAMPTZ NOT NULL,
    updated_at           TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_transfers_client_created
    ON transfers (client_id, created_at);

CREATE INDEX idx_transfers_reconcile
    ON transfers (next_check_at)
    WHERE status IN ('RESERVED', 'PROCESSING', 'RECONCILING');

CREATE TABLE transfer_transitions (
    id           BIGSERIAL PRIMARY KEY,
    transfer_id  TEXT NOT NULL REFERENCES transfers(transfer_id),
    from_status  TEXT,
    to_status    TEXT NOT NULL,
    initiated_by TEXT NOT NULL,
    reason       TEXT,
    created_at   TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_transitions_transfer
    ON transfer_transitions (transfer_id, id);

CREATE TABLE provider_attempts (
    attempt_id       TEXT PRIMARY KEY,
    transfer_id      TEXT NOT NULL REFERENCES transfers(transfer_id),
    provider_id      TEXT NOT NULL,
    idempotency_key  TEXT NOT NULL UNIQUE,
    status           TEXT NOT NULL,
    payout_id        TEXT,
    error_code       TEXT,
    error_message    TEXT,
    created_at       TIMESTAMPTZ NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_attempts_transfer
    ON provider_attempts (transfer_id, created_at);

CREATE TABLE webhook_events (
    provider_id  TEXT NOT NULL,
    event_id     TEXT NOT NULL,
    payout_id    TEXT NOT NULL,
    reference    TEXT NOT NULL,
    status       TEXT NOT NULL,
    occurred_at  TIMESTAMPTZ NOT NULL,
    received_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (provider_id, event_id)
);

CREATE INDEX idx_webhook_events_reference
    ON webhook_events (provider_id, reference);
