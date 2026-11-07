ALTER TABLE transfers
    ADD COLUMN scenario_header TEXT;

ALTER TABLE provider_attempts
    ADD COLUMN seq BIGSERIAL NOT NULL;
