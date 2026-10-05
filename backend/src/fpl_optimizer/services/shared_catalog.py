"""Atomically publish and read validated bootstrap and fixture data."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from fpl_optimizer.db.models import (
    CacheMetadata,
    Fixture,
    Gameweek,
    Player,
    Season,
    Team,
)
from fpl_optimizer.schemas.bootstrap import SharedPublication
from fpl_optimizer.schemas.fixtures import SharedCatalogEnvelope


SHARED_METADATA_KEY = "shared"


@dataclass(frozen=True)
class StoredSharedCatalog:
    """One season's shared catalog in deterministic source-ID order."""

    season: Season
    teams: tuple[Team, ...]
    players: tuple[Player, ...]
    gameweeks: tuple[Gameweek, ...]
    fixtures: tuple[Fixture, ...]
    metadata: CacheMetadata


@dataclass(frozen=True)
class TargetGameweekContext:
    """The next unexpired deadline and complete per-team fixture counts."""

    gameweek_id: int
    deadline_time: datetime
    fixture_counts_by_team: dict[int, int]


def _upsert_statement(model: type, rows: list[dict], update_columns: tuple[str, ...]):
    statement = insert(model).values(rows)
    return statement.on_conflict_do_update(
        index_elements=("season_id", "id"),
        set_={column: getattr(statement.excluded, column) for column in update_columns},
    )


async def _write_fixture_rows(
    session: AsyncSession,
    rows: list[dict],
) -> None:
    """Upsert fixtures while retaining the first finalization observation."""
    if not rows:
        return

    statement = insert(Fixture).values(rows)
    ordinary_columns = tuple(
        key
        for key in rows[0]
        if key not in {"season_id", "id", "gameweek_data_checked_at"}
    )
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=("season_id", "id"),
            set_={
                **{
                    column: getattr(statement.excluded, column)
                    for column in ordinary_columns
                },
                "gameweek_data_checked_at": case(
                    (
                        Fixture.gameweek_id == statement.excluded.gameweek_id,
                        func.coalesce(
                            Fixture.gameweek_data_checked_at,
                            statement.excluded.gameweek_data_checked_at,
                        ),
                    ),
                    else_=statement.excluded.gameweek_data_checked_at,
                ),
            },
        )
    )


