"""Static contract for the public scheduled ingestion workflow (Task D2)."""

from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/ingest.yml"
SOURCE = WORKFLOW.read_text(encoding="utf-8")


def step(name: str) -> str:
    marker = f"      - name: {name}\n"
    assert SOURCE.count(marker) == 1
    return SOURCE.split(marker, 1)[1].split("      - name: ", 1)[0]


def test_existing_ingestion_controls_unchanged():
    triggers = SOURCE.split("on:\n", 1)[1].split("\npermissions:\n", 1)[0]
    assert triggers == (
        "  schedule:\n"
        "    # Offset from the hour to reduce contention with other scheduled workflows.\n"
        '    - cron: "7,37 * * * *"\n'
        "  workflow_dispatch:\n"
        "    inputs:\n"
        "      research_capture_self_test:\n"
        "        description: Run the synthetic research storage self-test after ingestion\n"
        "        type: boolean\n"
        "        required: true\n"
        "        default: false\n"
    )
    assert SOURCE.split("permissions:\n", 1)[1].split("\n\n", 1)[0] == "  contents: read"
    assert SOURCE.split("concurrency:\n", 1)[1].split("\n\n", 1)[0] == (
        "  group: fpl-ingestion-production-database\n  cancel-in-progress: false"
    )
    assert SOURCE.count("uses: actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1") == 1
    assert SOURCE.count("uses: astral-sh/setup-uv@c771a70e6277c0a99b617c7a806ffedaca235ff9") == 1
    assert 'version: "0.12.19"' in SOURCE
    assert "uv python install 3.14.7" in SOURCE
    assert "uv sync --frozen --no-dev --no-python-downloads" in SOURCE


def test_capture_after_ingestion_without_changing_result():
    ingestion = step("Ingest shared data and resume history")
    capture = step("Capture research bundle")
    assert SOURCE.index("      - name: Capture research bundle") > SOURCE.index(
        "      - name: Ingest shared data and resume history"
    )
    assert "id: ingest_data" in ingestion
    assert "run: .venv/bin/python -m fpl_optimizer.ingest" in ingestion
    assert "always()" in capture
    assert "steps.ingest_data.outcome == 'success'" in capture
    assert "steps.ingest_data.outcome == 'failure'" in capture
    assert "timeout-minutes: 4" in capture
    assert "continue-on-error: true" in capture
    assert "run: .venv/bin/python -m fpl_optimizer.capture_research\n" in capture
    assert "timeout-minutes: 25" in SOURCE
    for name in ("DATABASE_URL", "SESSION_DATABASE_URL", "FPL_STORAGE_URL",
                 "FPL_STORAGE_KEY", "FPL_STORAGE_BUCKET"):
        assert f"{name}: ${{{{ secrets." in capture
    assert "FPL_STORAGE_ALLOWANCE_BYTES: ${{ vars.FPL_STORAGE_ALLOWANCE_BYTES }}" in capture


def test_self_test_unreachable_from_schedule():
    self_test = step("Self-test research storage")
    guard = next((line.strip() for line in self_test.splitlines()
                  if line.strip().startswith("if: ")), None)
    assert guard is not None, "self-test step has no event guard"
    assert "github.event_name == 'workflow_dispatch'" in guard
    assert "inputs.research_capture_self_test == true" in guard
    assert "steps.ingest_data.outcome == 'failure'" in guard
    assert "timeout-minutes: 4" in self_test
    assert "continue-on-error: true" in self_test
    assert "run: .venv/bin/python -m fpl_optimizer.capture_research --self-test" in self_test
    assert SOURCE.count("--self-test") == 1
    for name in ("DATABASE_URL", "SESSION_DATABASE_URL", "FPL_STORAGE_URL",
                 "FPL_STORAGE_KEY", "FPL_STORAGE_BUCKET"):
        assert f"{name}: ${{{{ secrets." in self_test
