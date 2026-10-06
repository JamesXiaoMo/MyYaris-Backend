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

    # 2. Real Google Maps pinpoint with !3d... and !4d... protobuf coordinates
    url_pinpoint = "https://www.google.com/maps/place/Tokyo+Tower/@35.6000,139.7000,12z/data=!4m6!3m5!1s0x60188bbd9009ec09:0x481a93f0d2a409dd!8m2!3d35.6585805!4d139.7454329"
    coords1 = extract_coordinates_from_url(url_pinpoint)
    assert coords1 is not None
    assert abs(coords1[0] - 35.6585805) < 1e-4
    assert abs(coords1[1] - 139.7454329) < 1e-4
    assert coords1[2] == "Tokyo Tower"

    # 3. Explicit lat,lon in query parameter
    url_query = "https://maps.google.com/?q=35.658581,139.745433"
    coords2 = extract_coordinates_from_url(url_query)
    assert coords2 is not None
    assert coords2[0] == 35.658581
    assert coords2[1] == 139.745433

    # 4. Directions destination parameter
    url_dir = "https://www.google.com/maps/dir/?api=1&destination=35.658581,139.745433"
    coords3 = extract_coordinates_from_url(url_dir)
    assert coords3 is not None
    assert coords3[0] == 35.658581
    assert coords3[1] == 139.745433
