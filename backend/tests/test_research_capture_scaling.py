"""Synthetic review regressions and measured capture-only request budgets."""
from dataclasses import replace
from datetime import timedelta
from email.utils import format_datetime
import gzip
import hashlib
import json
import time
from unittest.mock import patch

import pytest

from fpl_optimizer.services import research_capture as cap
from fpl_optimizer.services.shared_archive import ArchiveError
from test_research_capture import AS_OF, Storage, Time, client, fast, prepared, response, run
from test_research_capture_review import records
from test_research_receipt import receipt_key, write_document


@pytest.mark.asyncio
async def test_unjudgeable_receipt_does_not_block_other_slots():
    for defect in ('missing', 'naive', 'future', 'conflicting', 'corrupt', 'invalid_evidence', 'bad_timestamp', 'bad_meta_timestamp'):
        _, store, timer = await run()
        key = receipt_key(store)
        if defect == 'corrupt':
            write_document(store, key, {})
        elif defect == 'bad_meta_timestamp':
            meta_key = key.removesuffix('.verified.json') + '.meta.json'
            meta = json.loads(store.data[meta_key]); meta['capture_time'] = 'not-a-time'
            write_document(store, meta_key, meta)
            receipt = json.loads(store.data[key]); receipt['sha256_metadata'] = cap.digest(store.data[meta_key])
            write_document(store, key, receipt)
        elif defect == 'bad_timestamp':
            receipt = json.loads(store.data[key]); receipt['completion_at'] = 'not-a-time'
            write_document(store, key, receipt)
        elif defect == 'invalid_evidence':
            receipt = json.loads(store.data[key]); receipt['payload_storage_times'] = []
            write_document(store, key, receipt)
        else:
            value = {'missing': None, 'naive': AS_OF.replace(tzinfo=None),
                     'future': AS_OF + timedelta(days=1), 'conflicting': AS_OF - timedelta(seconds=60)}[defect]
            store.info[key] = replace(store.info[key], created_at=value)
        same, _, _ = await run(store=store, timer=timer)
        assert same.category == 'slot_reserved', 'unjudgeable receipt must reserve only its own slot'
        timer.advance(1800)
        view = replace(prepared(), as_of=timer.clock.now())
        next_slot, _, _ = await run(view, store=store, timer=timer)
        assert next_slot.status == 'captured', 'bad receipt must not block a different slot'
        try:
            valid = await records(store, timer, view)
        except ArchiveError as error:
            pytest.fail(f'receipt-local evidence must not abort discovery: {error.category}')
        assert len(valid) == 1 and valid[0][1] != key


@pytest.mark.asyncio
async def test_replaced_receipt_never_qualifies_and_only_reserves_its_slot():
    _, store, timer = await run()
    key = receipt_key(store)
    store.info[key] = replace(store.info[key], times=(AS_OF, AS_OF + timedelta(seconds=3)))
    assert not await records(store, timer, prepared()), 'provider time beyond creation tolerance must reject receipt'
    same, _, _ = await run(store=store, timer=timer)
    assert same.category == 'slot_reserved'
    timer.advance(1800)
    view = replace(prepared(), as_of=timer.clock.now())
    later, _, _ = await run(view, store=store, timer=timer)
    assert later.status == 'captured'
    valid = await records(store, timer, view)
    assert len(valid) == 1 and valid[0][1] != key


@pytest.mark.asyncio
async def test_live_replaced_receipt_is_unqualified():
    timer = Time()
    class Replaced(Storage):
        async def inventory(self):
            objects, dates = await super().inventory()
            return tuple(replace(i, times=i.times + (i.created_at + timedelta(seconds=3),))
                         if i.key.endswith('.verified.json') else i for i in objects), dates
    result, _, _ = await run(store=Replaced(timer.clock), timer=timer)
    assert result.status == 'unqualified', 'live capture must reject replaced receipt metadata'


@pytest.mark.asyncio
async def test_invalid_receipt_timestamps_do_not_invalidate_complete_inventory(monkeypatch):
    store = client()
    key = cap.PROTECTED + '2026-27/4/20261025T120000000000Z-' + '0' * 32 + '.verified.json'
    # Exercise adapter parsing directly with a folder listing containing a receipt.
    async def request(*args, **kwargs):
        prefix = kwargs['json']['prefix']; offset = kwargs['json']['offset']
        if offset:
            return response([])
        if prefix == '':
            return response([{'id': None, 'name': 'research-bundles', 'metadata': None}])
        if prefix == 'research-bundles':
            return response([{'id': 'receipt', 'name': key.rsplit('/', 1)[-1],
                              'created_at': 'not-a-time', 'metadata': {'size': 10}}])
        raise AssertionError('unexpected prefix')
    monkeypatch.setattr(store, '_request', request)
    try:
        objects = await cap.inventory(store, Time().clock)
    except ArchiveError as error:
        pytest.fail(f'invalid receipt timestamps must preserve listing identity: {error.category}')
    assert len(objects) == 1 and next(iter(objects)).endswith('.verified.json')


