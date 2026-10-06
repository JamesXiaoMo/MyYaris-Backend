import asyncio
import time
import httpx
import pytest
from app.config import settings
from app.providers.nominatim_provider import (
    NominatimMapsProvider,
    NominatimRateLimiter,
    normalize_japanese_address,
)


def test_japanese_address_normalization():
    # 1. Romaji + Chinese simplified mixed
    text1 = "Saijocho Jike, 东广岛市广岛县 739-0041"
    norm1 = normalize_japanese_address(text1)
    assert "東広島市" in norm1
    assert "西条町寺家" in norm1
    assert "広島県" in norm1

    # 2. Chinese simplified cities
    text2 = "东广岛市西条町寺家"
    norm2 = normalize_japanese_address(text2)
    assert norm2 == "東広島市西条町寺家"

    # 3. Known cities and station terms
    text3 = "东京站"
    norm3 = normalize_japanese_address(text3)
    assert norm3 == "東京駅"

    # 4. Standard Japanese address remains intact
    text4 = "東京都千代田区丸の内1丁目"
    norm4 = normalize_japanese_address(text4)
    assert norm4 == "東京都千代田区丸の内1丁目"


@pytest.mark.asyncio
async def test_nominatim_rate_limiter():
    limiter = NominatimRateLimiter(min_interval_seconds=0.15)
    t0 = time.time()
    await limiter.acquire()
    await limiter.acquire()
    elapsed = time.time() - t0
    # Must wait at least the minimum interval
    assert elapsed >= 0.14


@pytest.mark.asyncio
async def test_nominatim_in_memory_cache(monkeypatch):
    limiter = NominatimRateLimiter(min_interval_seconds=0.01)
    provider = NominatimMapsProvider(
        base_url="https://mock.nominatim.test/search",
        rate_limiter=limiter,
        cache_ttl_seconds=3600.0,
    )

    call_count = 0

    async def mock_get(self, url, params=None, headers=None):
        nonlocal call_count
        call_count += 1
        return httpx.Response(
            200,
            json=[
                {
                    "lat": "34.4322856",
                    "lon": "132.7265838",
                    "name": "西条町寺家",
                    "display_name": "西条町寺家, 東広島市, 広島県, 日本",
                    "type": "neighbourhood",
                    "addresstype": "neighbourhood",
                    "place_id": 267309351,
                }
            ],
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    # First call: hits network
    res1 = await provider.search("東広島市西条町寺家")
    assert len(res1) == 1
    assert call_count == 1
    assert "地区中心" in res1[0].title

    # Second call for same query: hits cache, 0 network calls
    res2 = await provider.search("東広島市西条町寺家")
    assert len(res2) == 1
    assert call_count == 1


@pytest.mark.asyncio
async def test_nominatim_concurrent_deduplication(monkeypatch):
    limiter = NominatimRateLimiter(min_interval_seconds=0.01)
    provider = NominatimMapsProvider(
        base_url="https://mock.nominatim.test/search",
        rate_limiter=limiter,
    )

    call_count = 0

    async def mock_async_get(self, url, params=None, headers=None):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)  # simulate slow network call
        return httpx.Response(
            200,
            json=[
                {
                    "lat": "35.6812",
                    "lon": "139.7671",
                    "name": "東京駅",
                    "display_name": "東京駅, 丸の内, 東京都, 日本",
                    "type": "station",
                    "addresstype": "station",
                    "place_id": 12345,
                }
            ],
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_async_get)

    # Launch 5 concurrent searches for the same place
    tasks = [provider.search("東京駅") for _ in range(5)]
    results = await asyncio.gather(*tasks)

    # All 5 return the same results, but only 1 HTTP request is made!
    assert len(results) == 5
    for r in results:
        assert len(r) == 1
        assert r[0].latitude == 35.6812
    assert call_count == 1


@pytest.mark.asyncio
async def test_nominatim_http_timeout_and_429(monkeypatch):
    limiter = NominatimRateLimiter(min_interval_seconds=0.01)
    provider = NominatimMapsProvider(
        base_url="https://mock.nominatim.test/search",
        rate_limiter=limiter,
    )

    # 1. Test 429 Too Many Requests
    async def mock_429(self, url, **kw):
        return httpx.Response(429, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_429)
    res_429 = await provider.search("京都駅")
    assert res_429 == []

    # 2. Test Timeout
    async def mock_timeout(self, url, **kw):
        raise httpx.TimeoutException("Connection timed out")

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_timeout)
    res_timeout = await provider.search("大阪駅")
    assert res_timeout == []


