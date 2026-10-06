import io
from app.providers.google_maps_parser import (
    extract_coordinates_from_url,
    is_allowed_hostname,
    is_viewport_only_url,
)


def test_voice_upload(client, car_headers):
    dummy_wav = io.BytesIO(b"RIFF....WAVEfmt ....data....fake_audio_bytes")
    dummy_wav.name = "test_audio.wav"

    response = client.post(
        "/voice",
        files={"file": ("test_audio.wav", dummy_wav, "audio/wav")},
        data={"target_device_id": "car_p3pro"},
        headers=car_headers,
    )
    assert response.status_code == 200
    data = response.json()
    assert data["request_id"] is not None
    assert data["status"] in ["resolved", "needs_clarification"]

    # Verify querying destination returns transcribed text
    query_resp = client.get(f"/destinations/{data['request_id']}", headers=car_headers)
    assert query_resp.status_code == 200
    q_data = query_resp.json()
    assert q_data["transcribed_text"] is not None


def test_idempotent_voice_upload(client, car_headers):
    dummy_wav = io.BytesIO(b"RIFF....WAVEfmt ....data....test_idempotent_voice")
    dummy_wav.name = "test_idem.wav"

    idem_key = "voice-idem-key-777"
    resp1 = client.post(
        "/voice",
        files={"file": ("test_idem.wav", dummy_wav, "audio/wav")},
        data={"target_device_id": "car_p3pro", "idempotency_key": idem_key},
        headers=car_headers,
    )
    assert resp1.status_code == 200
    req_id1 = resp1.json()["request_id"]

    # Re-upload with same idempotency key
    dummy_wav2 = io.BytesIO(b"RIFF....WAVEfmt ....data....test_idempotent_voice")
    dummy_wav2.name = "test_idem.wav"
    resp2 = client.post(
        "/voice",
        files={"file": ("test_idem.wav", dummy_wav2, "audio/wav")},
        data={"target_device_id": "car_p3pro", "idempotency_key": idem_key},
        headers=car_headers,
    )
    assert resp2.status_code == 200
    assert resp2.json()["request_id"] == req_id1


def test_ssrf_domain_filtering():
    assert is_allowed_hostname("maps.google.com") is True
    assert is_allowed_hostname("goo.gl") is True
    assert is_allowed_hostname("maps.app.goo.gl") is True
    assert is_allowed_hostname("malicious-site.com") is False
    assert is_allowed_hostname("169.254.169.254") is False
    assert is_allowed_hostname("localhost") is False


def test_coordinate_extraction_from_map_urls():
    # 1. Viewport-only URL (no destination pinpoint) must NOT return destination coordinates
    url_viewport = "https://www.google.com/maps/@35.658581,139.745433,17z"
    assert extract_coordinates_from_url(url_viewport) is None
    assert is_viewport_only_url(url_viewport) is True

    # 1b. Parameter ll alone must NOT return destination coordinates
    url_ll_only = "https://maps.google.com/?ll=35.6812,139.7671&z=15"
    assert extract_coordinates_from_url(url_ll_only) is None
    assert is_viewport_only_url(url_ll_only) is True

    # 1c. Integer ll alone
    url_ll_int = "https://maps.google.com/?ll=35,139&z=12"
    assert extract_coordinates_from_url(url_ll_int) is None
    assert is_viewport_only_url(url_ll_int) is True

    # 2. Real Google Maps pinpoint with !3d... and !4d... protobuf coordinates
    url_pinpoint = "https://www.google.com/maps/place/Tokyo+Tower/@35.6000,139.7000,12z/data=!4m6!3m5!1s0x60188bbd9009ec09:0x481a93f0d2a409dd!8m2!3d35.6585805!4d139.7454329"
    coords1 = extract_coordinates_from_url(url_pinpoint)
    assert coords1 is not None
    assert abs(coords1[0] - 35.6585805) < 1e-4
    assert abs(coords1[1] - 139.7454329) < 1e-4
    assert coords1[2] == "Tokyo Tower"

    # 2b. Integer protobuf coordinates
    url_pin_int = "https://www.google.com/maps/place/Zero/@0,0,10z/data=!4m5!3m4!1s0x0:0x0!8m2!3d35!4d139"
    coords_pin_int = extract_coordinates_from_url(url_pin_int)
    assert coords_pin_int is not None
    assert coords_pin_int[0] == 35.0
    assert coords_pin_int[1] == 139.0

    # 3. Explicit lat,lon in query parameter (including integer coordinates)
    url_query = "https://maps.google.com/?q=35.658581,139.745433"
    coords2 = extract_coordinates_from_url(url_query)
    assert coords2 is not None
    assert coords2[0] == 35.658581
    assert coords2[1] == 139.745433

    url_query_int = "https://maps.google.com/?q=35,139"
    coords_int = extract_coordinates_from_url(url_query_int)
    assert coords_int is not None
    assert coords_int[0] == 35.0
    assert coords_int[1] == 139.0

    # 3b. ll + q: Explicit q overrides viewport ll
    url_ll_and_q = "https://maps.google.com/?ll=35.6812,139.7671&q=35.6586,139.7454"
    coords_ll_q = extract_coordinates_from_url(url_ll_and_q)
    assert coords_ll_q is not None
    assert abs(coords_ll_q[0] - 35.6586) < 1e-4
    assert abs(coords_ll_q[1] - 139.7454) < 1e-4

    # 4. Directions destination parameter (float and integer)
    url_dir = "https://www.google.com/maps/dir/?api=1&destination=35.658581,139.745433"
    coords3 = extract_coordinates_from_url(url_dir)
    assert coords3 is not None
    assert coords3[0] == 35.658581
    assert coords3[1] == 139.745433

    url_dir_daddr = "https://maps.google.com/?saddr=35.68,139.76&daddr=35,139"
    coords_daddr = extract_coordinates_from_url(url_dir_daddr)
    assert coords_daddr is not None
    assert coords_daddr[0] == 35.0
    assert coords_daddr[1] == 139.0

    # 5. Directions link without destination parameter MUST NOT extract origin
    from app.providers.google_maps_parser import is_route_without_destination, has_multiple_coordinates
    url_dir_no_dest = "https://www.google.com/maps/dir/35.6812,139.7671/35.6586,139.7454/data=!4m2!4m1!3e0"
    assert extract_coordinates_from_url(url_dir_no_dest) is None
    assert is_route_without_destination(url_dir_no_dest) is True

    # 6. Multi-location URLs with multiple distinct coordinates
    url_multi = "https://www.google.com/maps/data=!3d35.1!4d139.1!3d35.2!4d139.2"
    assert extract_coordinates_from_url(url_multi) is None
    assert has_multiple_coordinates(url_multi) is True


