"""Pure §05B read-side eligibility over one consistent, caller-captured view."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from fpl_optimizer.services.shared_freshness import SharedFreshness


Mode = Literal["lineup", "transfers", "simulator"]
Status = Literal["current", "stale", "unknown"]
Action = Literal["use", "exclude", "BLOCK"]
HISTORY_AGE_LIMIT = timedelta(hours=48)
LINEUP_HISTORY_AGE_LIMIT = timedelta(hours=96)
LINEUP_SHARED_AGE_LIMIT = timedelta(hours=24)


@dataclass(frozen=True)
class RequiredFixture:
    """A player-specific required fixture, assembled from trusted membership evidence."""

    fixture_id: int
    event: int
    first_data_checked_at: datetime


@dataclass(frozen=True)
class PlayerHistoryEvidence:
    player_id: int
    source_observed_at: datetime | None
    last_success_at: datetime | None
    publication_version: int | None
    covered_fixture_ids: frozenset[int]
    covered_finalized_fixture_ids: frozenset[int]
    required_fixtures: tuple[RequiredFixture, ...] = ()
    missing_fixture_ids: tuple[int, ...] = ()
    invalid_fixture_ids: tuple[int, ...] = ()
    validated_empty_new_player: bool = False
    # Diagnostic only. Attempts and sweep completion cannot refresh an envelope.
    last_attempt_at: datetime | None = None
    completed_sweep_at: datetime | None = None


@dataclass(frozen=True)
class HistoryEligibilityView:
    shared: SharedFreshness
    players: tuple[PlayerHistoryEvidence, ...]
    history_publication_version: int | None
    shared_publication_version: int | None


@dataclass(frozen=True)
class CandidateDecision:
    player_id: int
    status: Status
    reason: str
    warning: bool
    action: Action
    usable_finalized_fixture_ids: tuple[int, ...]
    may_use_prior: bool
    history_publication_version: int | None
    last_success_at: datetime | None


@dataclass(frozen=True)
class EligibilityDecision:
    mode: Mode
    target_event: int
    shared_status: Status
    shared_reason: str
    warning: bool
    blocked: bool
    candidates: tuple[CandidateDecision, ...]
    history_publication_version: int | None
    shared_publication_version: int | None
    shared_last_success_at: datetime | None

    @property
    def exclusions(self) -> tuple[tuple[int, str], ...]:
        """Response-ready player ID and visible reason for every exclusion."""
        return tuple((p.player_id, p.reason) for p in self.candidates if p.action == "exclude")


def _utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must include a UTC offset")
    return value.astimezone(timezone.utc)


def decide_history_eligibility(
    view: HistoryEligibilityView,
    *,
    now: datetime,
    mode: Mode,
    owned_player_ids: frozenset[int],
    locked_player_ids: frozenset[int],
    target_event: int,
) -> EligibilityDecision:
    """Call this once from the later API layer after its repeatable-read view closes.

    Required fixtures must be player-specific evidence from that same view; this
    function never infers historical club membership from today's club. The caller
    supplies only candidates relevant to the solve. It must pass all owned/locked
    players so a missing required player cannot disappear from the decision.
    """
    now = _utc(now, "now")
    if mode not in ("lineup", "transfers", "simulator"):
        raise ValueError("unsupported mode")
    if target_event < 1:
        raise ValueError("target_event must be positive")
    by_id = {p.player_id: p for p in view.players}
    if len(by_id) != len(view.players):
        raise ValueError("duplicate player evidence")
    if not (owned_player_ids | locked_player_ids) <= by_id.keys():
        raise ValueError("required owned or locked player missing from view")

    shared = view.shared
    shared_status: Status = shared.status
    shared_reason = "shared catalog current"
    shared_warning = shared.age_warning
    if shared.status == "unknown":
        shared_reason = "no trusted shared catalog publication"
    elif shared.status == "stale":
        shared_reason = "shared catalog outside current freshness limit"
    if mode == "lineup" and shared.status != "unknown" and shared.source_age is not None:
        if shared.source_age <= LINEUP_SHARED_AGE_LIMIT:
            if shared.status == "stale":
                shared_warning = True
                shared_reason = "shared catalog within lineup allowance (warning)"
        else:
            shared_reason = "shared catalog older than 24 hours for lineup"
    shared_blocked = shared.status == "unknown" or (
        shared.status == "stale" and (
            mode != "lineup" or shared.source_age is None
            or shared.source_age > LINEUP_SHARED_AGE_LIMIT
        )
    )

    results = []
    for player_id, player in sorted(by_id.items()):
        if not player.covered_finalized_fixture_ids <= player.covered_fixture_ids:
            raise ValueError("finalized coverage must be a subset of validated rows")
        required = player_id in owned_player_ids or player_id in locked_player_ids
        age_limit = LINEUP_HISTORY_AGE_LIMIT if mode == "lineup" else HISTORY_AGE_LIMIT
        status: Status = "unknown"
        reason = "no validated player history publication"
        warning = False
        usable: tuple[int, ...] = ()
        may_use_prior = False
        usable_under_lineup_allowance = False
        if player.source_observed_at is not None and player.last_success_at is not None:
            source = _utc(player.source_observed_at, "source_observed_at")
            success = _utc(player.last_success_at, "last_success_at")
            if not source <= success <= now:
                raise ValueError("history publication chronology is invalid")
            age = now - source
            status = "current"
            reason = "validated history current"
            if age > age_limit:
                status = "stale"
                reason = f"player history older than {int(age_limit.total_seconds() // 3600)} hours"
            elif mode == "lineup" and age > HISTORY_AGE_LIMIT:
                status = "stale"
                warning = True
                reason = "player history within lineup allowance (warning)"

            # covered_fixture_ids can include provisional rows. Only the
            # checkpoint's finalized subset can become live prediction input.
            covered = player.covered_finalized_fixture_ids
            pending = []
            missing = []
            for fixture in player.required_fixtures:
                observed = _utc(fixture.first_data_checked_at, "first_data_checked_at")
                if observed > now:
                    raise ValueError("finalization observation is in the future")
                if fixture.event >= target_event:
                    continue
                if fixture.fixture_id not in covered:
                    if now - observed < timedelta(hours=2):
                        pending.append(fixture.fixture_id)
                    else:
                        missing.append(fixture.fixture_id)
            missing.extend(player.missing_fixture_ids)
            missing.extend(player.invalid_fixture_ids)
            if missing:
                status = "stale"
                reason = "missing or invalid finalized fixture coverage: " + ", ".join(
                    str(x) for x in sorted(set(missing))
                )
            elif pending:
                warning = True
                if status == "current":
                    reason = "using previous finalized history during coverage grace: " + ", ".join(
                        str(x) for x in sorted(set(pending))
                    )
            allowed_lineup_age = (mode == "lineup" and age <= LINEUP_HISTORY_AGE_LIMIT
                                  and not missing)
            if status == "current" or allowed_lineup_age:
                # The checkpoint subset excludes provisional rows. The target event
                # is excluded even if an upstream flag is unexpectedly checked.
                target_ids = {f.fixture_id for f in player.required_fixtures if f.event >= target_event}
                usable = tuple(sorted(covered - target_ids - set(missing) - set(pending)))
                may_use_prior = player.validated_empty_new_player and not player.covered_fixture_ids
                usable_under_lineup_allowance = allowed_lineup_age
        action: Action = "use" if status == "current" or usable_under_lineup_allowance else (
            "BLOCK" if required else "exclude"
        )
        if shared_blocked:
            action = "BLOCK"
        results.append(CandidateDecision(
            player_id, status, reason, warning, action, usable, may_use_prior,
            player.publication_version, player.last_success_at,
        ))

    return EligibilityDecision(
        mode, target_event, shared_status, shared_reason,
        shared_warning or any(p.warning for p in results),
        shared_blocked or any(p.action == "BLOCK" for p in results),
        tuple(results), view.history_publication_version,
        view.shared_publication_version,
        now - shared.success_age if shared.success_age is not None else None,
    )
