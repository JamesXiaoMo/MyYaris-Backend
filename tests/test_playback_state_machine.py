"""Real HTTP/WS routes with isolated SQLite and fake upstream; no live playback."""
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from app.config import settings
from app.repositories.models import CarDeviceStatus, MusicCommand, utc_now
from app.services.control_service import music_queue_snapshot_manager as queues, command_queue_manager
from app.services.netease_service import netease_service
from tests.test_music_queue import _make_dummy_songs


@pytest.fixture(autouse=True)
def clean_state(db_session):
    db_session.query(MusicCommand).delete()
    db_session.query(CarDeviceStatus).delete()
    db_session.commit()
    queues.clear_all()
    command_queue_manager.clear_all()
    yield
    queues.clear_all()
    command_queue_manager.clear_all()


def report(client, car, **extra):
    data = dict(playback_state="paused", position_ms=45000, capabilities=["queue_v2"], **extra)
    result = client.post('/api/v1/music/control/status', json=data, headers=car)
    assert result.status_code == 200


def ack(client, car, command, **extra):
    result = client.post(f'/api/v1/music/control/command/{command}/ack', json={'status': 'applied', **extra}, headers=car)
    assert result.status_code == 200
    return result.json()


def test_full_favorite_cold_default_cursor_and_same_snapshot_modes(client, phone_headers, car_headers, db_session):
    report(client, car_headers)
    songs = _make_dummy_songs(137)
    offsets = []
    async def tracks(offset=0, limit=100, **kwargs):
        offsets.append(offset)
        # Full count is authoritative even if legacy upstream omits has_more.
        return {'songs': songs[offset:offset + limit]}
    with patch.object(netease_service, 'get_favorite_playlist_info', AsyncMock(return_value={'playlist_id': 55, 'track_count': 137})), \
         patch.object(netease_service, 'get_favorite_playlist_tracks', tracks):
        res = client.post('/api/v1/music/control/command', json={'command_id': 'sm_cold', 'action': 'resume'}, headers=phone_headers)
    assert res.status_code == 200
    assert offsets == [0, 100]
    params = res.json()['params']
    assert params['source'] == 'netease_favorite'
    assert queues.get_active_queue(settings.DEFAULT_CAR_DEVICE_ID) is None
    ack(client, car_headers, 'sm_cold', current_index=120, current_track_id='netease_1120', applied_mode='list_loop')
    active = queues.get_active_queue(settings.DEFAULT_CAR_DEVICE_ID)
    assert active.total == 137
    assert db_session.query(CarDeviceStatus).first().current_track_id == 'netease_1120'
    report(client, car_headers, current_index=120, current_track_id='netease_1120', queue_id=active.queue_id,
           queue_version=active.queue_version, queue_source=active.source)
    with patch.object(netease_service, 'get_favorite_playlist_info', AsyncMock(side_effect=AssertionError('must reuse active complete favorite'))):
        mode = client.post('/api/v1/music/control/command', json={'command_id': 'sm_shuffle', 'action': 'set_playback_mode',
            'params': {'mode': 'shuffle'}}, headers=phone_headers)
    assert mode.status_code == 200
    assert mode.json()['params']['queue_version'] == active.queue_version
    ack(client, car_headers, 'sm_shuffle', applied_mode='shuffle', current_index=120)
    assert queues.get_active_queue(settings.DEFAULT_CAR_DEVICE_ID) is active
    resume = client.post('/api/v1/music/control/command', json={'command_id': 'sm_resume', 'action': 'resume'}, headers=phone_headers)
    assert resume.status_code == 200
    assert resume.json()['params']['track_id'] == 'netease_1120'
    assert resume.json()['params']['restore_position_ms'] == 45000
    assert resume.json()['params']['mode'] == 'shuffle'
    duplicate = client.post('/api/v1/music/control/command', json={'command_id': 'sm_resume', 'action': 'resume'}, headers=phone_headers)
    assert duplicate.status_code == 200


