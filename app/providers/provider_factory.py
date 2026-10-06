from app.config import settings
from app.providers.base import (
    BaseASRProvider,
    BaseMapsProvider,
    BaseNLPProvider,
    ProviderNotConfiguredError,
)
from app.providers.mock_providers import (
    MockASRProvider,
    MockMapsProvider,
    MockNLPProvider,
)


def get_asr_provider() -> BaseASRProvider:
    if settings.ALLOW_MOCK_FALLBACK and settings.ASR_PROVIDER == "mock":
        return MockASRProvider()
    raise ProviderNotConfiguredError("ASR provider is not configured.")


def get_nlp_provider() -> BaseNLPProvider:
    if settings.ALLOW_MOCK_FALLBACK and settings.NLP_PROVIDER == "mock":
        return MockNLPProvider()
    raise ProviderNotConfiguredError("NLP provider is not configured.")


def get_maps_provider() -> BaseMapsProvider:
    if settings.MAPS_PROVIDER == "nominatim":
        from app.providers.nominatim_provider import get_nominatim_provider
        return get_nominatim_provider()
    if settings.ALLOW_MOCK_FALLBACK and settings.MAPS_PROVIDER == "mock":
        return MockMapsProvider()
    raise ProviderNotConfiguredError(f"Maps provider '{settings.MAPS_PROVIDER}' is not configured.")

