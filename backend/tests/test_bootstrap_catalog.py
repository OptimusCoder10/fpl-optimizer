"""Boundary, migration, round-trip, and replay tests for bootstrap-static."""

from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import func, inspect, select

from fpl_optimizer.db.models import CacheMetadata, Gameweek, Player, Season, Team
from fpl_optimizer.db.session import create_session_factory
from fpl_optimizer.schemas.bootstrap import BootstrapEnvelope, BootstrapPublication
from fpl_optimizer.services.bootstrap_catalog import (
    read_bootstrap_catalog,
    store_bootstrap_catalog,
)


pytestmark = pytest.mark.database
FIXTURE_PATH = Path(__file__).parent / "fixtures" / "bootstrap_static.json"


def load_bootstrap_payload() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def test_saved_bootstrap_response_parses_units_and_nulls() -> None:
    envelope = BootstrapEnvelope.model_validate(load_bootstrap_payload())

    assert envelope.elements[0].now_cost == 61
    assert envelope.elements[0].form == Decimal("6.0")
    assert envelope.elements[0].chance_of_playing_next_round is None
    assert envelope.teams[0].strength is None
    assert envelope.events[0].deadline_time == datetime(
        2026, 8, 21, 17, 30, tzinfo=timezone.utc
    )


def test_nullable_gameweek_deadline_is_not_coerced() -> None:
    payload = load_bootstrap_payload()
    payload["events"][1]["deadline_time"] = None

    envelope = BootstrapEnvelope.model_validate(payload)

    assert envelope.events[1].deadline_time is None


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
def test_boundary_rejects_missing_or_mistyped_required_data(mutation, message) -> None:
    payload = deepcopy(load_bootstrap_payload())
    mutation(payload)

    with pytest.raises(ValidationError, match=message):
        BootstrapEnvelope.model_validate(payload)


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
                for table_name in ("teams", "players", "gameweeks")
            }
        )

    assert set(table_names) == {
        "alembic_version",
        "cache_metadata",
        "gameweeks",
        "players",
        "seasons",
        "teams",
    }
    assert primary_keys == {
        "teams": ["season_id", "id"],
        "players": ["season_id", "id"],
        "gameweeks": ["season_id", "id"],
    }


async def test_bootstrap_round_trip_replay_and_changed_value_upsert(
    migrated_test_engine,
) -> None:
    envelope = BootstrapEnvelope.model_validate(load_bootstrap_payload())
    publication = BootstrapPublication(
        season_id="2026-27",
        rules_version="fpl-2026-v1",
        source_observed_at=datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc),
        published_at=datetime(2026, 10, 6, 8, 1, tzinfo=timezone.utc),
    )
    session_factory = create_session_factory(migrated_test_engine)

    async with session_factory() as session:
        first_version = await store_bootstrap_catalog(session, envelope, publication)
        await session.commit()
        stored = await read_bootstrap_catalog(session, publication.season_id)

        assert stored is not None
        assert stored.season.rules_version == "fpl-2026-v1"
        assert [team.name for team in stored.teams] == ["Arsenal"]
        assert stored.teams[0].strength is None
        assert [player.web_name for player in stored.players] == ["Raya", "J.Timber"]
        assert stored.players[0].now_cost == 61
        assert stored.players[0].form == Decimal("6.000")
        assert stored.players[0].chance_of_playing_next_round is None
        assert stored.gameweeks[0].deadline_time == datetime(
            2026, 8, 21, 17, 30, tzinfo=timezone.utc
        )
        assert stored.metadata.source_observed_at == publication.source_observed_at

        second_version = await store_bootstrap_catalog(session, envelope, publication)
        await session.commit()

        changed_payload = load_bootstrap_payload()
        changed_payload["elements"][0]["now_cost"] = 62
        changed_envelope = BootstrapEnvelope.model_validate(changed_payload)
        changed_publication = BootstrapPublication(
            season_id="2026-27",
            rules_version="fpl-2026-v1",
            source_observed_at=datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc),
            published_at=datetime(2026, 10, 6, 9, 1, tzinfo=timezone.utc),
        )
        changed_version = await store_bootstrap_catalog(
            session, changed_envelope, changed_publication
        )
        await session.commit()

    async with session_factory() as session:
        updated = await read_bootstrap_catalog(session, publication.season_id)
        counts = {
            model.__tablename__: await session.scalar(
                select(func.count()).select_from(model)
            )
            for model in (Season, Team, Player, Gameweek, CacheMetadata)
        }

    assert first_version == second_version == 1
    assert changed_version == 2
    assert updated is not None
    assert updated.players[0].now_cost == 62
    assert updated.players[0].publication_version == 2
    assert counts == {
        "seasons": 1,
        "teams": 1,
        "players": 2,
        "gameweeks": 2,
        "cache_metadata": 1,
    }
