"""Single-player element-summary publication and reconciliation tests."""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import func, inspect, select

from fpl_optimizer.db.models import CacheMetadata, PlayerFixtureHistory
from fpl_optimizer.db.session import create_session_factory
from fpl_optimizer.schemas.bootstrap import SharedPublication
from fpl_optimizer.schemas.element_summary import (
    ElementSummaryEnvelope,
    PlayerHistoryPublication,
)
from fpl_optimizer.schemas.fixtures import SharedCatalogEnvelope
from fpl_optimizer.services import player_history as player_history_service
from fpl_optimizer.services.player_history import (
    HISTORY_METADATA_KEY,
    player_history_metadata_key,
    publish_player_history,
    read_player_event_totals,
    read_player_history,
)
from fpl_optimizer.services.shared_catalog import publish_shared_catalog


pytestmark = pytest.mark.database
FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures"


def load_json_fixture(name: str):
    return json.loads((FIXTURE_DIRECTORY / name).read_text(encoding="utf-8"))


def make_synthetic_double_fixture(source_fixture: dict) -> dict:
    # Synthetic test data only: this second fixture is not real FPL data.
    second_fixture = deepcopy(source_fixture)
    second_fixture.update(
        {
            "id": 999,
            "code": 9999999,
            "event": 1,
            "kickoff_time": "2026-08-23T15:00:00Z",
            "team_h": 1,
            "team_a": 5,
            "team_h_score": 2,
            "team_a_score": 0,
        }
    )
    return second_fixture


def make_synthetic_double_history_row(source_row: dict) -> dict:
    # Synthetic test data only: this second history row is not real FPL data.
    second_row = deepcopy(source_row)
    second_row.update(
        {
            "fixture": 999,
            "opponent_team": 5,
            "total_points": 6,
            "kickoff_time": "2026-08-23T15:00:00Z",
            "minutes": 90,
            "clean_sheets": 1,
            "starts": 1,
        }
    )
    return second_row


def shared_envelope(*, double_gameweek: bool = False) -> SharedCatalogEnvelope:
    fixtures = load_json_fixture("fixtures.json")
    if double_gameweek:
        fixtures.append(make_synthetic_double_fixture(fixtures[0]))
    return SharedCatalogEnvelope.model_validate(
        {
            "bootstrap": load_json_fixture("bootstrap_static.json"),
            "fixtures": fixtures,
        }
    )


def history_envelope(*, double_gameweek: bool = False) -> ElementSummaryEnvelope:
    payload = load_json_fixture("element_summary_5.json")
    if double_gameweek:
        payload["history"].append(
            make_synthetic_double_history_row(payload["history"][0])
        )
    return ElementSummaryEnvelope.model_validate(payload)


def shared_publication(season_id: str) -> SharedPublication:
    return SharedPublication(
        season_id=season_id,
        rules_version="fpl-2026-v1",
        source_observed_at=datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc),
        published_at=datetime(2026, 10, 6, 8, 1, tzinfo=timezone.utc),
    )


def history_publication(
    season_id: str,
    *,
    observed_hour: int = 11,
) -> PlayerHistoryPublication:
    return PlayerHistoryPublication(
        season_id=season_id,
        player_id=5,
        completed_sweep_id="daily-2026-10-06",
        source_observed_at=datetime(
            2026, 10, 6, observed_hour, 0, tzinfo=timezone.utc
        ),
        published_at=datetime(
            2026, 10, 6, observed_hour, 1, tzinfo=timezone.utc
        ),
    )


async def publish_catalog(
    migrated_test_engine,
    publication_settings,
    season_id: str,
    *,
    double_gameweek: bool = False,
):
    session_factory = create_session_factory(migrated_test_engine)
    await publish_shared_catalog(
        session_factory,
        shared_envelope(double_gameweek=double_gameweek),
        shared_publication(season_id),
        settings=publication_settings,
    )
    return session_factory


