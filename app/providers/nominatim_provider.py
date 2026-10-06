import asyncio
import logging
import re
import time
from typing import List, Optional
import httpx
from app.config import settings
from app.providers.base import BaseMapsProvider, PlaceCandidateItem

logger = logging.getLogger(__name__)

# Area & administrative boundary types in OpenStreetMap Nominatim
AREA_TYPES = {
    "country",
    "state",
    "province",
    "region",
    "county",
    "city",
    "town",
    "village",
    "suburb",
    "neighbourhood",
    "quarter",
    "administrative",
    "postcode",
}

# Known Romaji to Japanese Kanji conversions
ROMAJI_MAP = [
    (r"\bSaijocho\s+Jike\b", "西条町寺家"),
    (r"\bSaijo-cho\s+Jike\b", "西条町寺家"),
    (r"\bSaijocho\b", "西条町"),
    (r"\bSaijo-cho\b", "西条町"),
    (r"\bJike\b", "寺家"),
    (r"\bTokyo\b", "東京"),
    (r"\bKyoto\b", "京都"),
    (r"\bOsaka\b", "大阪"),
    (r"\bYokohama\b", "横浜"),
    (r"\bNagoya\b", "名古屋"),
    (r"\bHiroshima\b", "広島"),
    (r"\bKobe\b", "神戸"),
    (r"\bFukuoka\b", "福岡"),
    (r"\bSapporo\b", "札幌"),
    (r"\bSendai\b", "仙台"),
]

# Simplified Chinese to Japanese Shinjitai Kanji conversions
KANJI_MAP = [
    ("东广岛", "東広島"),
    ("广岛", "広島"),
    ("东", "東"),
    ("广", "広"),
    ("县", "県"),
    ("岛", "島"),
    ("冈", "岡"),
    ("静冈", "静岡"),
    ("福冈", "福岡"),
    ("鸟取", "鳥取"),
    ("鹿儿岛", "鹿児島"),
    ("长崎", "長崎"),
    ("爱知", "愛知"),
    ("爱媛", "愛媛"),
    ("冲绳", "沖縄"),
    ("车站", "駅"),
    ("站", "駅"),
    ("铁道", "鉄道"),
]


JAPAN_PREFECTURES = [
    "東京都", "北海道", "京都府", "大阪府",
    "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群马県", "埼玉県", "千葉県", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県",
    "岐阜県", "静岡県", "愛知県", "三重県", "滋賀県", "兵庫県",
    "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県", "広島県",
    "山口県", "徳島県", "香川県", "愛媛県", "高知県", "福岡県",
    "佐賀県", "長崎県", "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
]
PREF_REGEX = "(" + "|".join(JAPAN_PREFECTURES) + ")"


def normalize_japanese_address(text: str) -> str:
    """
    Normalizes mixed-language, simplified Chinese, or romanized Japanese addresses
    into standard Japanese Kanji format suitable for Nominatim OpenStreetMap queries.
    Avoids arbitrary hallucination while reliably handling common mobile share text.
    """
    if not text:
        return ""

    s = text.strip()
    s = re.sub(r"[\r\n\t]+", " ", s)

    # 1. Apply Romaji mappings
    for pat, rep in ROMAJI_MAP:
        s = re.sub(pat, rep, s, flags=re.IGNORECASE)

    # 2. Apply Kanji conversions
    for pat, rep in KANJI_MAP:
        s = s.replace(pat, rep)

    # 3. Clean up commas, plus signs
    s = s.replace(",", " ").replace("+", " ")

    # 4. Extract postal code
    s = re.sub(r"\b\d{3}-\d{4}\b", " ", s)

    # 5. Extract prefecture
    m_pref = re.search(PREF_REGEX, s)
    prefecture = ""
    if m_pref:
        prefecture = m_pref.group(0)
        s = s.replace(prefecture, " ")

    # 6. Extract city/county/ward
    m_city = re.search(r"([^\s]+?(?:市|郡))", s)
    city = ""
    if m_city:
        city = m_city.group(0).strip()
        s = s.replace(city, " ")

    # 7. Remainder (neighbourhood / street / district)
    remainder = re.sub(r"\s+", " ", s).strip()

    parts = [p for p in [prefecture, city, remainder] if p]
    norm_str = "".join(parts).strip()
    return norm_str if norm_str else text.strip()


class NominatimRateLimiter:
    """
    Enforces Nominatim OSM Foundation Usage Policy:
    Strict maximum of 1 request per second across the application.
    """

    def __init__(self, min_interval_seconds: float = 1.05):
        self.min_interval = min_interval_seconds
        self._lock = asyncio.Lock()
        self._last_request_time = 0.0

    async def acquire(self) -> None:
        async with self._lock:
            loop = asyncio.get_running_loop()
            now = loop.time()
            elapsed = now - self._last_request_time
            if elapsed < self.min_interval:
                await asyncio.sleep(self.min_interval - elapsed)
            self._last_request_time = loop.time()