def test_destination_share_link_nominatim_flow(client, phone_headers, monkeypatch):
    # Ensure MAPS_PROVIDER is nominatim
    monkeypatch.setattr(settings, "MAPS_PROVIDER", "nominatim")

    async def mock_head(self, url, **kw):
        return httpx.Response(200, request=httpx.Request("HEAD", url))

    async def mock_get(self, url, params=None, headers=None):
        if "nominatim" in str(url):
            return httpx.Response(
                200,
                json=[
                    {
                        "lat": "34.4322856",
                        "lon": "132.7265838",
                        "name": "西条町寺家",
                        "display_name": "西条町寺家, 西条東北町, 東広島市, 広島県, 739-0041, 日本",
                        "type": "neighbourhood",
                        "addresstype": "neighbourhood",
                        "place_id": 267309351,
                    }
                ],
                request=httpx.Request("GET", url),
            )
        return httpx.Response(200, text="<html>google maps</html>", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "head", mock_head)
    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    # Step 1: Submit URL that has no coordinates, only address query
    test_url = "https://www.google.com/maps?q=Saijocho+Jike,+%E4%B8%9C%E5%B9%BF%E5%B2%9B%E5%B8%82%E5%B9%BF%E5%B2%9B%E5%8E%BF+739-0041&ftid=0x35506fe1f7b9c61b:0xf88f7f99ef5d58cd"
    resp = client.post(
        "/destinations",
        json={"input_type": "share_link", "url": test_url, "target_device_id": "car_p3pro"},
        headers=phone_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    # MUST return needs_clarification and NOT auto-dispatch task!
    assert data["status"] == "needs_clarification"
    assert data["task_id"] is None
    assert len(data["candidates"]) == 1
    cand = data["candidates"][0]
    assert "地区中心" in cand["title"]
    assert "OpenStreetMap" in cand["address"]
    assert abs(cand["latitude"] - 34.4322856) < 1e-4

    # Step 2: Confirm candidate explicitly
    confirm_resp = client.post(
        f"/destinations/{data['request_id']}/confirm",
        json={"candidate_id": cand["candidate_id"], "target_device_id": "car_p3pro"},
        headers=phone_headers,
    )
    assert confirm_resp.status_code == 200
    task_data = confirm_resp.json()
    assert task_data["status"] == "queued"
    assert task_data["task_id"] is not None
    assert abs(task_data["latitude"] - 34.4322856) < 1e-4


def test_destination_text_without_nlp(client, phone_headers, monkeypatch):
    monkeypatch.setattr(settings, "MAPS_PROVIDER", "nominatim")
    monkeypatch.setattr(settings, "NLP_PROVIDER", "none")  # NLP disabled

    async def mock_get(self, url, params=None, headers=None):
        return httpx.Response(
            200,
            json=[
                {
                    "lat": "35.6812",
                    "lon": "139.7671",
                    "name": "東京駅",
                    "display_name": "東京駅, 丸の内, 東京都, 日本",
                    "type": "station",
                    "addresstype": "station",
                    "place_id": 99999,
                }
            ],
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    # Text search works directly even without NLP provider!
    resp = client.post(
        "/destinations",
        json={"input_type": "text", "text": "東京駅", "target_device_id": "car_p3pro"},
        headers=phone_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "needs_clarification"
    assert data["task_id"] is None
    assert len(data["candidates"]) == 1
    assert data["candidates"][0]["latitude"] == 35.6812


def test_nominatim_no_match_returns_clarification_no_fake_pin(client, phone_headers, monkeypatch):
    monkeypatch.setattr(settings, "MAPS_PROVIDER", "nominatim")

    async def mock_head(self, url, **kw):
        return httpx.Response(200, request=httpx.Request("HEAD", url))

    async def mock_get(self, url, **kw):
        if "nominatim" in str(url):
            return httpx.Response(200, json=[], request=httpx.Request("GET", url))
        return httpx.Response(200, text="<html>google maps</html>", request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.AsyncClient, "head", mock_head)
    monkeypatch.setattr(httpx.AsyncClient, "get", mock_get)

    test_url = "https://www.google.com/maps?q=NonExistentPlace12345XYZ"
    resp = client.post(
        "/destinations",
        json={"input_type": "share_link", "url": test_url, "target_device_id": "car_p3pro"},
        headers=phone_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "needs_clarification"
    assert data["task_id"] is None
    assert len(data["candidates"]) == 0
    assert "未检索到匹配结果" in data["clarification_question"]


def test_regression_direct_coordinates_and_protections(client, phone_headers):
    # Direct coordinates must resolve immediately to a task without searching Nominatim
    coord_resp = client.post(
        "/destinations",
        json={
            "input_type": "coords",
            "coordinate": {"latitude": 35.6586, "longitude": 139.7454},
            "target_device_id": "car_p3pro",
        },
        headers=phone_headers,
    )
    assert coord_resp.status_code == 200
    assert coord_resp.json()["status"] == "resolved"
    assert coord_resp.json()["task_id"] is not None

    # Viewport-only link must remain protected
    vp_resp = client.post(
        "/destinations",
        json={
            "input_type": "share_link",
            "url": "https://maps.google.com/?ll=35.6586,139.7454",
            "target_device_id": "car_p3pro",
        },
        headers=phone_headers,
    )
    assert vp_resp.status_code == 200
    assert vp_resp.json()["status"] == "needs_clarification"
    assert vp_resp.json()["task_id"] is None
    assert len(vp_resp.json()["candidates"]) == 0
    assert "视野中心" in vp_resp.json()["clarification_question"]


@pytest.mark.asyncio
async def test_locality_search_filters_poi_and_uses_separate_cache(monkeypatch):
    provider = NominatimMapsProvider(rate_limiter=NominatimRateLimiter(0.001))
    calls = []
    async def mock_get(self, url, params=None, headers=None):
        calls.append(params.copy())
        return httpx.Response(200, json=[
            {'lat':'34.42','lon':'132.74','name':'東広島市','display_name':'東広島市, 広島県, 日本', 'addresstype':'city'},
            {'lat':'34.42','lon':'132.74','name':'商店','display_name':'商店, 東広島市, 広島県, 日本','addresstype':'shop'},
        ], request=httpx.Request('GET',url))
    monkeypatch.setattr(httpx.AsyncClient, 'get', mock_get)
    assert len(await provider.search('东广岛市 广岛县')) == 2
    assert len(await provider.search('东广岛市 广岛县', locality_only=True)) == 1
    assert calls[-1]['featureType'] == 'city'
    assert len(await provider.search('东广岛市 广岛县', locality_only=True)) == 1
    assert len(calls) == 2
