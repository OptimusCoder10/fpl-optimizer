"""Pure J47 research inputs using the frozen current-season position prior.

This is a deliberate partial §04D deviation: previous-season inputs are never
loaded or accepted. Prediction, observation windows and capture belong later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Mapping

from fpl_optimizer.services.forecast_bundle_policy import PlayerHistoryLabel, eligible_current_rows
from fpl_optimizer.services.prior_selection import REQUIRED_COUNTS, PriorSelection, select_prior

if TYPE_CHECKING:
    from fpl_optimizer.services.forecast_bundle_view import BundleView, CurrentHistoryRow


@dataclass(frozen=True)
class _PositionRow:
    player_id: int
    position: int
    scoring_counts: Mapping[str, int]


@dataclass(frozen=True)
class _NoPreviousSeason:
    personal_player_id: None = None
    personal_reason: None = None
    personal_rows: tuple[()] = ()
    position_rows: tuple[()] = ()
    exclusions: tuple[()] = ()


_NO_PREVIOUS = _NoPreviousSeason()


@dataclass(frozen=True)
class PlayerInputs:
    player_id: int
    position: int
    team_id: int
    target_fixture_ids: tuple[int, ...]
    personal_rows: tuple[CurrentHistoryRow, ...]
    # None means excluded by the supplied policy label. For a used player,
    # source="unavailable" means no personal observations or position prior.
    prior: PriorSelection | None
    exclusion_reasons: tuple[str, ...]
    coverage_status: Literal["unverified_shadow_only"]
    previous_season_prior: Literal["not_loaded"] = "not_loaded"


def assemble_player_inputs(
    view: BundleView, labels: Mapping[int, PlayerHistoryLabel],
) -> tuple[PlayerInputs, ...]:
    """Group eligible rows once per catalog position, then reuse select_prior.

    Labels must cover exactly the catalog; exclusions remain visible records
    with no usable inputs. A view without a future target yields no records.
    Own rows stay in the grouped input so select_prior can count personal
    observations, but it excludes them from the other-player position mean,
    exactly as the frozen benchmark does. No window truncation happens here.
    """
    players = {player.id: player for player in view.players}
    if set(labels) != set(players):
        raise ValueError("labels must cover exactly the catalog players")
    for label in labels.values():
        if label.coverage_status != "unverified_shadow_only":
            raise ValueError("research inputs require unverified_shadow_only coverage")
        if label.action not in {"use", "exclude"} or (
            label.action == "use" and label.history_age_status != "current"
        ):
            raise ValueError("use requires a current research history label")
    if view.target is None:
        return ()

    eligible = eligible_current_rows(
        view.history_rows, view.fixtures, view.gameweeks,
        target_event_id=view.target.id, as_of=view.as_of,
    ).rows
    personal: dict[int, list[CurrentHistoryRow]] = {pid: [] for pid in players}
    grouped: dict[int, list[_PositionRow]] = {position: [] for position in (1, 2, 3, 4)}
    for row in eligible:
        player = players[row.player_id]
        if labels[player.id].action != "use":
            continue
        personal[player.id].append(row)
        grouped[player.position].append(_PositionRow(
            player.id, player.position,
            {name: getattr(row, name) for name in REQUIRED_COUNTS},
        ))
    pools = {position: tuple(rows) for position, rows in grouped.items()}
    targets = {club.team_id: club.fixture_ids for club in view.target_fixtures_by_club}
    records = []
    for player in sorted(players.values(), key=lambda item: item.id):
        label = labels[player.id]
        used = label.action == "use"
        prior = select_prior(
            player_id=player.id, position=player.position,
            current_rows=pools[player.position], previous=_NO_PREVIOUS,
        ) if used else None
        records.append(PlayerInputs(
            player.id, player.position, player.team_id, targets[player.team_id],
            tuple(personal[player.id]), prior, () if used else (label.reason,),
            label.coverage_status,
        ))
    return tuple(records)