import pytest
import httpx
from app.providers.google_maps_parser import resolve_and_parse_map_url, SSRFSecurityError


@pytest.mark.asyncio
async def test_relative_redirect_resolution(monkeypatch):
    class FakeResponse:
        def __init__(self, status_code, headers, url):
            self.status_code = status_code
            self.headers = headers
            self.url = url
            self.is_redirect = status_code in (301, 302, 303, 307, 308)

    async def fake_head(self, url, headers=None):
        if url == "https://maps.app.goo.gl/EXAMPLE_test":
            # Relative redirect to /maps/place/...
            return FakeResponse(302, {"location": "/maps/place/Tokyo/@35.68,139.76,17z/data=!4m5!3m4!1s0x0:0x0!8m2!3d35.6812!4d139.7671"}, url)
        return FakeResponse(200, {}, url)

    monkeypatch.setattr(httpx.AsyncClient, "head", fake_head)

    res = await resolve_and_parse_map_url("https://maps.app.goo.gl/EXAMPLE_test")
    assert res.candidate is not None
    assert abs(res.candidate.latitude - 35.6812) < 1e-4
    assert abs(res.candidate.longitude - 139.7671) < 1e-4
    assert "maps.app.goo.gl/maps/place" in res.resolved_url


@pytest.mark.asyncio
async def test_exhausted_redirect_hops_unit(monkeypatch):
    class FakeResponse:
        def __init__(self, status_code, headers, url):
            self.status_code = status_code
            self.headers = headers
            self.url = url
            self.is_redirect = True

    async def fake_head(self, url, headers=None):
        return FakeResponse(302, {"location": "https://maps.google.com/infinite_redirect"}, url)

    monkeypatch.setattr(httpx.AsyncClient, "head", fake_head)

    res = await resolve_and_parse_map_url("https://maps.google.com/start", max_hops=3)
    assert res.candidate is None
    assert res.error is not None
    assert "重定向跳数过多" in res.error


@pytest.mark.asyncio
async def test_redirect_to_disallowed_domain_raises_ssrf_unit(monkeypatch):
    class FakeResponse:
        def __init__(self, status_code, headers, url):
            self.status_code = status_code
            self.headers = headers
            self.url = url
            self.is_redirect = True

    async def fake_head(self, url, headers=None):
        return FakeResponse(302, {"location": "http://evil.com/phishing"}, url)

    monkeypatch.setattr(httpx.AsyncClient, "head", fake_head)

    with pytest.raises(SSRFSecurityError) as exc_info:
        await resolve_and_parse_map_url("https://maps.google.com/safe_start")
    assert "evil.com" in str(exc_info.value)

