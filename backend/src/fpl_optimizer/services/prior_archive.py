"""Publish offline history through the existing catalog and per-player writers."""

from dataclasses import dataclass
from datetime import datetime
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from fpl_optimizer.config import Settings
from fpl_optimizer.db.advisory_lock import ingestion_lock, require_same_database
from fpl_optimizer.db.models import CacheMetadata, Fixture, Player, PlayerFixtureHistory, Season
from fpl_optimizer.schemas.bootstrap import SharedPublication
from fpl_optimizer.schemas.element_summary import PlayerHistoryPublication
from fpl_optimizer.schemas.prior_archive import (
    ARCHIVE_COMMIT, ARCHIVE_RULES, ARCHIVE_SEASON, PINNED_SHA256, SCORING_ADAPTER, ValidatedArchive,
)
from fpl_optimizer.services.player_history import (
    _write_player_history, player_history_metadata_key, reconstruct_fixture_points,
)
from fpl_optimizer.services.shared_catalog import _write_shared_catalog

logger = logging.getLogger(__name__)
# Explicit compatibility policy for the §04D count-based scoring adapter.
# Awarded bonus remains the declared proxy; optional xG/ICT are irrelevant.
COMPATIBLE_RULES = {ARCHIVE_RULES, "fpl-2026-v1"}
SCORING_FIELDS = (
    "minutes", "total_points", "goals_scored", "assists", "clean_sheets",
    "goals_conceded", "saves", "penalties_saved", "penalties_missed",
    "yellow_cards", "red_cards", "own_goals", "bonus",
    "clearances_blocks_interceptions", "tackles", "recoveries", "defensive_contribution",
)


async def publish_prior_archive(
    session_factory: async_sessionmaker[AsyncSession],
    archive: ValidatedArchive,
    publication: SharedPublication,
    *,
    settings: Settings | None = None,
    allow_sample: bool = False,
) -> tuple[int, ...] | None:
    """Resume immutable import; shared catalog and each player's rows are atomic.

    source_observed_at records offline collection, not a historical deadline.
    None means another ingestion owns the lock. Returns newly published IDs.
    """
    if publication.season_id != ARCHIVE_SEASON or publication.rules_version != ARCHIVE_RULES:
        raise ValueError("archive publication requires its pinned season and scoring rules")
    if archive.provenance.get("kind") != "pinned" and not allow_sample:
        raise ValueError("sample archive publication requires explicit test opt-in")
    async with ingestion_lock(settings) as lock_connection:
        if lock_connection is None:
            logger.info("Shared ingestion already running")
            return None
        async with session_factory.begin() as session:
            await require_same_database(lock_connection, await session.connection())
            shared = await session.get(CacheMetadata, (ARCHIVE_SEASON, "shared"))
            if shared is None:
                await _write_shared_catalog(session, archive.catalog, publication)
                shared = await session.get(CacheMetadata, (ARCHIVE_SEASON, "shared"))
                shared.source_provenance = archive.provenance
            elif shared.source_provenance != archive.provenance:
                raise ValueError("archive namespace already has different source provenance")
        committed = []
        for player_id, envelope in sorted(archive.histories.items()):
            async with session_factory.begin() as session:
                await require_same_database(lock_connection, await session.connection())
                checkpoint = await session.get(CacheMetadata, (
                    ARCHIVE_SEASON, player_history_metadata_key(player_id),
                ))
                if checkpoint is not None and checkpoint.last_success_at is not None:
                    if checkpoint.source_provenance == archive.provenance:
                        continue
                    raise ValueError(f"archive player {player_id} has different source provenance")
                await _write_player_history(session, envelope, PlayerHistoryPublication(
                    season_id=ARCHIVE_SEASON, player_id=player_id,
                    completed_sweep_id=f"archive-{ARCHIVE_COMMIT}",
                    source_observed_at=publication.source_observed_at,
                    published_at=publication.published_at,
                ))
                checkpoint = await session.get(CacheMetadata, (
                    ARCHIVE_SEASON, player_history_metadata_key(player_id),
                ))
                checkpoint.source_provenance = archive.provenance
            committed.append(player_id)
        return tuple(committed)


@dataclass(frozen=True)
class PriorObservation:
    """Only validated historical context and complete scoring counts, no prices/xP."""

    player_id: int
    fixture_id: int
    gameweek_id: int
    kickoff_time: datetime
    position: int
    team_id: int
    opponent_team_id: int
    was_home: bool
    scoring_counts: dict[str, int]


@dataclass(frozen=True)
class PriorInputs:
    """Separate same-player and position pools; prediction hierarchy lives elsewhere."""

    prior_season_id: str
    personal_player_id: int | None
    personal_reason: str | None
    personal_rows: tuple[PriorObservation, ...]
    position_rows: tuple[PriorObservation, ...]
    exclusions: tuple[tuple[int, str], ...]
    provenance: dict | None
    history_publication_version: int | None


def _previous_season(season_id: str) -> str:
    start = int(season_id[:4])
    if season_id != f"{start}-{(start + 1) % 100:02d}":
        raise ValueError("invalid consecutive season namespace")
    return f"{start - 1}-{start % 100:02d}"


