"""Synthetic regressions for the read-only review of D1."""
import asyncio
from dataclasses import replace
from datetime import timedelta
import gzip
import json
import os
from pathlib import Path
import signal
import sys

import pytest

from fpl_optimizer.services import research_capture as cap
from fpl_optimizer.services.shared_archive import ArchiveError
from test_research_capture import AS_OF, Storage, Time, at_deadline, prepared, run


async def records(store, timer, view):
    return await cap.verified_records(store, store.info, cap.PROTECTED,
                                      view.target.deadline_time, timer.clock)


@pytest.mark.asyncio
async def test_receipt_late_readback_is_live_unqualified_but_historically_qualified():
    timer = Time(); store = Storage(timer.clock)
    view = at_deadline(timedelta(minutes=20))
    def delayed(action, key):
        if action == 'get' and key.endswith('.verified.json'):
            timer.advance(600)
    store.hook = delayed
    result, _, _ = await run(view, store=store, timer=timer)
    assert result.status == 'unqualified', 'live receipt failure must remain unqualified'
    store.hook = lambda *args: None
    assert await records(store, timer, view), 'late receipt GET must not re-date historical completion'


@pytest.mark.asyncio
async def test_receipt_interrupted_readback_is_live_unqualified_but_historically_qualified():
    timer = Time(); store = Storage(timer.clock); view = prepared()
    def interrupted(action, key):
        if action == 'get' and key.endswith('.verified.json'):
            raise ArchiveError('transport')
    store.hook = interrupted
    result, _, _ = await run(view, store=store, timer=timer)
    assert result.status == 'unqualified', 'live receipt failure must remain unqualified'
    store.hook = lambda *args: None
    assert await records(store, timer, view), 'interrupted receipt GET does not disprove timely creation'


@pytest.mark.asyncio
async def test_receipt_late_response_is_live_unqualified_but_historically_qualified():
    timer = Time()
    class LateReceipt(Storage):
        async def put(self, key, body, content_type):
            result = await super().put(key, body, content_type)
            if key.endswith('.verified.json'):
                return replace(result, times=(AS_OF + timedelta(minutes=10),))
            return result
    store = LateReceipt(timer.clock); view = at_deadline(timedelta(minutes=20))
    result, _, _ = await run(view, store=store, timer=timer)
    assert result.status == 'unqualified', 'live receipt failure must remain unqualified'
    assert await records(store, timer, view), 'live response margin must not replace provider creation time'


@pytest.mark.asyncio
async def test_capture_reserves_slot_without_reading_unreadable_history():
    result, store, timer = await run()
    assert result.status == 'captured'
    old_keys = set(store.data)
    def fail(action, key):
        if action == 'get' and key in old_keys and key.endswith('.meta.json'):
            raise ArchiveError('transport')
    store.hook = fail
    result, _, _ = await run(store=store, timer=timer)
    assert result.status == 'skipped' and result.category == 'slot_reserved'
    assert set(store.data) == old_keys, 'unreadable receipt slot must not be duplicated'
    with pytest.raises(ArchiveError, match='transport'):
        await cap.derive_prospective_record(store, prefix=cap.PROTECTED,
            deadline=prepared().target.deadline_time, clock=timer.clock)


@pytest.mark.asyncio
async def test_discovery_read_failure_cannot_promote_older_capture():
    _, store, timer = await run()
    timer.advance(1800)
    view = replace(prepared(), as_of=timer.clock.now())
    await run(view, store=store, timer=timer)
    latest_payload = max(key for key in store.data if key.endswith('.json.gz'))
    def fail(action, key):
        if action == 'get' and key == latest_payload:
            raise ArchiveError('transport')
    store.hook = fail
    with pytest.raises(ArchiveError, match='transport'):
        await records(store, timer, view)


