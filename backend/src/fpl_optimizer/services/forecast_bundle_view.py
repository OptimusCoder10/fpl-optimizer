"""One current-season database snapshot for J46 research, not coverage approval.

This reader preserves all stored scoring rows. The caller must apply
forecast_bundle_policy.eligible_current_rows before assembling predictor inputs.
No membership, new-player eligibility, priors or scoring are inferred here.
"""

from dataclasses import dataclass, fields
from datetime import datetime, timezone

from sqlalchemy import String, cast, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fpl_optimizer.db.models import (
    CacheMetadata, Fixture, Gameweek, Player, PlayerFixtureHistory, Season, Team,
)
from fpl_optimizer.services.forecast_bundle_policy import (
    FixtureInput, GameweekInput, TargetEvent, select_target_event,
)
from fpl_optimizer.services.history_eligibility import PlayerHistoryEvidence
from fpl_optimizer.services.player_history import HISTORY_METADATA_KEY, PLAYER_HISTORY_KEY_PREFIX
from fpl_optimizer.services.shared_catalog import SHARED_METADATA_KEY, read_shared_freshness
from fpl_optimizer.services.shared_freshness import SharedFreshness


@dataclass(frozen=True)
class BundleFixture(FixtureInput):
    home_team_id: int
    away_team_id: int


@dataclass(frozen=True)
class ClubTargetFixtures:
    team_id: int
    fixture_ids: tuple[int, ...]


@dataclass(frozen=True)
class CatalogPlayer:
    id: int
    position: int
    team_id: int
    status: str
    chance_of_playing_next_round: int | None
    can_select: bool
    can_transact: bool
    removed: bool
    external_code: int
    has_temporary_code: bool
    opta_code: str | None


@dataclass(frozen=True)
class CurrentHistoryRow:
    player_id: int
    fixture_id: int
    minutes: int
    goals_scored: int
    assists: int
    clean_sheets: int
    saves: int
    goals_conceded: int
    clearances_blocks_interceptions: int
    tackles: int
    recoveries: int
    bonus: int
    penalties_saved: int
    penalties_missed: int
    yellow_cards: int
    red_cards: int
    own_goals: int


@dataclass(frozen=True)
class SharedPublication:
    source_observed_at: datetime | None
    last_success_at: datetime | None
    publication_version: int | None


@dataclass(frozen=True)
class BundleView:
    season_id: str
    as_of: datetime
    rules_version: str | None
    shared_publication: SharedPublication
    # Freshness has no defined age window without a future deadline.
    shared_freshness: SharedFreshness | None
    history_publication_version: int | None
    gameweeks: tuple[GameweekInput, ...]
    fixtures: tuple[BundleFixture, ...]
    target: TargetEvent | None
    target_fixtures_by_club: tuple[ClubTargetFixtures, ...]
    players: tuple[CatalogPlayer, ...]
    player_history: tuple[PlayerHistoryEvidence, ...]
    history_rows: tuple[CurrentHistoryRow, ...]


async def read_bundle_view(
    session_factory: async_sessionmaker[AsyncSession], season_id: str, *, as_of: datetime,
) -> BundleView:
    """Close a single read-only repeatable-read transaction before returning.

    At most ten SQL statements, independent of player count: one SET and eight
    set-based reads, plus one metadata retry if the shared checkpoint is absent.
    Transaction/driver setup round trips are not SQL statements counted here.
    read_shared_freshness reuses the retained metadata identity in this session.
    Missing publication evidence remains unknown, never invented.
    """
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("as_of must include a UTC offset")
    as_of = as_of.astimezone(timezone.utc)
    async with session_factory() as session:
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        await session.execute(text("SET TRANSACTION READ ONLY"))
        view = await _read_in_transaction(session, season_id, as_of)
    return view


