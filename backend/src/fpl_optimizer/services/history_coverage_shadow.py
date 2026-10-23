"""Count provisional J44 coverage gaps without changing publication or eligibility.

The anchor rule cannot see gaps before the last row or for unresolved players.
Its apparent gaps can be false after an away-and-back club transfer. These
aggregate counts are observations for later review, never enforcement evidence.
``gaps_status_not_a`` counts gap players whose catalog status is not ``a``.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import String, cast, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fpl_optimizer.db.models import CacheMetadata, Fixture, Gameweek, Player, PlayerFixtureHistory
from fpl_optimizer.services.history_requirements import (
    PublishedHistoryRow, SeasonFixture, derive_history_requirements,
)
from fpl_optimizer.services.player_history import PLAYER_HISTORY_KEY_PREFIX


@dataclass(frozen=True)
class CoverageShadowCounts:
    players: int = 0
    provisional: int = 0
    unresolved_no_rows: int = 0
    unresolved_club_change: int = 0
    players_with_gaps: int = 0
    missing_total: int = 0
    within_grace_total: int = 0
    gaps_status_not_a: int = 0

    def log_line(self) -> str:
        return (
            f"Coverage shadow: players={self.players} provisional={self.provisional} "
            f"unresolved_no_rows={self.unresolved_no_rows} "
            f"unresolved_club_change={self.unresolved_club_change} "
            f"players_with_gaps={self.players_with_gaps} missing_total={self.missing_total} "
            f"within_grace_total={self.within_grace_total} "
            f"gaps_status_not_a={self.gaps_status_not_a}"
        )


async def collect_history_coverage_shadow(
    session_factory: async_sessionmaker[AsyncSession], season_id: str, *, now: datetime,
) -> CoverageShadowCounts:
    """Read one season in a single PostgreSQL read-only repeatable-read view."""
    async with session_factory() as session:
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        await session.execute(text("SET TRANSACTION READ ONLY"))
        return await _collect_in_transaction(session, season_id, now)


async def _collect_in_transaction(
    session: AsyncSession, season_id: str, now: datetime,
) -> CoverageShadowCounts:
    fixture_rows = (await session.execute(
        select(
            Fixture.id, Fixture.home_team_id, Fixture.away_team_id,
            Fixture.gameweek_id, Fixture.kickoff_time, Fixture.finished,
            Fixture.gameweek_data_checked_at, Gameweek.data_checked,
        ).outerjoin(
            Gameweek,
            (Gameweek.season_id == Fixture.season_id) & (Gameweek.id == Fixture.gameweek_id),
        ).where(Fixture.season_id == season_id)
    )).all()
    season_fixtures = tuple(SeasonFixture(*row) for row in fixture_rows)

    players = (await session.execute(
        select(Player.id, Player.team_id, Player.status).join(
            CacheMetadata,
            (CacheMetadata.season_id == Player.season_id)
            & (CacheMetadata.key == PLAYER_HISTORY_KEY_PREFIX + cast(Player.id, String)),
        ).where(Player.season_id == season_id, CacheMetadata.last_success_at.is_not(None))
    )).all()
    if not players:
        return CoverageShadowCounts()

    player_ids = tuple(player_id for player_id, _, _ in players)
    history_rows = (await session.execute(
        select(
            PlayerFixtureHistory.player_id, PlayerFixtureHistory.fixture_id,
            Fixture.kickoff_time, PlayerFixtureHistory.was_home,
            Fixture.home_team_id, Fixture.away_team_id, PlayerFixtureHistory.minutes,
        ).join(
            Fixture,
            (Fixture.season_id == PlayerFixtureHistory.season_id)
            & (Fixture.id == PlayerFixtureHistory.fixture_id),
        ).where(
            PlayerFixtureHistory.season_id == season_id,
            PlayerFixtureHistory.player_id.in_(player_ids),
        )
    )).all()

    by_player: dict[int, list[PublishedHistoryRow]] = {player_id: [] for player_id in player_ids}
    row_ids: dict[int, set[int]] = {player_id: set() for player_id in player_ids}
    for player_id, fixture_id, kickoff, was_home, home, away, minutes in history_rows:
        row_ids[player_id].add(fixture_id)
        # A row lacking a fixture kickoff cannot anchor the interim rule.
        if kickoff is not None:
            by_player[player_id].append(PublishedHistoryRow(
                fixture_id, kickoff, home if was_home else away, minutes,
            ))

    provisional = no_rows = club_change = gap_players = missing_total = grace = status_not_a = 0
    for player_id, team_id, status in players:
        derived = derive_history_requirements(
            player_id=player_id, catalog_team_id=team_id,
            published_rows=tuple(by_player[player_id]),
            season_fixtures=season_fixtures, now=now,
        )
        if derived.status == "unresolved":
            if derived.reason == "no_rows":
                no_rows += 1
            else:
                club_change += 1
            continue
        provisional += 1
        grace += len(derived.within_grace)
        missing = {item.fixture_id for item in derived.required_fixtures} - row_ids[player_id]
        if missing:
            gap_players += 1
            missing_total += len(missing)
            status_not_a += status != "a"

    return CoverageShadowCounts(
        len(players), provisional, no_rows, club_change, gap_players,
        missing_total, grace, status_not_a,
    )