def test_saved_element_summary_parses_numeric_strings_and_zero_minutes() -> None:
    envelope = history_envelope()
    row = envelope.history[0]

    assert row.element == 5
    assert row.minutes == 0
    assert row.influence == Decimal("0.0")
    assert row.expected_goals == Decimal("0.00")
    assert row.kickoff_time == datetime(
        2026, 8, 21, 19, 0, tzinfo=timezone.utc
    )


def test_nullable_optional_values_remain_none() -> None:
    payload = load_json_fixture("element_summary_5.json")
    row = payload["history"][0]
    row["influence"] = None
    row["expected_goals"] = None
    row["starts"] = None

    parsed = ElementSummaryEnvelope.model_validate(payload).history[0]

    assert parsed.influence is None
    assert parsed.expected_goals is None
    assert parsed.starts is None


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda row: row.pop("minutes"), "minutes"),
        (lambda row: row.pop("expected_goals"), "expected_goals"),
        (
            lambda row: row.__setitem__("influence", 0.0),
            "numeric value must be a string or null",
        ),
        (
            lambda row: row.__setitem__("influence", "NaN"),
            "numeric string must be finite",
        ),
        (lambda row: row.__setitem__("starts", 2), "less than or equal to 1"),
    ],
)
def test_history_boundary_rejects_missing_or_mistyped_fields(
    mutation, message
) -> None:
    payload = load_json_fixture("element_summary_5.json")
    mutation(payload["history"][0])

    with pytest.raises(ValidationError, match=message):
        ElementSummaryEnvelope.model_validate(payload)


def test_history_boundary_rejects_duplicate_fixture_rows() -> None:
    payload = load_json_fixture("element_summary_5.json")
    payload["history"].append(deepcopy(payload["history"][0]))

    with pytest.raises(ValidationError, match="duplicate fixture id"):
        ElementSummaryEnvelope.model_validate(payload)


async def test_history_migration_columns_apply_from_empty(
    migrated_test_engine,
) -> None:
    async with migrated_test_engine.connect() as connection:
        history_columns = await connection.run_sync(
            lambda sync_connection: {
                column["name"]: column["nullable"]
                for column in inspect(sync_connection).get_columns(
                    "player_fixture_history"
                )
            }
        )
        metadata_columns = await connection.run_sync(
            lambda sync_connection: {
                column["name"]: column["nullable"]
                for column in inspect(sync_connection).get_columns("cache_metadata")
            }
        )

    assert history_columns["minutes"] is False
    assert history_columns["expected_goals"] is True
    assert history_columns["starts"] is True
    assert metadata_columns["completed_sweep_id"] is True
    assert metadata_columns["covered_finalized_fixture_ids"] is True
    assert metadata_columns["last_success_at"] is True


