"""Development archive boundary tests; no forecast or benchmark execution."""

from dataclasses import asdict, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from fpl_optimizer.schemas.prior_archive import (
    ArchiveConflictError, PINNED_SHA256, load_pinned_archive, validate_archive_files,
)
from fpl_optimizer.services import retrospective_archive as service
from fpl_optimizer.services.retrospective_cohort import (
    BenchmarkFixture, BenchmarkRow, select_cohort,
)

SAMPLE = Path(__file__).parent / "fixtures" / "prior_archive"
KICKOFF = datetime(2026, 1, 1, 15, tzinfo=timezone.utc)
SCORING_FIELDS = (
    "minutes", "goals_scored", "assists", "clean_sheets", "saves",
    "goals_conceded", "clearances_blocks_interceptions", "tackles",
    "recoveries", "bonus", "penalties_saved", "penalties_missed",
    "yellow_cards", "red_cards", "own_goals",
)


def fixture(identifier: int, event: int | None, *, home: int = 2, away: int = 3):
    return SimpleNamespace(
        id=identifier, event=event, kickoff_time=KICKOFF + timedelta(days=identifier),
        team_h=home, team_a=away,
    )


def source_row(identifier: int, *, was_home: bool = True):
    values = {name: index for index, name in enumerate(SCORING_FIELDS, 1)}
    return SimpleNamespace(element=7, fixture=identifier, was_home=was_home,
                           team="WRONG RAW CLUB", **values)


def archive(fixtures, rows):
    return SimpleNamespace(
        catalog=SimpleNamespace(
            fixtures=SimpleNamespace(root=fixtures),
            bootstrap=SimpleNamespace(elements=[SimpleNamespace(id=7, element_type=2)]),
        ),
        histories={7: SimpleNamespace(history=rows)},
        audit=SimpleNamespace(distinct_keys=len(rows)),
    )


def test_adapter_maps_archive_row_to_whitelisted_benchmark_row():
    source = source_row(11)
    view = service.adapt_validated_archive(archive([fixture(11, 18)], [source]))
    row = view.rows[0]
    assert isinstance(row, BenchmarkRow)
    assert isinstance(view.fixtures[0], BenchmarkFixture)
    assert (row.player_id, row.fixture_id, row.event, row.was_home, row.position) == (
        7, 11, 18, True, 2,
    )
    assert row.kickoff_time == view.fixtures[0].kickoff_time
    assert {name: getattr(row, name) for name in SCORING_FIELDS} == {
        name: getattr(source, name) for name in SCORING_FIELDS
    }
    assert set(vars(row)) == {field.name for field in fields(BenchmarkRow)}


def test_adapter_never_reads_xp_value_ownership_or_availability():
    class GuardedRow:
        def __init__(self, source):
            object.__setattr__(self, "source", source)
            object.__setattr__(self, "reads", [])

        def __getattr__(self, name):
            if name not in {"element", "fixture", "was_home", *SCORING_FIELDS}:
                raise AssertionError(f"adapter read non-whitelisted field: {name}")
            self.reads.append(name)
            return getattr(self.source, name)

    clean = service.adapt_validated_archive(archive([fixture(11, 18)], [source_row(11)]))
    extreme = source_row(11)
    vars(extreme).update({
        "xP": "1e100", "value": 10**100, "selected": 10**100,
        "availability": "unavailable", "chance_of_playing_next_round": -10**100,
        "expected_goals": "1e100", "selected_by_percent": "1e100",
    })
    guarded = GuardedRow(extreme)
    view = service.adapt_validated_archive(archive([fixture(11, 18)], [guarded]))
    assert view == clean
    assert set(guarded.reads) == {"element", "fixture", "was_home", *SCORING_FIELDS}
    assert not {"xP", "value", "selected", "availability", "team"} & set(guarded.reads)


def test_adapter_derives_club_from_fixture_sides_not_team_column():
    earlier = fixture(11, 18, home=2, away=3)
    target = fixture(18, 19, home=3, away=4)
    source = source_row(11, was_home=False)
    view = service.adapt_validated_archive(archive([earlier, target], [source]))
    selected = select_cohort(19, view.fixtures, view.rows, [7])
    assert selected.players[0].last_eligible_club_id == 3
    assert selected.players[0].scheduled_fixture_ids == (18,)


def test_dev_view_validates_full_archive_before_dropping(monkeypatch):
    # The saved sample has a conflicting duplicate in an event above GW19.
    def full_validation(_directory):
        files = {name: (SAMPLE / name).read_bytes() for name in PINNED_SHA256}
        return validate_archive_files(files)

    monkeypatch.setattr(service, "load_pinned_archive", full_validation)
    with pytest.raises(ArchiveConflictError):
        service.load_development_view(SAMPLE)


def test_dev_view_contains_no_row_or_fixture_after_gw19(monkeypatch):
    source = archive(
        [fixture(11, 18), fixture(18, 19), fixture(20, 20), fixture(21, None)],
        [source_row(11), source_row(18), source_row(20)],
    )
    monkeypatch.setattr(service, "load_pinned_archive", lambda _directory: source)
    view = service.load_development_view(SAMPLE)
    assert {row.event for row in view.rows} == {18, 19}
    assert {fixture.event for fixture in view.fixtures} == {18, 19}
    assert all(row.event <= 19 for row in view.rows)
    assert all(fixture.event <= 19 for fixture in view.fixtures)
    assert view.counts.dropped_rows == 1
    assert view.counts.dropped_fixtures == 2  # GW20 plus unassigned.


def test_dev_view_counts_are_reported_without_row_data(monkeypatch):
    source = archive(
        [fixture(11, 18), fixture(20, 20), fixture(21, None)],
        [source_row(11), source_row(20)],
    )
    monkeypatch.setattr(service, "load_pinned_archive", lambda _directory: source)
    report = asdict(service.load_development_view(SAMPLE).counts)
    assert report == {
        "rows": 1, "fixtures": 1, "events": 1,
        "dropped_rows": 1, "dropped_fixtures": 2,
    }
    assert all(type(value) is int for value in report.values())


def test_pinned_archive_adapter_counts_if_files_available():
    """Opt-in pin check; a fresh clone has only the deliberately small sample."""
    import os

    directory_text = os.environ.get("FPL_PINNED_ARCHIVE_DIR")
    if not directory_text:
        pytest.skip("FPL_PINNED_ARCHIVE_DIR is not configured")
    directory = Path(directory_text)
    if not all((directory / name).is_file() for name in PINNED_SHA256):
        pytest.skip("the four pinned archive files are not present")
    # Count the complete adapter output, then expose only the development view.
    complete = service.adapt_validated_archive(load_pinned_archive(directory))
    assert complete.counts.rows == 29_747
    development = service.load_development_view(directory)
    assert all(row.event <= 19 for row in development.rows)
    assert all(fixture.event <= 19 for fixture in development.fixtures)