async def publish_shared_catalog(
    session: AsyncSession,
    envelope: SharedCatalogEnvelope,
    publication: SharedPublication,
) -> int:
    """Publish bootstrap and fixtures as one replay-safe database transaction."""
    existing_season = await session.get(Season, publication.season_id)
    if (
        existing_season is not None
        and existing_season.rules_version != publication.rules_version
    ):
        raise ValueError("rules_version cannot change for an existing season namespace")

    existing_metadata = await session.get(
        CacheMetadata, (publication.season_id, SHARED_METADATA_KEY)
    )
    is_replay = (
        existing_metadata is not None
        and existing_metadata.source_observed_at == publication.source_observed_at
    )
    publication_version = (
        existing_metadata.publication_version
        if is_replay
        else (existing_metadata.publication_version + 1 if existing_metadata else 1)
    )

    bootstrap = envelope.bootstrap
    async with session.begin_nested():
        await session.execute(
            insert(Season)
            .values(
                id=publication.season_id,
                rules_version=publication.rules_version,
            )
            .on_conflict_do_nothing(index_elements=("id",))
        )

        team_rows = [
            {
                "season_id": publication.season_id,
                "id": team.id,
                "external_code": team.code,
                "name": team.name,
                "short_name": team.short_name,
                "strength": team.strength,
                "strength_overall_home": team.strength_overall_home,
                "strength_overall_away": team.strength_overall_away,
                "strength_attack_home": team.strength_attack_home,
                "strength_attack_away": team.strength_attack_away,
                "strength_defence_home": team.strength_defence_home,
                "strength_defence_away": team.strength_defence_away,
            }
            for team in bootstrap.teams
        ]
        if team_rows:
            await session.execute(
                _upsert_statement(
                    Team,
                    team_rows,
                    tuple(key for key in team_rows[0] if key not in {"season_id", "id"}),
                )
            )

        player_rows = [
            {
                "season_id": publication.season_id,
                "id": player.id,
                "first_name": player.first_name,
                "second_name": player.second_name,
                "web_name": player.web_name,
                "position": player.element_type,
                "team_id": player.team,
                "external_code": player.code,
                "has_temporary_code": player.has_temporary_code,
                "opta_code": player.opta_code,
                "now_cost": player.now_cost,
                "can_select": player.can_select,
                "can_transact": player.can_transact,
                "removed": player.removed,
                "status": player.status,
                "chance_of_playing_next_round": player.chance_of_playing_next_round,
                "form": player.form,
                "ep_next": player.ep_next,
                "selected_by_percent": player.selected_by_percent,
                "ict_index": player.ict_index,
                "expected_goals": player.expected_goals,
                "expected_assists": player.expected_assists,
                "expected_goal_involvements": player.expected_goal_involvements,
                "expected_goals_conceded": player.expected_goals_conceded,
                "source_observed_at": publication.source_observed_at,
                "publication_version": publication_version,
            }
            for player in bootstrap.elements
        ]
        if player_rows:
            await session.execute(
                _upsert_statement(
                    Player,
                    player_rows,
                    tuple(
                        key
                        for key in player_rows[0]
                        if key not in {"season_id", "id"}
                    ),
                )
            )

        gameweek_rows = [
            {
                "season_id": publication.season_id,
                "id": gameweek.id,
                "name": gameweek.name,
                "deadline_time": gameweek.deadline_time,
                "is_current": gameweek.is_current,
                "is_next": gameweek.is_next,
                "finished": gameweek.finished,
                "data_checked": gameweek.data_checked,
            }
            for gameweek in bootstrap.events
        ]
        if gameweek_rows:
            await session.execute(
                _upsert_statement(
                    Gameweek,
                    gameweek_rows,
                    tuple(
                        key
                        for key in gameweek_rows[0]
                        if key not in {"season_id", "id"}
                    ),
                )
            )

        checked_gameweeks = {
            gameweek.id for gameweek in bootstrap.events if gameweek.data_checked
        }
        fixture_rows = [
            {
                "season_id": publication.season_id,
                "id": fixture.id,
                "external_code": fixture.code,
                "gameweek_id": fixture.event,
                "kickoff_time": fixture.kickoff_time,
                "started": fixture.started,
                "finished": fixture.finished,
                "finished_provisional": fixture.finished_provisional,
                "home_team_id": fixture.team_h,
                "away_team_id": fixture.team_a,
                "home_team_score": fixture.team_h_score,
                "away_team_score": fixture.team_a_score,
                "home_team_difficulty": fixture.team_h_difficulty,
                "away_team_difficulty": fixture.team_a_difficulty,
                "gameweek_data_checked_at": (
                    publication.source_observed_at
                    if fixture.event in checked_gameweeks
                    else None
                ),
            }
            for fixture in envelope.fixtures.root
        ]
        await _write_fixture_rows(session, fixture_rows)

        metadata_statement = insert(CacheMetadata).values(
            season_id=publication.season_id,
            key=SHARED_METADATA_KEY,
            publication_version=publication_version,
            last_success_at=publication.published_at,
            source_observed_at=publication.source_observed_at,
        )
        await session.execute(
            metadata_statement.on_conflict_do_update(
                index_elements=("season_id", "key"),
                set_={
                    "publication_version": publication_version,
                    "last_success_at": publication.published_at,
                    "source_observed_at": publication.source_observed_at,
                },
            )
        )

    return publication_version


async def read_shared_catalog(
    session: AsyncSession, season_id: str
) -> StoredSharedCatalog | None:
    """Read one complete shared publication with stable ordering."""
    season = await session.get(Season, season_id)
    metadata = await session.get(CacheMetadata, (season_id, SHARED_METADATA_KEY))
    if season is None or metadata is None:
        return None

    async def ordered_rows(model: type):
        return tuple(
            (
                await session.scalars(
                    select(model)
                    .where(model.season_id == season_id)
                    .order_by(model.id)
                )
            ).all()
        )

    return StoredSharedCatalog(
        season=season,
        teams=await ordered_rows(Team),
        players=await ordered_rows(Player),
        gameweeks=await ordered_rows(Gameweek),
        fixtures=await ordered_rows(Fixture),
        metadata=metadata,
    )


async def read_target_gameweek_context(
    session: AsyncSession,
    season_id: str,
    as_of: datetime,
) -> TargetGameweekContext | None:
    """Read the earliest future deadline and each club's target fixture count."""
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must include a UTC offset")

    metadata = await session.get(CacheMetadata, (season_id, SHARED_METADATA_KEY))
    if metadata is None:
        return None

    target = await session.scalar(
        select(Gameweek)
        .where(
            Gameweek.season_id == season_id,
            Gameweek.deadline_time.is_not(None),
            Gameweek.deadline_time > as_of,
        )
        .order_by(Gameweek.deadline_time, Gameweek.id)
        .limit(1)
    )
    if target is None or target.deadline_time is None:
        return None

    team_ids = tuple(
        (
            await session.scalars(
                select(Team.id).where(Team.season_id == season_id).order_by(Team.id)
            )
        ).all()
    )
    target_fixtures = tuple(
        (
            await session.scalars(
                select(Fixture).where(
                    Fixture.season_id == season_id,
                    Fixture.gameweek_id == target.id,
                )
            )
        ).all()
    )

    counts = {team_id: 0 for team_id in team_ids}
    for fixture in target_fixtures:
        counts[fixture.home_team_id] += 1
        counts[fixture.away_team_id] += 1

    return TargetGameweekContext(
        gameweek_id=target.id,
        deadline_time=target.deadline_time,
        fixture_counts_by_team=counts,
    )
