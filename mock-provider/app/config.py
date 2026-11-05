import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    mode: str
    seed: str
    default_scenario: str
    data_dir: str
    webhook_secret: str
    settle_sec: float
    slow_sec: float
    hang_sec: float
    port: int


def load_settings():
    mode = os.environ.get("MOCK_MODE", "normal").lower()
    if mode not in ("normal", "chaos"):
        raise SystemExit(f"MOCK_MODE must be normal or chaos, got {mode}")
    return Settings(
        mode=mode,
        seed=os.environ.get("MOCK_SEED", "1"),
        default_scenario=os.environ.get("MOCK_SCENARIO", "ok"),
        data_dir=os.environ.get("DATA_DIR", "/data"),
        webhook_secret=os.environ.get("PROVIDER_WEBHOOK_SECRET", "local-dev-webhook-secret"),
        settle_sec=float(os.environ.get("MOCK_SETTLE_SEC", "2")),
        slow_sec=float(os.environ.get("MOCK_SLOW_SEC", "30")),
        hang_sec=float(os.environ.get("MOCK_HANG_SEC", "35")),
        port=int(os.environ.get("MOCK_PORT", "9000")),
    )
