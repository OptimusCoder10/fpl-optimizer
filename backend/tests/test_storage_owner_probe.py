"""Fake HTTP only: no owner configuration or network."""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from fpl_optimizer import storage_owner_probe as mod


@pytest.fixture
def fake(monkeypatch):
    state = SimpleNamespace(seconds=0, calls=[], data={}, duplicate=409, corrupt=False, missing=False, fault=None, stream_failure=None, fallback_failure=None)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    class Clock:
        @staticmethod
        def now(tz):
            return start + timedelta(seconds=state.seconds)
    async def sleep(seconds):
        assert seconds == 30
        state.seconds += seconds
    class Client:
        def __init__(self, **kwargs):
            assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def request(self, method, path, **kwargs):
            if method == 'POST': return await self.post(path, **kwargs)
            if method == 'GET': return await self.get(path, **kwargs)
            raise AssertionError('no delete or other operations allowed')
        async def post(self, path, **kwargs):
            state.calls.append(('POST', path, kwargs))
            if path.startswith('/object/list/'):
                assert kwargs['json']['prefix'].startswith(mod.PREFIX)
                if state.fault == 'listing_failed': return response(503, json={})
                if state.fault == 'listing_invalid': return response(200, json=[])
                row = dict(name='probe.bin', metadata={'size': 64}, created_at=Clock.now(None).isoformat(),
                           updated_at=Clock.now(None).isoformat())
                if state.missing: row.pop('created_at')
                return response(200, json=[row])
            assert kwargs['headers'].get('x-upsert') == 'false', 'overwrite-disable header required'
            body = kwargs['content']
            if not isinstance(body, bytes):
                body = b''.join([chunk async for chunk in body])
            assert len(body) == 64
            if path in state.data:
                if state.fault == 'overwrite_check_failed': raise ValueError('private-marker')
                if state.duplicate == 200: state.data[path] = body
                return response(state.duplicate, json={'error': 'Duplicate'})
            if state.fault == 'first_upload_failed': raise ValueError('private-marker')
            if state.fault == 'first_upload_rejected': return response(403, json={'private': 'private-marker'})
            if len(state.data) == 1 and state.fallback_failure:
                if isinstance(state.fallback_failure, type): raise state.fallback_failure('private-marker')
                return response(state.fallback_failure, json={'private': 'private-marker'})
            state.data[path] = body
            if len(state.data) == 1 and state.stream_failure:
                if state.stream_failure == 'timeout': raise httpx.ReadTimeout('private-marker')
                if state.stream_failure == 'interrupted': raise httpx.RemoteProtocolError('private-marker')
                if isinstance(state.stream_failure, type): raise state.stream_failure('private-marker')
                return response(state.stream_failure, json={'private': 'private-marker'})
            if state.fault == 'upload_timestamp_invalid': return response(200, json={}, no_date=True)
            return response(200, json={})
        async def get(self, path, **kwargs):
            state.calls.append(('GET', path, kwargs))
            if state.fault == 'readback_failed': raise httpx.ReadError('private-marker')
            body = state.data[path.replace('/authenticated', '')]
            return response(200, content=b'bad' if state.corrupt else body,
                            no_date=state.fault == 'readback_timestamp_invalid')
    def response(status, no_date=False, **kwargs):
        return httpx.Response(status, headers={} if no_date else {'Date': format_datetime(Clock.now(None))},
                              request=httpx.Request('GET', 'https://example.invalid'), **kwargs)
    monkeypatch.setattr(mod, 'datetime', Clock)
    monkeypatch.setattr(mod.time, 'monotonic', lambda: state.seconds)
    monkeypatch.setattr(mod.asyncio, 'sleep', sleep)
    monkeypatch.setattr(mod.httpx, 'AsyncClient', Client)
    monkeypatch.setattr(mod, 'SupabaseStorage', lambda settings: SimpleNamespace(
        _url='https://example.invalid', _bucket='synthetic', _key='synthetic'))
    for key in ('URL', 'BUCKET', 'KEY'): monkeypatch.setenv('FPL_STORAGE_' + key, 'synthetic')
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("duplicate_status", [400, 409])
async def test_fixed_synthetic_destination_slow_stream_and_upsert_guard(fake, duplicate_status):
    fake.duplicate = duplicate_status
    report = mod.empty_report()
    assert await mod.probe(report)
    assert report['overwrite_rejected'] == report['a_intact'] == 'yes'
    assert report['created_minus_before_seconds'] == 30
    assert report['created_minus_after_seconds'] == 0
    writes = [path for verb, path, _ in fake.calls if verb == 'POST' and '/list/' not in path]
    assert len(writes) == 2 and writes[0] == writes[1]
    assert mod.PREFIX in writes[0] and 'deadline-window' not in writes[0]
    assert [verb for verb, _, _ in fake.calls] == ['POST', 'POST', 'GET', 'POST']
    assert fake.calls[-1][2]['json']['prefix'] + '/probe.bin' == writes[0].split('/synthetic/')[1]
    await mod.probe(mod.empty_report())
    assert len(fake.data) == 2, 'each invocation must generate a fresh destination'


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['overwrite', 'corrupt', 'unrelated_rejection', 'missing_creation'])
async def test_failures_cannot_report_success(fake, failure):
    if failure == 'overwrite': fake.duplicate = 200
    if failure == 'corrupt': fake.corrupt = True
    if failure == 'unrelated_rejection': fake.duplicate = 403
    if failure == 'missing_creation':
        fake.missing = True
        with pytest.raises(KeyError): await mod.probe(mod.empty_report())
    else:
        assert not await mod.probe(mod.empty_report())