async def test_player_history_round_trip_nulls_and_idempotent_replay(
    migrated_test_engine,
    publication_settings,
) -> None:
    season_id = "2040-41"
    session_factory = await publish_catalog(
        migrated_test_engine, publication_settings, season_id
    )
    payload = load_json_fixture("element_summary_5.json")
    payload["history"][0]["expected_goals"] = None
    payload["history"][0]["starts"] = None
    envelope = ElementSummaryEnvelope.model_validate(payload)
    publication = history_publication(season_id)

    first_version = await publish_player_history(
        session_factory,
        envelope,
        publication,
        settings=publication_settings,
    )
    second_version = await publish_player_history(
        session_factory,
        envelope,
        publication,
        settings=publication_settings,
    )

    async with session_factory() as session:
        stored = await read_player_history(session, season_id, 5)
        totals = await read_player_event_totals(session, season_id, 5)
        row_count = await session.scalar(
            select(func.count())
            .select_from(PlayerFixtureHistory)
            .where(PlayerFixtureHistory.season_id == season_id)
        )
        metadata_count = await session.scalar(
            select(func.count())
            .select_from(CacheMetadata)
            .where(CacheMetadata.season_id == season_id)
        )

    assert first_version == second_version == 1
    assert row_count == 1
    assert metadata_count == 3
    assert stored is not None
    assert len(stored.rows) == 1
    assert stored.rows[0].minutes == 0
    assert stored.rows[0].expected_goals is None
    assert stored.rows[0].starts is None
    assert stored.rows[0].publication_version == 1
    assert stored.history_publication_version == 1
    assert stored.checkpoint.key == player_history_metadata_key(5)
    assert stored.checkpoint.completed_sweep_id == "daily-2026-10-06"
    assert stored.checkpoint.rules_version == "fpl-2026-v1"
    assert stored.checkpoint.source_shared_publication_version == 1
    assert stored.checkpoint.identity_dependencies == {
        "player_id": 5,
        "external_code": 445122,
        "position": 2,
    }
    assert stored.checkpoint.covered_fixture_ids == [1]
    assert stored.checkpoint.covered_finalized_fixture_ids == [1]
    assert stored.checkpoint.missing_fixture_ids == []
    assert stored.checkpoint.invalid_fixture_ids == []
    assert totals == (
        player_history_service.PlayerEventTotal(
            gameweek_id=1,
            fixture_ids=(1,),
            recorded_total_points=0,
            reconstructed_total_points=0,
            mismatched_fixture_ids=(),
        ),
    )


async def test_double_gameweek_rows_are_summed_and_reconciled_by_event(
    migrated_test_engine,
    publication_settings,
) -> None:
    season_id = "2041-42"
    session_factory = await publish_catalog(
        migrated_test_engine,
        publication_settings,
        season_id,
        double_gameweek=True,
    )

    version = await publish_player_history(
        session_factory,
        history_envelope(double_gameweek=True),
        history_publication(season_id),
        settings=publication_settings,
    )
    async with session_factory() as session:
        stored = await read_player_history(session, season_id, 5)
        totals = await read_player_event_totals(session, season_id, 5)

    assert version == 1
    assert stored is not None
    assert [row.fixture_id for row in stored.rows] == [1, 999]
    assert [row.minutes for row in stored.rows] == [0, 90]
    assert stored.checkpoint.covered_fixture_ids == [1, 999]
    assert len(totals) == 1
    assert totals[0].gameweek_id == 1
    assert totals[0].fixture_ids == (1, 999)
    assert totals[0].recorded_total_points == 6
    assert totals[0].reconstructed_total_points == 6
    assert totals[0].reconciled is True


@pytest.mark.parametrize(
    ("mutate_envelope", "mutate_publication", "message"),
    [
        (
            lambda payload: payload["history"][0].__setitem__("element", 1),
            lambda publication: publication,
            "does not belong to the requested player",
        ),
        (
            lambda payload: payload["history"][0].__setitem__("fixture", 12345),
            lambda publication: publication,
            "fixtures absent from the shared catalog",
        ),
        (
            lambda payload: payload["history"][0].__setitem__("round", 38),
            lambda publication: publication,
            "conflicts with its catalog gameweek",
        ),
        (
            lambda payload: payload["history"][0].__setitem__(
                "opponent_team", 5
            ),
            lambda publication: publication,
            "conflicts with its catalog sides",
        ),
        (
            lambda payload: payload["history"][0].__setitem__(
                "total_points", 99
            ),
            lambda publication: publication,
            "scoring components reconstruct 0",
        ),
        (
            lambda payload: None,
            lambda publication: publication.model_copy(
                update={"player_id": 999}
            ),
            "player is absent from the shared catalog",
        ),
        (
            lambda payload: None,
            lambda publication: publication.model_copy(
                update={"season_id": "2098-99"}
            ),
            "season is absent from the shared catalog",
        ),
    ],
)
async def test_catalog_identity_validation_happens_before_commit(
    migrated_test_engine,
    publication_settings,
    mutate_envelope,
    mutate_publication,
    message,
) -> None:
    season_id = "2042-43"
    session_factory = await publish_catalog(
        migrated_test_engine, publication_settings, season_id
    )
    payload = load_json_fixture("element_summary_5.json")
    mutate_envelope(payload)
    envelope = ElementSummaryEnvelope.model_validate(payload)
    publication = mutate_publication(history_publication(season_id))

    with pytest.raises(ValueError, match=message):
        await publish_player_history(
            session_factory,
            envelope,
            publication,
            settings=publication_settings,
        )

    async with session_factory() as session:
        history_count = await session.scalar(
            select(func.count())
            .select_from(PlayerFixtureHistory)
            .where(PlayerFixtureHistory.season_id == season_id)
        )
        history_metadata = await session.get(
            CacheMetadata, (season_id, HISTORY_METADATA_KEY)
        )

    assert history_count == 0
    assert history_metadata is None