async def _read_in_transaction(
    session: AsyncSession, season_id: str, as_of: datetime,
) -> BundleView:
    rules_version = await session.scalar(select(Season.rules_version).where(Season.id == season_id))
    metadata = await session.get(CacheMetadata, (season_id, SHARED_METADATA_KEY))
    publication = SharedPublication(
        metadata.source_observed_at if metadata else None,
        metadata.last_success_at if metadata else None,
        metadata.publication_version if metadata else None,
    )
    team_ids = tuple((await session.scalars(
        select(Team.id).where(Team.season_id == season_id).order_by(Team.id)
    )).all())
    gameweeks = tuple(GameweekInput(*row) for row in (await session.execute(
        select(Gameweek.id, Gameweek.deadline_time, Gameweek.data_checked)
        .where(Gameweek.season_id == season_id).order_by(Gameweek.id)
    )).all())
    fixtures = tuple(BundleFixture(*row) for row in (await session.execute(
        select(*(getattr(Fixture, field.name) for field in fields(BundleFixture)))
        .where(Fixture.season_id == season_id).order_by(Fixture.id)
    )).all())
    target = select_target_event(gameweeks, as_of=as_of)
    # A missing row is not cached by session.get; in that case the existing
    # reader makes one extra metadata read and supplies the unknown result.
    if target is not None:
        shared_freshness = await read_shared_freshness(
            session, season_id, now=as_of, next_deadline=target.deadline_time,
        )
    else:
        shared_freshness = None
    target_fixtures = tuple(ClubTargetFixtures(
        team_id,
        tuple(fixture.id for fixture in fixtures
              if target is not None and fixture.gameweek_id == target.id
              and team_id in (fixture.home_team_id, fixture.away_team_id)),
    ) for team_id in team_ids)

    players = []
    evidence = []
    player_fields = fields(CatalogPlayer)
    records = (await session.execute(
        select(
            *(getattr(Player, field.name) for field in player_fields),
            CacheMetadata.source_observed_at, CacheMetadata.last_success_at,
            CacheMetadata.publication_version, CacheMetadata.covered_fixture_ids,
            CacheMetadata.covered_finalized_fixture_ids, CacheMetadata.missing_fixture_ids,
            CacheMetadata.invalid_fixture_ids, CacheMetadata.last_attempt_at,
        ).outerjoin(
            CacheMetadata,
            (CacheMetadata.season_id == Player.season_id)
            & (CacheMetadata.key == PLAYER_HISTORY_KEY_PREFIX + cast(Player.id, String)),
        ).where(Player.season_id == season_id).order_by(Player.id)
    )).all()
    for record in records:
        player = CatalogPlayer(*record[:len(player_fields)])
        players.append(player)
        source, success, version, covered, finalized, missing, invalid, attempt = record[len(player_fields):]
        evidence.append(PlayerHistoryEvidence(
            player_id=player.id, source_observed_at=source, last_success_at=success,
            publication_version=version, covered_fixture_ids=frozenset(covered or ()),
            covered_finalized_fixture_ids=frozenset(finalized or ()),
            missing_fixture_ids=tuple(missing or ()), invalid_fixture_ids=tuple(invalid or ()),
            last_attempt_at=attempt,
            # There is no persisted proof of newly registered empty history, or
            # verified player-specific required fixtures, in today's schema.
        ))
    history_version = await session.scalar(select(CacheMetadata.publication_version).where(
        CacheMetadata.season_id == season_id, CacheMetadata.key == HISTORY_METADATA_KEY,
    ))
    history_rows = await _read_history_rows(session, season_id)
    return BundleView(
        season_id, as_of, rules_version, publication, shared_freshness, history_version,
        gameweeks, fixtures, target, target_fixtures, tuple(players), tuple(evidence), history_rows,
    )


async def _read_history_rows(session: AsyncSession, season_id: str) -> tuple[CurrentHistoryRow, ...]:
    """Read counts without cutoff/finalization filtering; policy owns that step."""
    return tuple(CurrentHistoryRow(*row) for row in (await session.execute(
        select(*(getattr(PlayerFixtureHistory, field.name) for field in fields(CurrentHistoryRow)))
        .where(PlayerFixtureHistory.season_id == season_id)
        .order_by(PlayerFixtureHistory.player_id, PlayerFixtureHistory.fixture_id)
    )).all())
