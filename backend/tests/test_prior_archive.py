"""Pinned archive validation, publication and identity-based prior read-back."""

from copy import deepcopy
import csv
from datetime import datetime, timezone
import io
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, func, select, update

from fpl_optimizer.db.advisory_lock import ingestion_lock
from fpl_optimizer.db.models import CacheMetadata, Fixture, Gameweek, Player, PlayerFixtureHistory, Season
from fpl_optimizer.db.session import create_session_factory
from fpl_optimizer.schemas.bootstrap import SharedPublication
from fpl_optimizer.schemas.prior_archive import (
    ARCHIVE_COMMIT, ARCHIVE_RULES, ARCHIVE_SEASON, PINNED_SHA256,
    ArchiveConflictError, load_pinned_archive, validate_archive_files,
)
from fpl_optimizer.services import prior_archive as service
from fpl_optimizer.services.player_history import read_player_event_totals, read_player_history
from fpl_optimizer.services.shared_catalog import publish_shared_catalog

SAMPLE = Path(__file__).parent / "fixtures" / "prior_archive"


async def publish_sample(*args, **kwargs):
    return await service.publish_prior_archive(*args, allow_sample=True, **kwargs)


async def read_sample(*args, **kwargs):
    return await service.read_prior_inputs(*args, allow_sample=True, **kwargs)


def sample_files(*, conflict=False):
    files = {name: (SAMPLE / name).read_bytes() for name in PINNED_SHA256}
    if not conflict:
        mutate_csv(files, "merged_gw.csv", lambda rows: rows.pop())
    return files


def mutate_csv(files, name, mutate):
    rows = list(csv.DictReader(io.StringIO(files[name].decode())))
    mutate(rows)
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    files[name] = output.getvalue().encode()


def publication():
    # Deliberately old collection time: pinned priors have no current age gate.
    return SharedPublication(
        season_id=ARCHIVE_SEASON, rules_version=ARCHIVE_RULES,
        source_observed_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        published_at=datetime(2026, 6, 2, tzinfo=timezone.utc),
    )


@pytest.fixture
async def archive_db(migrated_test_engine, publication_settings):
    factory = create_session_factory(migrated_test_engine)
    async with factory.begin() as session:
        await session.execute(delete(Season).where(Season.id.in_([ARCHIVE_SEASON, "2026-27"])))
    yield factory, publication_settings
    async with factory.begin() as session:
        await session.execute(delete(Season).where(Season.id.in_([ARCHIVE_SEASON, "2026-27"])))


async def import_sample(archive_db):
    factory, settings = archive_db
    archive = validate_archive_files(sample_files())
    await publish_sample(factory, archive, publication(), settings=settings)
    return archive


async def current_catalog(archive_db, archive, *, position=4, code=0):
    factory, settings = archive_db
    catalog = deepcopy(archive.catalog)
    old = next(p for p in catalog.bootstrap.elements if p.id == 817)
    catalog.bootstrap.elements = [old.model_copy(update={
        "id": 42, "web_name": "Completely different display name", "element_type": position,
        "code": code or old.code,
    })]
    await publish_shared_catalog(factory, catalog, publication().model_copy(update={
        "season_id": "2026-27", "rules_version": "fpl-2026-v1",
    }), settings=settings)


def test_saved_conflict_quarantines_all_variants_even_unused_fields(tmp_path):
    with pytest.raises(ArchiveConflictError) as error:
        validate_archive_files(sample_files(conflict=True))
    audit = error.value.audit
    assert (audit.rows, audit.exact_duplicates, audit.distinct_rows, audit.distinct_keys) == (5, 1, 4, 3)
    assert audit.conflicting_keys == ((658, 257),)
    assert [(row.line, row.source["xP"]) for row in error.value.quarantine] == [(4, "0.0"), (6, "999")]
    saved = tmp_path / "quarantine.json"
    error.value.write_quarantine(saved)
    assert '"999"' in saved.read_text() and '"conflicting_keys"' in saved.read_text()


def test_exact_duplicates_removed_double_and_genuine_zeros_preserved():
    archive = validate_archive_files(sample_files())
    assert (archive.audit.rows, archive.audit.exact_duplicates, archive.audit.distinct_keys) == (4, 1, 3)
    assert archive.audit.multi_fixture_player_events == 1
    assert archive.audit.reconciled_rows == 3
    assert archive.audit.players_without_history == ()
    assert [row.fixture for row in archive.histories[817].history] == [257, 310]
    assert archive.histories[658].history[0].minutes == 0
    assert archive.provenance["commit"] == ARCHIVE_COMMIT
    assert archive.provenance["kind"] == "sample"
    assert len(archive.provenance["sha256"]) == 4