@pytest.mark.asyncio
async def test_payload_storage_times_control_capture_margin():
    for channel in ('put', 'get', 'list'):
        timer = Time()
        class LatePayload(Storage):
            async def put(self, key, body, content_type):
                result = await super().put(key, body, content_type)
                if channel == 'put' and key.endswith('.json.gz'):
                    return replace(result, times=(AS_OF + timedelta(minutes=10),))
                return result
            async def get(self, key):
                result = await super().get(key)
                if channel == 'get' and key.endswith('.json.gz'):
                    return replace(result, times=(AS_OF + timedelta(minutes=10),))
                return result
            async def inventory(self):
                objects, dates = await super().inventory()
                if channel == 'list':
                    objects = tuple(replace(i, times=(AS_OF + timedelta(minutes=10),))
                                    if i.key.endswith('.json.gz') else i for i in objects)
                return objects, dates
        result, _, _ = await run(at_deadline(timedelta(minutes=20)),
                                 store=LatePayload(timer.clock), timer=timer)
        assert result.status == 'unqualified', f'payload {channel} timestamp must enforce strict margin'


@pytest.mark.asyncio
async def test_payload_listing_time_controls_historical_qualification():
    view = at_deadline(timedelta(minutes=20))
    _, store, timer = await run(view)
    key = next(key for key in store.info if key.endswith('.json.gz'))
    store.info[key] = replace(store.info[key], times=(AS_OF + timedelta(minutes=10),))
    assert not await records(store, timer, view), 'late payload listing time must prevent qualification'


def alive(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().split()[2] != 'Z'
    except (FileNotFoundError, ProcessLookupError):
        return False


@pytest.mark.parametrize("termination", ["cancel", "timeout"])
@pytest.mark.asyncio
async def test_worker_cancellation_kills_descendant_processes(monkeypatch, tmp_path, termination):
    real = asyncio.create_subprocess_exec
    pidfile = tmp_path / 'grandchild.pid'
    children = []
    script = ("import subprocess,sys,time; "
              "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)'],"
              "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
              f"open({str(pidfile)!r},'w').write(str(p.pid)); time.sleep(60)")
    async def spawn(*args, **kwargs):
        child = await real(args[0], '-c', script, **kwargs)
        children.append(child)
        return child
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    task = asyncio.create_task(cap.build_in_process(prepared()))
    grandchild = None
    try:
        async with asyncio.timeout(5):
            while not pidfile.exists():
                await asyncio.sleep(.01)
        grandchild = int(pidfile.read_text())
        if termination == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            with pytest.raises(TimeoutError):
                async with asyncio.timeout(.01):
                    await task
        for _ in range(100):
            if not alive(grandchild):
                break
            await asyncio.sleep(.01)
        assert not alive(grandchild), 'cancelled worker must leave no live descendant'
        assert children[0].returncode is not None
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if grandchild and alive(grandchild):
            os.kill(grandchild, signal.SIGKILL)
        for child in children:
            if child.returncode is None:
                child.kill(); await child.wait()


@pytest.mark.asyncio
async def test_worker_runs_in_installed_actions_venv_with_minimal_environment(monkeypatch):
    real = asyncio.create_subprocess_exec
    observed = []
    async def spawn(*args, **kwargs):
        observed.append((args, kwargs))
        return await real(*args, **kwargs)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setenv('FPL_STORAGE_KEY', 'synthetic-must-not-inherit')
    canonical, compressed = await cap.build_in_process(prepared())
    assert gzip.decompress(compressed) == canonical
    assert json.loads(canonical)['schema_version'] == 'research-bundle-v1'
    assert sys.prefix != sys.base_prefix, 'exercise the installed virtual environment used by Actions'
    assert observed[0][0][0] == sys.executable
    assert observed[0][1]['env'] == {'PATH': '/usr/bin:/bin'}
    assert observed[0][1]['start_new_session'] is True, 'worker must start a new session'


@pytest.mark.asyncio
async def test_payload_response_time_is_preserved_in_historical_completion():
    timer = Time()
    observed = AS_OF + timedelta(minutes=5)
    class FuturePayload(Storage):
        async def put(self, key, body, content_type):
            result = await super().put(key, body, content_type)
            return replace(result, times=(observed,)) if key.endswith('.json.gz') else result
    store = FuturePayload(timer.clock)
    result, _, _ = await run(store=store, timer=timer)
    assert result.status == 'captured' and result.completion == observed
    historical = await records(store, timer, prepared())
    assert historical and historical[0][0] == observed, 'persisted payload observations must determine completion'
    again, _, _ = await run(store=store, timer=timer)
    assert again.category == 'slot_reserved'
