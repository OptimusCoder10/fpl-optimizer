"""Boundary, migration, atomic publication, and read-back tests."""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import func, inspect, select

from fpl_optimizer.db.models import (
    CacheMetadata,
    Fixture,
    Gameweek,
    Player,
    Season,
    Team,
)
from fpl_optimizer.db.session import create_session_factory
from fpl_optimizer.schemas.bootstrap import BootstrapEnvelope, SharedPublication
from fpl_optimizer.schemas.fixtures import FixturesEnvelope, SharedCatalogEnvelope
from fpl_optimizer.services import shared_catalog as shared_catalog_service
from fpl_optimizer.services.shared_catalog import (
    publish_shared_catalog,
    read_shared_catalog,
    read_target_gameweek_context,
)


pytestmark = pytest.mark.database
FIXTURE_DIRECTORY = Path(__file__).parent / "fixtures"


def load_json_fixture(name: str):
    return json.loads((FIXTURE_DIRECTORY / name).read_text(encoding="utf-8"))


def load_shared_envelope() -> SharedCatalogEnvelope:
    return SharedCatalogEnvelope.model_validate(
        {
            "bootstrap": load_json_fixture("bootstrap_static.json"),
            "fixtures": load_json_fixture("fixtures.json"),
        }
    )


def publication(
    season_id: str = "2026-27",
    *,
    observed_hour: int = 8,
) -> SharedPublication:
    return SharedPublication(
        season_id=season_id,
        rules_version="fpl-2026-v1",
        source_observed_at=datetime(
            2026, 10, 6, observed_hour, 0, tzinfo=timezone.utc
        ),
        published_at=datetime(
            2026, 10, 6, observed_hour, 1, tzinfo=timezone.utc
        ),
    )


def test_saved_responses_parse_units_nulls_and_per_side_fdr() -> None:
    envelope = load_shared_envelope()

    assert envelope.bootstrap.elements[0].now_cost == 61
    assert envelope.bootstrap.elements[0].form == Decimal("6.0")
    assert envelope.bootstrap.elements[0].chance_of_playing_next_round is None
    assert envelope.bootstrap.teams[0].strength is None
    assert envelope.bootstrap.events[0].deadline_time == datetime(
        2026, 8, 21, 17, 30, tzinfo=timezone.utc
    )
    assert envelope.fixtures.root[1].team_h_difficulty == 3
    assert envelope.fixtures.root[1].team_a_difficulty == 5
    assert envelope.fixtures.root[1].team_h_score is None


def test_nullable_event_kickoff_and_gameweek_deadline_are_not_coerced() -> None:
    bootstrap_payload = load_json_fixture("bootstrap_static.json")
    bootstrap_payload["events"][1]["deadline_time"] = None
    fixtures_payload = load_json_fixture("fixtures.json")
    fixtures_payload[1]["event"] = None
    fixtures_payload[1]["kickoff_time"] = None

    bootstrap = BootstrapEnvelope.model_validate(bootstrap_payload)
    fixtures = FixturesEnvelope.model_validate(fixtures_payload)

    assert bootstrap.events[1].deadline_time is None
    assert fixtures.root[1].event is None
    assert fixtures.root[1].kickoff_time is None


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload["elements"][0].pop("can_select"), "can_select"),
        (
            lambda payload: payload["elements"][0].pop(
                "chance_of_playing_next_round"
            ),
            "chance_of_playing_next_round",
        ),
        (
            lambda payload: payload["elements"][0].__setitem__("form", 6.0),
            "numeric value must be a string or null",
        ),
        (
            lambda payload: payload["elements"][0].__setitem__("team", 999),
            "players reference teams missing",
        ),
    ],
)
def test_bootstrap_boundary_rejects_missing_or_mistyped_required_data(
    mutation, message
) -> None:
    payload = deepcopy(load_json_fixture("bootstrap_static.json"))
    mutation(payload)

    with pytest.raises(ValidationError, match=message):
        BootstrapEnvelope.model_validate(payload)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload[0].pop("team_h_difficulty"), "team_h_difficulty"),
        (
            lambda payload: payload[0].__setitem__("kickoff_time", 123),
            "timestamp must be a string or null",
        ),
        (
            lambda payload: payload[0].__setitem__(
                "kickoff_time", "2026-08-21T19:00:00"
            ),
            "timestamp must include a UTC offset",
        ),
        (lambda payload: payload[0].pop("event"), "event"),
        (
            lambda payload: payload[0].__setitem__("team_a_difficulty", 6),
            "less than or equal to 5",
        ),
        (
            lambda payload: payload[1].__setitem__("event", 99),
            "fixtures reference gameweeks missing",
        ),
        (
            lambda payload: payload[1].__setitem__("team_a", 99),
            "fixtures reference teams missing",
        ),
    ],
)
def test_fixture_boundary_rejects_invalid_or_unrelated_data(mutation, message) -> None:
    bootstrap_payload = load_json_fixture("bootstrap_static.json")
    fixtures_payload = deepcopy(load_json_fixture("fixtures.json"))
    mutation(fixtures_payload)

    with pytest.raises(ValidationError, match=message):
        SharedCatalogEnvelope.model_validate(
            {"bootstrap": bootstrap_payload, "fixtures": fixtures_payload}
        )