class NominatimMapsProvider(BaseMapsProvider):
    """
    OpenStreetMap Nominatim Maps Provider compliant with OSMF usage guidelines:
    - Identifiable User-Agent with contact/app information.
    - Global 1 request/s rate limiting.
    - In-memory result caching and in-flight request deduplication.
    - Honest attribution and regional precision indication.
    """

    def __init__(
        self,
        base_url: str = settings.NOMINATIM_BASE_URL,
        user_agent: str = settings.NOMINATIM_USER_AGENT,
        country_codes: str = settings.NOMINATIM_COUNTRY_CODES,
        rate_limiter: Optional[NominatimRateLimiter] = None,
        cache_ttl_seconds: float = settings.NOMINATIM_CACHE_TTL_SECONDS,
    ):
        self.base_url = base_url
        self.user_agent = user_agent
        self.country_codes = country_codes
        self.rate_limiter = rate_limiter or NominatimRateLimiter(settings.NOMINATIM_RATE_LIMIT_SECONDS)
        self.cache_ttl = cache_ttl_seconds

        # Cache: normalized_query -> (timestamp, List[PlaceCandidateItem])
        self._cache: dict[str, tuple[float, list[PlaceCandidateItem]]] = {}
        # In-flight deduplication: normalized_query -> asyncio.Future
        self._in_flight: dict[str, asyncio.Future] = {}
        self._in_flight_lock = asyncio.Lock()

    async def search(
        self,
        query: str,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
    ) -> List[PlaceCandidateItem]:
        if not query or not query.strip():
            return []

        raw_query = query.strip()
        norm_query = normalize_japanese_address(raw_query)
        cache_key = norm_query.lower()

        # 1. Check in-memory cache
        if cache_key in self._cache:
            cached_time, cached_items = self._cache[cache_key]
            if time.time() - cached_time < self.cache_ttl:
                logger.info(f"Nominatim cache hit for '{cache_key}' ({len(cached_items)} candidates)")
                return [item.model_copy() for item in cached_items]

        # 2. In-flight request deduplication (stampede prevention)
        future_to_await: Optional[asyncio.Future] = None
        is_leader = False

        async with self._in_flight_lock:
            if cache_key in self._in_flight:
                future_to_await = self._in_flight[cache_key]
            else:
                loop = asyncio.get_running_loop()
                future_to_await = loop.create_future()
                self._in_flight[cache_key] = future_to_await
                is_leader = True

        if not is_leader and future_to_await is not None:
            logger.info(f"Joining in-flight Nominatim request for '{cache_key}'")
            try:
                results = await asyncio.shield(future_to_await)
                return [item.model_copy() for item in results]
            except Exception as e:
                logger.warning(f"In-flight request failed for '{cache_key}': {e}")
                return []

        # 3. Leader executes the request pipeline
        try:
            results = await self._execute_search(norm_query, raw_query)
            self._cache[cache_key] = (time.time(), results)
            if future_to_await and not future_to_await.done():
                future_to_await.set_result(results)
            return [item.model_copy() for item in results]
        except Exception as e:
            if future_to_await and not future_to_await.done():
                future_to_await.set_exception(e)
            logger.error(f"Nominatim search error for '{raw_query}': {e}", exc_info=True)
            return []
        finally:
            async with self._in_flight_lock:
                self._in_flight.pop(cache_key, None)

    async def _execute_search(self, norm_query: str, raw_query: str) -> List[PlaceCandidateItem]:
        # Formulate ordered candidate search terms
        terms: list[str] = []
        if norm_query:
            terms.append(norm_query)
        if raw_query and raw_query != norm_query and raw_query not in terms:
            terms.append(raw_query)

        # Extract postal code as ultimate fallback
        m_postal = re.search(r"\b(\d{3}-\d{4})\b", raw_query or norm_query)
        if m_postal:
            postal_code = m_postal.group(1)
            if postal_code not in terms:
                terms.append(postal_code)

        for term in terms:
            items = await self._query_nominatim_api(term)
            if items:
                return items

        return []

    async def _query_nominatim_api(self, term: str) -> List[PlaceCandidateItem]:
        # Enforce strict 1 req/s rate limit before network dispatch
        await self.rate_limiter.acquire()

        headers = {
            "User-Agent": self.user_agent,
            "Accept": "application/json",
        }
        params = {
            "q": term,
            "format": "jsonv2",
            "addressdetails": "1",
            "countrycodes": self.country_codes,
            "limit": "3",
            "accept-language": "ja,en",
        }

        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                resp = await client.get(self.base_url, params=params, headers=headers)
                if resp.status_code == 429:
                    logger.warning(f"Nominatim 429 Too Many Requests encountered for term '{term}'")
                    return []
                resp.raise_for_status()
                data = resp.json()
                if not isinstance(data, list):
                    return []

                candidates: list[PlaceCandidateItem] = []
                for item in data[:3]:
                    try:
                        lat = float(item["lat"])
                        lon = float(item["lon"])
                    except (KeyError, ValueError, TypeError):
                        continue

                    name = (item.get("name") or "").strip()
                    display_name = (item.get("display_name") or "").strip()
                    item_type = item.get("type", "place")
                    addresstype = item.get("addresstype", item_type)
                    category = item.get("category", "")

                    is_area = (
                        addresstype in AREA_TYPES
                        or item_type in AREA_TYPES
                        or category in ("boundary", "place")
                    )

                    base_name = name if name else (display_name.split(",")[0].strip() if display_name else "未知地点")
                    if is_area:
                        title = f"{base_name} (地区中心·非精确图钉)"
                    else:
                        title = base_name

                    address_str = f"{display_name} [© OpenStreetMap contributors]"

                    cand = PlaceCandidateItem(
                        title=title,
                        address=address_str,
                        latitude=lat,
                        longitude=lon,
                        coord_system="wgs84",
                        source=f"nominatim:{addresstype}",
                        place_id=f"osm_{item.get('place_id', '')}",
                    )
                    candidates.append(cand)

                return candidates

        except (httpx.TimeoutException, httpx.RequestError) as e:
            logger.warning(f"Nominatim HTTP query error for '{term}': {e}")
            return []


# Singleton instance for process-wide cache & rate limiter sharing
_singleton_nominatim_provider: Optional[NominatimMapsProvider] = None


def get_nominatim_provider() -> NominatimMapsProvider:
    global _singleton_nominatim_provider
    if _singleton_nominatim_provider is None:
        _singleton_nominatim_provider = NominatimMapsProvider()
    return _singleton_nominatim_provider
