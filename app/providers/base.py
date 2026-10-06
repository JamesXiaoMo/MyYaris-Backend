from abc import ABC, abstractmethod
from typing import List, Optional
from pydantic import BaseModel


class ProviderNotConfiguredError(Exception):
    pass


class SSRFSecurityError(Exception):
    pass


class PlaceCandidateItem(BaseModel):
    title: str
    address: Optional[str] = None
    latitude: float
    longitude: float
    coord_system: str = "wgs84"
    source: str = "search"
    place_id: Optional[str] = None


class BaseASRProvider(ABC):
    @abstractmethod
    async def transcribe(self, audio_path: str) -> str:
        """Transcribe audio file to text."""
        pass


class BaseNLPProvider(ABC):
    @abstractmethod
    async def extract_destinations(self, text: str, context: Optional[dict] = None) -> List[str]:
        """Extract candidate search queries or intents from natural language."""
        pass


class BaseMapsProvider(ABC):
    @abstractmethod
    async def search(self, query: str, lat: Optional[float] = None, lon: Optional[float] = None) -> List[PlaceCandidateItem]:
        """Search places by query keyword and optional current location context."""
        pass
