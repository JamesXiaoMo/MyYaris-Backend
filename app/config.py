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

    # Media library & Cache (V2.0)
    LOCAL_TRACKS_DIR: str = ""
    MEDIA_CACHE_DIR: str = ""
    MEDIA_CACHE_MAX_BYTES: int = 50 * 1024 * 1024 * 1024  # 50 GB
    NETEASE_API_BASE_URL: str = "http://127.0.0.1:3001"

    # Task & Polling policies
    TASK_DEFAULT_EXPIRY_MINUTES: int = 60
    ACK_TIMEOUT_SECONDS: int = 30
    LONG_POLL_TIMEOUT_SECONDS: int = 20
    MAX_ACTIVE_LONG_POLL_PER_DEVICE: int = 2
    DEFAULT_CAR_DEVICE_ID: str = "car_p3pro"

    # Pre-shared tokens for authentication (Must be configured in .env or environment)
    CAR_TOKEN: str = ""
    PHONE_TOKEN: str = ""
    ADMIN_TOKEN: str = ""

    # External providers
    ASR_PROVIDER: str = "none"  # none | whisper_local | openai | mock (test only)
    ASR_API_KEY: str = ""
    NLP_PROVIDER: str = "none"  # none | openai | gemini | mock (test only)
    NLP_API_KEY: str = ""
    MAPS_PROVIDER: str = "nominatim"  # nominatim | google | none | mock (test only)
    GOOGLE_MAPS_API_KEY: str = ""

    # Nominatim (OpenStreetMap) Provider Settings
    NOMINATIM_BASE_URL: str = "https://nominatim.openstreetmap.org/search"
    NOMINATIM_USER_AGENT: str = "MyYaris/1.0 (https://github.com/myyaris-user/MyYaris; contact: myyaris-user@local)"
    NOMINATIM_COUNTRY_CODES: str = "jp"
    NOMINATIM_RATE_LIMIT_SECONDS: float = 1.05
    NOMINATIM_CACHE_TTL_SECONDS: float = 86400.0

    # Mock fallback is strictly disabled in production to prevent fake navigation coordinates
    ALLOW_MOCK_FALLBACK: bool = False

    def init_directories(self) -> None:
        data_path = Path(self.DATA_DIR)
        data_path.mkdir(parents=True, exist_ok=True)
        if not self.AUDIO_TEMP_DIR:
            self.AUDIO_TEMP_DIR = str(data_path / "audio_temp")
        Path(self.AUDIO_TEMP_DIR).mkdir(parents=True, exist_ok=True)

        if not self.LOCAL_TRACKS_DIR:
            self.LOCAL_TRACKS_DIR = os.getenv(
                "MYYARIS_LOCAL_TRACKS_DIR",
                str(data_path / "media" / "local_tracks"),
            )
        Path(self.LOCAL_TRACKS_DIR).mkdir(parents=True, exist_ok=True)

        if not self.MEDIA_CACHE_DIR:
            self.MEDIA_CACHE_DIR = os.getenv(
                "MYYARIS_MEDIA_CACHE_DIR",
                str(data_path / "media" / "cache"),
            )
        Path(self.MEDIA_CACHE_DIR).mkdir(parents=True, exist_ok=True)

        if not self.DATABASE_URL:
            db_file = data_path / "myyaris.db"
            self.DATABASE_URL = f"sqlite:///{db_file}"


settings = Settings()
settings.init_directories()
