"""Validate, atomically publish, and reconcile one player's fixture history."""

from dataclasses import dataclass
from datetime import datetime
import logging

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fpl_optimizer.config import Settings
from fpl_optimizer.db.advisory_lock import ingestion_lock, require_same_database
from fpl_optimizer.db.models import (
    CacheMetadata,
    Fixture,
    Player,
    PlayerFixtureHistory,
    Season,
)
from fpl_optimizer.schemas.element_summary import (
    ElementFixtureHistory,
    ElementSummaryEnvelope,
    PlayerHistoryPublication,
)
from fpl_optimizer.services.shared_catalog import SHARED_METADATA_KEY


HISTORY_METADATA_KEY = "history"
PLAYER_HISTORY_KEY_PREFIX = "history/player/"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StoredPlayerHistory:
    """A player's rows and the checkpoint committed with them."""

    rows: tuple[PlayerFixtureHistory, ...]
    checkpoint: CacheMetadata
    history_publication_version: int


@dataclass(frozen=True)
class PlayerEventTotal:
    """Recorded and independently reconstructed points for one event."""

    gameweek_id: int | None
    fixture_ids: tuple[int, ...]
    recorded_total_points: int
    reconstructed_total_points: int
    mismatched_fixture_ids: tuple[int, ...]

    @property
    def reconciled(self) -> bool:
        return not self.mismatched_fixture_ids


def player_history_metadata_key(player_id: int) -> str:
    return f"{PLAYER_HISTORY_KEY_PREFIX}{player_id}"


