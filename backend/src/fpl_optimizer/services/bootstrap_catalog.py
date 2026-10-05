"""Store and read the validated bootstrap catalog without making HTTP calls."""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from fpl_optimizer.db.models import CacheMetadata, Gameweek, Player, Season, Team
from fpl_optimizer.schemas.bootstrap import BootstrapEnvelope, BootstrapPublication


SHARED_METADATA_KEY = "shared"


@dataclass(frozen=True)
class StoredBootstrapCatalog:
    """A season's persisted slice 1.1 catalog in deterministic source-ID order."""

    season: Season
    teams: tuple[Team, ...]
    players: tuple[Player, ...]
    gameweeks: tuple[Gameweek, ...]
    metadata: CacheMetadata


def _upsert_statement(model: type, rows: list[dict], update_columns: tuple[str, ...]):
    statement = insert(model).values(rows)
    return statement.on_conflict_do_update(
        index_elements=("season_id", "id"),
        set_={column: getattr(statement.excluded, column) for column in update_columns},
    )


async def store_bootstrap_catalog(
    session: AsyncSession,
    envelope: BootstrapEnvelope,
    publication: BootstrapPublication,
) -> int:
    """Atomically upsert one validated partial shared catalog publication."""
    existing_season = await session.get(Season, publication.season_id)
    if (
        existing_season is not None
        and existing_season.rules_version != publication.rules_version
    ):
        raise ValueError(
            "rules_version cannot change for an existing season namespace"
        )

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
            for team in envelope.teams
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
            for player in envelope.elements
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
            for gameweek in envelope.events
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


async def read_bootstrap_catalog(
    session: AsyncSession, season_id: str
) -> StoredBootstrapCatalog | None:
    """Read one persisted bootstrap catalog with stable ordering."""
    season = await session.get(Season, season_id)
    metadata = await session.get(CacheMetadata, (season_id, SHARED_METADATA_KEY))
    if season is None or metadata is None:
        return None

    teams = tuple(
        (
            await session.scalars(
                select(Team).where(Team.season_id == season_id).order_by(Team.id)
            )
        ).all()
    )
    players = tuple(
        (
            await session.scalars(
                select(Player)
                .where(Player.season_id == season_id)
                .order_by(Player.id)
            )
        ).all()
    )
    gameweeks = tuple(
        (
            await session.scalars(
                select(Gameweek)
                .where(Gameweek.season_id == season_id)
                .order_by(Gameweek.id)
            )
        ).all()
    )
    return StoredBootstrapCatalog(
        season=season,
        teams=teams,
        players=players,
        gameweeks=gameweeks,
        metadata=metadata,
    )