def test_sample_cannot_claim_full_pin():
    with pytest.raises(ValueError, match="checksum mismatch"):
        load_pinned_archive(SAMPLE)
    with pytest.raises(ValueError, match="checksum mismatch"):
        validate_archive_files(sample_files(), source_kind="pinned")


@pytest.mark.parametrize("name,mutate,message", [
    ("players_raw.csv", lambda rows: rows[0].update(team_code="999"), "external-code join"),
    ("players_raw.csv", lambda rows: rows[1].update(code=rows[0]["code"]), "one-to-one"),
    ("players_raw.csv", lambda rows: rows[1].update(id=rows[0]["id"]), "duplicate player id"),
    ("merged_gw.csv", lambda rows: rows[1].update(element="9999"), "unknown season identity"),
    ("merged_gw.csv", lambda rows: rows[1].update(position="MID"), "position evidence"),
    ("merged_gw.csv", lambda rows: rows[1].update(round="27"), "gameweek conflicts"),
    ("merged_gw.csv", lambda rows: rows[1].update(opponent_team="999"), "sides conflict"),
    ("merged_gw.csv", lambda rows: rows[1].update(kickoff_time="2025-01-01T12:00:00Z"), "kickoff conflicts"),
    ("merged_gw.csv", lambda rows: rows[1].update(total_points="99"), "total_points mismatch"),
    ("merged_gw.csv", lambda rows: rows[1].pop("tackles"), "tackles"),
    ("merged_gw.csv", lambda rows: rows[1].update(tackles="None"), "tackles"),
    ("merged_gw.csv", lambda rows: rows[1].update(was_home="perhaps"), "CSV boolean"),
])
def test_invalid_identity_assignments_or_scoring_fail_before_publication(name, mutate, message):
    files = sample_files()
    mutate_csv(files, name, mutate)
    with pytest.raises((ValueError, ValidationError), match=message):
        validate_archive_files(files)


def test_optional_gaps_do_not_disqualify_complete_scoring():
    files = sample_files()
    def remove_optional(rows):
        for row in rows:
            for name in ("expected_goals", "ict_index", "starts"):
                row[name] = ""
    mutate_csv(files, "merged_gw.csv", remove_optional)
    archive = validate_archive_files(files)
    assert archive.audit.reconciled_rows == 3
    assert archive.histories[817].history[0].expected_goals is None


async def test_archive_import_keeps_all_stored_gameweek_deadlines_null(archive_db):
    archive = await import_sample(archive_db)
    async with archive_db[0]() as session:
        gameweeks = (await session.scalars(select(Gameweek).where(
            Gameweek.season_id == ARCHIVE_SEASON,
        ).order_by(Gameweek.id))).all()

    assert [gameweek.id for gameweek in gameweeks] == [
        event.id for event in archive.catalog.bootstrap.events
    ]
    assert gameweeks
    assert all(gameweek.deadline_time is None for gameweek in gameweeks)


async def test_publication_roundtrip_doubles_seasons_provenance_and_replay(archive_db):
    factory, settings = archive_db
    archive = validate_archive_files(sample_files())
    first = await publish_sample(factory, archive, publication(), settings=settings)
    replay = await publish_sample(factory, archive, publication().model_copy(update={
        "source_observed_at": datetime(2026, 10, 7, tzinfo=timezone.utc),
        "published_at": datetime(2026, 10, 7, tzinfo=timezone.utc),
    }), settings=settings)
    assert first == (658, 817)
    assert replay == ()
    async with factory() as session:
        stored = await read_player_history(session, ARCHIVE_SEASON, 817)
        totals = await read_player_event_totals(session, ARCHIVE_SEASON, 817)
        fixture = await session.get(Fixture, (ARCHIVE_SEASON, 257))
        assert stored.checkpoint.source_provenance == archive.provenance
        assert stored.checkpoint.last_success_at == publication().published_at
        assert stored.history_publication_version == 2
        assert len(stored.rows) == 2
        assert len(totals) == 1 and totals[0].recorded_total_points == 4 and totals[0].reconciled
        assert fixture.gameweek_data_checked_at is None
        assert await session.scalar(select(func.count()).select_from(PlayerFixtureHistory).where(
            PlayerFixtureHistory.season_id == ARCHIVE_SEASON)) == 3
    await current_catalog(archive_db, archive)
    inputs = await read_sample(factory, "2026-27", 42)
    assert inputs.personal_player_id == 817 and inputs.personal_reason is None
    assert len(inputs.personal_rows) == 2 and len(inputs.position_rows) == 3
    assert any(row.scoring_counts["minutes"] == 0 for row in inputs.position_rows)
    assert all("source_value" not in row.scoring_counts and "xP" not in row.scoring_counts
               and "selected" not in row.scoring_counts for row in inputs.position_rows)
    assert inputs.provenance == archive.provenance


