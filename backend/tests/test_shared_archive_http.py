"""Offline HTTP checks for the private raw snapshot uploader."""

from datetime import datetime, timezone
import logging

import httpx
import pytest
from pydantic import SecretStr

from fpl_optimizer.config import Settings
from fpl_optimizer.services import shared_archive
from synthetic_fpl import SEASON, full_season


def fake_storage(monkeypatch, handler, *, key="sb_secret_test-key", url="https://storage.example.invalid"):
    actual_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)

    def client_with_fake_transport(**kwargs):
        return actual_client(transport=transport, **kwargs)

    monkeypatch.setattr(shared_archive.httpx, "AsyncClient", client_with_fake_transport)
    settings = Settings(
        _env_file=None,
        database_url="postgresql+asyncpg://test:test@127.0.0.1/test_db",
        fpl_storage_url=url,
        fpl_storage_key=SecretStr(key),
        fpl_storage_bucket="private-archive",
        fpl_storage_allowance_bytes="10000000",
    )
    return shared_archive.SupabaseStorage(settings)


async def capture_with(storage):
    bootstrap, fixtures = full_season()
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    return await shared_archive.archive_shared_publication(
        storage, bootstrap=bootstrap, fixtures=fixtures, season_id=SEASON,
        rules_version="synthetic-v1", collected_from=now,
        collected_to=now, published_at=now, allowance_bytes=10_000_000,
    )


@pytest.mark.asyncio
async def test_upload_sends_key_in_apikey_header_only(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[] if "/object/list/" in request.url.path else {})

    storage = fake_storage(monkeypatch, handler)
    result = await capture_with(storage)
    assert result.status == "captured"
    assert len(requests) == 3
    for request in requests:
        assert request.headers["apikey"] == "sb_secret_test-key"
        assert "authorization" not in request.headers


@pytest.mark.asyncio
async def test_upload_content_types_match_allowed_bucket_mime_types(monkeypatch):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=[] if "/object/list/" in request.url.path else {})

    result = await capture_with(fake_storage(monkeypatch, handler))
    allowed_mime_types = {"application/gzip", "application/x-gzip", "application/json"}
    uploads = [request for request in requests if request.method == "POST"
               and "/object/list/" not in request.url.path]
    assert result.status == "captured" and len(uploads) == 2
    assert all(request.headers["content-type"] in allowed_mime_types for request in uploads)
    assert {request.headers["content-type"] for request in uploads} == {
        "application/gzip", "application/json",
    }
    assert uploads[0].url.path.endswith(".json.gz")
    assert uploads[1].url.path.endswith(".meta.json")


@pytest.mark.asyncio
async def test_upload_headers_never_logged_even_on_error(monkeypatch, caplog, capsys):
    secret_url = "https://private-storage.example.invalid"
    secret_key = "sb_secret_do-not-print-this"

    def handler(request):
        logging.getLogger("httpcore.http11").debug("request headers: %s", dict(request.headers))
        if "/object/list/" in request.url.path:
            return httpx.Response(200, json=[])
        return httpx.Response(500, text=f"failure: {secret_url} {secret_key}")

    storage = fake_storage(monkeypatch, handler, key=secret_key, url=secret_url)
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(shared_archive.ArchiveError) as error:
            await capture_with(storage)
    captured = capsys.readouterr()
    assert error.value.category == "storage_response"
    for output in (caplog.text, captured.out, captured.err, repr(error.value)):
        assert secret_key not in output
        assert secret_url not in output
