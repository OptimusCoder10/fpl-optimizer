"""J49 three-object receipt creation, integrity and conservative reservation."""
from dataclasses import replace
from datetime import timedelta
import json

import pytest

from fpl_optimizer.services import research_capture as cap
from fpl_optimizer.services.shared_archive import ArchiveError
from test_research_capture import AS_OF, Storage, Time, at_deadline, client, prepared, response, run
from test_research_capture_review import records


def write_document(store, key, value):
    store.data[key] = cap.encoded(value)
    store.info[key] = replace(store.info[key], size=len(store.data[key]))


def receipt_key(store):
    return next(k for k in store.data if k.endswith('.verified.json'))


@pytest.mark.asyncio
async def test_receipt_created_at_controls_completion_and_strict_margin():
    for seconds, expected in ((59.999999, 'captured'), (60, 'unqualified'), (61, 'unqualified')):
        timer = Time()
        class CreatedLater(Storage):
            async def inventory(self):
                for key, item in tuple(self.info.items()):
                    if key.endswith('.verified.json'):
                        # Generic times deliberately differ: only explicit creation counts.
                        self.info[key] = replace(item, created_at=AS_OF + timedelta(seconds=seconds))
                return await super().inventory()
        view = at_deadline(timedelta(minutes=11)); store = CreatedLater(timer.clock)
        result, _, _ = await run(view, store=store, timer=timer)
        assert result.status == expected, 'receipt creation plus margin must be strictly before deadline'
        historical = await records(store, timer, view)
        if expected == 'captured':
            assert result.completion == AS_OF + timedelta(seconds=seconds), 'live completion must include receipt creation'
            assert historical[0][0] == result.completion, 'historical completion must include receipt creation'
        else:
            assert not historical, 'late receipt creation must never qualify'


@pytest.mark.asyncio
async def test_receipt_missing_or_odd_created_at_fails_closed_and_blocks_retry():
    for value in (None, 'invalid', AS_OF.replace(tzinfo=None),
                  AS_OF - timedelta(seconds=121), AS_OF + timedelta(seconds=121)):
        timer = Time()
        class OddCreation(Storage):
            async def inventory(self):
                for key, item in tuple(self.info.items()):
                    if key.endswith('.verified.json'):
                        self.info[key] = replace(item, created_at=value)
                return await super().inventory()
        store = OddCreation(timer.clock)
        result, _, _ = await run(store=store, timer=timer)
        assert result.status == 'unqualified', 'missing or odd receipt creation must fail live qualification'
        assert not await records(store, timer, prepared())
        before = tuple(store.data)
        retry, _, _ = await run(store=store, timer=timer)
        assert retry.category == 'slot_reserved' and tuple(store.data) == before, 'uncertain receipt must reserve its slot'


@pytest.mark.asyncio
async def test_receipt_absent_from_complete_listing_cannot_qualify():
    timer = Time()
    class MissingReceipt(Storage):
        async def inventory(self):
            objects, dates = await super().inventory()
            return tuple(i for i in objects if not i.key.endswith('.verified.json')), dates
    store = MissingReceipt(timer.clock)
    result, _, _ = await run(store=store, timer=timer)
    assert result.status == 'unqualified' and result.category == 'listing_incomplete', 'receipt must appear in complete listing'
    objects = await cap.inventory(store, timer.clock)
    assert not await cap.verified_records(store, objects, cap.PROTECTED, prepared().target.deadline_time, timer.clock)


@pytest.mark.asyncio
async def test_receipt_post_upload_incomplete_listing_is_unqualified():
    timer = Time()
    class Incomplete(Storage):
        async def inventory(self):
            if any(k.endswith('.verified.json') for k in self.data):
                raise ArchiveError('listing_incomplete')
            return await super().inventory()
    result, _, _ = await run(store=Incomplete(timer.clock), timer=timer)
    assert result.status == 'unqualified' and result.category == 'listing_incomplete', 'receipt requires a complete post-upload listing'


@pytest.mark.asyncio
async def test_receipt_post_upload_listing_size_must_match():
    timer = Time()
    class WrongSize(Storage):
        async def inventory(self):
            objects, dates = await super().inventory()
            return tuple(replace(i, size=i.size + 1) if i.key.endswith('.verified.json') else i for i in objects), dates
    result, _, _ = await run(store=WrongSize(timer.clock), timer=timer)
    assert result.status == 'unqualified', 'receipt listing size must match verified bytes'