@pytest.mark.parametrize('channel', ['http', 'ws'])
def test_heart_real_pending_applied_and_no_fallback(client, phone_headers, car_headers, db_session, channel):
    report(client, car_headers)
    common = dict(command_id=f'sm_heart_{channel}', action='set_playback_mode', params={'mode': 'intelligence', 'seed_song_id': 1000})
    with patch.object(netease_service, 'is_song_in_favorite_playlist', AsyncMock(return_value=True)), \
         patch.object(netease_service, 'get_favorite_playlist_info', AsyncMock(return_value={'playlist_id': 55})), \
         patch.object(netease_service, 'get_intelligence', AsyncMock(return_value=('simi_fallback', _make_dummy_songs(2)))):
        bad = client.post('/api/v1/music/control/command', json={**common, 'command_id': common['command_id'] + '_bad'}, headers=phone_headers)
        assert bad.status_code == 502
        assert queues.get_active_queue(settings.DEFAULT_CAR_DEVICE_ID) is None
    with patch.object(netease_service, 'is_song_in_favorite_playlist', AsyncMock(return_value=True)), \
         patch.object(netease_service, 'get_favorite_playlist_info', AsyncMock(return_value={'playlist_id': 55})), \
         patch.object(netease_service, 'get_intelligence', AsyncMock(return_value=('intelligence', _make_dummy_songs(4)))):
        if channel == 'http':
            sent = client.post('/api/v1/music/control/command', json=common, headers=phone_headers)
            assert sent.status_code == 200
        else:
            with client.websocket_connect('/api/v1/ws/music?client_type=phone', headers=phone_headers) as ws:
                ws.receive_json()
                ws.send_json({'type': 'command', 'data': common})
                assert ws.receive_json()['status'] == 'dispatched'
        assert queues.get_active_queue(settings.DEFAULT_CAR_DEVICE_ID) is None
        replay = client.post('/api/v1/music/control/command', json=common, headers=phone_headers)
        assert replay.status_code == 200
        ack(client, car_headers, common['command_id'], applied_mode='intelligence', current_index=0)
    active = queues.get_active_queue(settings.DEFAULT_CAR_DEVICE_ID)
    assert active.source == 'netease_intelligence'
    assert active.is_complete is False
    body = dict(device_id=settings.DEFAULT_CAR_DEVICE_ID, queue_id=active.queue_id, queue_version=active.queue_version,
                request_id=f'sm_batch_{channel}', seed_song_id=1003, batch_index=1, count=3)
    with patch.object(netease_service, 'get_intelligence', AsyncMock(return_value=('intelligence', _make_dummy_songs(3, 2000)))) as upstream:
        first = client.post('/api/v1/music/netease/intelligence', json=body, headers=car_headers)
        duplicate = client.post('/api/v1/music/netease/intelligence', json=body, headers=car_headers)
        assert first.status_code == duplicate.status_code == 200
        assert first.json()['tracks'] == duplicate.json()['tracks']
        assert first.json()['queue_version'] == duplicate.json()['queue_version']
        assert upstream.await_count == 1
        conflict = client.post('/api/v1/music/netease/intelligence', json={**body, 'seed_song_id': 1002}, headers=car_headers)
        assert conflict.status_code == 409
    newer = queues.get_active_queue(settings.DEFAULT_CAR_DEVICE_ID)
    assert newer.queue_version > active.queue_version
    assert newer.tracks[:4] == active.tracks
    with patch.object(netease_service, 'get_intelligence', AsyncMock(return_value=('empty', []))):
        failed = client.post('/api/v1/music/netease/intelligence', json={**body, 'queue_version': newer.queue_version,
            'batch_index': 2, 'seed_song_id': 2002, 'request_id': f'sm_failure_{channel}'}, headers=car_headers)
        assert failed.status_code == 502
    assert queues.get_active_queue(settings.DEFAULT_CAR_DEVICE_ID) is newer

    async def upstream_with_late_queue_switch(**kwargs):
        prepared = await queues.prepare_single_track_queue(settings.DEFAULT_CAR_DEVICE_ID,
            song_id=9000, title="Explicit single", artist="Fake", command_id=f"race_switch_{channel}", db=db_session)
        queues.apply_command_queue(settings.DEFAULT_CAR_DEVICE_ID, command_id=f"race_switch_{channel}", db=db_session)
        return "intelligence", _make_dummy_songs(3, 3000)
    with patch.object(netease_service, 'get_intelligence', upstream_with_late_queue_switch):
        raced = client.post('/api/v1/music/netease/intelligence', json={**body, 'queue_version': newer.queue_version,
            'batch_index': 2, 'seed_song_id': 2002, 'request_id': f'sm_race_{channel}'}, headers=car_headers)
        assert raced.status_code == 409
    assert queues.get_active_queue(settings.DEFAULT_CAR_DEVICE_ID).source == 'single_track'


