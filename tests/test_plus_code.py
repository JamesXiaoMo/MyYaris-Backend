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