def test_no_caller_supplied_key_guard(monkeypatch, capsys):
    called = []
    async def bounded(report): called.append(True); return True
    monkeypatch.setattr(mod, 'bounded', bounded)
    monkeypatch.setattr(mod.sys, 'argv', ['probe', '--key', 'forbidden'])
    assert mod.main() == 1, 'caller-supplied arguments must be refused'
    assert not called, 'argument rejection must precede storage access'
    assert 'forbidden' not in capsys.readouterr().out


def test_exception_details_never_escape(monkeypatch, capsys):
    async def bounded(report): raise ValueError('private-marker')
    monkeypatch.setattr(mod, 'bounded', bounded)
    monkeypatch.setattr(mod.sys, 'argv', ['probe'])
    assert mod.main() == 1
    output = capsys.readouterr()
    assert 'private-marker' not in output.out + output.err
    assert json.loads(output.out) == mod.empty_report()


def test_workflow_is_manual_no_inputs_and_storage_secrets_only():
    root = Path(__file__).resolve().parents[2]
    text = (root / '.github/workflows/storage-owner-probe.yml').read_text()
    assert 'workflow_dispatch:' in text and "github.event_name == 'workflow_dispatch'" in text
    for forbidden in ('inputs:', 'schedule:', 'upload-artifact', 'DATABASE', 'capture_research'):
        assert forbidden not in text
    assert 'contents: read' in text
    assert 'fpl_optimizer.storage_owner_probe' in text
    assert text.count('secrets.') == 3


@pytest.mark.parametrize('category', [
    'arguments_rejected', 'config_missing', 'config_invalid', 'client_setup_failed',
    'clock_failed', 'first_upload_failed', 'first_upload_rejected', 'clock_jump',
    'upload_timestamp_invalid', 'overwrite_check_failed', 'overwrite_not_rejected',
    'readback_failed', 'readback_timestamp_invalid', 'listing_failed', 'listing_invalid',
    'provider_timestamp_invalid', 'probe_timeout', 'internal_error',
    'fallback_upload_failed', 'fallback_upload_rejected',
])
def test_each_failure_category_prints_without_private_details(fake, monkeypatch, capsys, category):
    def fail(*args, **kwargs): raise ValueError('private-marker')
    monkeypatch.setattr(mod.sys, 'argv', ['probe'])
    fake.fault = category
    if category == 'arguments_rejected': monkeypatch.setattr(mod.sys, 'argv', ['probe', 'private-marker'])
    if category == 'config_missing': monkeypatch.delenv('FPL_STORAGE_URL')
    if category == 'config_invalid': monkeypatch.setattr(mod, 'SupabaseStorage', fail)
    if category == 'client_setup_failed': monkeypatch.setattr(mod.httpx, 'AsyncClient', fail)
    if category == 'clock_failed': monkeypatch.setattr(mod.datetime, 'now', fail)
    if category == 'clock_jump':
        ticks = iter([0, 999])
        monkeypatch.setattr(mod.time, 'monotonic', lambda: next(ticks, 999))
    if category.startswith('fallback_upload_'):
        fake.stream_failure = httpx.WriteError
        fake.fallback_failure = 403 if category.endswith('rejected') else httpx.ConnectError
    if category == 'overwrite_not_rejected': fake.duplicate = 403
    if category == 'provider_timestamp_invalid': fake.missing = True
    if category == 'internal_error': monkeypatch.setattr(mod, 'uuid4', fail)
    if category == 'probe_timeout':
        async def timeout(report): raise TimeoutError('private-marker')
        monkeypatch.setattr(mod, 'bounded', timeout)
    assert mod.main() == 1
    output = capsys.readouterr()
    assert 'private-marker' not in output.out + output.err
    assert 'example.invalid' not in output.out + output.err
    assert json.loads(output.out)['failure_category'] == category


