"""Deterministic shared-catalog age policy for transfers and simulator (§05B)."""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal


@dataclass(frozen=True)
class SharedFreshness:
    status: Literal["current", "stale", "unknown"]
    source_age: timedelta | None
    success_age: timedelta | None
    age_limit: timedelta
    final_window: bool
    age_warning: bool


def check_shared_freshness(
    *,
    source_observed_at: datetime | None,
    last_success_at: datetime | None,
    now: datetime,
    next_deadline: datetime,
) -> SharedFreshness:
    """Use source age only from a complete successful shared publication.

    Limits are inclusive; the final window starts exactly 24 hours before an
    unexpired deadline. Callers must supply a future deadline, not a stale target
    or an inferred normal-window default. Attempt timestamps have no role here.
    Missing publication evidence is unknown; impossible chronology is an input
    error. This gate neither computes nor changes the material context digest.
    """
    for name, value in (
        ("now", now),
        ("next_deadline", next_deadline),
        ("source_observed_at", source_observed_at),
        ("last_success_at", last_success_at),
    ):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError(f"{name} must include a UTC offset")
    # Compare elapsed time even if a caller supplies a DST-observing timezone.
    now = now.astimezone(timezone.utc)
    next_deadline = next_deadline.astimezone(timezone.utc)
    if source_observed_at is not None:
        source_observed_at = source_observed_at.astimezone(timezone.utc)
    if last_success_at is not None:
        last_success_at = last_success_at.astimezone(timezone.utc)
    if next_deadline <= now:
        raise ValueError("next_deadline must be strictly after now")

    final_window = next_deadline - now <= timedelta(hours=24)
    age_limit = timedelta(hours=2 if final_window else 6)
    if source_observed_at is None or last_success_at is None:
        return SharedFreshness("unknown", None, None, age_limit, final_window, False)
    if not source_observed_at <= last_success_at <= now:
        raise ValueError(
            "publication must satisfy source_observed_at <= last_success_at <= now"
        )

    source_age = now - source_observed_at
    return SharedFreshness(
        status="current" if source_age <= age_limit else "stale",
        source_age=source_age,
        success_age=now - last_success_at,
        age_limit=age_limit,
        final_window=final_window,
        age_warning=final_window and source_age > timedelta(hours=1),
    )
