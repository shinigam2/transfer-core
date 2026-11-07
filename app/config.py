import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    database_url: str
    mock_provider_url: str
    webhook_secret: str
    data_dir: Path
    callback_base_url: str = "http://api:8080"
    max_rate_age_sec: int = 3600
    provider_timeout_sec: float = 32.0
    reconcile_interval_sec: float = 1.5
    max_uncertainty_sec: float = 45.0


def load_settings() -> Settings:
    return Settings(
        database_url=os.environ["DATABASE_URL"],
        mock_provider_url=os.environ.get("MOCK_PROVIDER_URL", "http://localhost:9000").rstrip("/"),
        webhook_secret=os.environ.get("PROVIDER_WEBHOOK_SECRET", ""),
        data_dir=Path(os.environ.get("DATA_DIR", "data")),
        callback_base_url=os.environ.get("CALLBACK_BASE_URL", "http://api:8080").rstrip("/"),
        max_rate_age_sec=int(os.environ.get("MAX_RATE_AGE_SEC", "3600")),
        provider_timeout_sec=float(os.environ.get("PROVIDER_TIMEOUT_SEC", "32.0")),
        reconcile_interval_sec=float(os.environ.get("RECONCILE_INTERVAL_SEC", "1.5")),
        max_uncertainty_sec=float(os.environ.get("MAX_UNCERTAINTY_SEC", "45.0")),
    )