async def test_failure_rolls_back_player_then_resumes_committed_work(archive_db, monkeypatch):
    factory, settings = archive_db
    archive = validate_archive_files(sample_files())
    writer = service._write_player_history
    async def fail_second(session, envelope, context):
        await writer(session, envelope, context)
        if context.player_id == 817:
            raise RuntimeError("injected failure after rows/checkpoint")
    monkeypatch.setattr(service, "_write_player_history", fail_second)
    with pytest.raises(RuntimeError, match="injected failure"):
        await publish_sample(factory, archive, publication(), settings=settings)
    async with factory() as session:
        assert await read_player_history(session, ARCHIVE_SEASON, 658) is not None
        assert await read_player_history(session, ARCHIVE_SEASON, 817) is None
        assert (await session.get(CacheMetadata, (ARCHIVE_SEASON, "history"))).publication_version == 1
    monkeypatch.setattr(service, "_write_player_history", writer)
    assert await publish_sample(factory, archive, publication(), settings=settings) == (817,)


async def test_lock_contention_has_no_catalog_writes(archive_db):
    factory, settings = archive_db
    async with ingestion_lock(settings):
        assert await publish_sample(factory, validate_archive_files(sample_files()),
                                                  publication(), settings=settings) is None
    async with factory() as session:
        assert await session.get(Season, ARCHIVE_SEASON) is None


async def test_different_provenance_cannot_overwrite_immutable_archive(archive_db):
    archive = await import_sample(archive_db)
    files = sample_files()
    mutate_csv(files, "merged_gw.csv", lambda rows: rows[1].update(xP="123"))
    with pytest.raises(ValueError, match="different source provenance"):
        await publish_sample(archive_db[0], validate_archive_files(files),
                                          publication(), settings=archive_db[1])
    async with archive_db[0]() as session:
        assert (await session.get(CacheMetadata, (ARCHIVE_SEASON, "shared"))).source_provenance == archive.provenance


@pytest.mark.parametrize("mutation,reason", [
    ({"has_temporary_code": True}, "temporary_or_invalid_current_code"),
    ({"external_code": 123456789}, "external_code_match_not_one_to_one"),
    ({"position": 3}, "position_changed"),
])
async def test_personal_prior_requires_code_identity_and_matching_position(archive_db, mutation, reason):
    archive = await import_sample(archive_db)
    await current_catalog(archive_db, archive)
    async with archive_db[0].begin() as session:
        await session.execute(update(Player).where(Player.season_id == "2026-27").values(**mutation))
    inputs = await read_sample(archive_db[0], "2026-27", 42)
    assert inputs.personal_rows == () and inputs.personal_reason == reason
    if "position" not in mutation:
        assert len(inputs.position_rows) == 3


async def test_ambiguous_current_code_and_temporary_prior_never_supply_personal_prior(archive_db):
    archive = await import_sample(archive_db)
    await current_catalog(archive_db, archive)
    async with archive_db[0].begin() as session:
        player = await session.get(Player, ("2026-27", 42))
        values = {c.name: getattr(player, c.name) for c in Player.__table__.columns}
        values["id"] = 43
        session.add(Player(**values))
    inputs = await read_sample(archive_db[0], "2026-27", 42)
    assert inputs.personal_reason == "external_code_match_not_one_to_one"
    async with archive_db[0].begin() as session:
        await session.execute(delete(Player).where(Player.season_id == "2026-27", Player.id == 43))
        await session.execute(update(Player).where(Player.season_id == ARCHIVE_SEASON,
                                                  Player.id == 817).values(has_temporary_code=True))
    inputs = await read_sample(archive_db[0], "2026-27", 42)
    assert inputs.personal_rows == () and inputs.personal_reason == "temporary_or_invalid_prior_code"