class FakeTransport:
    """Real research pagination over synthetic storage; each request costs 1 fake second."""
    def __init__(self, timer, data, created):
        self.timer, self.data, self.created = timer, dict(data), dict(created)
        self.old_keys = frozenset(data)
        self.calls = []
        self.scans = 0
        self.decompressions = 0

    async def request(self, method, path, **kwargs):
        self.timer.advance(1)
        now = self.timer.clock.now()
        headers = {'Date': format_datetime(now)}
        import httpx
        if path.startswith('/object/list/'):
            self.calls.append(('list', kwargs['json']['prefix']))
            prefix = kwargs['json']['prefix']
            start = prefix + '/' if prefix else ''
            rows = {}
            for key, body in self.data.items():
                if not key.startswith(start):
                    continue
                rest = key[len(start):]; name = rest.split('/', 1)[0]
                if '/' in rest:
                    rows[name] = {'id': None, 'name': name, 'metadata': None}
                else:
                    rows[name] = {'id': hashlib.sha256(key.encode()).hexdigest(), 'name': name,
                                  'created_at': self.created[key].isoformat(),
                                  'updated_at': self.created[key].isoformat(),
                                  'metadata': {'size': len(body)}}
            offset, limit = kwargs['json']['offset'], kwargs['json']['limit']
            page = sorted(rows.values(), key=lambda row: row['name'])[offset:offset+limit]
            return httpx.Response(200, json=page, headers=headers)
        if method == 'GET':
            key = path.split('/', 4)[4]
            self.calls.append(('get', key))
            return httpx.Response(200, content=self.data[key], headers=headers)
        key = path.split('/', 3)[3]
        assert key not in self.data
        self.calls.append(('put', key)); self.data[key] = kwargs['content']; self.created[key] = now
        return httpx.Response(200, json={'created_at': now.isoformat()}, headers=headers)


async def measured_capture(count, *, fresh):
    # Construct historical artifacts from one synthetic valid bundle, without DB or HTTP.
    _, template, _ = await run()
    original_receipt = receipt_key(template)
    original_base = original_receipt.removesuffix('.verified.json')
    data = {}; created = {}
    for index in range(count):
        stamp = AS_OF - timedelta(minutes=30 * index)
        base = cap.PROTECTED + '2026-27/4/' + stamp.strftime('%Y%m%dT%H%M%S%fZ') + '-' + f'{index:032x}'
        document = json.loads(gzip.decompress(template.data[original_base + '.json.gz']))
        document['capture_time'] = stamp.isoformat()
        canonical = cap.encoded(document); compressed = gzip.compress(canonical, mtime=0)
        meta = json.loads(template.data[original_base + '.meta.json'])
        meta.update(capture_time=stamp.isoformat(), slot=int(stamp.timestamp()) // 1800,
                    sha256_canonical=cap.digest(canonical), sha256_gzip=cap.digest(compressed),
                    canonical_bytes=len(canonical), gzip_bytes=len(compressed))
        metadata = cap.encoded(meta)
        receipt = json.loads(template.data[original_receipt])
        receipt.update(completion_at=stamp.isoformat(), runner_verified_at=stamp.isoformat(),
                       sidecar_storage_times=[stamp.isoformat()], payload_storage_times=[stamp.isoformat()],
                       sha256_metadata=cap.digest(metadata), sha256_gzip=cap.digest(compressed), slot=meta['slot'])
        for suffix, body in (('.json.gz', compressed), ('.meta.json', metadata), ('.verified.json', cap.encoded(receipt))):
            data[base + suffix] = body; created[base + suffix] = stamp
    timer = Time()
    if fresh:
        timer.advance(1800)
    transport = FakeTransport(timer, data, created)
    storage = client(); storage._request = transport.request
    scan = storage._scan
    async def counted_scan():
        transport.scans += 1
        return await scan()
    storage._scan = counted_scan
    view = replace(prepared(), as_of=timer.clock.now())
    async def build(view):
        timer.advance(22)  # Existing assembly measurement, charged explicitly to fake budget.
        return await fast(view)
    begin_fake = timer.seconds; begin = time.perf_counter()
    decompress = gzip.decompress
    def counted_decompress(value):
        transport.decompressions += 1
        return decompress(value)
    with patch.object(cap.gzip, 'decompress', counted_decompress):
        result, _, _ = await run(view, store=storage, timer=timer, builder=build)
    elapsed = time.perf_counter() - begin
    return result, transport, timer.seconds - begin_fake, elapsed


