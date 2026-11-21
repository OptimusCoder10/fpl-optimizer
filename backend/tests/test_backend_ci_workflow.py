"""Repository contract for the offline backend test workflow."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/backend-tests.yml"
INGESTION = ROOT / ".github/workflows/ingest.yml"


def workflow_text() -> str:
    return WORKFLOW.read_text()


def test_backend_ci_triggers_push_main_and_pull_requests():
    triggers = workflow_text().split("\non:\n", 1)[1].split("\npermissions:", 1)[0]
    assert triggers == "  push:\n    branches: [main]\n  pull_request:\n"


def test_backend_ci_permissions_are_read_only():
    workflow = workflow_text()
    assert len(re.findall(r"(?m)^\s*permissions:", workflow)) == 1
    permissions = workflow.split("\npermissions:\n", 1)[1].split(
        "\nconcurrency:", 1
    )[0]
    assert permissions.strip() == "contents: read"


def test_backend_ci_has_no_secrets_reference():
    assert "secrets." not in workflow_text()


def test_backend_ci_has_no_schedule_trigger():
    assert "schedule:" not in workflow_text()


def test_backend_ci_concurrency_is_distinct_from_ingestion():
    group = re.search(r"(?m)^  group: (.+)$", workflow_text())
    ingestion_group = re.search(r"(?m)^  group: (.+)$", INGESTION.read_text())
    assert group is not None and ingestion_group is not None
    assert group.group(1).startswith("fpl-backend-tests-")
    assert group.group(1) != ingestion_group.group(1)


def test_backend_ci_uses_disposable_database_and_pinned_tools():
    workflow = workflow_text()
    assert "image: postgres:17-alpine" in workflow
    for setting in (
        "POSTGRES_DB: fpl_optimizer_test",
        "POSTGRES_USER: fpl_optimizer",
        "POSTGRES_PASSWORD: fpl_optimizer_test",
        "- 5433:5432",
        "TEST_DATABASE_URL: postgresql+asyncpg://fpl_optimizer:fpl_optimizer_test@127.0.0.1:5433/fpl_optimizer_test",
        'version: "0.12.19"',
    ):
        assert setting in workflow
    assert not re.search(r"(?m)^\s+(?:DATABASE_URL|SESSION_DATABASE_URL):", workflow)
    assert re.findall(r"(?m)^\s+uses: (\S+)", workflow) == [
        "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1",
        "astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9",
    ]
    assert re.findall(r"(?m)^\s+run: (.+)$", workflow) == [
        "uv python install 3.14.7",
        "uv sync --frozen --all-groups",
        "uv run --frozen pytest",
    ]


def test_backend_ci_checkout_does_not_persist_credentials():
    checkout = workflow_text().split("      - name: Check out source\n", 1)[1].split(
        "      - name:", 1
    )[0]
    assert "          persist-credentials: false\n" in checkout


def test_backend_ci_uv_cache_is_disabled():
    setup_uv = workflow_text().split("      - name: Install uv\n", 1)[1].split(
        "      - name:", 1
    )[0]
    assert "          enable-cache: false\n" in setup_uv