def _reconstruct_fixture_points(
    row: ElementFixtureHistory | PlayerFixtureHistory,
    position: int,
) -> int:
    appearance = 0 if row.minutes == 0 else (1 if row.minutes < 60 else 2)
    goal_points = {1: 10, 2: 6, 3: 5, 4: 4}[position]
    clean_sheet_points = {1: 4, 2: 4, 3: 1, 4: 0}[position]
    defensive_points = 0
    if position == 2:
        defensive_points = (
            2
            if row.clearances_blocks_interceptions + row.tackles >= 10
            else 0
        )
    elif position in {3, 4}:
        defensive_points = (
            2
            if row.clearances_blocks_interceptions
            + row.tackles
            + row.recoveries
            >= 12
            else 0
        )

    return (
        appearance
        + row.goals_scored * goal_points
        + row.assists * 3
        + (row.clean_sheets * clean_sheet_points if row.minutes >= 60 else 0)
        + (row.saves // 3 if position == 1 else 0)
        - (row.goals_conceded // 2 if position in {1, 2} else 0)
        + defensive_points
        + row.bonus
        + row.penalties_saved * 5
        - row.penalties_missed * 2
        - row.yellow_cards
        - row.red_cards * 3
        - row.own_goals * 2
    )


async def publish_player_history(
    session_factory: async_sessionmaker[AsyncSession],
    envelope: ElementSummaryEnvelope,
    publication: PlayerHistoryPublication,
    *,
    settings: Settings | None = None,
) -> int | None:
    """Commit one player envelope under the shared ingestion advisory lock."""
    async with ingestion_lock(settings) as lock_connection:
        if lock_connection is None:
            logger.info("Shared ingestion already running")
            return None
        async with session_factory.begin() as session:
            await require_same_database(lock_connection, await session.connection())
            return await _write_player_history(session, envelope, publication)


async def _validate_catalog_dependencies(
    session: AsyncSession,
    envelope: ElementSummaryEnvelope,
    publication: PlayerHistoryPublication,
) -> tuple[Season, Player, CacheMetadata, dict[int, Fixture]]:
    season = await session.get(Season, publication.season_id)
    if season is None:
        raise ValueError("history season is absent from the shared catalog")

    player = await session.get(Player, (publication.season_id, publication.player_id))
    if player is None:
        raise ValueError("history player is absent from the shared catalog")

    shared_metadata = await session.get(
        CacheMetadata, (publication.season_id, SHARED_METADATA_KEY)
    )
    if shared_metadata is None:
        raise ValueError("history requires a published shared catalog")

    wrong_player_rows = sorted(
        {row.element for row in envelope.history if row.element != publication.player_id}
    )
    if wrong_player_rows:
        raise ValueError(
            "element-summary history does not belong to the requested player: "
            f"{wrong_player_rows}"
        )

    fixture_ids = {row.fixture for row in envelope.history}
    fixtures: dict[int, Fixture] = {}
    if fixture_ids:
        fixtures = {
            fixture.id: fixture
            for fixture in (
                await session.scalars(
                    select(Fixture).where(
                        Fixture.season_id == publication.season_id,
                        Fixture.id.in_(fixture_ids),
                    )
                )
            ).all()
        }
    missing_fixture_ids = sorted(fixture_ids - fixtures.keys())
    if missing_fixture_ids:
        raise ValueError(
            "element-summary history references fixtures absent from the shared "
            f"catalog: {missing_fixture_ids}"
        )

    for row in envelope.history:
        fixture = fixtures[row.fixture]
        if fixture.gameweek_id is None or fixture.gameweek_id != row.round:
            raise ValueError(
                f"history fixture {row.fixture} conflicts with its catalog gameweek"
            )
        if fixture.kickoff_time != row.kickoff_time:
            raise ValueError(
                f"history fixture {row.fixture} conflicts with its catalog kickoff"
            )

        expected_team_id = (
            fixture.home_team_id if row.was_home else fixture.away_team_id
        )
        expected_opponent_id = (
            fixture.away_team_id if row.was_home else fixture.home_team_id
        )
        if row.opponent_team != expected_opponent_id:
            raise ValueError(
                f"history fixture {row.fixture} conflicts with its catalog sides"
            )
        if expected_team_id == expected_opponent_id:
            raise ValueError(f"history fixture {row.fixture} has invalid catalog sides")
        reconstructed_points = _reconstruct_fixture_points(row, player.position)
        if reconstructed_points != row.total_points:
            raise ValueError(
                f"history fixture {row.fixture} recorded total_points "
                f"{row.total_points}, but scoring components reconstruct "
                f"{reconstructed_points}"
            )

    existing_fixture_ids = set(
        (
            await session.scalars(
                select(PlayerFixtureHistory.fixture_id).where(
                    PlayerFixtureHistory.season_id == publication.season_id,
                    PlayerFixtureHistory.player_id == publication.player_id,
                )
            )
        ).all()
    )
    disappeared_fixture_ids = sorted(existing_fixture_ids - fixture_ids)
    if disappeared_fixture_ids:
        raise ValueError(
            "element-summary history dropped previously published fixtures: "
            f"{disappeared_fixture_ids}"
        )

    return season, player, shared_metadata, fixtures


def _history_rows(
    envelope: ElementSummaryEnvelope,
    publication: PlayerHistoryPublication,
    player: Player,
    fixtures: dict[int, Fixture],
    publication_version: int,
) -> list[dict]:
    rows: list[dict] = []
    for source in envelope.history:
        fixture = fixtures[source.fixture]
        rows.append(
            {
                "season_id": publication.season_id,
                "player_id": publication.player_id,
                "fixture_id": source.fixture,
                "source_round": source.round,
                "position": player.position,
                "team_id": (
                    fixture.home_team_id
                    if source.was_home
                    else fixture.away_team_id
                ),
                "opponent_team_id": source.opponent_team,
                "was_home": source.was_home,
                "source_value": source.value,
                "minutes": source.minutes,
                "total_points": source.total_points,
                "goals_scored": source.goals_scored,
                "assists": source.assists,
                "clean_sheets": source.clean_sheets,
                "goals_conceded": source.goals_conceded,
                "saves": source.saves,
                "penalties_saved": source.penalties_saved,
                "penalties_missed": source.penalties_missed,
                "yellow_cards": source.yellow_cards,
                "red_cards": source.red_cards,
                "own_goals": source.own_goals,
                "bonus": source.bonus,
                "clearances_blocks_interceptions": (
                    source.clearances_blocks_interceptions
                ),
                "tackles": source.tackles,
                "recoveries": source.recoveries,
                "defensive_contribution": source.defensive_contribution,
                "bps": source.bps,
                "influence": source.influence,
                "creativity": source.creativity,
                "threat": source.threat,
                "ict_index": source.ict_index,
                "expected_goals": source.expected_goals,
                "expected_assists": source.expected_assists,
                "expected_goal_involvements": source.expected_goal_involvements,
                "expected_goals_conceded": source.expected_goals_conceded,
                "starts": source.starts,
                "publication_version": publication_version,
            }
        )
    return rows


async def _write_history_rows(
    session: AsyncSession,
    rows: list[dict],
) -> None:
    if not rows:
        return
    statement = insert(PlayerFixtureHistory).values(rows)
    await session.execute(
        statement.on_conflict_do_update(
            index_elements=("season_id", "player_id", "fixture_id"),
            set_={
                column: getattr(statement.excluded, column)
                for column in rows[0]
                if column not in {"season_id", "player_id", "fixture_id"}
            },
        )
    )


async def _write_player_history(
    session: AsyncSession,
    envelope: ElementSummaryEnvelope,
    publication: PlayerHistoryPublication,
) -> int:
    """Validate against the catalog, then write rows and metadata together."""
    season, player, shared_metadata, fixtures = await _validate_catalog_dependencies(
        session, envelope, publication
    )
    checkpoint_key = player_history_metadata_key(publication.player_id)
    existing_checkpoint = await session.get(
        CacheMetadata, (publication.season_id, checkpoint_key)
    )
    history_metadata = await session.get(
        CacheMetadata, (publication.season_id, HISTORY_METADATA_KEY)
    )
    is_replay = (
        existing_checkpoint is not None
        and existing_checkpoint.source_observed_at == publication.source_observed_at
    )
    publication_version = (
        existing_checkpoint.publication_version
        if is_replay
        else (history_metadata.publication_version + 1 if history_metadata else 1)
    )

    rows = _history_rows(
        envelope, publication, player, fixtures, publication_version
    )
    await _write_history_rows(session, rows)

    if not is_replay:
        history_statement = insert(CacheMetadata).values(
            season_id=publication.season_id,
            key=HISTORY_METADATA_KEY,
            publication_version=publication_version,
            last_success_at=None,
            source_observed_at=None,
        )
        await session.execute(
            history_statement.on_conflict_do_update(
                index_elements=("season_id", "key"),
                set_={"publication_version": publication_version},
            )
        )

    fixture_dependencies = [
        {
            "fixture_id": fixture.id,
            "gameweek_id": fixture.gameweek_id,
            "kickoff_time": (
                fixture.kickoff_time.isoformat()
                if fixture.kickoff_time is not None
                else None
            ),
            "home_team_id": fixture.home_team_id,
            "away_team_id": fixture.away_team_id,
        }
        for fixture in sorted(fixtures.values(), key=lambda item: item.id)
    ]
    covered_fixture_ids = sorted(fixtures)
    covered_finalized_fixture_ids = sorted(
        fixture.id
        for fixture in fixtures.values()
        if fixture.gameweek_data_checked_at is not None
    )
    checkpoint_values = {
        "season_id": publication.season_id,
        "key": checkpoint_key,
        "publication_version": publication_version,
        "last_success_at": publication.published_at,
        "source_observed_at": publication.source_observed_at,
        "completed_sweep_id": publication.completed_sweep_id,
        "rules_version": season.rules_version,
        "source_shared_publication_version": shared_metadata.publication_version,
        "identity_dependencies": {
            "player_id": player.id,
            "external_code": player.external_code,
            "position": player.position,
        },
        "fixture_dependencies": fixture_dependencies,
        "covered_fixture_ids": covered_fixture_ids,
        "covered_finalized_fixture_ids": covered_finalized_fixture_ids,
        "missing_fixture_ids": [],
        "invalid_fixture_ids": [],
        "last_attempt_at": publication.published_at,
        "last_error": None,
        "last_error_category": None,
        "retry_count": 0,
        "next_retry_at": None,
    }
    checkpoint_statement = insert(CacheMetadata).values(**checkpoint_values)
    await session.execute(
        checkpoint_statement.on_conflict_do_update(
            index_elements=("season_id", "key"),
            set_={
                column: getattr(checkpoint_statement.excluded, column)
                for column in checkpoint_values
                if column not in {"season_id", "key"}
            },
        )
    )
    return publication_version


async def read_player_history(
    session: AsyncSession,
    season_id: str,
    player_id: int,
) -> StoredPlayerHistory | None:
    checkpoint = await session.get(
        CacheMetadata, (season_id, player_history_metadata_key(player_id))
    )
    history_metadata = await session.get(
        CacheMetadata, (season_id, HISTORY_METADATA_KEY)
    )
    if (
        checkpoint is None
        or checkpoint.last_success_at is None
        or history_metadata is None
    ):
        return None
    rows = tuple(
        (
            await session.scalars(
                select(PlayerFixtureHistory)
                .where(
                    PlayerFixtureHistory.season_id == season_id,
                    PlayerFixtureHistory.player_id == player_id,
                )
                .order_by(PlayerFixtureHistory.fixture_id)
            )
        ).all()
    )
    return StoredPlayerHistory(
        rows=rows,
        checkpoint=checkpoint,
        history_publication_version=history_metadata.publication_version,
    )


def reconstruct_fixture_points(row: PlayerFixtureHistory) -> int:
    """Apply the §04D per-fixture scoring adapter to stored required counts."""
    return _reconstruct_fixture_points(row, row.position)


async def read_player_event_totals(
    session: AsyncSession,
    season_id: str,
    player_id: int,
) -> tuple[PlayerEventTotal, ...]:
    """Group fixture rows by current catalog assignment and reconcile points."""
    result = await session.execute(
        select(PlayerFixtureHistory, Fixture.gameweek_id)
        .join(
            Fixture,
            (Fixture.season_id == PlayerFixtureHistory.season_id)
            & (Fixture.id == PlayerFixtureHistory.fixture_id),
        )
        .where(
            PlayerFixtureHistory.season_id == season_id,
            PlayerFixtureHistory.player_id == player_id,
        )
        .order_by(Fixture.gameweek_id, PlayerFixtureHistory.fixture_id)
    )
    grouped: dict[int | None, list[PlayerFixtureHistory]] = {}
    for row, gameweek_id in result.all():
        grouped.setdefault(gameweek_id, []).append(row)

    return tuple(
        PlayerEventTotal(
            gameweek_id=gameweek_id,
            fixture_ids=tuple(row.fixture_id for row in rows),
            recorded_total_points=sum(row.total_points for row in rows),
            reconstructed_total_points=sum(
                reconstruct_fixture_points(row) for row in rows
            ),
            mismatched_fixture_ids=tuple(
                row.fixture_id
                for row in rows
                if reconstruct_fixture_points(row) != row.total_points
            ),
        )
        for gameweek_id, rows in sorted(
            grouped.items(), key=lambda item: (item[0] is None, item[0] or 0)
        )
    )
