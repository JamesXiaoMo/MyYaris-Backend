import os
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    PROJECT_NAME: str = "MyYaris Backend"
    VERSION: str = "1.0.0"
    HOST: str = "0.0.0.0"
    PORT: int = 8000

    # Base storage directory (local to host)
    DATA_DIR: str = os.getenv(
        "MYYARIS_DATA_DIR",
        str(Path.home() / ".local" / "share" / "myyaris"),
    )

    # Database
    DATABASE_URL: str = ""

    # Audio handling
    AUDIO_TEMP_DIR: str = ""
    AUDIO_RETENTION_HOURS: int = 24
    MAX_AUDIO_FILE_SIZE_BYTES: int = 15 * 1024 * 1024  # 15 MB

    # Task & Polling policies
    TASK_DEFAULT_EXPIRY_MINUTES: int = 60
    ACK_TIMEOUT_SECONDS: int = 30
    LONG_POLL_TIMEOUT_SECONDS: int = 20
    MAX_ACTIVE_LONG_POLL_PER_DEVICE: int = 2
    DEFAULT_CAR_DEVICE_ID: str = "car_p3pro"

    # Pre-shared tokens for authentication
    CAR_TOKEN: str = "myyaris-car-secret-token"
    PHONE_TOKEN: str = "myyaris-phone-secret-token"
    ADMIN_TOKEN: str = "myyaris-admin-secret-token"

    # External providers
    ASR_PROVIDER: str = "mock"  # mock | none | whisper_local | openai
    ASR_API_KEY: str = ""
    NLP_PROVIDER: str = "mock"  # mock | none | openai | gemini
    NLP_API_KEY: str = ""
    MAPS_PROVIDER: str = "mock"  # mock | google
    GOOGLE_MAPS_API_KEY: str = ""

    # Allow mock providers when real keys are missing
    ALLOW_MOCK_FALLBACK: bool = True

    def init_directories(self) -> None:
        data_path = Path(self.DATA_DIR)
        data_path.mkdir(parents=True, exist_ok=True)
        if not self.AUDIO_TEMP_DIR:
            self.AUDIO_TEMP_DIR = str(data_path / "audio_temp")
        Path(self.AUDIO_TEMP_DIR).mkdir(parents=True, exist_ok=True)

        if not self.DATABASE_URL:
            db_file = data_path / "myyaris.db"
            self.DATABASE_URL = f"sqlite:///{db_file}"


settings = Settings()
settings.init_directories()