def test_unknown_failure_category_is_never_printed(monkeypatch, capsys):
    async def fail(report):
        report['failure_category'] = 'private-marker'
        report['stream_failure_category'] = 'private-marker'
        raise ValueError('private-marker')
    monkeypatch.setattr(mod, 'bounded', fail)
    monkeypatch.setattr(mod.sys, 'argv', ['probe'])
    assert mod.main() == 1
    output = capsys.readouterr()
    assert 'private-marker' not in output.out + output.err, 'unknown categories must never escape'
    assert json.loads(output.out)['failure_category'] == 'internal_error'


@pytest.mark.parametrize('failure,category', [
    ('timeout', 'stream_timeout'), ('interrupted', 'stream_interrupted'),
    (408, 'stream_rejected'), (411, 'stream_rejected'), (501, 'stream_rejected'),
    (httpx.ConnectError, 'stream_connection_failed'),
    (httpx.ReadError, 'stream_connection_failed'),
    (httpx.WriteError, 'stream_connection_failed'),
    (httpx.CloseError, 'stream_connection_failed'),
    (httpx.LocalProtocolError, 'stream_connection_failed'),
    (httpx.ProxyError, 'stream_connection_failed'),
    (httpx.UnsupportedProtocol, 'stream_connection_failed'),
])
def test_fallback_uses_fresh_synthetic_key_and_leaves_gate2_untested(fake, monkeypatch, capsys, failure, category):
    fake.stream_failure = failure
    monkeypatch.setattr(mod.sys, 'argv', ['probe'])
    assert mod.main() == 0
    output = capsys.readouterr()
    assert 'private-marker' not in output.out + output.err
    report = json.loads(output.out)
    assert report['failure_category'] is None
    assert report['fallback_upload_status'] == 200
    assert report['first_upload_status'] == (failure if type(failure) is int else None)
    assert report['stream_failure_category'] == category
    assert report['upload_mode'] == 'normal_fallback'
    assert report['gate2_timing_observation'] == 'untested'
    assert report['overwrite_rejected'] == report['a_intact'] == 'yes'
    writes = [path for verb, path, _ in fake.calls if verb == 'POST' and '/list/' not in path]
    assert len(writes) == 3 and writes[0] != writes[1] == writes[2], 'fallback must use a fresh synthetic key'
    assert len(fake.data) == 2 and all(body == mod.A for body in fake.data.values())
    assert all(mod.PREFIX in path for path in writes)


@pytest.mark.parametrize('status', [400, 401, 403, 409, 500])
def test_first_upload_status_is_numeric_and_does_not_retry_other_rejections(fake, monkeypatch, capsys, status):
    fake.stream_failure = status
    monkeypatch.setattr(mod.sys, 'argv', ['probe'])
    assert mod.main() == 1
    output = capsys.readouterr()
    assert 'private-marker' not in output.out + output.err
    report = json.loads(output.out)
    assert type(report['first_upload_status']) is int
    assert report['first_upload_status'] == status
    assert report['failure_category'] == 'first_upload_rejected'
    assert report['stream_failure_category'] is None
    assert report['upload_mode'] == 'streamed'
    assert len(fake.calls) == 1, 'auth and unrelated HTTP errors must not trigger fallback'


def test_unexpected_stream_exception_remains_failure_without_retry(fake, monkeypatch, capsys):
    fake.stream_failure = ValueError
    monkeypatch.setattr(mod.sys, 'argv', ['probe'])
    assert mod.main() == 1
    output = capsys.readouterr()
    assert 'private-marker' not in output.out + output.err
    report = json.loads(output.out)
    assert report['failure_category'] == 'first_upload_failed'
    assert report['first_upload_status'] is None
    assert report['stream_failure_category'] is None
    assert len(fake.calls) == 1