async def test_invalid_or_failed_republication_preserves_prior_player_state(
    migrated_test_engine,
    publication_settings,
    monkeypatch,
) -> None:
    season_id = "2043-44"
    session_factory = await publish_catalog(
        migrated_test_engine,
        publication_settings,
        season_id,
        double_gameweek=True,
    )
    baseline = history_envelope(double_gameweek=True)
    baseline_publication = history_publication(season_id)
    await publish_player_history(
        session_factory,
        baseline,
        baseline_publication,
        settings=publication_settings,
    )

    other_payload = load_json_fixture("element_summary_5.json")
    other_payload["history"][0]["element"] = 1
    other_payload["history"][0]["value"] = 60
    other_envelope = ElementSummaryEnvelope.model_validate(other_payload)
    other_publication = history_publication(
        season_id, observed_hour=12
    ).model_copy(
        update={"player_id": 1, "completed_sweep_id": "daily-other-player"}
    )
    await publish_player_history(
        session_factory,
        other_envelope,
        other_publication,
        settings=publication_settings,
    )

    incomplete_payload = load_json_fixture("element_summary_5.json")
    with pytest.raises(ValueError, match="dropped previously published fixtures"):
        await publish_player_history(
            session_factory,
            ElementSummaryEnvelope.model_validate(incomplete_payload),
            history_publication(season_id, observed_hour=12),
            settings=publication_settings,
        )

    changed = history_envelope(double_gameweek=True)
    changed.history[0].minutes = 90
    changed.history[0].clean_sheets = 1
    changed.history[0].starts = 1
    changed.history[0].total_points = 6
    original_writer = player_history_service._write_history_rows

    async def fail_after_row_write(session, rows) -> None:
        await original_writer(session, rows)
        raise RuntimeError("simulated player history write failure")

    monkeypatch.setattr(
        player_history_service, "_write_history_rows", fail_after_row_write
    )
    with pytest.raises(RuntimeError, match="simulated player history write failure"):
        await publish_player_history(
            session_factory,
            changed,
            history_publication(season_id, observed_hour=13),
            settings=publication_settings,
        )

    async with session_factory() as session:
        preserved = await read_player_history(session, season_id, 5)
        other_preserved = await read_player_history(session, season_id, 1)

    assert preserved is not None
    assert [row.fixture_id for row in preserved.rows] == [1, 999]
    assert [row.minutes for row in preserved.rows] == [0, 90]
    assert [row.total_points for row in preserved.rows] == [0, 6]
    assert {row.publication_version for row in preserved.rows} == {1}
    assert preserved.history_publication_version == 2
    assert preserved.checkpoint.publication_version == 1
    assert preserved.checkpoint.last_success_at == baseline_publication.published_at
    assert (
        preserved.checkpoint.source_observed_at
        == baseline_publication.source_observed_at
    )
    assert other_preserved is not None
    assert len(other_preserved.rows) == 1
    assert other_preserved.rows[0].fixture_id == 1
    assert other_preserved.rows[0].publication_version == 2
    assert other_preserved.checkpoint.publication_version == 2
    assert other_preserved.checkpoint.last_success_at == other_publication.published_at