def test_shared_boundary_rejects_empty_or_incomplete_fixture_collections() -> None:
    bootstrap_payload = load_json_fixture("bootstrap_static.json")

    with pytest.raises(ValidationError, match="fixtures response must not be empty"):
        SharedCatalogEnvelope.model_validate(
            {"bootstrap": bootstrap_payload, "fixtures": []}
        )

    with pytest.raises(ValidationError, match="teams have no fixture"):
        SharedCatalogEnvelope.model_validate(
            {
                "bootstrap": bootstrap_payload,
                "fixtures": [load_json_fixture("fixtures.json")[1]],
            }
        )


async def test_migration_applies_from_an_empty_database(
    migrated_test_engine,
) -> None:
    async with migrated_test_engine.connect() as connection:
        table_names = await connection.run_sync(
            lambda sync_connection: inspect(sync_connection).get_table_names()
        )
        primary_keys = await connection.run_sync(
            lambda sync_connection: {
                table_name: inspect(sync_connection)
                .get_pk_constraint(table_name)
                .get("constrained_columns")
            for table_name in (
                "teams",
                "players",
                "gameweeks",
                "fixtures",
                "player_fixture_history",
            )
            }
        )
        fixture_columns = await connection.run_sync(
            lambda sync_connection: {
                column["name"]: column["nullable"]
                for column in inspect(sync_connection).get_columns("fixtures")
            }
        )

    assert set(table_names) == {
        "alembic_version",
        "cache_metadata",
        "fixtures",
        "gameweeks",
        "players",
        "player_fixture_history",
        "seasons",
        "teams",
    }
    assert primary_keys == {
        "teams": ["season_id", "id"],
        "players": ["season_id", "id"],
        "gameweeks": ["season_id", "id"],
        "fixtures": ["season_id", "id"],
        "player_fixture_history": ["season_id", "player_id", "fixture_id"],
    }
    assert fixture_columns["gameweek_id"] is True
    assert fixture_columns["kickoff_time"] is True


async def test_shared_round_trip_target_counts_and_idempotent_replay(
    migrated_test_engine,
    publication_settings,
) -> None:
    envelope = load_shared_envelope()
    first_publication = publication()
    session_factory = create_session_factory(migrated_test_engine)

    first_version = await publish_shared_catalog(
        session_factory, envelope, first_publication, settings=publication_settings
    )
    async with session_factory() as session:
        stored = await read_shared_catalog(session, first_publication.season_id)
        target = await read_target_gameweek_context(
            session,
            first_publication.season_id,
            datetime(2026, 10, 6, tzinfo=timezone.utc),
        )

        assert stored is not None
        assert stored.season.rules_version == "fpl-2026-v1"
        assert [team.name for team in stored.teams] == [
            "Arsenal",
            "Brighton",
            "Coventry City",
        ]
        assert stored.teams[0].strength is None
        assert [player.web_name for player in stored.players] == ["Raya", "J.Timber"]
        assert stored.players[0].now_cost == 61
        assert stored.players[0].form == Decimal("6.000")
        assert stored.players[0].chance_of_playing_next_round is None
        assert stored.fixtures[0].gameweek_data_checked_at == datetime(
            2026, 10, 6, 8, 0, tzinfo=timezone.utc
        )
        assert stored.fixtures[1].home_team_difficulty == 3
        assert stored.fixtures[1].away_team_difficulty == 5
        assert stored.metadata.source_observed_at == first_publication.source_observed_at

        assert target is not None
        assert target.gameweek_id == 38
        assert target.deadline_time == datetime(
            2027, 5, 30, 13, 30, tzinfo=timezone.utc
        )
        assert target.fixture_counts_by_team == {1: 1, 5: 1, 7: 0}

    second_version = await publish_shared_catalog(
        session_factory, envelope, first_publication, settings=publication_settings
    )

    changed_payload = load_json_fixture("bootstrap_static.json")
    changed_payload["elements"][0]["now_cost"] = 62
    changed_envelope = SharedCatalogEnvelope.model_validate(
        {
            "bootstrap": changed_payload,
            "fixtures": load_json_fixture("fixtures.json"),
        }
    )
    changed_version = await publish_shared_catalog(
        session_factory, changed_envelope, publication(observed_hour=9),
        settings=publication_settings,
    )

    async with session_factory() as session:
        updated = await read_shared_catalog(session, first_publication.season_id)
        counts = {
            model.__tablename__: await session.scalar(
                select(func.count())
                .select_from(model)
                .where(
                    (Season.id if model is Season else model.season_id)
                    == first_publication.season_id
                )
            )
            for model in (Season, Team, Player, Gameweek, Fixture, CacheMetadata)
        }

    assert first_version == second_version == 1
    assert changed_version == 2
    assert updated is not None
    assert updated.players[0].now_cost == 62
    assert updated.players[0].publication_version == 2
    assert updated.fixtures[0].gameweek_data_checked_at == datetime(
        2026, 10, 6, 8, 0, tzinfo=timezone.utc
    )
    assert counts == {
        "seasons": 1,
        "teams": 3,
        "players": 2,
        "gameweeks": 2,
        "fixtures": 2,
        "cache_metadata": 1,
    }


