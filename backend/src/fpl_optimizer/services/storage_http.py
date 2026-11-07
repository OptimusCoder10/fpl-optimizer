"""Shared storage HTTP client settings; credentials never enter logs here."""

import httpx


def storage_client(base_url, api_key, *, timeout=20.0):
    return httpx.AsyncClient(
        base_url=base_url, trust_env=False, follow_redirects=False,
        timeout=httpx.Timeout(timeout), headers={"apikey": api_key},
    )