async def read_prior_inputs(
    session_factory: async_sessionmaker[AsyncSession],
    current_season_id: str,
    player_id: int,
    *,
    allow_sample: bool = False,
) -> PriorInputs:
    """Capture compatible immediately previous-season pools in one consistent view.

    No current-data age limit, name matching, scoring means or prediction code.
    A missing personal match leaves the separate position pool available.
    """
    prior_season = _previous_season(current_season_id)
    async with session_factory() as session:
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        current = await session.get(Player, (current_season_id, player_id))
        target_season = await session.get(Season, current_season_id)
        if current is None or target_season is None:
            raise ValueError("current player/season is absent from the catalog")
        source_season = await session.get(Season, prior_season)
        metadata = await session.get(CacheMetadata, (prior_season, "shared"))
        history = await session.get(CacheMetadata, (prior_season, "history"))
        provenance = metadata.source_provenance if metadata else None
        reason = None
        if source_season is None or provenance is None or history is None:
            reason = "prior_archive_unavailable"
        elif provenance.get("kind") == "sample" and not allow_sample:
            reason = "prior_archive_not_pinned"
        elif (
            prior_season != ARCHIVE_SEASON or provenance.get("commit") != ARCHIVE_COMMIT
            or provenance.get("scoring_adapter") != SCORING_ADAPTER
            or provenance.get("kind") not in {"pinned", "sample"}
            or (provenance.get("kind") == "pinned" and provenance.get("sha256") != PINNED_SHA256)
            or source_season.rules_version not in COMPATIBLE_RULES
            or target_season.rules_version not in COMPATIBLE_RULES
        ):
            reason = "incompatible_prior_rules_or_source"
        if reason:
            return PriorInputs(prior_season, None, reason, (), (), (), provenance,
                               history.publication_version if history else None)

        prior_players = (await session.scalars(select(Player).where(
            Player.season_id == prior_season,
        ))).all()
        matches = [p for p in prior_players if p.external_code == current.external_code]
        current_matches = (await session.scalars(select(Player).where(
            Player.season_id == current_season_id,
            Player.external_code == current.external_code,
        ))).all()
        personal = None
        if current.has_temporary_code or current.external_code <= 0:
            reason = "temporary_or_invalid_current_code"
        elif len(matches) != 1 or len(current_matches) != 1:
            reason = "external_code_match_not_one_to_one"
        elif matches[0].has_temporary_code or matches[0].external_code <= 0:
            reason = "temporary_or_invalid_prior_code"
        elif matches[0].position != current.position:
            reason = "position_changed"
        else:
            personal = matches[0].id

        checkpoints = {state.key: state for state in (await session.scalars(
            select(CacheMetadata).where(CacheMetadata.season_id == prior_season)
        )).all()}
        result = (await session.execute(select(PlayerFixtureHistory, Fixture).join(
            Fixture, (Fixture.season_id == PlayerFixtureHistory.season_id)
            & (Fixture.id == PlayerFixtureHistory.fixture_id),
        ).where(
            PlayerFixtureHistory.season_id == prior_season,
            PlayerFixtureHistory.position == current.position,
        ).order_by(PlayerFixtureHistory.player_id, Fixture.kickoff_time, Fixture.id))).all()
        grouped = {}
        for row, fixture in result:
            grouped.setdefault(row.player_id, []).append((row, fixture))
        observations = []
        excluded = []
        for prior_player in prior_players:
            if prior_player.position != current.position:
                continue
            pid = prior_player.id
            checkpoint = checkpoints.get(player_history_metadata_key(pid))
            rows = grouped.get(pid, [])
            coverage_reason = None
            if checkpoint is None or checkpoint.last_success_at is None or not rows:
                coverage_reason = "prior_history_unavailable"
            elif checkpoint.source_provenance != provenance or checkpoint.rules_version != source_season.rules_version:
                coverage_reason = "prior_provenance_or_rules_mismatch"
            elif checkpoint.missing_fixture_ids or checkpoint.invalid_fixture_ids:
                coverage_reason = "prior_history_coverage_gap"
            elif set(checkpoint.covered_fixture_ids or []) != {row.fixture_id for row, _ in rows}:
                coverage_reason = "prior_history_coverage_gap"
            elif checkpoint.identity_dependencies != {
                "player_id": pid, "external_code": prior_player.external_code,
                "position": prior_player.position,
            }:
                coverage_reason = "prior_identity_changed"
            elif checkpoint.fixture_dependencies != [
                {
                    "fixture_id": fixture.id, "gameweek_id": fixture.gameweek_id,
                    "kickoff_time": fixture.kickoff_time.isoformat() if fixture.kickoff_time else None,
                    "home_team_id": fixture.home_team_id, "away_team_id": fixture.away_team_id,
                }
                for _, fixture in sorted(rows, key=lambda pair: pair[1].id)
            ]:
                coverage_reason = "prior_fixture_dependencies_changed"
            elif any(
                not fixture.finished or fixture.kickoff_time is None or fixture.gameweek_id is None
                or fixture.gameweek_id != row.source_round
                or row.team_id != (fixture.home_team_id if row.was_home else fixture.away_team_id)
                or row.opponent_team_id != (fixture.away_team_id if row.was_home else fixture.home_team_id)
                or reconstruct_fixture_points(row) != row.total_points
                for row, fixture in rows
            ):
                coverage_reason = "prior_fixture_or_scoring_incompatible"
            if coverage_reason:
                excluded.append((pid, coverage_reason))
                continue
            for row, fixture in rows:
                observations.append(PriorObservation(
                    pid, row.fixture_id, fixture.gameweek_id, fixture.kickoff_time,
                    row.position, row.team_id, row.opponent_team_id, row.was_home,
                    {field: getattr(row, field) for field in SCORING_FIELDS},
                ))
        personal_rows = tuple(row for row in observations if row.player_id == personal)
        if personal is not None and not personal_rows:
            reason = dict(excluded).get(personal, "prior_history_unavailable")
        return PriorInputs(prior_season, personal, reason, personal_rows,
                           tuple(observations), tuple(sorted(excluded)), provenance,
                           history.publication_version if history else None)
