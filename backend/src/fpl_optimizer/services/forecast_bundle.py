"""Pure J48 baseline research bundle; canonical JSON is not storage completion.

Scoring rows are embedded once. Window and position-pool references resolve
inside this document, including older pool rows outside personal windows.
PlayerInputs must come from the unchanged J47 assembler; prior arithmetic is
not recomputed here. No manager state or J44 required-fixture candidates enter.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import TYPE_CHECKING, Mapping

from fpl_optimizer.schemas.prior_archive import SCORING_ADAPTER
from fpl_optimizer.services.forecast_bundle_policy import (
    PlayerHistoryLabel, eligible_current_rows, label_player_history, select_target_event,
)
from fpl_optimizer.services.history_eligibility import HistoryEligibilityView
from fpl_optimizer.services.prediction_engine import (
    MODEL_VERSION, FixtureObservation, _recent_window, predict, resolve_availability,
)
from fpl_optimizer.services.prior_selection import REQUIRED_COUNTS

if TYPE_CHECKING:
    from fpl_optimizer.services.forecast_bundle_inputs import PlayerInputs
    from fpl_optimizer.services.forecast_bundle_view import BundleView


SCHEMA_VERSION = "research-bundle-v1"
BASELINE_PARAMETERS = {"window_size": 12, "half_life": 6, "prior_strength": 3}


@dataclass(frozen=True)
class ResearchBundle:
    bundle: dict
    canonical_bytes: bytes
    sha256: str


def _json_value(value):
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("bundle timestamps must include a UTC offset")
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (set, frozenset)):
        return [_json_value(item) for item in sorted(value)]
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def assemble_research_bundle(
    view: BundleView, labels: Mapping[int, PlayerHistoryLabel],
    inputs: tuple[PlayerInputs, ...], *, input_version: str,
) -> ResearchBundle:
    """Predict unowned research candidates with 12/6/3 and no opponent adjustment.

    Reject inconsistent joins and out-of-policy rows before predicting. Prior
    summaries are supplied by J47; embedded pools allow independent verification.
    Canonical format: UTF-8 JSON, sorted keys, compact separators, no NaN/Infinity,
    UTC ISO timestamps and source-ID ordered collections. SHA-256 covers exactly
    these bytes, with no runtime clock, random ID or self-referential hash field.
    """
    if not input_version or not view.rules_version:
        raise ValueError("rules and input versions are required")
    target = select_target_event(view.gameweeks, as_of=view.as_of)
    if target is None or target != view.target:
        raise ValueError("research bundle requires the captured future target")
    players = {player.id: player for player in view.players}
    assembled = {item.player_id: item for item in inputs}
    evidence = {item.player_id: item for item in view.player_history}
    if (len(players) != len(view.players) or len(assembled) != len(inputs)
            or len(evidence) != len(view.player_history)
            or set(players) != set(assembled) or set(players) != set(labels)
            or set(players) != set(evidence) or view.shared_freshness is None):
        raise ValueError("bundle requires one input, label and checkpoint per catalog player")
    history_view = HistoryEligibilityView(
        view.shared_freshness, view.player_history, view.history_publication_version,
        view.shared_publication.publication_version,
    )
    for pid in players:
        expected_label = label_player_history(
            history_view, evidence[pid], target_event_id=target.id, as_of=view.as_of,
        )
        if labels[pid] != expected_label:
            raise ValueError("label does not match captured J42 research evidence")

    fixtures = {fixture.id: fixture for fixture in view.fixtures}
    targets = {club.team_id: tuple(sorted(club.fixture_ids)) for club in view.target_fixtures_by_club}
    eligible = eligible_current_rows(
        view.history_rows, view.fixtures, view.gameweeks,
        target_event_id=target.id, as_of=view.as_of,
    ).rows
    personal = {pid: [] for pid in players}
    for row in eligible:
        if labels[row.player_id].action == "use":
            personal[row.player_id].append(row)
    rows = []
    pools = {position: [] for position in (1, 2, 3, 4)}
    records = []
    exclusions = []
    for pid, player in sorted(players.items()):
        item, label = assembled[pid], labels[pid]
        if (item.position != player.position or item.team_id != player.team_id
                or tuple(sorted(item.target_fixture_ids)) != targets[player.team_id]
                or item.personal_rows != tuple(personal[pid])
                or item.exclusion_reasons != (() if label.action == "use" else (label.reason,))
                or item.coverage_status != "unverified_shadow_only"
                or item.previous_season_prior != "not_loaded"):
            raise ValueError("assembled inputs do not match captured research view")
        used = label.action == "use"
        if (used and (item.prior is None or item.prior.source not in {
            "current_season_position", "personal_current_only", "unavailable",
        })) or (not used and (item.prior is not None or not item.exclusion_reasons)):
            raise ValueError("research prior or exclusion is inconsistent with label")
        for row in item.personal_rows:
            key = [pid, row.fixture_id]
            rows.append({"key": key, "counts": {name: getattr(row, name) for name in REQUIRED_COUNTS}})
            pools[player.position].append(key)
        window = _recent_window(tuple(
            FixtureObservation(row.fixture_id, fixtures[row.fixture_id].kickoff_time, row)
            for row in item.personal_rows
        ), BASELINE_PARAMETERS["window_size"])
        availability = resolve_availability(
            chance=player.chance_of_playing_next_round, status=player.status, owned=False,
        )
        output = None
        reasons = list(item.exclusion_reasons)
        warnings = []
        if used:
            if availability.factor is None:
                reasons.append(availability.reason)
                warnings.append("availability_unresolved")
            if availability.factor is not None or (
                view.shared_freshness.status == "current" and not targets[player.team_id]
            ):
                output = predict(
                    personal_rows=window, prior=item.prior, position=player.position,
                    target_fixture_ids=targets[player.team_id],
                    catalog_trusted=view.shared_freshness.status == "current",
                    chance=player.chance_of_playing_next_round, status=player.status, owned=False,
                    rules_version=view.rules_version, input_version=input_version,
                    **BASELINE_PARAMETERS,
                )
                warnings.extend(warning for warning in output.warnings if warning not in warnings)
                if output.status == "unavailable":
                    reasons.append(output.reason)
        if reasons:
            exclusions.append({"player_id": pid, "reasons": reasons})
        checkpoint = evidence[pid]
        records.append({
            "player_id": pid, "position": player.position, "team_id": player.team_id,
            "target_fixture_ids": targets[player.team_id],
            "history_label": asdict(label),
            "history_publication": {
                "source_observed_at": checkpoint.source_observed_at,
                "last_success_at": checkpoint.last_success_at,
                "publication_version": checkpoint.publication_version,
                "covered_fixture_ids": checkpoint.covered_fixture_ids,
                "covered_finalized_fixture_ids": checkpoint.covered_finalized_fixture_ids,
                "missing_fixture_ids": sorted(checkpoint.missing_fixture_ids),
                "invalid_fixture_ids": sorted(checkpoint.invalid_fixture_ids),
            },
            "personal_window": [[pid, row.fixture_id] for row in window],
            "prior_pool_position": player.position,
            "prior": asdict(item.prior) if item.prior is not None else None,
            "availability": {"chance": player.chance_of_playing_next_round, "status": player.status},
            "catalog_flags": {"can_select": player.can_select, "can_transact": player.can_transact,
                              "removed": player.removed},
            "prediction": asdict(output) if output is not None else None,
            "exclusion_reasons": reasons, "warnings": warnings,
            "coverage_status": "unverified_shadow_only", "previous_season_prior": "not_loaded",
        })
    payload = _json_value({
        "schema_version": SCHEMA_VERSION, "season_id": view.season_id,
        "capture_time": view.as_of, "target": asdict(target),
        "versions": {"model": MODEL_VERSION, "rules": view.rules_version,
                     "scoring_adapter": SCORING_ADAPTER, "input": input_version},
        "baseline_parameters": dict(BASELINE_PARAMETERS), "opponent_adjustment": "none",
        "coverage_status": "unverified_shadow_only", "previous_season_prior": "not_loaded",
        "shared_publication": asdict(view.shared_publication),
        "history_publication_version": view.history_publication_version,
        "shared_freshness": asdict(view.shared_freshness),
        "warnings": ["shared_catalog_age_warning"] if view.shared_freshness.age_warning else [],
        "evidence": {
            "rows": sorted(rows, key=lambda row: row["key"]),
            "position_pools": [{"position": position, "row_keys": sorted(keys)}
                               for position, keys in sorted(pools.items())],
            "prior_rule": "current_season_same_position_excluding_self_unweighted",
            "fixtures": [asdict(fixture) for fixture in sorted(view.fixtures, key=lambda f: f.id)],
            "gameweeks": [asdict(gw) for gw in sorted(view.gameweeks, key=lambda gw: gw.id)],
        },
        "players": records, "exclusions": exclusions,
    })
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False, allow_nan=False).encode("utf-8")
    return ResearchBundle(payload, canonical, hashlib.sha256(canonical).hexdigest())