@pytest.mark.parametrize("failure,reason", [
    ("coverage", "prior_history_coverage_gap"),
    ("scoring", "prior_fixture_or_scoring_incompatible"),
    ("identity", "prior_identity_changed"),
    ("provenance", "prior_provenance_or_rules_mismatch"),
    ("fixture", "prior_fixture_dependencies_changed"),
])
async def test_unusable_players_do_not_leak_into_position_pool(archive_db, failure, reason):
    archive = await import_sample(archive_db)
    await current_catalog(archive_db, archive)
    async with archive_db[0].begin() as session:
        checkpoint = await session.get(CacheMetadata, (ARCHIVE_SEASON, "history/player/817"))
        if failure == "coverage":
            checkpoint.missing_fixture_ids = [999]
        elif failure == "scoring":
            await session.execute(update(PlayerFixtureHistory).where(
                PlayerFixtureHistory.season_id == ARCHIVE_SEASON,
                PlayerFixtureHistory.player_id == 817).values(total_points=99))
        elif failure == "identity":
            checkpoint.identity_dependencies = {}
        elif failure == "fixture":
            await session.execute(update(Fixture).where(
                Fixture.season_id == ARCHIVE_SEASON, Fixture.id == 310,
            ).values(kickoff_time=datetime(2026, 1, 1, tzinfo=timezone.utc)))
        else:
            checkpoint.source_provenance = {}
    inputs = await read_sample(archive_db[0], "2026-27", 42)
    assert inputs.personal_rows == () and inputs.personal_reason == reason
    assert [row.player_id for row in inputs.position_rows] == [658]
    assert inputs.exclusions == ((817, reason),)


async def test_unknown_rules_and_non_adjacent_season_are_explicitly_unavailable(archive_db):
    archive = await import_sample(archive_db)
    await current_catalog(archive_db, archive)
    async with archive_db[0].begin() as session:
        await session.execute(update(Season).where(Season.id == "2026-27").values(rules_version="unknown"))
    inputs = await read_sample(archive_db[0], "2026-27", 42)
    assert inputs.position_rows == () and inputs.personal_reason == "incompatible_prior_rules_or_source"
    assert service._previous_season("2027-28") == "2026-27"
    with pytest.raises(ValueError, match="consecutive"):
        service._previous_season("2026-99")


async def test_catalog_players_without_rows_are_reported_as_gaps_not_zeros(archive_db):
    files = sample_files()
    mutate_csv(files, "merged_gw.csv", lambda rows: rows.__setitem__(slice(None), [
        row for row in rows if row["element"] != "658"
    ]))
    archive = validate_archive_files(files)
    assert archive.audit.players_without_history == (658,)
    await publish_sample(archive_db[0], archive, publication(), settings=archive_db[1])
    await current_catalog(archive_db, archive)
    inputs = await read_sample(archive_db[0], "2026-27", 42)
    assert inputs.exclusions == ((658, "prior_history_unavailable"),)
    assert len(inputs.position_rows) == 2


async def test_shared_write_failure_rolls_back_catalog_and_provenance(archive_db, monkeypatch):
    original = service._write_shared_catalog
    async def fail(session, envelope, context):
        await original(session, envelope, context)
        raise RuntimeError("catalog failure")
    monkeypatch.setattr(service, "_write_shared_catalog", fail)
    with pytest.raises(RuntimeError, match="catalog failure"):
        await publish_sample(archive_db[0], validate_archive_files(sample_files()),
                                          publication(), settings=archive_db[1])
    async with archive_db[0]() as session:
        assert await session.get(Season, ARCHIVE_SEASON) is None
        assert await session.get(CacheMetadata, (ARCHIVE_SEASON, "shared")) is None


async def test_missing_or_non_adjacent_archive_does_not_use_older_data(archive_db):
    archive = await import_sample(archive_db)
    await current_catalog(archive_db, archive)
    await publish_shared_catalog(archive_db[0], archive.catalog, publication().model_copy(update={
        "season_id": "2027-28", "rules_version": "fpl-2026-v1",
    }), settings=archive_db[1])
    inputs = await read_sample(archive_db[0], "2027-28", 817)
    assert inputs.prior_season_id == "2026-27" and inputs.personal_rows == inputs.position_rows == ()
    assert inputs.personal_reason == "prior_archive_unavailable"
    async with archive_db[0].begin() as session:
        await session.execute(delete(Season).where(Season.id == "2027-28"))


async def test_sample_requires_explicit_test_opt_in(archive_db):
    archive = validate_archive_files(sample_files())
    with pytest.raises(ValueError, match="explicit test opt-in"):
        await service.publish_prior_archive(archive_db[0], archive, publication(), settings=archive_db[1])
    await import_sample(archive_db)
    await current_catalog(archive_db, archive)
    inputs = await service.read_prior_inputs(archive_db[0], "2026-27", 42)
    assert inputs.personal_reason == "prior_archive_not_pinned" and inputs.position_rows == ()
