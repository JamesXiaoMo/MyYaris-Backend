import io
from app.providers.google_maps_parser import extract_coordinates_from_url, is_allowed_hostname


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


def test_ssrf_domain_filtering():
    assert is_allowed_hostname("maps.google.com") is True
    assert is_allowed_hostname("goo.gl") is True
    assert is_allowed_hostname("maps.app.goo.gl") is True
    assert is_allowed_hostname("malicious-site.com") is False
    assert is_allowed_hostname("169.254.169.254") is False
    assert is_allowed_hostname("localhost") is False


def test_coordinate_extraction_from_map_urls():
    url1 = "https://www.google.com/maps/place/Tokyo+Tower/@35.658581,139.745433,17z/data=!3m1!4b1"
    coords1 = extract_coordinates_from_url(url1)
    assert coords1 is not None
    assert round(coords1[0], 4) == 35.6586
    assert round(coords1[1], 4) == 139.7454

    url2 = "https://maps.google.com/?q=35.658581,139.745433"
    coords2 = extract_coordinates_from_url(url2)
    assert coords2 is not None
    assert coords2[0] == 35.658581
    assert coords2[1] == 139.745433
