import pytest
from openlocationcode import openlocationcode as olc


def submit(client, headers, **fields):
    return client.post('/destinations', headers=headers, json={
        'input_type': 'plus_code', 'target_device_id': 'test_plus_code_car', **fields,
    })


def test_full_code_offline_exact_area_center(client, phone_headers):
    code = olc.encode(34.399133, 132.711467, 11)
    area = olc.decode(code)
    data = submit(client, phone_headers, plus_code=code.lower()).json()
    assert data['status'] == 'resolved'
    assert data['task_id']
    assert data['candidates'][0]['latitude'] == area.latitudeCenter
    assert data['candidates'][0]['longitude'] == area.longitudeCenter


@pytest.mark.parametrize('value', ['', '739-0041', 'ABCD+EF', 'ZZZZZZZZ+ZZ'])
def test_invalid_no_dispatch(client, phone_headers, value):
    data = submit(client, phone_headers, plus_code=value).json()
    assert data['status'] == 'failed'
    assert data['task_id'] is None
    assert not data['candidates']


def test_short_city_does_not_guess(client, phone_headers):
    data = submit(client, phone_headers, plus_code='CPX6+MJ 東広島市').json()
    assert data['status'] == 'needs_clarification'
    assert data['task_id'] is None
    assert not data['candidates']


def test_short_reference_requires_confirmation(client, phone_headers):
    full = olc.encode(34.399133, 132.711467)
    short = olc.shorten(full, 34.40, 132.71)
    data = submit(client, phone_headers, plus_code=short,
                  reference_coordinate={'latitude': 34.40, 'longitude': 132.71}).json()
    assert data['status'] == 'needs_clarification'
    assert data['task_id'] is None
    area = olc.decode(full)
    assert data['candidates'][0]['latitude'] == area.latitudeCenter
    confirmed = client.post(f"/destinations/{data['request_id']}/confirm", headers=phone_headers,
                            json={'candidate_id': data['candidates'][0]['candidate_id'],
                                  'target_device_id': 'test_plus_code_car'})
    assert confirmed.status_code == 200
    assert confirmed.json()['latitude'] == area.latitudeCenter


def test_coarse_code_requires_confirmation(client, phone_headers):
    data = submit(client, phone_headers, plus_code=olc.encode(34.4, 132.7, 6)).json()
    assert data['status'] == 'needs_clarification'
    assert data['task_id'] is None


def test_invalid_reference_rejected(client, phone_headers):
    assert submit(client, phone_headers, plus_code='CPX6+MJ',
                  reference_coordinate={'latitude': 91, 'longitude': 132}).status_code == 422


def test_plus_code_retry_idempotent(client, phone_headers):
    fields = dict(plus_code=olc.encode(34.399133, 132.711467), idempotency_key='plus-code-test-retry')
    first = submit(client, phone_headers, **fields).json()
    second = submit(client, phone_headers, **fields).json()
    assert first['task_id'] == second['task_id']


def mock_locality(monkeypatch, refs):
    from app.config import settings
    from app.providers import nominatim_provider
    monkeypatch.setattr(settings, 'MAPS_PROVIDER', 'nominatim')
    class Provider:
        async def search(self, text, *, locality_only=False):
            assert locality_only
            return refs
    monkeypatch.setattr(nominatim_provider, 'get_nominatim_provider', lambda: Provider())


def locality_ref(lat=34.42, lon=132.74, address='東広島市, 広島県, 日本'):
    from app.providers.base import PlaceCandidateItem
    return PlaceCandidateItem(title='城市参照', address=address, latitude=lat, longitude=lon)


def test_google_copied_city_prefecture_auto_resolves(client, phone_headers, monkeypatch):
    mock_locality(monkeypatch, [locality_ref()])
    data = submit(client, phone_headers, plus_code='CPF8+Q7 东广岛市 广岛县').json()
    assert data['status'] == 'resolved'
    assert data['task_id']
    assert '8Q6JCPF8+Q7' in data['candidates'][0]['title']
    assert abs(data['candidates'][0]['latitude'] - 34.4244375) < 1e-8


def test_fuzzy_wrong_prefecture_no_dispatch(client, phone_headers, monkeypatch):
    mock_locality(monkeypatch, [locality_ref(address='東広島市, 岡山県, 日本')])
    data = submit(client, phone_headers, plus_code='CPF8+Q7 东广岛市 广岛县').json()
    assert data['status'] == 'needs_clarification'
    assert not data['candidates']
    assert not data['task_id']


def test_ambiguous_city_candidates_not_auto_sent(client, phone_headers, monkeypatch):
    mock_locality(monkeypatch, [locality_ref(), locality_ref(lat=35.42)])
    data = submit(client, phone_headers, plus_code='CPF8+Q7 东广岛市 广岛县').json()
    assert data['status'] == 'needs_clarification'
    assert len(data['candidates']) == 2
    assert not data['task_id']


def test_same_cell_references_deduplicated(client, phone_headers, monkeypatch):
    mock_locality(monkeypatch, [locality_ref(), locality_ref(lat=34.43)])
    data = submit(client, phone_headers, plus_code='CPF8+Q7 東広島市 広島県').json()
    assert data['status'] == 'resolved'
    assert len(data['candidates']) == 1


def test_city_only_needs_confirmation(client, phone_headers, monkeypatch):
    mock_locality(monkeypatch, [locality_ref()])
    data = submit(client, phone_headers, plus_code='CPF8+Q7 東広島市').json()
    assert data['status'] == 'needs_clarification'
    assert len(data['candidates']) == 1
    assert not data['task_id']


def test_city_lookup_unavailable_no_task(client, phone_headers, monkeypatch):
    mock_locality(monkeypatch, [])
    data = submit(client, phone_headers, plus_code='CPF8+Q7 东广岛市 广岛县').json()
    assert data['status'] == 'needs_clarification'
    assert not data['task_id']
