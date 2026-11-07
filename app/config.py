import os
from dataclasses import dataclass
from pathlib import Path


# только для чтения
@dataclass(frozen=True)
class Settings:
    database_url: str
    mock_provider_url: str
    webhook_secret: str
    data_dir: Path


def load_settings() -> Settings:
    return Settings(
        database_url=os.environ["DATABASE_URL"],
        mock_provider_url=os.environ.get(
            "MOCK_PROVIDER_URL", "http://localhost:9000"
        ).rstrip("/"),
        webhook_secret=os.environ.get("PROVIDER_WEBHOOK_SECRET", ""),
        data_dir=Path(os.environ.get("DATA_DIR", "data")),
    )
