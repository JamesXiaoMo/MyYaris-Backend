import re
from typing import List, Optional
from app.providers.base import BaseASRProvider, BaseNLPProvider, BaseMapsProvider, PlaceCandidateItem


class MockASRProvider(BaseASRProvider):
    async def transcribe(self, audio_path: str) -> str:
        # Check if test file name or content hints specific test string
        if "narita" in audio_path.lower():
            return "导航去成田机场"
        if "lawson" in audio_path.lower():
            return "去附近的罗森便利店"
        return "我想去东京铁塔"


class MockNLPProvider(BaseNLPProvider):
    async def extract_destinations(self, text: str, context: Optional[dict] = None) -> List[str]:
        cleaned = text.strip()
        # Remove common speech navigation prefixes
        patterns = [
            r"^(我想去|我要去|去|导航到|导航去|请带我去|帮我查一下|去附近的)\s*",
        ]
        target = cleaned
        for p in patterns:
            target = re.sub(p, "", target)
        
        target = target.strip("。，！？,.!? ")
        if not target:
            return [cleaned]
        return [target]


class MockMapsProvider(BaseMapsProvider):
    KNOWN_PLACES = {
        "东京铁塔": PlaceCandidateItem(
            title="东京铁塔 (Tokyo Tower)",
            address="东京都港区芝公园4丁目2-8",
            latitude=35.658581,
            longitude=139.745433,
            coord_system="wgs84",
            source="mock_maps",
            place_id="mock_tokyo_tower",
        ),
        "成田机场": PlaceCandidateItem(
            title="成田国际机场 (NRT)",
            address="千叶县成田市古込1-1",
            latitude=35.771986,
            longitude=140.392850,
            coord_system="wgs84",
            source="mock_maps",
            place_id="mock_narita_airport",
        ),
        "秋叶原": PlaceCandidateItem(
            title="秋叶原站",
            address="东京都千代田区外神田1丁目",
            latitude=35.698383,
            longitude=139.773072,
            coord_system="wgs84",
            source="mock_maps",
            place_id="mock_akihabara",
        ),
        "罗森": PlaceCandidateItem(
            title="罗森便利店 芝公园店",
            address="东京都港区芝公园3丁目",
            latitude=35.659000,
            longitude=139.746000,
            coord_system="wgs84",
            source="mock_maps",
            place_id="mock_lawson",
        ),
    }

    async def search(self, query: str, lat: Optional[float] = None, lon: Optional[float] = None) -> List[PlaceCandidateItem]:
        query_lower = query.lower()
        results = []
        for key, item in self.KNOWN_PLACES.items():
            if key in query or key.lower() in query_lower:
                results.append(item)

        if not results:
            # Fallback mock candidate using hash-like offset around Tokyo if unknown
            results.append(
                PlaceCandidateItem(
                    title=query,
                    address=f"东京都中央区（模拟地址：{query}）",
                    latitude=35.681236,
                    longitude=139.767125,
                    coord_system="wgs84",
                    source="mock_maps_fallback",
                    place_id=f"mock_gen_{abs(hash(query)) % 10000}",
                )
            )
        return results
