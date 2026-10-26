"""Synthetic J49 tests; no live calls."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from email.utils import format_datetime
import gzip
import json
import time

import httpx
import pytest

from fpl_optimizer.config import Settings
from fpl_optimizer.services import research_capture as cap
from fpl_optimizer.services.research_storage import ObjectInfo, ResearchStorage, StoredResponse
from fpl_optimizer.services.shared_archive import ArchiveError, StoredObject, expired_intraday_keys
from test_forecast_bundle import AS_OF, build, prepared


class Time:
    def __init__(self):
        self.seconds = 0
        self.clock = cap.CaptureClock(lambda: AS_OF + timedelta(seconds=self.seconds), lambda: self.seconds)
    def advance(self, seconds):
        self.seconds += seconds


class Storage:
    def __init__(self, clock):
        self.clock, self.data, self.info, self.calls = clock, {}, {}, []
        self.hook = lambda action, key: None
    def response(self, body=b''):
        now = self.clock.now()
        return StoredResponse(body, (now,), now)
    async def put(self, key, body, content_type):
        self.calls.append(('put', key))
        self.hook('put', key)
        assert key not in self.data, 'immutable upload must not overwrite'
        self.data[key] = body
        self.info[key] = ObjectInfo(key, len(body), (self.clock.now(),), self.clock.now())
        return self.response()
    async def get(self, key):
        self.calls.append(('get', key))
        self.hook('get', key)
        return self.response(self.data[key])
    async def inventory(self):
        self.hook('list', '')
        return tuple(self.info.values()), (self.clock.now(),)


async def fast(view):
    value = build(view).canonical_bytes
    return value, gzip.compress(value, mtime=0)


async def run(view=None, *, store=None, timer=None, budget=180, allowance=10**7, self_test=False, builder=fast):
    timer = timer or Time()
    store = store or Storage(timer.clock)
    attempt = cap.Attempt()
    result = await cap.isolated(lambda: cap.capture(
        prepared() if view is None else view, store, allowance=allowance, clock=timer.clock,
        attempt=attempt, build=builder, self_test=self_test), attempt, budget=budget)
    return result, store, timer


def at_deadline(delta):
    view = prepared()
    target = replace(view.target, deadline_time=AS_OF + delta)
    weeks = tuple(replace(g, deadline_time=target.deadline_time) if g.id == target.id else g for g in view.gameweeks)
    return replace(view, target=target, gameweeks=weeks)


@pytest.mark.asyncio
async def test_capture_only_final_24_hours_and_future_database_target():
    for delta, expected in ((timedelta(hours=24), 'captured'), (timedelta(hours=24, microseconds=1), 'skipped'),
                            (timedelta(0), 'skipped'), (timedelta(seconds=-1), 'skipped')):
        result, store, _ = await run(at_deadline(delta))
        assert result.status == expected
        if expected == 'skipped':
            assert not store.data
    result, _, _ = await run(replace(prepared(), target=None))
    assert result.category == 'no_target'


@pytest.mark.asyncio
async def test_capture_margin_is_strict_at_equality():
    for seconds, expected in ((600, 'skipped'), (600.000001, 'captured')):
        result, _, _ = await run(at_deadline(timedelta(seconds=seconds)))
        assert result.status == expected, 'completion plus ten minutes must be strictly before deadline'


@pytest.mark.asyncio
async def test_capture_pair_order_hashes_and_durable_receipt():
    result, store, timer = await run()
    assert result.status == 'captured'
    puts = [key for op, key in store.calls if op == 'put']
    assert [key.split('.')[-2:] for key in puts] == [['json', 'gz'], ['meta', 'json'], ['verified', 'json']]
    first_receipt = store.calls.index(('put', puts[2]))
    assert ('get', puts[0]) in store.calls[:first_receipt] and ('get', puts[1]) in store.calls[:first_receipt]
    meta = json.loads(store.data[puts[1]])
    assert cap.digest(store.data[puts[0]]) == meta['sha256_gzip']
    assert cap.digest(gzip.decompress(store.data[puts[0]])) == meta['sha256_canonical']
    assert json.loads(store.data[puts[2]])['completion_at'] == result.completion.isoformat()
    records = await cap.verified_records(store, store.info, cap.PROTECTED, prepared().target.deadline_time, timer.clock)
    assert len(records) == 1 and cap.prospective_record(records) == records[0]


@pytest.mark.asyncio
async def test_capture_partial_upload_never_qualifies_and_retry_is_allowed():
    timer = Time(); store = Storage(timer.clock)
    def fail(action, key):
        if action == 'put' and key.endswith('.meta.json'):
            raise ArchiveError('transport')
    store.hook = fail
    result, _, _ = await run(store=store, timer=timer)
    assert result.status == 'unqualified' and len(store.data) == 1
    store.hook = lambda *args: None
    result, _, _ = await run(store=store, timer=timer)
    assert result.status == 'captured'


@pytest.mark.asyncio
async def test_capture_crossing_upload_or_readback_has_no_qualification_receipt():
    for action in ('put', 'get'):
        timer = Time(); store = Storage(timer.clock)
        def cross(op, key):
            if op == action and key.endswith('.meta.json'):
                timer.advance(600)
        store.hook = cross
        result, _, _ = await run(at_deadline(timedelta(minutes=20)), store=store, timer=timer)
        assert result.status == 'unqualified', 'crossing the margin cannot qualify'
        assert not any(key.endswith('.verified.json') for key in store.data)


@pytest.mark.asyncio
async def test_capture_later_storage_timestamp_controls_completion():
    timer = Time(); store = Storage(timer.clock)
    original = store.inventory
    async def future():
        items, dates = await original()
        return tuple(replace(i, times=(AS_OF + timedelta(minutes=11),)) if i.key.endswith('.meta.json') else i
                     for i in items), dates
    store.inventory = future
    result, _, _ = await run(at_deadline(timedelta(minutes=20)), store=store, timer=timer)
    assert result.status == 'unqualified', 'later sidecar storage time must control completion'
    # A conservative future upper bound still reserves its slot immediately.
    timer = Time(); store = Storage(timer.clock)
    original = store.inventory
    store.inventory = future
    result, _, _ = await run(store=store, timer=timer)
    assert result.status == 'captured' and result.completion == AS_OF + timedelta(minutes=11)
    result, _, _ = await run(store=store, timer=timer)
    assert result.category == 'slot_reserved', 'stored completion upper bound must not allow duplicate slots'


@pytest.mark.asyncio
async def test_capture_checks_gzip_and_canonical_checksums_and_sidecar():
    for mode in ('gzip', 'canonical', 'sidecar'):
        timer = Time(); store = Storage(timer.clock)
        original = store.get
        async def corrupt(key):
            response = await original(key)
            if mode == 'gzip' and key.endswith('.json.gz'):
                return replace(response, content=gzip.compress(b'{}', mtime=0))
            if mode == 'sidecar' and key.endswith('.meta.json'):
                return replace(response, content=b'{}')
            return response
        store.get = corrupt
        async def mismatched(view):
            canonical, compressed = await fast(view)
            return (b'{}', compressed) if mode == 'canonical' else (canonical, compressed)
        result, _, _ = await run(store=store, timer=timer, builder=mismatched)
        assert result.status == 'unqualified' and result.category == 'checksum'
        assert not any(key.endswith('.verified.json') for key in store.data)


@pytest.mark.asyncio
async def test_capture_capacity_counts_whole_bucket_and_reserves_receipt():
    timer = Time(); store = Storage(timer.clock)
    store.info['raw-shared/deadline-window/other'] = ObjectInfo('raw-shared/deadline-window/other', 10000, (AS_OF,))
    result, _, _ = await run(store=store, timer=timer, allowance=10500)
    assert result.category == 'capacity' and not store.data
    _, baseline, _ = await run()
    pair_bytes = sum(len(v) for k, v in baseline.data.items() if not k.endswith('.verified.json'))
    result, store, _ = await run(allowance=pair_bytes + cap.RECEIPT_RESERVE - 1)
    assert result.category == 'capacity' and not store.data


@pytest.mark.asyncio
async def test_capture_incomplete_listing_fails_closed():
    timer = Time(); store = Storage(timer.clock)
    async def incomplete():
        raise ArchiveError('listing_incomplete')
    store.inventory = incomplete
    result, _, _ = await run(store=store, timer=timer)
    assert result.status == 'missed' and result.category == 'listing_incomplete' and not store.data


@pytest.mark.asyncio
async def test_capture_suppresses_qualified_slot_and_derives_latest():
    result, store, timer = await run()
    second, _, _ = await run(store=store, timer=timer)
    assert second.status == 'skipped' and second.category == 'slot_reserved' and len(store.data) == 3
    timer.advance(1800)
    view = replace(prepared(), as_of=timer.clock.now())
    third, _, _ = await run(view, store=store, timer=timer)
    assert third.status == 'captured' and len(store.data) == 6
    records = await cap.verified_records(store, store.info, cap.PROTECTED, view.target.deadline_time, timer.clock)
    assert len(records) == 2 and cap.prospective_record(records)[0] == third.completion
    assert not any('latest' in key for key in store.data)


@pytest.mark.asyncio
async def test_capture_bare_pair_cannot_suppress_slot_or_become_prospective():
    _, store, timer = await run()
    key = next(key for key in store.data if key.endswith('.verified.json'))
    del store.data[key]; del store.info[key]
    records = await cap.verified_records(store, store.info, cap.PROTECTED, prepared().target.deadline_time, timer.clock)
    assert cap.prospective_record(records) is None
    result, _, _ = await run(store=store, timer=timer)
    assert result.status == 'captured'


@pytest.mark.asyncio
async def test_capture_timeout_is_bounded_and_isolated():
    async def stall(view):
        await asyncio.Event().wait()
    started = time.monotonic()
    result, store, _ = await run(builder=stall, budget=0.02)
    assert result.status == 'missed' and result.category == 'timeout'
    assert time.monotonic() - started < 1 and not store.data


@pytest.mark.asyncio
async def test_capture_worker_replays_bundle_and_is_killed_on_cancellation(monkeypatch):
    value, compressed = await cap.build_in_process(prepared())
    assert gzip.decompress(compressed) == value and json.loads(value)['schema_version'] == 'research-bundle-v1'
    real = asyncio.create_subprocess_exec
    children = []
    async def stalled(*args, **kwargs):
        child = await real(args[0], '-c', 'import time; time.sleep(60)', **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', stalled)
    try:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.1):
                await cap.build_in_process(prepared())
        assert children and children[0].returncode is not None, 'timed out assembly worker must be dead'
    finally:
        for child in children:
            if child.returncode is None:
                child.kill(); await child.wait()


@pytest.mark.asyncio
async def test_capture_clock_uncertainty_fails_closed():
    timer = Time(); timer.clock.wall = lambda: AS_OF.replace(tzinfo=None)
    result, store, _ = await run(timer=timer)
    assert result.category == 'clock_uncertain' and not store.data
    timer = Time(); timer.clock.wall = lambda: AS_OF + timedelta(minutes=11)
    result, _, _ = await run(timer=timer)
    assert result.category == 'clock_uncertain'
    with pytest.raises(ArchiveError, match='clock_uncertain'):
        Time().clock.observe(StoredResponse(b'', (AS_OF,), AS_OF - timedelta(minutes=3)))


@pytest.mark.asyncio
async def test_capture_self_test_is_synthetic_and_never_prospective():
    result, store, timer = await run(self_test=True, view=object())
    assert result.status == 'captured' and result.category == 'self_test_ok'
    assert all(key.startswith(cap.SELF_TEST) for key in store.data)
    body = next(body for key, body in store.data.items() if key.endswith('.json.gz'))
    assert json.loads(gzip.decompress(body)) == {'schema': 'research-storage-self-test-v1', 'synthetic': True}
    assert not await cap.verified_records(store, store.info, cap.PROTECTED, AS_OF + timedelta(hours=1), timer.clock)


def test_capture_namespaces_cannot_be_pruned_by_raw_archive():
    objects = [StoredObject(prefix + 'sample', 100, AS_OF - timedelta(days=100)) for prefix in
               (cap.PROTECTED, cap.SELF_TEST, 'raw-shared/deadline-window/', 'raw-shared/intraday/')]
    assert expired_intraday_keys(objects, AS_OF) == ['raw-shared/intraday/sample']


def client():
    return ResearchStorage(Settings(_env_file=None, database_url='postgresql+asyncpg://test@localhost/test',
        fpl_storage_url='https://storage.invalid', fpl_storage_bucket='synthetic',
        fpl_storage_key='synthetic-secret', fpl_storage_allowance_bytes='100000'))


def response(body=None, *, content=None, headers=None):
    kwargs = {'content': content} if content is not None else {'json': body}
    return httpx.Response(200, headers={'Date': format_datetime(AS_OF), **(headers or {})}, **kwargs)


@pytest.mark.asyncio
async def test_research_storage_observes_response_times_without_changing_raw_adapter(monkeypatch):
    store = client(); requests = []
    async def request(method, path, **kwargs):
        requests.append((method, path, kwargs))
        if method == 'POST':
            return response({'Key': 'synthetic', 'created_at': AS_OF.isoformat()})
        return response(content=b'payload', headers={'Last-Modified': format_datetime(AS_OF)})
    monkeypatch.setattr(store, '_request', request)
    result = await store.put('test/file', b'payload', 'application/gzip')
    assert result.times == (AS_OF, AS_OF) and requests[0][2]['headers']['x-upsert'] == 'false'
    result = await store.get('test/file')
    assert result.content == b'payload' and result.times == (AS_OF, AS_OF)
    assert '/object/authenticated/' in requests[1][1]


@pytest.mark.asyncio
async def test_research_storage_requires_timestamp_evidence(monkeypatch):
    store = client()
    async def request(*args, **kwargs):
        return httpx.Response(200, json={})
    monkeypatch.setattr(store, '_request', request)
    with pytest.raises(ArchiveError, match='timestamp_missing'):
        await store.put('test/file', b'payload', 'application/gzip')


@pytest.mark.asyncio
async def test_research_storage_paginates_short_pages_and_checks_stability(monkeypatch):
    store = client(); calls = []
    async def request(method, path, **kwargs):
        offset = kwargs['json']['offset']; calls.append(offset)
        rows = [{'id': str(offset), 'name': str(offset), 'created_at': AS_OF.isoformat(),
                 'metadata': {'size': 5}}] if offset < 2 else []
        return response(rows)
    monkeypatch.setattr(store, '_request', request)
    items, _ = await store.inventory()
    assert len(items) == 2 and calls == [0, 1, 2, 0, 1, 2]
    async def changed(method, path, **kwargs):
        result = await request(method, path, **kwargs)
        return response([]) if len(calls) > 9 and kwargs['json']['offset'] == 0 else result
    monkeypatch.setattr(store, '_request', changed)
    with pytest.raises(ArchiveError, match='listing_incomplete'):
        await store.inventory()


@pytest.mark.asyncio
async def test_research_storage_rejects_duplicate_or_malformed_listing(monkeypatch):
    store = client()
    for size in (-1, 1):
        async def repeat(*args, **kwargs):
            return response([{'id': 'x', 'name': 'x', 'metadata': {'size': size}, 'created_at': AS_OF.isoformat()}])
        monkeypatch.setattr(store, '_request', repeat)
        with pytest.raises(ArchiveError, match='listing_incomplete'):
            await store.inventory()


def test_capture_command_failure_logs_only_safe_fields_and_returns_zero(monkeypatch, caplog):
    from fpl_optimizer import capture_research as command
    from fpl_optimizer import ingest
    original = ingest.run_ingestion
    def bad_settings():
        raise RuntimeError('synthetic-secret private-payload')
    monkeypatch.setattr(command, 'Settings', bad_settings)
    monkeypatch.setattr('sys.argv', ['capture_research'])
    with caplog.at_level('INFO'):
        assert command.main() == 0
    assert 'status=missed category=internal' in caplog.text
    assert 'synthetic-secret' not in caplog.text and 'private-payload' not in caplog.text
    assert ingest.run_ingestion is original


@pytest.mark.asyncio
async def test_capture_receipt_crossing_margin_is_never_selected():
    timer = Time(); store = Storage(timer.clock)
    def cross(op, key):
        if op == 'put' and key.endswith('.verified.json'):
            timer.advance(600)
    store.hook = cross
    view = at_deadline(timedelta(minutes=20))
    result, _, _ = await run(view, store=store, timer=timer)
    assert result.status == 'unqualified'
    records = await cap.verified_records(store, store.info, cap.PROTECTED, view.target.deadline_time, timer.clock)
    assert cap.prospective_record(records) is None, 'late receipt cannot promote a pair'


@pytest.mark.asyncio
async def test_capture_corrupt_receipt_or_payload_cannot_qualify():
    for mode in ('receipt', 'payload', 'deadline'):
        _, store, timer = await run()
        if mode == 'receipt':
            key = next(k for k in store.data if k.endswith('.verified.json'))
            record = json.loads(store.data[key]); record['completion_at'] = (AS_OF - timedelta(seconds=1)).isoformat()
            store.data[key] = cap.encoded(record)
        elif mode == 'payload':
            key = next(k for k in store.data if k.endswith('.json.gz'))
            store.data[key] = gzip.compress(b'{}', mtime=0)
        deadline = prepared().target.deadline_time + (timedelta(seconds=1) if mode == 'deadline' else timedelta(0))
        assert not await cap.verified_records(store, store.info, cap.PROTECTED, deadline, timer.clock)


@pytest.mark.asyncio
async def test_capture_command_holds_ingestion_lock_and_reads_view_once(monkeypatch):
    from contextlib import asynccontextmanager
    from fpl_optimizer import capture_research as command
    events = []; locked = False
    timer = Time(); store = Storage(timer.clock)
    config = Settings(_env_file=None, database_url='postgresql+asyncpg://test@localhost/test',
        session_database_url='postgresql+asyncpg://test@localhost/test', fpl_season_id='2026-27',
        fpl_storage_url='https://storage.invalid', fpl_storage_bucket='synthetic',
        fpl_storage_key='synthetic', fpl_storage_allowance_bytes='1000000')
    @asynccontextmanager
    async def lock(_):
        nonlocal locked
        locked = True; events.append('lock')
        try:
            yield 'connection'
        finally:
            locked = False; events.append('unlock')
    class Session:
        async def connection(self):
            return 'reader'
    @asynccontextmanager
    async def session():
        yield Session()
    class Engine:
        async def dispose(self):
            events.append('dispose')
    async def identity(first, second):
        assert locked and (first, second) == ('connection', 'reader')
    async def read_view(factory, season, *, as_of):
        assert locked and season == '2026-27' and as_of == AS_OF
        events.append('snapshot_closed')
        return prepared()
    async def captured(view, storage, **kwargs):
        assert locked and events[-1] == 'snapshot_closed'
        events.append('capture')
        return await cap.capture(view, storage, build=fast, **kwargs)
    monkeypatch.setattr(command, 'Settings', lambda: config)
    monkeypatch.setattr(command, 'CaptureClock', lambda: timer.clock)
    monkeypatch.setattr(command, 'ResearchStorage', lambda _: store)
    monkeypatch.setattr(command, 'ingestion_lock', lock)
    monkeypatch.setattr(command, 'require_same_database', identity)
    monkeypatch.setattr(command, 'create_database_engine', lambda _: Engine())
    monkeypatch.setattr(command, 'create_session_factory', lambda _: session)
    monkeypatch.setattr(command, 'read_bundle_view', read_view)
    monkeypatch.setattr(command, 'capture', captured)
    assert (await command.run()).status == 'captured'
    assert events == ['lock', 'snapshot_closed', 'capture', 'dispose', 'unlock']


@pytest.mark.asyncio
async def test_research_storage_traverses_folders_and_rejects_missing_created_at(monkeypatch):
    store = client()
    async def request(method, path, **kwargs):
        prefix, offset = kwargs['json']['prefix'], kwargs['json']['offset']
        if offset:
            return response([])
        return response([{'id': None, 'name': 'folder', 'metadata': None}] if prefix == '' else
                        [{'id': 'x', 'name': 'file', 'created_at': AS_OF.isoformat(), 'metadata': {'size': 9}}])
    monkeypatch.setattr(store, '_request', request)
    items, _ = await store.inventory()
    assert [i.key for i in items] == ['folder/file']
    async def missing(*args, **kwargs):
        return response([{'id': 'x', 'name': 'file', 'metadata': {'size': 9}}])
    monkeypatch.setattr(store, '_request', missing)
    with pytest.raises(ArchiveError, match='listing_incomplete'):
        await store.inventory()