async def test_validation_and_write_failure_preserve_previous_publication(
    migrated_test_engine,
    publication_settings,
    monkeypatch,
) -> None:
    season_id = "2027-28"
    baseline_envelope = load_shared_envelope()
    session_factory = create_session_factory(migrated_test_engine)

    await publish_shared_catalog(
        session_factory, baseline_envelope, publication(season_id=season_id),
        settings=publication_settings,
    )

    invalid_fixtures = load_json_fixture("fixtures.json")
    invalid_fixtures[1]["team_a"] = 999
    with pytest.raises(ValidationError, match="fixtures reference teams missing"):
        SharedCatalogEnvelope.model_validate(
            {
                "bootstrap": load_json_fixture("bootstrap_static.json"),
                "fixtures": invalid_fixtures,
            }
        )

    changed_bootstrap = load_json_fixture("bootstrap_static.json")
    changed_bootstrap["elements"][0]["now_cost"] = 99
    changed_fixtures = load_json_fixture("fixtures.json")
    changed_fixtures[0]["team_h_score"] = 4
    changed_fixtures[0]["team_h_difficulty"] = 5
    changed_envelope = SharedCatalogEnvelope.model_validate(
        {
            "bootstrap": changed_bootstrap,
            "fixtures": changed_fixtures,
        }
    )

    original_fixture_writer = shared_catalog_service._write_fixture_rows

    async def fail_after_first_fixture_write(session, rows) -> None:
        await original_fixture_writer(session, rows[:1])
        raise RuntimeError("simulated fixture write failure")

    monkeypatch.setattr(
        shared_catalog_service,
        "_write_fixture_rows",
        fail_after_first_fixture_write,
    )
    with pytest.raises(RuntimeError, match="simulated fixture write failure"):
        await publish_shared_catalog(
            session_factory,
            changed_envelope,
            publication(season_id=season_id, observed_hour=9),
            settings=publication_settings,
        )

    async with session_factory() as session:
        preserved = await read_shared_catalog(session, season_id)

    assert preserved is not None
    assert [team.name for team in preserved.teams] == [
        "Arsenal",
        "Brighton",
        "Coventry City",
    ]
    assert preserved.players[0].now_cost == 61
    assert preserved.players[0].publication_version == 1
    assert [fixture.id for fixture in preserved.fixtures] == [1, 371]
    assert preserved.fixtures[0].home_team_score == 3
    assert preserved.fixtures[0].home_team_difficulty == 2
    assert preserved.fixtures[1].home_team_score is None
    assert preserved.fixtures[1].home_team_difficulty == 3
    assert preserved.metadata.publication_version == 1
    assert preserved.metadata.source_observed_at == datetime(
        2026, 10, 6, 8, 0, tzinfo=timezone.utc
    )


async def test_target_counts_preserve_a_double_gameweek(
    migrated_test_engine,
    publication_settings,
) -> None:
    fixture_payload = load_json_fixture("fixtures.json")
    second_target_fixture = deepcopy(fixture_payload[0])
    second_target_fixture.update(
        {
            "id": 999,
            "code": 9999999,
            "event": 38,
            "kickoff_time": "2027-05-27T19:00:00Z",
            "started": False,
            "finished": False,
            "finished_provisional": False,
            "team_h_score": None,
            "team_a_score": None,
        }
    )
    fixture_payload.append(second_target_fixture)
    envelope = SharedCatalogEnvelope.model_validate(
        {
            "bootstrap": load_json_fixture("bootstrap_static.json"),
            "fixtures": fixture_payload,
        }
    )
    season_id = "2028-29"
    session_factory = create_session_factory(migrated_test_engine)

    await publish_shared_catalog(
        session_factory, envelope, publication(season_id=season_id),
        settings=publication_settings,
    )
    async with session_factory() as session:
        target = await read_target_gameweek_context(
            session,
            season_id,
            datetime(2026, 10, 6, tzinfo=timezone.utc),
        )

    assert target is not None
    assert target.fixture_counts_by_team == {1: 2, 5: 1, 7: 1}
