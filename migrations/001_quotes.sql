CREATE TABLE quotes (
    quote_id             TEXT PRIMARY KEY,
    direction_id         TEXT NOT NULL,
    source_country       TEXT NOT NULL,
    source_currency      TEXT NOT NULL,
    target_country       TEXT NOT NULL,
    target_currency      TEXT NOT NULL,
    payout_method        TEXT NOT NULL,
    source_amount        NUMERIC NOT NULL CHECK (source_amount > 0),
    fee_amount           NUMERIC NOT NULL CHECK (fee_amount >= 0),
    fee_currency         TEXT NOT NULL,
    amount_to_convert    NUMERIC NOT NULL CHECK (amount_to_convert > 0),
    convert_currency     TEXT NOT NULL,
    rate                 TEXT NOT NULL,
    target_amount        NUMERIC NOT NULL CHECK (target_amount > 0),
    breakdown            JSONB NOT NULL,
    created_at           TIMESTAMPTZ NOT NULL,
    expires_at           TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_quotes_expires_at ON quotes (expires_at);