@pytest.mark.asyncio
async def test_capture_48_slots_uses_listing_only_for_suppression():
    for count in (1, 24, 48):
        result, transport, simulated, elapsed = await measured_capture(count, fresh=False)
        gets = [key for op, key in transport.calls if op == 'get']
        assert not gets, 'slot suppression must not download any historical objects'
        assert result.category == 'slot_reserved'
        assert transport.scans == 2 and simulated < cap.BUDGET_SECONDS
        assert transport.decompressions == 0, 'slot suppression must not decompress history'
        print(f'suppressed slots={count} requests={len(transport.calls)} scans={transport.scans} fake_s={simulated} wall_s={elapsed:.6f}')


@pytest.mark.asyncio
async def test_capture_48_slots_reads_only_its_new_artifacts():
    for count in (1, 24, 48):
        result, transport, simulated, elapsed = await measured_capture(count, fresh=True)
        gets = [key for op, key in transport.calls if op == 'get']
        assert not any(key in transport.old_keys for key in gets), 'new capture must not download historical objects'
        assert result.status == 'captured', 'fresh slot must still verify and capture its own artifacts'
        assert len(gets) == 3 and transport.scans == 6
        assert transport.decompressions == 1, 'only the new payload may be decompressed'
        assert simulated < cap.BUDGET_SECONDS, 'capture must stay within the simulated 180-second budget'
        print(f'fresh slots={count} requests={len(transport.calls)} scans={transport.scans} fake_s={simulated} wall_s={elapsed:.6f}')


@pytest.mark.asyncio
async def test_receipt_replacement_tolerance_excludes_later_http_date():
    for seconds, expected in ((2, True), (2.000001, False)):
        _, store, timer = await run()
        key = receipt_key(store)
        store.info[key] = replace(store.info[key], times=(AS_OF, AS_OF + timedelta(seconds=seconds)))
        timer.advance(86400)
        valid = await records(store, timer, prepared())
        assert bool(valid) is expected, 'replacement tolerance must be two seconds and exclude HTTP Date'


@pytest.mark.asyncio
async def test_receipt_last_modified_response_detects_replacement():
    timer = Time()
    class ChangedResponse(Storage):
        async def get(self, key):
            result = await super().get(key)
            return replace(result, object_times=(AS_OF + timedelta(seconds=3),)) if key.endswith('.verified.json') else result
    store = ChangedResponse(timer.clock)
    result, _, _ = await run(store=store, timer=timer)
    assert result.status == 'unqualified', 'response object timestamps must detect replacement live'
    assert not await records(store, timer, prepared()), 'response object timestamps must detect replacement historically'


@pytest.mark.asyncio
async def test_invalid_receipt_metadata_changes_still_fail_listing_stability(monkeypatch):
    store = client(); scans = 0
    async def request(*args, **kwargs):
        nonlocal scans
        prefix = kwargs['json']['prefix']
        if kwargs['json']['offset']:
            return response([])
        if prefix == '':
            scans += 1
            return response([{'id': None, 'name': 'research-bundles', 'metadata': None}])
        return response([{'id': 'receipt', 'name': 'sample.verified.json',
                          'created_at': f'bad-time-{scans}', 'metadata': {'size': 10}}])
    monkeypatch.setattr(store, '_request', request)
    with pytest.raises(ArchiveError, match='listing_incomplete'):
        await store.inventory()


@pytest.mark.asyncio
async def test_separate_derivation_propagates_read_failures_without_older_fallback():
    for suffix in ('.verified.json', '.meta.json', '.json.gz'):
        _, store, timer = await run()
        timer.advance(1800)
        view = replace(prepared(), as_of=timer.clock.now())
        await run(view, store=store, timer=timer)
        result = await cap.derive_prospective_record(store, prefix=cap.PROTECTED,
            deadline=view.target.deadline_time, clock=timer.clock)
        assert result and result[0] == view.as_of
        latest_base = result[1].removesuffix('.verified.json')
        def fail(action, key):
            if action == 'get' and key == latest_base + suffix:
                raise ArchiveError('transport')
        store.hook = fail
        with pytest.raises(ArchiveError, match='transport'):
            await cap.derive_prospective_record(store, prefix=cap.PROTECTED,
                deadline=view.target.deadline_time, clock=timer.clock)


@pytest.mark.asyncio
async def test_storage_separates_object_times_from_http_date(monkeypatch):
    store = client()
    async def request(method, path, **kwargs):
        if method == 'GET':
            return response(content=b'bytes', headers={
                'Date': format_datetime(AS_OF + timedelta(hours=1)),
                'Last-Modified': format_datetime(AS_OF),
            })
        return response({'created_at': AS_OF.isoformat()}, headers={
            'Date': format_datetime(AS_OF + timedelta(seconds=10)),
        })
    monkeypatch.setattr(store, '_request', request)
    read = await store.get('synthetic')
    written = await store.put('synthetic', b'bytes', 'application/json')
    assert read.object_times == (AS_OF,) and written.object_times == (AS_OF,), 'HTTP Date must not enter object replacement evidence'
    assert read.server_date == AS_OF + timedelta(hours=1)