@pytest.mark.asyncio
async def test_saved_pair_evidence_inside_margin_cannot_qualify():
    for field in ('payload_storage_times', 'sidecar_storage_times', 'runner_verified_at'):
        view = at_deadline(timedelta(minutes=11))
        _, store, timer = await run(view)
        key = receipt_key(store); receipt = json.loads(store.data[key])
        late = AS_OF + timedelta(seconds=60)
        receipt[field] = late.isoformat() if field == 'runner_verified_at' else [late.isoformat()]
        receipt['completion_at'] = late.isoformat()
        write_document(store, key, receipt)
        assert not await records(store, timer, view), 'saved pair evidence must satisfy the margin independently'


@pytest.mark.asyncio
async def test_historical_receipt_get_after_deadline_does_not_redate_completion():
    result, store, timer = await run()
    timer.advance(86400)
    historical = await records(store, timer, prepared())
    assert historical and historical[0][0] == result.completion, 'later GET Date is integrity-only, not completion evidence'


@pytest.mark.asyncio
async def test_corrupt_receipt_reserves_slot_without_qualification():
    _, store, timer = await run()
    store.data[receipt_key(store)] = b'{}'
    assert not await records(store, timer, prepared())
    before = tuple(store.data)
    retry, _, _ = await run(store=store, timer=timer)
    assert retry.status == 'skipped' and retry.category == 'slot_reserved', 'invalid existing receipt must reserve its slot'
    assert tuple(store.data) == before, 'reserved slot must not upload another pair'


@pytest.mark.asyncio
async def test_unidentifiable_receipt_slot_aborts_target_discovery():
    _, store, timer = await run()
    key = receipt_key(store); bad = key.rsplit('/', 1)[0] + '/unknown.verified.json'
    store.data[bad] = store.data.pop(key)
    store.info[bad] = replace(store.info.pop(key), key=bad)
    with pytest.raises(ArchiveError, match='invalid_evidence'):
        await records(store, timer, prepared())
    retry, _, _ = await run(store=store, timer=timer)
    assert retry.status == 'missed' and len(store.data) == 3, 'unknown slot must abort target discovery'


@pytest.mark.asyncio
async def test_receipt_target_and_slot_must_match_pair_and_key():
    for field in ('slot', 'target_event', 'season_id', 'pair_target', 'pair_slot'):
        _, store, timer = await run()
        key = receipt_key(store); receipt = json.loads(store.data[key])
        if field in ('pair_target', 'pair_slot'):
            meta_key = key.removesuffix('.verified.json') + '.meta.json'
            meta = json.loads(store.data[meta_key])
            if field == 'pair_target':
                meta['target_event'] += 1
                receipt['target_event'] = meta['target_event']
            else:
                meta['slot'] += 1
            write_document(store, meta_key, meta)
            receipt['sha256_metadata'] = cap.digest(store.data[meta_key])
        else:
            receipt[field] = 'another-season' if field == 'season_id' else receipt[field] + 1
        write_document(store, key, receipt)
        assert not await records(store, timer, prepared()), 'receipt target and slot must match pair and immutable key'


@pytest.mark.asyncio
async def test_research_listing_preserves_explicit_provider_created_at(monkeypatch):
    store = client()
    async def request(*args, **kwargs):
        return response([{'id': 'x', 'name': 'receipt',
                          'metadata': {'size': 1, 'updated_at': (AS_OF + timedelta(days=1)).isoformat()},
                          'created_at': AS_OF.isoformat()}] if kwargs['json']['offset'] == 0 else [])
    monkeypatch.setattr(store, '_request', request)
    objects, _ = await store.inventory()
    assert objects[0].created_at == AS_OF, 'creation must come from explicit provider created_at'
    assert objects[0].times[0] != objects[0].created_at


@pytest.mark.asyncio
async def test_receipt_empty_response_times_fail_live_qualification():
    for channel in ('put', 'get'):
        timer = Time()
        class MissingResponseTimes(Storage):
            async def put(self, key, body, content_type):
                result = await super().put(key, body, content_type)
                return replace(result, times=()) if channel == 'put' and key.endswith('.verified.json') else result
            async def get(self, key):
                result = await super().get(key)
                return replace(result, times=()) if channel == 'get' and key.endswith('.verified.json') else result
        result, _, _ = await run(store=MissingResponseTimes(timer.clock), timer=timer)
        assert result.status == 'unqualified' and result.category == 'timestamp_missing', 'live receipt response times must be present'


@pytest.mark.asyncio
async def test_historical_receipt_listing_size_must_match():
    _, store, timer = await run()
    key = receipt_key(store)
    store.info[key] = replace(store.info[key], size=store.info[key].size + 1)
    assert not await records(store, timer, prepared()), 'historical receipt listing size must match read bytes'