@pytest.mark.parametrize('status', [400, 401, 403, 409, 500])
def test_stream_transport_failure_then_fallback_rejection_has_its_own_status(fake, monkeypatch, capsys, status):
    fake.stream_failure = httpx.WriteError
    fake.fallback_failure = status
    monkeypatch.setattr(mod.sys, 'argv', ['probe'])
    assert mod.main() == 1
    output = capsys.readouterr()
    assert 'private-marker' not in output.out + output.err
    report = json.loads(output.out)
    assert report['first_upload_status'] is None
    assert report['fallback_upload_status'] == status, 'fallback HTTP response must have its own numeric status'
    assert report['failure_category'] == 'fallback_upload_rejected'
    assert report['stream_failure_category'] == 'stream_connection_failed'
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_probe_and_uploader_share_client_policy_with_fresh_fallback(monkeypatch):
    from fpl_optimizer.services import research_storage, shared_archive, storage_http
    from pydantic import SecretStr
    real_client = httpx.AsyncClient
    clients, calls, objects = [], [], {}
    async def handle(request):
        body = await request.aread()
        calls.append(request)
        path = request.url.path
        headers = {'Date': format_datetime(datetime.now(timezone.utc))}
        if '/object/list/' in path:
            return httpx.Response(200, headers=headers, json=[{
                'name': 'probe.bin', 'metadata': {'size': 64},
                'created_at': datetime.now(timezone.utc).isoformat(),
            }])
        if request.method == 'GET':
            return httpx.Response(200, headers=headers, content=objects[path.replace('/authenticated', '')])
        assert request.method == 'POST', 'no delete calls'
        assert request.headers['apikey'] == 'synthetic'
        assert 'authorization' not in request.headers
        assert request.headers['x-upsert'] == 'false'
        assert request.headers['cache-control'] == 'private, max-age=0'
        assert request.headers['content-type'] == 'application/json'
        assert int(request.headers['content-length']) == len(body)
        json.loads(body)
        if len(calls) == 1: raise httpx.WriteError('private-marker')
        if path in objects:
            return httpx.Response(409, headers=headers, json={'errorCode': 'Duplicate'})
        objects[path] = body
        return httpx.Response(200, headers=headers, json={})
    def factory(**kwargs):
        assert kwargs['trust_env'] is False and kwargs['follow_redirects'] is False
        assert 'verify' not in kwargs and 'cert' not in kwargs
        clients.append(kwargs)
        return real_client(**kwargs, transport=httpx.MockTransport(handle))
    async def no_wait(seconds): assert seconds == 30
    monkeypatch.setattr(httpx, 'AsyncClient', factory)
    monkeypatch.setattr(mod.asyncio, 'sleep', no_wait)
    for key, value in [('URL', 'https://example.invalid'), ('BUCKET', 'synthetic'), ('KEY', 'synthetic')]:
        monkeypatch.setenv('FPL_STORAGE_' + key, value)
    assert mod.storage_client is shared_archive.storage_client is storage_http.storage_client
    report = mod.empty_report()
    assert await mod.probe(report)
    assert report['fallback_upload_status'] == 200 and report['failure_category'] is None
    assert report['gate2_timing_observation'] == 'untested'
    assert len(clients) == len(calls) == 5, 'each request must use a fresh client'
    assert clients[0]['timeout'] == httpx.Timeout(60)
    assert all(c['timeout'] == httpx.Timeout(20) for c in clients[1:])
    assert calls[0].url.path != calls[1].url.path
    assert all('/storage/v1/object/' in r.url.path for r in calls)
    settings = SimpleNamespace(fpl_storage_url='https://example.invalid', fpl_storage_bucket='synthetic',
                               fpl_storage_key=SecretStr('synthetic'), fpl_storage_allowance_bytes=1)
    uploader = research_storage.ResearchStorage(settings)
    await uploader.put(mod.PREFIX + 'synthetic-comparison.json', mod.A, 'application/json')
    assert len(clients) == 6 and clients[-1]['timeout'] == httpx.Timeout(20)
    for header in ('apikey', 'content-type', 'cache-control', 'x-upsert', 'content-length'):
        assert calls[1].headers[header] == calls[-1].headers[header]