def test_same_queue_late_ack_duplicate_sequence_and_expiry_do_not_revert(client, phone_headers, car_headers, db_session):
    report(client, car_headers)
    with patch.object(netease_service, 'get_favorite_playlist_info', AsyncMock(return_value={'playlist_id': 55, 'track_count': 4})), \
         patch.object(netease_service, 'get_favorite_playlist_tracks', AsyncMock(return_value={'songs': _make_dummy_songs(4)})):
        first = client.post('/api/v1/music/control/command', json={'command_id': 'sm_initial', 'action': 'resume'}, headers=phone_headers)
    ack(client, car_headers, 'sm_initial')
    for command, mode in [('sm_old', 'shuffle'), ('sm_new', 'list_loop')]:
        result = client.post('/api/v1/music/control/command', json={'command_id': command, 'action': 'set_playback_mode', 'params': {'mode': mode}}, headers=phone_headers)
        assert result.status_code == 200
    ack(client, car_headers, 'sm_new', applied_mode='list_loop', current_index=3)
    late = ack(client, car_headers, 'sm_old', applied_mode='shuffle', current_index=0)
    assert late['status'] == 'expired'
    row = db_session.query(CarDeviceStatus).first()
    assert row.applied_mode == 'list_loop'
    assert row.current_index == 3
    assert row.last_applied_command_id == 'sm_new'
    report(client, car_headers, status_seq=101, current_index=3, last_applied_command_id='sm_new')
    report(client, car_headers, status_seq=101, current_index=0, last_applied_command_id='sm_new')
    report(client, car_headers, status_seq=102, current_index=0, last_applied_command_id='sm_old')
    db_session.refresh(row)
    assert row.current_index == 3
    assert row.status_seq == 101
    expired = client.post('/api/v1/music/control/command', json={'command_id': 'sm_expired', 'action': 'pause'}, headers=phone_headers)
    assert expired.status_code == 200
    command = db_session.query(MusicCommand).filter_by(command_id='sm_expired').one()
    command.expires_at = utc_now() - timedelta(seconds=1)
    db_session.commit()
    assert ack(client, car_headers, 'sm_expired')['status'] == 'expired'
    db_session.refresh(row)
    assert row.last_applied_command_id == 'sm_new'


def test_bound_car_can_request_default_and_cannot_control_other_vehicle(client, car_headers):
    with patch.object(netease_service, 'get_favorite_playlist_info', AsyncMock(return_value={'playlist_id': 55, 'track_count': 1})), \
         patch.object(netease_service, 'get_favorite_playlist_tracks', AsyncMock(return_value={'songs': _make_dummy_songs(1)})):
        result = client.post('/api/v1/music/control/command', json={'command_id': 'sm_car_default', 'action': 'resume'}, headers=car_headers)
    assert result.status_code == 200
    forbidden = client.post('/api/v1/music/control/command', json={'command_id': 'sm_car_other', 'action': 'pause', 'device_id': 'another_vehicle'}, headers=car_headers)
    assert forbidden.status_code == 403
    read = client.get('/api/v1/music/control/status?device_id=another_vehicle', headers=car_headers)
    assert read.status_code == 403
    from starlette.websockets import WebSocketDisconnect
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/api/v1/ws/music?client_type=phone', headers=car_headers):
            pass
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect('/api/v1/ws/music?client_type=car&device_id=another_vehicle', headers=car_headers):
            pass
