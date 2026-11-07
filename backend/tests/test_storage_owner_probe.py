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
    state = SimpleNamespace(seconds=0, calls=[], data={}, duplicate=409, corrupt=False, missing=False)
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
        async def post(self, path, **kwargs):
            state.calls.append(('POST', path, kwargs))
            if path.startswith('/object/list/'):
                assert kwargs['json']['prefix'].startswith(mod.PREFIX)
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
                if state.duplicate == 200: state.data[path] = body
                return response(state.duplicate, json={'error': 'Duplicate'})
            state.data[path] = body
            return response(200, json={})
        async def get(self, path, **kwargs):
            state.calls.append(('GET', path, kwargs))
            body = state.data[path.replace('/authenticated', '')]
            return response(200, content=b'bad' if state.corrupt else body)
    def response(status, **kwargs):
        return httpx.Response(status, headers={'Date': format_datetime(Clock.now(None))},
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
